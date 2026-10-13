from __future__ import annotations

import re
from typing import Any

from core.qa_engine import (KEYWORDS, parse_question, _extract_years, _detect_period, _detect_metrics,
                            normalize_company_references, _stock_code_aliases)
from core.company_identity import ambiguous_company_names, explicit_company_names


PRONOUNS = ['它', '那家公司', '这家公司', '另一家', '那个', '那家']
FOLLOW_UP_MARKERS = ['那', '呢', '改成', '换成', '再看', '它', '另一家', '那个', '上一年', '前一年', '下一年', '后一年']
METRIC_LABELS = {
    'revenue': '营业收入',
    'net_profit': '归母净利润',
    'operating_cashflow': '经营现金流',
    'roe': 'ROE',
    'debt_ratio': '资产负债率',
    'gross_margin': '毛利率',
    'eps': '每股收益',
}


def new_conversation_context() -> dict[str, Any]:
    return {
        'primary_company': None,
        'compare_company': None,
        'years': [],
        'metrics': [],
        'intent': None,
        'awaiting_clarification': False,
        'clarification': None,
    }


def _contains_any(text: str, words: list[str]) -> bool:
    lowered = text.lower()
    return any(word.lower() in lowered for word in words)


def _explicit_years(question: str) -> list[int]:
    return _extract_years(question)


def _explicit_metrics(question: str) -> list[str]:
    return _detect_metrics(question)


def _company_alias(name: str) -> str:
    return name.replace('股份有限公司', '').replace('新能源科技', '').replace('有限公司', '')


def _explicit_companies(question: str, companies: list[str]) -> list[str]:
    question = normalize_company_references(question, companies)
    return explicit_company_names(question, companies, _stock_code_aliases(question, companies))


def _ambiguous_company_candidates(question: str, companies: list[str]) -> list[str]:
    ambiguous = ambiguous_company_names(question, companies, _stock_code_aliases(question, companies))
    if ambiguous:
        return ambiguous
    exact = _explicit_companies(question, companies)
    if exact:
        return exact
    candidates = []
    for name in companies:
        alias = _company_alias(name)
        for size in range(min(4, len(alias)), 1, -1):
            if any(alias[start:start + size] in question for start in range(len(alias) - size + 1)):
                candidates.append(name)
                break
    return candidates


def _is_follow_up(question: str, explicit: dict[str, list[Any]]) -> bool:
    condition_count = sum(bool(explicit[key]) for key in ['companies', 'years', 'metrics'])
    return condition_count < 3 and _contains_any(question, FOLLOW_UP_MARKERS)


def _clarification(
    context: dict[str, Any],
    field: str,
    question: str,
    options: list[dict[str, str]],
    pending: dict[str, Any],
) -> dict[str, Any]:
    context = dict(context)
    context['awaiting_clarification'] = True
    context['clarification'] = {
        'field': field,
        'question': question,
        'options': options,
        'pending': pending,
    }
    return {
        'status': 'needs_clarification',
        'field': field,
        'question': question,
        'options': options,
        'context': context,
    }


