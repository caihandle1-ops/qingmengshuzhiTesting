from __future__ import annotations

from typing import Any
from core.retrieval import is_explanation_query


DOCUMENT_TERMS = {
    '年报', '报告中', '披露', '原文', '提到', '描述', '管理层', '研发投入',
    '海外业务', '业务因素', '怎么解释', '如何解释', '原因', '为什么',
}
QUANTITATIVE_TERMS = {'多少', '是多少', '分别是多少', '数值', '数据', '同比', '变化多少'}
STRUCTURED_INTENTS = {'finance_query', 'trend_analysis', 'company_compare'}


def plan_query(question: str, parsed_context: dict[str, Any]) -> dict[str, str]:
    """Select a deterministic, auditable data route without model dependency."""
    intent = parsed_context.get('intent') or 'unknown'
    has_metric = bool(parsed_context.get('metrics'))
    asks_document = any(term in question for term in DOCUMENT_TERMS)
    asks_explanation = is_explanation_query(question)
    asks_quantity = any(term in question for term in QUANTITATIVE_TERMS)

    if has_metric and asks_document and asks_quantity:
        structured_intent = intent if intent in STRUCTURED_INTENTS else 'finance_query'
        return {
            'route': 'hybrid', 'structured_intent': structured_intent,
            'reason': '问题要求结构化财务数值及报告证据，数值由 SQL 提供。',
        }
    if asks_explanation and (has_metric or intent in STRUCTURED_INTENTS or len(parsed_context.get('companies') or []) > 1):
        plan = {'route': 'hybrid', 'reason': '问题同时需要结构化财务事实与报告中的原因解释。'}
        if len(parsed_context.get('companies') or []) > 1:
            plan['structured_intent'] = 'company_compare'
        else:
            plan['structured_intent'] = intent if intent in STRUCTURED_INTENTS else 'finance_query'
        return plan
    if asks_document or (intent == 'risk_warning' and ('年报' in question or '报告' in question)):
        return {'route': 'rag', 'reason': '问题要求从已接入报告中查找定性描述或原文依据。'}
    return {'route': 'sql', 'reason': '问题可由结构化财务数据或既有可信分析链路回答。'}
