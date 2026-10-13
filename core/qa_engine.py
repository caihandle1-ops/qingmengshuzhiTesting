from __future__ import annotations

import re
import json
import sqlite3
from copy import deepcopy
from time import perf_counter
from typing import Any

import pandas as pd

from core.analysis import (
    METRIC_LABELS,
    build_summary,
    compare_companies,
    compute_alerts,
    explain_metric,
    generate_report,
    metric_query,
    trend_text,
)
from core.llm import llm_enabled, parse_question_with_llm, polish_answer_with_llm, validate_enhancement
from core.agents import agent_trace_text
from core.text_to_sql import run_text_to_sql
from core.planner import plan_query
from core.retrieval import retrieve_documents
from core.retrieval import is_explanation_query
from core.attribution import build_net_profit_attribution
from core.execution_budget import budgeted_answer, check_budget
from core.company_identity import company_mentions, explicit_company_names, strip_known_companies

KEYWORDS = {
    'revenue': ['营业收入', '营收', '收入', 'revenue'],
    'net_profit': ['归母净利润', '净利润', '利润', 'net profit'],
    'operating_cashflow': ['经营现金流', '现金流', '经营活动现金流', '经营净现金'],
    'roe': ['roe', '净资产收益率', '净资产回报率'],
    'debt_ratio': ['资产负债率', '负债率', '杠杆'],
    'gross_margin': ['毛利率'],
    'eps': ['每股收益', '每股盈利', 'eps'],
}

METRIC_FROM_CN = {v: k for k, arr in KEYWORDS.items() for v in arr}


def _contains_any(text: str, words: list[str]) -> bool:
    low = text.lower()
    return any(w.lower() in low for w in words)


def _extract_years(question: str) -> list[int]:
    ranges = re.findall(r'(20\d{2})\s*年?\s*(?:至|到|—|–|-|~|～)\s*(20\d{2})\s*年?', question)
    years = {int(x) for x in re.findall(r'(?<!\d)20\d{2}(?!\d)', question)}
    for start, end in ranges:
        if int(start) <= int(end):
            years.update(range(int(start), int(end) + 1))
    return sorted(years)


def _stock_code_aliases(question: str, companies: list[str]) -> dict[str, list[str]]:
    """Read imported ticker identities only from the current workspace."""
    if not re.search(r'\d{4,6}', question):
        return {}
    from core.db import get_db_path

    path = get_db_path()
    allowed = set(companies)
    rows = []
    if path.is_file():
        try:
            connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
            try:
                rows = connection.execute('SELECT name, stock_code FROM companies').fetchall()
            finally:
                connection.close()
        except sqlite3.OperationalError:
            # Catalog references also work before database initialization.
            pass
    extra_aliases = {}
    for name, code in rows:
        if name not in allowed or not code:
            continue
        code = str(code).strip()
        if not re.fullmatch(r'\d{4,6}', code):
            continue
        extra_aliases.setdefault(name, []).append(code)
    return extra_aliases


def normalize_company_references(question: str, companies: list[str]) -> str:
    """Resolve unambiguous tickers among the caller's freshly loaded companies.

    Catalog identities work offline; imported identities use the current
    workspace read-only. No lookup creates a database or crosses sessions.
    """
    if not re.search(r'\d{4,6}', question):
        return question
    extra_aliases = _stock_code_aliases(question, companies)
    # Ambiguous codes are left explicit so the parser asks for clarification.
    for mention in reversed(company_mentions(question, companies, extra_aliases)):
        if len(mention.names) == 1 and question[mention.start:mention.end].isdigit():
            question = question[:mention.start] + mention.names[0] + question[mention.end:]
    return question


def _detect_period(question: str) -> str | None:
    for code, tokens in [('H1', ['上半年', '半年度', '中报']), ('H2', ['下半年']),
                         ('Q1', ['一季度', '第一季度', '1季度', 'Q1', 'q1']),
                         ('Q2', ['二季度', '第二季度', '2季度', 'Q2', 'q2']),
                         ('Q3', ['三季度', '第三季度', '3季度', 'Q3', 'q3']),
                         ('Q4', ['四季度', '第四季度', '4季度', 'Q4', 'q4']),
                         ('annual', ['全年', '年度报告', '年报'])]:
        if any(token in question for token in tokens):
            return code
    return None


