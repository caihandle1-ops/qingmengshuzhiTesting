from __future__ import annotations

from dataclasses import dataclass
from typing import Any


AGENT_CATALOG: list[dict[str, str]] = [
    {
        'name': '问答调度智能体',
        'role': '总控与任务编排',
        'desc': '识别用户意图，判断任务类型，并调度数据解析、财务分析、风险预警、企业对比和报告生成等子智能体。',
    },

    {
        'name': '在线信息披露检索智能体',
        'role': '公开披露检索与在线数据接入',
        'desc': '根据公司名称或股票代码检索上交所/深交所公开披露公告，筛选年报等 PDF 文件，下载解析并沉淀到本地缓存数据库。',
    },
    {
        'name': '数据解析智能体',
        'role': '多源数据接入',
        'desc': '负责 CSV、Excel、PDF 文件识别、字段标准化、单位换算、缺失字段提示和入库前校验。',
    },
    {
        'name': '财务分析智能体',
        'role': '指标解释与趋势分析',
        'desc': '围绕营业收入、归母净利润、经营现金流、ROE、资产负债率等指标生成趋势与经营表现解读。',
    },
    {
        'name': '风险预警智能体',
        'role': '异常识别与预警解释',
        'desc': '基于净利润下滑、增收不增利、现金流恶化、ROE 下滑、资产负债率偏高等规则输出预警。',
    },
    {
        'name': '企业对比智能体',
        'role': '横向对比与评分拆解',
        'desc': '调用四维评分模型，从盈利能力、现金流质量、偿债稳健性、成长能力对企业进行横向比较。',
    },
    {
        'name': '报告生成智能体',
        'role': '成果沉淀与交付',
        'desc': '汇总企业概况、核心指标、风险预警、评分拆解、对比结论和来源说明，生成可下载报告。',
    },
]


INTENT_AGENT_MAP: dict[str, list[str]] = {
    'finance_query': ['问答调度智能体', '财务分析智能体'],
    'online_disclosure': ['问答调度智能体', '在线信息披露检索智能体', '数据解析智能体', '财务分析智能体', '风险预警智能体'],
    'trend_analysis': ['问答调度智能体', '财务分析智能体', '风险预警智能体'],
    'risk_warning': ['问答调度智能体', '风险预警智能体'],
    'company_compare': ['问答调度智能体', '企业对比智能体', '财务分析智能体', '风险预警智能体'],
    'investment_summary': ['问答调度智能体', '财务分析智能体', '风险预警智能体'],
    'metric_explain': ['问答调度智能体', '财务分析智能体'],
    'report_generate': ['问答调度智能体', '财务分析智能体', '风险预警智能体', '企业对比智能体', '报告生成智能体'],
    'out_of_scope': ['问答调度智能体'],
    'refusal': ['问答调度智能体'],
    'unknown': ['问答调度智能体', '财务分析智能体'],
}


STEP_TEXT: dict[str, str] = {
    '问答调度智能体': '解析自然语言问题，识别任务意图和涉及企业，决定调用哪些专业智能体。',
    '在线信息披露检索智能体': '检索上交所/深交所公开披露页面，筛选公告 PDF，下载并交给数据解析智能体入库。',
    '数据解析智能体': '当用户上传文件时执行字段抽取、列名标准化、数值清洗和入库校验。',
    '财务分析智能体': '读取本地数据库中的核心财务指标，生成摘要、趋势和指标解释。',
    '风险预警智能体': '根据规则引擎检查财务风险，并输出数据依据、规则依据和来源。',
    '企业对比智能体': '计算两家企业四维评分和综合评分，形成横向对比结论。',
    '报告生成智能体': '汇总各智能体结果，生成投资者分析报告并支持下载。',
}


def build_agent_trace(intent: str, parsed: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """Return a plan only. Execution events are recorded by the actual caller."""
    parsed = parsed or {}
    names = INTENT_AGENT_MAP.get(intent, INTENT_AGENT_MAP['unknown'])
    trace: list[dict[str, str]] = []
    for idx, name in enumerate(names, start=1):
        trace.append({
            '步骤': str(idx),
            '智能体': name,
            '任务': STEP_TEXT.get(name, ''),
            '状态': '计划执行',
        })
    return trace


def agent_trace_text(trace: list[dict[str, str]]) -> str:
    if not trace:
        return '暂无智能体调用记录。'
    lines = []
    for item in trace:
        duration = f"，{item['耗时ms']} ms" if '耗时ms' in item else ''
        reason = f"；{item['原因']}" if item.get('原因') else ''
        lines.append(f"{item['步骤']}. {item['智能体']}：{item['任务']}（{item['状态']}{duration}{reason}）")
    return '\n'.join(lines)


def catalog_as_rows() -> list[dict[str, str]]:
    return [dict(x) for x in AGENT_CATALOG]