def resolve_turn(
    context: dict[str, Any] | None,
    question: str,
    companies: list[str],
    page_company: str | None = None,
    page_compare: str | None = None,
    page_year: int | None = None,
    profile: str = '平衡型',
) -> dict[str, Any]:
    """Resolve one user turn with explicit > page > history > default priority."""
    context = dict(context or new_conversation_context())
    explicit_companies = _explicit_companies(question, companies)
    candidate_companies = _ambiguous_company_candidates(question, companies)
    ambiguous_reference = bool(ambiguous_company_names(question, companies, _stock_code_aliases(question, companies)))
    explicit_years = _explicit_years(normalize_company_references(question, companies))
    explicit_metrics = _explicit_metrics(question)
    parsed = parse_question(question, companies, '', profile, None)
    unknown = parsed.get('unknown_companies') or []
    if unknown and not (ambiguous_reference or (not explicit_companies and len(candidate_companies) > 1)):
        return {
            'status': 'ready', 'question': question, 'context': context,
            'resolved': {**parsed, 'companies': explicit_companies + unknown,
                         'reason': '问题包含未接入或无法确认的企业，禁止使用页面企业替代。'},
        }
    intent = parsed.get('intent') or 'unknown'
    explicit = {'companies': explicit_companies, 'years': explicit_years, 'metrics': explicit_metrics}
    follow_up = _is_follow_up(question, explicit)
    history_years = list(context.get('years') or [])
    if not explicit_years and history_years:
        if _contains_any(question, ['上一年', '前一年']):
            explicit_years = [year - 1 for year in history_years]
        elif _contains_any(question, ['下一年', '后一年']):
            explicit_years = [year + 1 for year in history_years]
    history_metrics = list(context.get('metrics') or [])
    inherited_years = explicit_years or (history_years if follow_up else []) or ([page_year] if page_year else [])
    inherited_metrics = explicit_metrics or (history_metrics if follow_up else [])
    inherited_intent = intent
    if inherited_intent == 'unknown' and follow_up and context.get('intent'):
        inherited_intent = context['intent']

    if ambiguous_reference or (not explicit_companies and len(candidate_companies) > 1):
        pending = {
            'question': question, 'intent': inherited_intent, 'companies': [], 'years': inherited_years,
            'metrics': inherited_metrics, 'page_company': page_company, 'page_compare': page_compare,
            'page_year': page_year, 'follow_up': follow_up,
        }
        return _clarification(
            context, 'company', '你提到的企业名称可能对应多家公司，请选择目标企业。',
            [{'label': name, 'value': name} for name in candidate_companies], pending,
        )

    uses_pronoun = _contains_any(question, PRONOUNS)
    history_company = context.get('primary_company')
    asks_other_company = '另一家' in question
    other_company = None
    if asks_other_company and not explicit_companies:
        comparison_primary = history_company if history_company in companies else page_company
        for candidate in [context.get('compare_company'), page_compare]:
            if candidate in companies and candidate != comparison_primary:
                other_company = candidate
                break
        if other_company is None:
            preserve_primary = intent == 'company_compare' and comparison_primary in companies
            pending = {
                'question': question, 'intent': inherited_intent,
                'companies': [comparison_primary] if preserve_primary else [], 'years': inherited_years,
                'metrics': inherited_metrics, 'page_company': page_company, 'page_compare': page_compare,
                'page_year': page_year, 'follow_up': True,
            }
            options = [{'label': name, 'value': name} for name in companies if name != comparison_primary]
            field = 'compare_company' if preserve_primary else 'company'
            return _clarification(context, field, '你说的“另一家”是指哪家公司？', options, pending)
    if uses_pronoun and not explicit_companies and not page_company and not history_company:
        pending = {
            'question': question, 'intent': inherited_intent, 'companies': [], 'years': inherited_years,
            'metrics': inherited_metrics, 'page_company': None, 'page_compare': page_compare,
            'page_year': page_year, 'follow_up': True,
        }
        return _clarification(
            context, 'company', '当前上下文不足以确定你指的是哪家公司，请选择目标企业。',
            [{'label': name, 'value': name} for name in companies], pending,
        )

    if explicit_companies:
        primary = explicit_companies[0]
    elif other_company and intent != 'company_compare':
        primary = other_company
    elif follow_up and history_company in companies:
        primary = history_company
    elif page_company in companies:
        primary = page_company
    elif history_company in companies:
        primary = history_company
    else:
        primary = None
    if primary is None:
        pending = {
            'question': question, 'intent': inherited_intent, 'companies': [], 'years': inherited_years,
            'metrics': inherited_metrics, 'page_company': page_company, 'page_compare': page_compare,
            'page_year': page_year, 'follow_up': follow_up,
        }
        return _clarification(
            context, 'company', '你想查询哪家公司？',
            [{'label': name, 'value': name} for name in companies], pending,
        )

    years = explicit_years or (history_years if follow_up else []) or ([page_year] if page_year else []) or history_years
    metrics = explicit_metrics or list(context.get('metrics') or [])
    if intent == 'unknown' and follow_up and context.get('intent'):
        intent = context['intent']
    if explicit_metrics and intent == 'unknown':
        intent = 'finance_query'

    asks_for_value = _contains_any(question, ['多少', '财务数据', '财务指标', '查一下', '查询'])
    needs_metric = intent in {'finance_query', 'trend_analysis'} or asks_for_value or (
        follow_up and intent not in {'company_compare', 'risk_warning', 'report_generate', 'investment_summary'}
    )
    if needs_metric and not metrics:
        pending = {
            'question': question, 'intent': intent, 'companies': [primary], 'years': years,
            'metrics': [], 'page_company': page_company, 'page_compare': page_compare,
            'page_year': page_year, 'follow_up': follow_up,
        }
        return _clarification(
            context, 'metric', '你想查询哪项财务指标？',
            [{'label': label, 'value': key} for key, label in METRIC_LABELS.items()], pending,
        )

    compare_company = None
    if intent == 'company_compare':
        if len(explicit_companies) > 1:
            compare_company = explicit_companies[1]
        elif other_company and other_company != primary:
            compare_company = other_company
        elif page_compare in companies and page_compare != primary:
            compare_company = page_compare
        elif context.get('compare_company') in companies and context.get('compare_company') != primary:
            compare_company = context['compare_company']
        if compare_company is None:
            options = [{'label': name, 'value': name} for name in companies if name != primary]
            pending = {
                'question': question, 'intent': intent, 'companies': [primary], 'years': years,
                'metrics': metrics, 'page_company': page_company, 'page_compare': page_compare,
                'page_year': page_year, 'follow_up': follow_up,
            }
            return _clarification(context, 'compare_company', f'你想将{primary}与哪家公司对比？', options, pending)

    resolved_companies = explicit_companies or ([primary] + ([compare_company] if compare_company else []))
    period = _detect_period(question) or (context.get('period') if follow_up else None)
    context.update({
        'primary_company': primary,
        'compare_company': compare_company or context.get('compare_company'),
        'years': years,
        'metrics': metrics,
        'intent': intent,
        'period': period,
        'awaiting_clarification': False,
        'clarification': None,
    })
    return {
        'status': 'ready',
        'question': question,
        'resolved': {
            'intent': intent,
            'companies': resolved_companies,
            'years': years,
            'metrics': metrics,
            'investor_profile': profile,
            'reason': '会话上下文解析',
            'period': period,
            'explicit_scope': parsed.get('explicit_scope', {}),
        },
        'context': context,
    }