def _unknown_company_mentions(question: str, companies: list[str]) -> list[str]:
    """Conservatively detect an explicit unrecognised name; never substitute the UI selection.

    This is a bounded financial-query grammar, not an entity recognizer. Ambiguous
    noun phrases are surfaced for clarification instead of silently mapped.
    """
    residual = strip_known_companies(question, companies, _stock_code_aliases(question, companies))
    # Company names commonly occur before a year or a financial metric.
    boundaries = r'上一年|前一年|下一年|后一年|20\d{2}|营业收入|营收|归母净利润|净利润|利润|现金流|资产负债率|毛利率|ROE|roe|年报|风险|近[一二三四五六七八九十\d]+年'
    candidates = []
    stop = {'', '它', '这家', '那家', '这家公司', '那家公司', '另一家', '另一家企业', '另一家公司', '上一年', '前一年', '下一年', '后一年', '公司', '企业', '两家', '两家公司', '分别', '同比', '年度', '最新', '今年', '去年', '上半年', '下半年', '一季度', '二季度', '三季度', '四季度', '最近', '近三年', '近两年', '当前', '当前企业', '当前公司', '是什么', '是多少', '多少', '怎么样', '如何', '变化', '变化如何', '有什么', '有哪些', '存在什么', '是否存在', '为什么', '原因', '下降原因', '总额', '数据', '数值', '和', '及', '与', '的', '那么', '那', '呢'}
    # Inspect the leading entity in each company clause, including a company
    # mentioned after a different company's explicit year.
    for clause in re.split(r'[、,，和与及]|相比|对比', residual):
        piece = re.split(boundaries, clause)[0]
        piece = re.sub(r'^(?:请问|请|帮我|帮忙|查询一下|查询|查一下|查|看看|看一下|看|分析一下|分析|比较|我想知道|我想了解|了解|告诉我|那么|那|再看|改成|换成)+', '', piece.strip())
        piece = re.sub(r'[\s\-—:：?？的年呢吗吧]+$', '', piece)
        if piece.startswith(('为什么', '怎么', '如何', '是否', '哪个', '多少', '能否', '可以', '这里', '最近', '今年', '去年', '财务', '经营', '年度', '总结', '摘要', '盈利能力', '的盈利', '盈利表现', '投资', '生成', '报表', '报告', '业务', '分析报告', '核心指标', '核心财务', '关键指标', '主要指标', '成长能力')):
            continue
        if piece.startswith(('关键财务', '全部财务', '净资产回报率', '每股盈利')):
            continue
        if piece not in stop and re.fullmatch(r'[\u4e00-\u9fffA-Za-z0-9]{2,30}', piece):
            candidates.append(piece)
    # A full company suffix is explicit even when it appears later in a sentence.
    for name in re.findall(r'[\u4e00-\u9fffA-Za-z]{2,30}(?:股份有限公司|有限公司)', residual):
        name = re.sub(r'^(?:请问|查询|对比|比较|和|与)+', '', name)
        if name not in candidates:
            candidates.append(name)
    return candidates


def _detect_metrics(question: str) -> list[str]:
    if _contains_any(question, ['核心财务指标', '关键财务指标', '全部财务指标', '财务数据一览', '财务指标总览']):
        return list(KEYWORDS)
    hits = []
    for key, words in KEYWORDS.items():
        if _contains_any(question, words):
            hits.append(key)
    return hits


def _detect_companies(question: str, companies: list[str], default_company: str) -> list[str]:
    hits = explicit_company_names(question, companies, _stock_code_aliases(question, companies))
    if not hits and default_company and not _unknown_company_mentions(question, companies):
        hits = [default_company]
    return hits


def _detect_profile(question: str, selected_profile: str) -> str:
    if '稳健' in question:
        return '稳健型'
    if '成长' in question:
        return '成长型'
    if '平衡' in question or '均衡' in question:
        return '平衡型'
    return selected_profile or '平衡型'


