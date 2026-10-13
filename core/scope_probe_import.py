"""Reviewed, independently labelled financial facts for the scope probe.

Legacy ``net_profit`` stays attributable-to-parent profit. Ambiguous legacy
columns are never promoted into a second independent fact for the probe.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import re

from core.db import get_conn

FIELD_METRICS = {'net_profit': 'parent_net_profit',
                 'consolidated_net_profit': 'consolidated_net_profit'}


def _currency(value):
    text = re.sub(r'\s+', '', str(value or '')).upper()
    return 'CNY' if text in ('CNY', 'RMB', '人民币', '人民币元') else text or None


def _scope(value):
    text = re.sub(r'\s+', '', str(value or '')).lower()
    if text in ('consolidated', '合并', '合并报表', '合并范围', '合并利润表'):
        return 'consolidated'
    return text or None


def annotate_probe_records(records: list[dict], *, currency=None, statement_scope=None,
                           report_version=None) -> tuple[list[dict], list[str], list[str]]:
    """Attach metadata without inventing missing accounting conditions.

    Explicit source metadata wins. Manual confirmations only fill unknowns;
    conflicts block the import. Unknown conditions remain visible in preview.
    """
    facts, warnings, errors = [], [], []
    manual_currency, manual_scope = _currency(currency), _scope(statement_scope)
    for record in records:
        # Preserve accounting conditions for every independently sourced input,
        # including assets/liabilities/equity and minority interests. Unknown
        # historical metadata stays unknown; no database backfill guesses it.
        for source in record.get('_provenance', {}).values():
            actual_currency, actual_scope = _currency(source.get('currency_hint')), _scope(source.get('scope_hint'))
            if actual_currency and manual_currency and actual_currency != manual_currency:
                errors.append(f'{record.get("company_name")} {record.get("year")}：确认币种与原始来源冲突。')
            if actual_scope and manual_scope and actual_scope != manual_scope:
                errors.append(f'{record.get("company_name")} {record.get("year")}：确认报表范围与原始来源冲突。')
            if source.get('version_hint') and report_version and str(source['version_hint']) != str(report_version):
                errors.append(f'{record.get("company_name")} {record.get("year")}：确认披露版本与原始来源冲突。')
            basis = {'currency': 'source' if actual_currency else 'manual_confirmation' if manual_currency else 'unknown',
                     'statement_scope': 'source' if actual_scope else 'manual_confirmation' if manual_scope else 'unknown',
                     'report_version': 'source' if source.get('version_hint') else 'manual_confirmation' if report_version else 'unknown'}
            source['metadata_basis'] = basis
            source['currency_hint'] = actual_currency or manual_currency
            source['scope_hint'] = actual_scope or manual_scope
            source['version_hint'] = source.get('version_hint') or report_version or 'unknown'
            if any(value == 'manual_confirmation' for value in basis.values()):
                source['source_note'] = '原件定位已复核；补充条件由用户明确确认：' + json.dumps(basis, ensure_ascii=False, sort_keys=True)
        for field, metric in FIELD_METRICS.items():
            source = record.get('_provenance', {}).get(field)
            if not source or record.get(field) is None:
                continue
            label = re.sub(r'[\s（）()]', '', str(source.get('metric_label') or '')).lower()
            # English net_profit is the documented legacy parent-profit contract.
            parent_label = ('归母' in label or '归属于' in label or
                            label.startswith('net_profit') or label.startswith('parent_net_profit'))
            total_label = (any(word in label for word in ('合并净利润', '合并报表净利润', '净利润合计',
                           'consolidated_net_profit', 'total_net_profit')) or
                           (label.startswith('净利润') and source.get('scope_hint') == 'consolidated'))
            if (field == 'net_profit' and not parent_label) or (field == 'consolidated_net_profit' and not total_label):
                warnings.append(f'{record.get("company_name")} {record.get("year")} {source.get("metric_label") or field}：指标定义未明确，保留普通导入，暂不作为口径探针事实。')
                continue
            actual_currency, actual_scope = _currency(source.get('currency_hint')), _scope(source.get('scope_hint'))
            if actual_currency and manual_currency and actual_currency != manual_currency:
                errors.append(f'{record.get("company_name")} {record.get("year")}：确认币种与原始来源冲突。')
            if actual_scope and manual_scope and actual_scope != manual_scope:
                errors.append(f'{record.get("company_name")} {record.get("year")}：确认报表范围与原始来源冲突。')
            fact_currency, fact_scope = actual_currency or manual_currency, actual_scope or manual_scope
            if fact_currency and fact_currency != 'CNY':
                errors.append('探针当前只支持人民币亿元，不自动换汇；请上传人民币同口径原始数据。')
            if fact_scope and fact_scope != 'consolidated':
                warnings.append('非合并报表范围不进入当前双企业探针比较。')
            version = source.get('version_hint') or report_version or 'unknown'
            meta = {'metric': metric, 'currency': fact_currency, 'period': 'annual',
                    'statement_scope': fact_scope, 'report_version': str(version),
                    'metadata_basis': deepcopy(source['metadata_basis'])}
            source['scope_probe'] = meta
            fact = {'company_name': record.get('company_name'), 'year': record.get('year'),
                    'value': record[field], **deepcopy(meta), 'evidence': deepcopy(source)}
            facts.append(fact)
            if not fact_currency or not fact_scope:
                warnings.append(f'{record.get("company_name")} {record.get("year")} {metric}：币种或合并范围未知，探针将提示证据不足；可按原件补充确认。')
    return facts, list(dict.fromkeys(warnings)), list(dict.fromkeys(errors))


def persist_probe_facts(company_id: int, record: dict) -> int:
    """Use the caller's atomic import transaction and only changed source fields."""
    conn = get_conn()
    count = 0
    try:
        for field, metric in FIELD_METRICS.items():
            source = record.get('_provenance', {}).get(field) or {}
            meta = source.get('scope_probe')
            if record.get(field) is None or not meta:
                continue
            evidence = deepcopy(source)
            evidence['reviewed'] = True
            conn.execute('UPDATE scope_probe_facts SET is_current=0 WHERE company_id=? AND year=? AND metric=?',
                         (company_id, record['year'], metric))
            conn.execute('''INSERT INTO scope_probe_facts
                (company_id,year,metric,value,currency,period,statement_scope,report_version,
                 evidence_json,import_id,created_at,is_current) VALUES (?,?,?,?,?,?,?,?,?,?,?,1)''',
                (company_id, record['year'], metric, record[field], meta.get('currency'),
                 meta['period'], meta.get('statement_scope'), meta['report_version'],
                 json.dumps(evidence, ensure_ascii=False, sort_keys=True), source['import_id'],
                 datetime.now(timezone.utc).isoformat()))
            count += 1
        conn.commit()
    finally:
        conn.close()
    return count
