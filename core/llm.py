from __future__ import annotations

import json
import re
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable
from urllib.parse import urlsplit

import requests
from core.storage import is_public_deployment
from core.execution_budget import current_budget


_call_observer: ContextVar[Callable | None] = ContextVar('llm_call_observer', default=None)

# These are deliberately fixed messages: request exceptions can contain keys,
# endpoint URLs or provider response bodies and must never reach saved traces.
CONNECTION_MESSAGES = {
    'configuration': '模型配置或服务地址不完整，请检查系统设置。',
    'authentication': '服务商未接受当前凭据，请检查 API Key 与模型权限。',
    'not_found': '服务商未找到该模型或接口，请检查模型名称。',
    'rate_limit': '服务商暂时限制请求，请检查额度或稍后重试。',
    'timeout': '本次模型请求超时，已继续使用本地分析。',
    'network': '本次无法连接模型服务，已继续使用本地分析。',
    'response': '模型未返回可用的文本响应，已继续使用本地分析。',
    'service': '本次模型服务请求失败，已继续使用本地分析。',
}


class LLMRequestError(RuntimeError):
    def __init__(self, code: str):
        self.code = code if code in CONNECTION_MESSAGES else 'service'
        super().__init__(CONNECTION_MESSAGES[self.code])


@contextmanager
def observe_llm_calls(callback: Callable):
    """Observe this execution context only; never create a global/session cache."""
    token = _call_observer.set(callback)
    try:
        yield
    finally:
        _call_observer.reset(token)


def _notify_call(config: dict[str, str], *, ok: bool, code: str = '') -> None:
    observer = _call_observer.get()
    if observer is not None:
        # Observability must not make an otherwise working analysis fail.
        try:
            observer(config, {'ok': ok, 'code': code})
        except Exception:
            pass

PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    'DeepSeek': {
        'base_url': 'https://api.deepseek.com/v1',
        'models': ['deepseek-chat', 'deepseek-reasoner'],
        'default_model': 'deepseek-chat',
    },
    '豆包': {
        'base_url': 'https://ark.cn-beijing.volces.com/api/v3',
        'models': ['doubao-seed-1-6', 'doubao-seed-1-6-thinking', '自定义模型/Endpoint ID'],
        'default_model': 'doubao-seed-1-6',
    },
    '通义千问': {
        'base_url': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
        'models': ['qwen-plus', 'qwen-max', 'qwen-turbo', '自定义模型名称'],
        'default_model': 'qwen-plus',
    },
}

# Cloud models choose relevant educational notes; they never rewrite financial
# facts. The renderer owns every numeric value, company, period and conclusion.
EXPLANATION_NOTES = {
    'revenue': '营业收入反映经营规模，收入增长本身不等于盈利质量改善。',
    'profit': '净利润反映会计盈利，应结合经营现金流观察盈利的现金转化。',
    'cashflow': '经营现金流反映经营活动的现金收支，需要结合回款及营运资本分析。',
    'roe': '净资产收益率需结合盈利水平、资产效率和杠杆一起理解。',
    'debt': '资产负债率应结合行业特点、债务期限和偿债现金来源理解。',
    'period': '跨期比较需要保持报告期间、合并范围和指标口径一致。',
    'evidence': '企业经营原因应以对应报告原文为依据，指标变化本身不能证明因果关系。',
}
ENHANCEMENT_MARKER = '\n\n解读提示：\n'


def validate_enhancement(draft: str, candidate: str) -> dict[str, Any]:
    if not isinstance(candidate, str) or not candidate:
        return {'valid': False, 'reason': '云端输出为空。'}
    if candidate == draft:
        return {'valid': True, 'reason': ''}
    prefix = draft + ENHANCEMENT_MARKER
    if not candidate.startswith(prefix):
        return {'valid': False, 'reason': '云端输出试图改写原始事实；已拒绝并保留结构化结果。'}
    notes = candidate[len(prefix):].splitlines()
    allowed = set(EXPLANATION_NOTES.values())
    if not notes or len(notes) > 3 or any(note not in allowed for note in notes):
        return {'valid': False, 'reason': '云端输出包含未通过程序校验的事实或解释。'}
    return {'valid': True, 'reason': ''}