def _local_parse(question: str, companies: list[str], default_company: str, selected_profile: str) -> dict[str, Any]:
    q = question.lower()
    years = _extract_years(question)
    metrics = _detect_metrics(question)
    companies_found = _detect_companies(question, companies, default_company)
    unknown_companies = _unknown_company_mentions(question, companies)
    profile = _detect_profile(question, selected_profile)

    if _contains_any(question, ['天气', '吃什么', '电影', '旅游', '笑话', '翻译一下']) and not metrics:
        intent = 'out_of_scope'
    elif _contains_any(question, ['会不会涨', '明天涨', '该不该买', '能买吗', '买入', '卖出', '收益保证', '稳赚']):
        intent = 'refusal'
    elif ('报告' in question and not (is_explanation_query(question) and metrics)
          or _contains_any(question, ['生成报告', '生成分析报告', '撰写报告'])):
        intent = 'report_generate'
    elif is_explanation_query(question) and metrics:
        intent = 'trend_analysis'
    elif _contains_any(question, ['什么意思', '是什么', '解释']) and metrics:
        intent = 'metric_explain'
    elif _contains_any(question, ['对比', '相比', '谁更', '哪个更', '更适合', '更值得关注']):
        intent = 'company_compare'
    elif _contains_any(question, ['风险', '预警', '问题', '隐患']):
        intent = 'risk_warning'
    elif _contains_any(question, ['趋势', '近三年', '变化', '增长', '下降原因', '为什么', '增收不增利', '年化', '同比']):
        intent = 'trend_analysis'
    elif metrics:
        intent = 'finance_query'
    elif _contains_any(question, ['摘要', '总结', '结论', '分析一下']):
        intent = 'investment_summary'
    else:
        intent = 'unknown'
    return {
        'intent': intent,
        'companies': companies_found,
        'years': years,
        'metrics': metrics,
        'investor_profile': profile,
        'reason': '可信数据层解析',
        'period': _detect_period(question),
        'unknown_companies': unknown_companies,
        'parse_source': 'local',
        'explicit_scope': {'companies': _detect_companies(question, companies, ''), 'years': years, 'metrics': metrics, 'period': _detect_period(question)},
    }


def parse_question(question: str, companies: list[str], default_company: str, selected_profile: str, llm_config: dict | str | None = None) -> dict[str, Any]:
    question = normalize_company_references(question, companies)
    local = _local_parse(question, companies, default_company, selected_profile)
    if not llm_enabled(llm_config):
        return local
    try:
        parsed = parse_question_with_llm(llm_config or {}, question, companies)
        valid_intents = {'finance_query', 'trend_analysis', 'risk_warning', 'company_compare', 'investment_summary', 'metric_explain', 'report_generate', 'out_of_scope', 'refusal'}
        if parsed.get('intent') in valid_intents:
            result = dict(local)
            # Explicit locally parsed conditions are immutable. Cloud only fills
            # unresolved semantics, and cannot invent a company or report period.
            if local['intent'] == 'unknown':
                result['intent'] = parsed['intent']
            for key, allowed in [('companies', set(companies)), ('metrics', set(KEYWORDS))]:
                values = parsed.get(key)
                if not local['explicit_scope'].get(key) and not local['unknown_companies'] and isinstance(values, list):
                    clean = [value for value in values if isinstance(value, str) and value in allowed]
                    if clean:
                        result[key] = clean
            if not local['years'] and isinstance(parsed.get('years'), list):
                # Relative time phrases require local data context; cloud years
                # without a literal year cannot become a hard reporting condition.
                result['cloud_suggested_years'] = parsed['years']
            result['parse_source'] = 'local_constraints+cloud_semantics'
            return result
    except Exception as exc:
        local['parse_fallback_reason'] = f'云端解析不可用：{type(exc).__name__}'
        return local
    return local


def _company_data(name: str, data_map: dict[str, pd.DataFrame]) -> pd.DataFrame:
    return data_map.get(name, pd.DataFrame())


def _attach_attribution_provenance(attribution: dict | None, frame: pd.DataFrame) -> None:
    """Attach only metric-specific provenance whose recorded value matches SQL.

    Row-level raw_source can belong to a different, later-updated metric. It is
    retained as a legacy hint, never promoted into verified numeric evidence.
    """
    if not attribution or frame.empty or 'year' not in frame:
        return
    nodes = [attribution.get('root'), *(attribution.get('drivers') or [])]
    for node in filter(None, nodes):
        for source in node.get('quantitative_sources') or []:
            source['metric_source'] = None
            rows = frame[pd.to_numeric(frame['year'], errors='coerce') == source['year']]
            if len(rows) != 1:
                continue
            provenance = rows.iloc[0].get('metric_provenance')
            metric_source = provenance.get(node['metric']) if isinstance(provenance, dict) else None
            if not isinstance(metric_source, dict):
                continue
            try:
                recorded = json.loads(metric_source.get('value_json', 'null'))
                value = node['previous_value'] if source['year'] == node['previous_year'] else node['current_value']
                if not isinstance(recorded, bool) and isinstance(recorded, (int, float)) and recorded == value:
                    source['metric_source'] = deepcopy(metric_source)
            except (TypeError, ValueError):
                continue