def confirm_clarification(
    context: dict[str, Any],
    value: str,
    companies: list[str],
    profile: str = '平衡型',
) -> dict[str, Any]:
    clarification = (context or {}).get('clarification')
    if not clarification:
        raise ValueError('当前没有待确认的澄清问题。')
    pending = dict(clarification['pending'])
    field = clarification['field']
    resolved_companies = list(pending.get('companies') or [])
    if field == 'company':
        resolved_companies = [value]
    elif field == 'compare_company':
        resolved_companies = resolved_companies[:1] + [value]
    elif field == 'metric':
        pending['metrics'] = [value]

    primary = resolved_companies[0] if resolved_companies else None
    compare_company = resolved_companies[1] if len(resolved_companies) > 1 else None
    updated = dict(context)
    updated.update({
        'primary_company': primary,
        'compare_company': compare_company or updated.get('compare_company'),
        'years': list(pending.get('years') or []),
        'metrics': list(pending.get('metrics') or []),
        'intent': pending.get('intent') or updated.get('intent') or 'finance_query',
        'awaiting_clarification': False,
        'clarification': None,
    })
    return {
        'status': 'ready',
        'question': pending['question'],
        'resolved': {
            'intent': updated['intent'],
            'companies': resolved_companies,
            'years': updated['years'],
            'metrics': updated['metrics'],
            'investor_profile': profile,
            'reason': '用户已确认澄清条件',
        },
        'context': updated,
    }