def _select_explanations(config_or_key: dict[str, str] | str, question: str, draft: str) -> str:
    prompt = (
        '你是阅读辅助模块。原始财务事实已经由程序渲染，不允许重写、计算或新增数字。'
        '从可用解释中选择与用户问题相关的至多三个编号，只返回JSON：{"explanation_ids":[]}。'
        '无法确定时返回空数组。可用解释：' + json.dumps(EXPLANATION_NOTES, ensure_ascii=False)
        + '\n用户问题：' + question + '\n已经确认的分析结果：' + draft
    )
    raw = _post_chat(config_or_key, [{'role': 'user', 'content': prompt}], temperature=0)
    data = _extract_json(raw)
    identifiers = data.get('explanation_ids')
    if set(data) != {'explanation_ids'} or not isinstance(identifiers, list) or len(identifiers) > 3:
        raise ValueError('云端解释未返回允许的结构，原始事实保持不变。')
    if any(not isinstance(item, str) or item not in EXPLANATION_NOTES for item in identifiers):
        raise ValueError('云端解释包含不受支持的内容。')
    notes = [EXPLANATION_NOTES[item] for item in dict.fromkeys(identifiers)]
    candidate = draft + (ENHANCEMENT_MARKER + '\n'.join(notes) if notes else '')
    validation = validate_enhancement(draft, candidate)
    if not validation['valid']:
        raise ValueError(validation['reason'])
    return candidate


def build_llm_config(provider: str, api_key: str, model: str | None = None, base_url: str | None = None) -> dict[str, str]:
    preset = PROVIDER_PRESETS.get(provider, PROVIDER_PRESETS['DeepSeek'])
    return {
        'provider': provider,
        'api_key': (api_key or '').strip(),
        'model': (preset.get('default_model', '') if model is None else model).strip(),
        'base_url': (preset.get('base_url', '') if base_url is None else base_url).strip().rstrip('/'),
    }


def _normalize_config(config_or_key: dict[str, str] | str | None) -> dict[str, str]:
    if isinstance(config_or_key, dict):
        provider = config_or_key.get('provider') or 'DeepSeek'
        return build_llm_config(provider, config_or_key.get('api_key') or '',
                                config_or_key.get('model'), config_or_key.get('base_url'))
    return build_llm_config('DeepSeek', config_or_key or '')


def llm_enabled(config_or_key: dict[str, str] | str | None) -> bool:
    cfg = _normalize_config(config_or_key)
    return bool(cfg.get('api_key') and cfg.get('base_url') and cfg.get('model'))


def _post_chat(config_or_key: dict[str, str] | str, messages: list[dict[str, str]], temperature: float = 0.2, timeout: int = 45, *, max_tokens: int | None = None) -> str:
    cfg = _normalize_config(config_or_key)
    try:
        if not llm_enabled(cfg):
            raise ValueError('incomplete configuration')
        validate_api_endpoint(cfg)
    except ValueError:
        _notify_call(cfg, ok=False, code='configuration')
        raise LLMRequestError('configuration') from None
    payload = {
        'model': cfg['model'],
        'messages': messages,
        'temperature': temperature,
    }
    if max_tokens is not None:
        payload['max_tokens'] = max_tokens
    headers = {
        'Authorization': f"Bearer {cfg['api_key']}",
        'Content-Type': 'application/json',
    }
    budget = current_budget()
    network_timeout = budget.network_timeout(5, min(timeout, 60)) if budget else (5, min(timeout, 60))
    try:
        resp = requests.post(f"{cfg['base_url']}/chat/completions", headers=headers, json=payload,
                             timeout=network_timeout, allow_redirects=False)
        if resp.status_code in {401, 403}:
            raise LLMRequestError('authentication')
        if resp.status_code == 404:
            raise LLMRequestError('not_found')
        if resp.status_code == 429:
            raise LLMRequestError('rate_limit')
        if resp.status_code < 200 or resp.status_code >= 300:
            raise LLMRequestError('service')
        data = resp.json()
        content = data['choices'][0]['message']['content']
        if not isinstance(content, str) or not content.strip():
            raise LLMRequestError('response')
    except Exception as error:
        if isinstance(error, LLMRequestError):
            code = error.code
        elif isinstance(error, requests.Timeout):
            code = 'timeout'
        elif isinstance(error, (ValueError, KeyError, IndexError, TypeError)):
            code = 'response'
        elif isinstance(error, requests.RequestException):
            code = 'network'
        else:
            code = 'service'
        _notify_call(cfg, ok=False, code=code)
        raise LLMRequestError(code) from None
    _notify_call(cfg, ok=True)
    if budget:
        budget.check('云端响应后校验')
    return content.strip()