def _evidence_from(company: str, df: pd.DataFrame, alerts: list[dict[str, str]], extra: str = '', source_df: pd.DataFrame | None = None) -> str:
    parts = [f'企业：{company}']
    if not df.empty:
        for _, row in df.sort_values('year').iterrows():
            year = int(row['year'])
            original = source_df[source_df['year'] == year] if source_df is not None and not source_df.empty and 'year' in source_df else pd.DataFrame()
            provenance = original.iloc[-1].get('metric_provenance', {}) if not original.empty else row.get('metric_provenance', {})
            provenance = provenance if isinstance(provenance, dict) else {}
            for key, label in METRIC_LABELS.items():
                if key in row and pd.notna(row.get(key)):
                    source = provenance.get(key, {})
                    suffix = ''
                    if source:
                        where = f'PDF第{source["page"]}页' if source.get('page') else (f'单元格{source["cell"]}' if source.get('cell') else '未记录页码或单元格')
                        suffix = f'；来源：{source.get("file_name") or source.get("source_note") or "未命名来源"}，{where}；原始单位：{source.get("raw_unit") or "未记录"}；导入版本：{source.get("import_id") or "未记录"}'
                    else:
                        suffix = f'；历史记录来源：{row.get("raw_source") or "未记录"}（未提供该指标独立定位）'
                    parts.append(f'{year}年{label}：{row.get(key)}{suffix}')
    if alerts:
        parts.append('风险预警：' + '；'.join([f'{a["title"]}（{a["basis"]}；{a["rule"]}）' for a in alerts]))
    if extra:
        parts.append(extra)
    return '\n'.join(parts)


@budgeted_answer
def answer_question(
    question: str,
    selected_company: str,
    selected_compare: str,
    all_company_names: list[str],
    data_map: dict[str, pd.DataFrame],
    profile: str = '平衡型',
    llm_config: dict | str | None = None,
    resolved_context: dict[str, Any] | None = None,
    *, budget_seconds: float = 60, progress=None,
) -> dict[str, Any]:
    started = perf_counter()
    agent_trace: list[dict[str, Any]] = []

    def notify(stage: str, status: str, detail: str) -> None:
        # An optional presentation observer cannot change query control flow.
        if progress is not None:
            try:
                progress(stage, status, detail)
            except Exception:
                pass

    def record(name: str, task: str, stage_start: float, status: str = '已完成', reason: str = '') -> None:
        agent_trace.append({'步骤': str(len(agent_trace) + 1), '智能体': name, '任务': task,
                            '状态': status, '耗时ms': round((perf_counter() - stage_start) * 1000, 3), '原因': reason})

    notify('understand', 'running', '正在解析企业、年度、指标与研究条件。')
    raw_parsed = parse_question(question, all_company_names, selected_company, profile, llm_config if llm_config else None)
    check_budget('条件解析后')
    parsed = dict(raw_parsed)
    if resolved_context:
        for key in ['companies', 'years', 'metrics', 'period', 'investor_profile']:
            if key in resolved_context and not raw_parsed.get('explicit_scope', {}).get(key):
                # Validated cloud semantics win over a local unknown intent, but
                # confirmed context supplies omitted conditions for follow-ups.
                parsed[key] = resolved_context[key]
        if raw_parsed.get('intent') == 'unknown' and resolved_context.get('intent'):
            parsed['intent'] = resolved_context['intent']
    record('条件解析', '解析本轮显式条件并补齐允许继承的上下文', started,
           reason=raw_parsed.get('parse_fallback_reason', parsed.get('parse_source', 'local')))
    scope_started = perf_counter()
    intent = parsed.get('intent') or 'unknown'
    companies = list(dict.fromkeys(parsed.get('companies') or []))
    years = parsed.get('years') or []
    metrics = parsed.get('metrics') or []
    investor_profile = parsed.get('investor_profile') or profile
    query_plan = plan_query(question, parsed)
    analysis_intent = query_plan.get('structured_intent') or intent
    primary = companies[0] if companies else ''
    secondary = None
    for c in companies[1:]:
        if c != primary and c in data_map:
            secondary = c
            break
    if secondary is None and analysis_intent in {'company_compare', 'report_generate'} and len(companies) <= 1 and selected_compare in data_map and selected_compare != primary:
        secondary = selected_compare
        if analysis_intent == 'company_compare':
            companies.append(secondary)
            parsed['companies'] = companies

    sql_result: dict[str, Any] = {
        'status': 'not_applicable', 'sql_status': 'not_applicable', 'attempts': [], 'rows': [],
    }
    retrieval_result: dict[str, Any] = {
        'status': 'not_applicable', 'answer': '', 'hits': [], 'citations': [],
        'candidate_documents': 0, 'chunk_count': 0, 'hit_count': 0,
    }
    attribution = None

    def blocked(status: str, reason: str, missing: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        record('条件校验', '核对请求范围与可用数据', scope_started, '已拦截', reason)
        notify('understand', 'blocked', reason)
        return {'status': status, 'reason': reason, 'missing_scope': missing or [],
                'answer': reason, 'draft': reason, 'parsed': parsed, 'evidence': '', 'chart': None,
                'report_text': None, 'model_used': '条件校验', 'company': primary,
                'agent_trace': agent_trace, 'sql_result': sql_result, 'sql_status': 'not_applicable',
                'query_plan': query_plan, 'retrieval_result': retrieval_result, 'analysis_intent': analysis_intent,
                'attribution': None,
                'duration_ms': round((perf_counter() - started) * 1000, 3)}

    unknown = list(dict.fromkeys((raw_parsed.get('unknown_companies') or []) + [name for name in companies if name not in all_company_names]))
    if resolved_context and resolved_context.get('reason') == '用户已确认澄清条件':
        unknown = [name for name in unknown if not any(name in company for company in companies)]
    if unknown and analysis_intent not in {'out_of_scope', 'refusal'}:
        return blocked('unknown_company', '未接入或无法确认企业：' + '、'.join(unknown) + '。请确认企业名称或先导入对应数据；不会改用当前选中企业。', [{'company': name} for name in unknown])
    if parsed.get('period') not in {None, '', 'annual'}:
        return blocked('unsupported_period', f'请求的报告期为 {parsed["period"]}；当前结构化库仅有年度指标，不能用全年数据替代半年或季度数据。请导入该报告期并使用支持相应期间的数据结构。', [{'period': parsed['period'], 'years': years}])
    if not companies and analysis_intent not in {'out_of_scope', 'refusal', 'metric_explain'}:
        return blocked('missing_scope', '尚未确认目标企业，请先选择或明确企业名称。')
    # One scope filter is shared by SQL failures, risk, reports, summaries and
    # comparisons. No path may silently broaden the requested company/year.
    query_data_map = {}
    missing_scope = []
    for name in companies:
        scoped = _company_data(name, data_map).copy()
        if years:
            actual_years = set(pd.to_numeric(scoped.get('year', pd.Series(dtype=float)), errors='coerce').dropna().astype(int))
            missing_scope.extend({'company': name, 'year': year} for year in years if year not in actual_years)
            if 'year' in scoped:
                scoped = scoped[pd.to_numeric(scoped['year'], errors='coerce').isin(years)].copy()
        elif scoped.empty:
            missing_scope.append({'company': name})
        query_data_map[name] = scoped
    if missing_scope and query_plan['route'] != 'rag' and analysis_intent not in {'out_of_scope', 'refusal', 'metric_explain'}:
        desc = '、'.join(f'{item["company"]}{str(item.get("year", "")) + "年" if item.get("year") else ""}' for item in missing_scope)
        return blocked('missing_scope', f'缺少请求范围的数据：{desc}。请导入对应企业和报告期；不会改用最新年度或其他企业。', missing_scope)
    scoped_source_map = query_data_map
    record('条件校验', '限定企业、年度及报告期间', scope_started)
    notify('understand', 'completed', '企业、年度与报告范围已解析；后续证据仍独立核验。')
    if analysis_intent in {'out_of_scope', 'refusal'}:
        notify('retrieve', 'skipped', '该请求不进入财务资料查询。')
    else:
        notify('retrieve', 'running', '正在读取限定范围的指标与报告依据。')
    if query_plan['route'] in {'sql', 'hybrid'} and analysis_intent in {'finance_query', 'trend_analysis', 'company_compare'}:
        check_budget('只读SQL开始')
        stage = perf_counter()
        sql_context = {**parsed, 'intent': analysis_intent}
        sql_result = run_text_to_sql(sql_context)
        check_budget('只读SQL结果核验后')
        if sql_result['status'] == 'success':
            sql_df = pd.DataFrame(sql_result['rows'])
            query_data_map = {}
            for company_name in companies:
                company_rows = sql_df[sql_df['company'] == company_name].drop(columns=['company'], errors='ignore')
                source_rows = _company_data(company_name, scoped_source_map)
                if not source_rows.empty and 'metric_provenance' in source_rows:
                    provenance_by_year = {int(row['year']): row['metric_provenance'] for _, row in source_rows.iterrows()}
                    # SQL projections contain scalar facts only; restore the
                    # trusted per-metric locations for answer source rendering.
                    company_rows = company_rows.copy()
                    company_rows['metric_provenance'] = [provenance_by_year.get(int(year), {}) for year in company_rows['year']]
                query_data_map[company_name] = company_rows.reset_index(drop=True)
        else:
            sql_result['sql_status'] = 'fallback'
            sql_result['fallback_reason'] = '；'.join(item.get('error', '') for item in sql_result.get('attempts', []))
        record('只读 SQL', '参数化查询、安全检查与结果条件校验', stage,
               '已完成' if sql_result['status'] == 'success' else '已降级', sql_result.get('fallback_reason', ''))

    df = _company_data(primary, query_data_map)
    analysis_basis_years: dict[str, list[int]] = {}

    def analysis_data(name: str) -> pd.DataFrame:
        scoped = _company_data(name, scoped_source_map)
        if scoped.empty or not years or 'year' not in scoped:
            return scoped
        target = max(int(year) for year in years)
        if target not in set(scoped['year']):
            return scoped
        original = _company_data(name, data_map)
        base = original[original['year'] == target - 1] if 'year' in original else pd.DataFrame()
        if not base.empty and target - 1 not in set(scoped['year']):
            analysis_basis_years[name] = [target - 1]
            return pd.concat([base, scoped], ignore_index=True).sort_values('year')
        return scoped
    if query_plan['route'] in {'rag', 'hybrid'}:
        check_budget('原文检索开始')
        stage = perf_counter()
        structured_facts = sql_result.get('rows') or []
        if query_plan['route'] == 'hybrid' and not structured_facts:
            structured_facts = [{**row, 'company': name} for name in companies
                                for row in _company_data(name, query_data_map).to_dict('records')]
        retrieval_context = {**parsed, 'structured_facts': structured_facts}
        retrieval_result = retrieve_documents(question, retrieval_context)
        check_budget('原文检索结果核验后')
        record('文档检索', '按目标企业、年份检索支持答案的原文', stage,
               '已完成' if retrieval_result.get('status') == 'success' else '无证据', retrieval_result.get('reason', ''))

    if analysis_intent not in {'out_of_scope', 'refusal'}:
        notify('retrieve', 'completed', '资料读取结束；引用是否充分以结果与核验状态为准。')
    numerical_analysis = (query_plan['route'] != 'rag'
                          and analysis_intent not in {'out_of_scope', 'refusal'}
                          and not (analysis_intent == 'metric_explain' and df.empty))
    notify('calculate', 'running' if numerical_analysis else 'skipped',
           '正在生成限定范围的指标、比较或分析结果。' if numerical_analysis else '本次请求不执行数值分析。')

    # Attribution is an additive, deterministic result. It never widens the
    # main answer's scope or accepts values generated by the language model.
    if (query_plan['route'] == 'hybrid' and 'net_profit' in metrics
            and is_explanation_query(question)
            and intent not in {'refusal', 'out_of_scope'}):
        stage = perf_counter()
        if len(companies) != 1:
            record('财务归因', '构建单企业净利润年度变化线索', stage, '已跳过',
                   '归因树当前支持单家企业；本轮多企业问答照常执行。')
        else:
            try:
                original = _company_data(primary, data_map)
                available_years = sorted(set(pd.to_numeric(
                    original.get('year', pd.Series(dtype=float)), errors='coerce').dropna().astype(int)))
                attribution = build_net_profit_attribution(primary, years, question,
                                                            available_years=available_years)
                _attach_attribution_provenance(attribution, original)
                complete = bool(attribution and attribution.get('status') == 'success')
                record('财务归因', '只读查询比较基期与目标年度，核验同年度报告线索', stage,
                       '已完成' if complete else '数据不足',
                       (attribution or {}).get('reason') or (attribution or {}).get('scope_note', ''))
            except Exception as exc:
                attribution = None
                record('财务归因', '构建净利润年度变化线索', stage, '已降级',
                       f'{type(exc).__name__}：归因组件不可用，保留主问答结果。')

    # A successful structured query may deliberately project only one metric.
    # Do not infer an all-clear risk conclusion from absent (unqueried) fields.
    stage = perf_counter()
    check_budget('结构化事实分析')
    needs_risk = analysis_intent in {'risk_warning', 'report_generate', 'investment_summary', 'unknown'}
    alert_map = {name: compute_alerts(analysis_data(name) if analysis_intent == 'risk_warning' else _company_data(name, query_data_map))
                 for name in companies} if needs_risk and query_plan['route'] != 'rag' else {}
    alerts = alert_map.get(primary, [])
    draft = ''
    evidence_extra = ''
    chart = None
    report_text = None

    if query_plan['route'] == 'rag':
        draft = retrieval_result['answer']
    elif analysis_intent == 'out_of_scope':
        draft = '本系统聚焦上市公司财务分析、风险预警、企业对比和投资者参考结论，暂不提供天气、生活服务或通用闲聊回答。你可以询问企业财务、风险或对比相关问题。'
    elif analysis_intent == 'refusal':
        draft = '本系统不预测短期股价涨跌，也不提供买入、卖出或保证收益类建议。可以基于已接入的财务指标、风险预警和企业对比结果，生成投资者参考结论。'
    elif analysis_intent == 'metric_explain':
        metric = metrics[0] if metrics else 'roe'
        draft = explain_metric(metric)
        if df is not None and not df.empty and metrics:
            draft += '\n\n' + metric_query(primary, df, metric, years[0] if years else None)
    elif analysis_intent == 'finance_query':
        query_metrics = metrics or ['net_profit']
        query_years = years or [None]
        draft = '\n'.join(
            metric_query(name, _company_data(name, query_data_map), metric, year)
            for name in companies
            for year in query_years
            for metric in query_metrics
        )
    elif analysis_intent == 'trend_analysis':
        summaries = []
        for name in companies:
            for metric in (metrics or ['revenue']):
                if (metric == 'net_profit' and attribution
                        and attribution.get('status') == 'success'
                        and name == attribution.get('company')):
                    root = attribution['root']
                    direction = {'up': '增加', 'down': '减少', 'flat': '持平'}[root['direction']]
                    summary = (f'{name}归母净利润：{attribution["previous_year"]}年'
                               f'{root["previous_value"]:,.2f}亿元 → {attribution["current_year"]}年'
                               f'{root["current_value"]:,.2f}亿元，{direction}，变动'
                               f'{root["delta"]:+,.2f}亿元。')
                    if root['growth_rate'] is not None:
                        summary += f'{root["growth_label"]} {root["growth_rate"]:+,.2f}%。'
                    else:
                        summary += root['growth_label'] + '。'
                    summaries.append(summary)
                else:
                    summaries.append(trend_text(name, _company_data(name, query_data_map), metric))
        draft = '\n'.join(summaries)
        chart = 'trend'
    elif analysis_intent == 'risk_warning':
        draft = '\n\n'.join(name + '\n' + '；'.join([f'{a["title"]}：{a["message"]}\n规则依据：{a.get("rule", "-")}\n数据依据：{a.get("basis", "-")}\n来源：{a.get("source", "-")}' for a in alert_map.get(name, [])]) for name in companies)
    elif analysis_intent == 'company_compare':
        if secondary is None or secondary == primary:
            draft = '当前没有可用于对比的第二家企业，请在左侧选择对比企业，或上传新的企业结构化财务数据。'
        else:
            secondary_df = _company_data(secondary, query_data_map)
            if metrics:
                draft = '\n'.join(metric_query(name, _company_data(name, query_data_map), metric, year)
                                  for name in companies for year in (years or [None]) for metric in metrics)
                draft += '\n当前仅比较所选指标，不据此进行综合评分或推荐企业。'
            else:
                comparisons = [compare_companies(primary, analysis_data(primary), name, analysis_data(name), investor_profile) for name in companies[1:]]
                draft = '\n\n'.join(cmp['reasoning'] + ('\n评分拆解：\n' + cmp['score_table'].to_string(index=False) if not cmp['score_table'].empty else '') for cmp in comparisons)
                evidence_extra = '\n'.join('企业对比依据：\n' + '\n'.join([f'{k}：' + '；'.join(v) for k, v in cmp.get('basis', {}).items()]) for cmp in comparisons)
    elif analysis_intent == 'report_generate':
        cmp = None
        if secondary and secondary in query_data_map and secondary != primary:
            cmp = compare_companies(primary, df, secondary, _company_data(secondary, query_data_map), investor_profile)
        report_text = generate_report(primary, df, alerts, cmp, investor_profile)
        draft = '已生成投资者分析报告，可使用本回答内的“生成分析报告”入口查看并下载。\n\n' + report_text[:800] + ('...' if len(report_text) > 800 else '')
    elif analysis_intent == 'investment_summary' or analysis_intent == 'unknown':
        if analysis_intent == 'unknown':
            draft = '我暂时没有识别出非常具体的分析意图。以下先给出企业摘要，你也可以继续询问“净利润是多少”“有哪些风险”“和宁德时代谁更适合稳健型投资者”等问题。\n\n'
        draft += build_summary(primary, df, alerts, investor_profile)
    else:
        draft = build_summary(primary, df, alerts, investor_profile)

    if query_plan['route'] == 'rag':
        draft = retrieval_result['answer']
    elif query_plan['route'] == 'hybrid':
        draft = f'结构化数据：\n{draft}\n\n年报解释：\n{retrieval_result["answer"]}'
        if retrieval_result.get('reason'):
            draft += '\n' + retrieval_result['reason']

    record('结构化分析', '根据限定范围生成指标、比较或报告文本', stage,
           '已跳过' if query_plan['route'] == 'rag' else '已完成')
    check_budget('结构化分析完成')
    if numerical_analysis:
        notify('calculate', 'completed', '限定范围的分析已执行；不代表原文与口径核验通过。')
    if analysis_basis_years:
        basis_note = '辅助基期（仅用于同比/评分，不替代请求年度）：' + '；'.join(
            f'{name}使用{year}年作为{year + 1}年的比较基期' for name, basis_years in analysis_basis_years.items() for year in basis_years)
        draft += '\n\n' + basis_note
        evidence_extra += '\n' + basis_note
        agent_trace[-1]['原因'] = basis_note
    if attribution:
        attribution_note = '\n'.join(filter(None, [attribution.get('scope_note'), attribution.get('disclaimer')]))
        draft += '\n\n归因补充：\n' + attribution_note
        evidence_extra += '\n' + attribution_note
    evidence = '\n\n'.join(_evidence_from(name, _company_data(name, query_data_map), alerts if name == primary else [], evidence_extra if name == primary else '', _company_data(name, scoped_source_map)) for name in companies)
    if retrieval_result.get('citations'):
        document_evidence = '\n'.join(
            f'[{item["number"]}] {item["file_name"]}，' +
            (f'PDF第{item["page"]}页' if item.get('page') is not None else item.get('location') or '未记录原文位置') +
            f'：{item["snippet"]}'
            for item in retrieval_result['citations']
        )
        evidence += '\n\n文档证据：\n' + document_evidence
    evidence = evidence + '\n\n实际执行记录：\n' + agent_trace_text(agent_trace)
    check_budget('事实依据汇总完成')
    final_answer = draft
    model_used = '可信数据分析'
    if llm_enabled(llm_config) and intent not in ['out_of_scope', 'refusal']:
        stage = perf_counter()
        try:
            check_budget('可选云端解读')
            candidate = polish_answer_with_llm(llm_config or {}, question, evidence, draft, investor_profile)
            check_budget('云端解读后事实校验')
            validation = validate_enhancement(draft, candidate)
            if not validation['valid']:
                raise ValueError(validation['reason'])
            final_answer = candidate
            model_used = '云端大模型增强'
            record('云端解释', '保留结构化事实，校验模型选择的解释提示', stage)
        except Exception as e:
            final_answer = draft
            model_used = '可信数据分析'
            record('云端解释', '校验模型解释输出', stage, '已降级', f'{type(e).__name__}：{str(e)[:180]}')
    return {
        'status': 'no_evidence' if query_plan['route'] == 'rag' and not retrieval_result.get('citations') else 'success',
        'reason': retrieval_result.get('reason', '') if query_plan['route'] == 'rag' else '',
        'missing_scope': missing_scope,
        'duration_ms': round((perf_counter() - started) * 1000, 3),
        'analysis_basis_years': analysis_basis_years,
        'answer': final_answer,
        'draft': draft,
        'parsed': parsed,
        'evidence': evidence,
        'chart': chart,
        'report_text': report_text,
        'model_used': model_used,
        'company': primary,
        'agent_trace': agent_trace,
        'sql_result': sql_result,
        'sql_status': sql_result.get('sql_status', 'not_applicable'),
        'query_plan': query_plan,
        'retrieval_result': retrieval_result,
        'analysis_intent': analysis_intent,
        'attribution': attribution,
    }