def probe_llm_connection(config_or_key: dict[str, str] | str) -> None:
    """One explicit, small request through the same transport used by analysis."""
    _post_chat(config_or_key, [{'role': 'user', 'content': 'Reply with OK only.'}],
               temperature=0, timeout=15, max_tokens=64)


def validate_api_endpoint(config_or_key: dict[str, str] | str) -> None:
    cfg = _normalize_config(config_or_key)
    value = cfg['base_url']
    url = urlsplit(value)
    if url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('模型API必须是无凭据、无查询参数的HTTP(S)服务地址。')
    if any(char.isspace() for char in value) or '\\' in value:
        raise ValueError('模型API地址包含非法字符。')
    if is_public_deployment():
        preset = PROVIDER_PRESETS.get(cfg['provider'])
        if not preset or value.rstrip('/') != preset['base_url']:
            raise ValueError('公共部署仅允许所选服务商的官方HTTPS端点，不允许自定义或内网模型地址。')


def _extract_json(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r'\{.*\}', text, flags=re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return {}
    return {}


def parse_question_with_llm(config_or_key: dict[str, str] | str, question: str, companies: list[str]) -> dict[str, Any]:
    company_text = '、'.join(companies)
    prompt = f'''
你是一个上市公司投资分析系统的问题解析模块。请只输出JSON，不要解释。
可选企业：{company_text}

请从用户问题中识别以下字段：
- intent：finance_query、trend_analysis、risk_warning、company_compare、investment_summary、metric_explain、report_generate、out_of_scope、refusal
- companies：问题涉及的企业名称列表，只能使用可选企业中的名称；如果没有明确企业则为空列表
- years：年份列表，例如[2024]；没有则为空列表
- metrics：指标列表，可选 revenue、net_profit、operating_cashflow、roe、debt_ratio、gross_margin、eps；没有则为空列表
- investor_profile：稳健型、平衡型、成长型、未知
- reason：简短说明判断理由

如果用户询问天气、闲聊、生活服务等非上市公司财务分析问题，intent用out_of_scope。
如果用户要求预测短期股价、直接买卖建议、保证收益，intent用refusal。

用户问题：{question}
'''
    text = _post_chat(config_or_key, [{'role': 'user', 'content': prompt}], temperature=0)
    data = _extract_json(text)
    return data if isinstance(data, dict) else {}


def polish_answer_with_llm(config_or_key: dict[str, str] | str, question: str, evidence: str, draft_answer: str, investor_profile: str = '平衡型') -> str:
    return _select_explanations(config_or_key, question, draft_answer)




def enhance_report_with_llm(config_or_key: dict[str, str] | str, report_text: str, company: str, investor_profile: str = '平衡型') -> str:
    return _select_explanations(config_or_key, f'{company}财务报告阅读辅助，{investor_profile}', report_text)




# Backward-compatible aliases for modules that may still import old names.
def parse_question_with_deepseek(api_key: str, question: str, companies: list[str]) -> dict[str, Any]:
    return parse_question_with_llm(build_llm_config('DeepSeek', api_key), question, companies)


def polish_answer_with_deepseek(api_key: str, question: str, evidence: str, draft_answer: str, investor_profile: str = '平衡型') -> str:
    return polish_answer_with_llm(build_llm_config('DeepSeek', api_key), question, evidence, draft_answer, investor_profile)


def rewrite_with_deepseek(api_key: str, user_question: str, system_facts: str, compare_facts: str | None = None) -> str:
    evidence = system_facts + ('\n' + compare_facts if compare_facts else '')
    return polish_answer_with_deepseek(api_key, user_question, evidence, system_facts)
