"""Describe the origin and gaps of the exact annual facts used in a view.

These labels identify ingestion, never verify the truth of a financial value.
No database access or filesystem lookup is performed, and summaries contain no
source paths, URLs, import identifiers, or untrusted source descriptions.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json
import math
from typing import Any, Iterable, Mapping


METRIC_LABELS = {
    'revenue': '营业收入', 'net_profit': '归母净利润',
    'operating_cashflow': '经营现金流', 'roe': 'ROE',
    'debt_ratio': '资产负债率', 'gross_margin': '毛利率', 'eps': '每股收益',
    'consolidated_net_profit': '合并净利润', 'total_assets': '总资产',
    'total_liabilities': '总负债', 'total_equity': '所有者权益',
    'minority_profit': '少数股东损益', 'audit_opinion': '审计意见',
}
DEFAULT_METRICS = tuple(METRIC_LABELS)[:7]
IDENTITY_LABELS = {'builtin': '内置样本', 'imported': '用户导入',
                   'mixed': '混合来源', 'unknown': '来源待核验'}
_IMPORT_TYPES = {'upload', 'exchange', 'direct_url'}


def _records(rows: Any) -> list[dict]:
    if hasattr(rows, 'to_dict'):
        rows = rows.to_dict('records')
    return [dict(row) for row in (rows if rows is not None else []) if isinstance(row, Mapping)]


def _year(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number.is_integer() and 1900 <= number <= 2200 else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or type(value).__name__ == 'bool_':
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _valid_value(value: Any, metric: str) -> bool:
    if metric == 'audit_opinion':
        return isinstance(value, str) and value.strip() not in {'', '-', '--', '暂无', '暂无数据', '未记录'}
    return _number(value) is not None


def _same_value(left: Any, right: Any, metric: str) -> bool:
    return str(left).strip() == str(right).strip() if metric == 'audit_opinion' else _number(left) == _number(right)


def source_identity(source: Any, *, value: Any = None, metric: str = '') -> str:
    """Classify structured per-metric metadata; names/paths are not evidence.

    ``source_origin`` is attached from the report registry when workspace data
    is loaded. ``builtin-v1`` is the explicit token emitted by the bundled seed.
    A legacy, stale, or value-mismatched source remains unknown.
    """
    if not isinstance(source, Mapping) or source.get('is_current') in (0, False):
        return 'unknown'
    if 'value_json' in source and value is not None:
        try:
            stored = json.loads(source['value_json'])
            if not _valid_value(stored, metric) or not _same_value(value, stored, metric):
                return 'unknown'
        except (TypeError, ValueError):
            return 'unknown'
    import_id = str(source.get('import_id') or '')
    if import_id.startswith('legacy-') or 'unverified' in import_id:
        return 'unknown'
    # A registry enrichment that detects conflicting identity takes precedence.
    if source.get('source_origin') == 'unknown':
        return 'unknown'
    if import_id == 'builtin-v1' or source.get('source_origin') == 'builtin':
        return 'builtin'
    if source.get('source_origin') == 'imported' or source.get('source_type') in _IMPORT_TYPES:
        return 'imported'
    return 'unknown'


def attach_source_origins(rows: Any, reports: Iterable[Mapping]) -> list[dict]:
    """Copy rows and join source identity to exact report import identities.

    Company and path equality constrain the join when both sides provide them.
    Multiple annual registrations of the same imported file are valid. Mixed
    or conflicting metadata never becomes a single trusted identity. The
    existing provenance is preserved for downstream source viewing.
    """
    result = deepcopy(_records(rows))
    registry = [dict(report) for report in reports]
    for row in result:
        provenance = row.get('metric_provenance')
        if not isinstance(provenance, dict):
            continue
        for metric, source in provenance.items():
            if not isinstance(source, dict):
                continue
            import_id = source.get('import_id')
            candidates = [report for report in registry
                          if import_id and report.get('import_id') == import_id
                          and (source.get('company_id') is None or report.get('company_id') == source['company_id'])
                          and (not source.get('file_path') or not report.get('file_path')
                               or source['file_path'] == report['file_path'])]
            types = {report.get('source_type') for report in candidates}
            if import_id == 'builtin-v1' and (not types or types == {'builtin'}):
                origin, source_type = 'builtin', 'builtin'
            elif import_id != 'builtin-v1' and len(types) == 1 and next(iter(types)) in _IMPORT_TYPES:
                origin, source_type = 'imported', next(iter(types))
            else:
                origin, source_type = 'unknown', None
            source['source_origin'] = origin
            if source_type:
                source['source_type'] = source_type
            else:
                source.pop('source_type', None)
    return result


def summarize_data_identity(rows: Any, *, metrics: Iterable[str] | None = None,
                            years: Iterable[int] | None = None,
                            reports: Iterable[Mapping] | None = None) -> dict:
    """Summarize only selected metrics/years for one company's facts.

    Null, NaN, infinity and booleans never count as numeric observations. An
    absent requested year is a real gap. Without requested years, gaps between
    the earliest and latest supplied annual records are included. Repeated
    identical cells count once; conflicting duplicate cells remain unknown.
    """
    records = attach_source_origins(rows, reports) if reports is not None else _records(rows)
    metric_keys = tuple(dict.fromkeys(key for key in (DEFAULT_METRICS if metrics is None else metrics)
                                     if key in METRIC_LABELS))
    row_years = sorted({_year(row.get('year')) for row in records} - {None})
    requested = sorted({_year(year) for year in years} - {None}) if years is not None else (
        list(range(row_years[0], row_years[-1] + 1)) if row_years else [])
    cells: dict[tuple[int, str], list[tuple[Any, str]]] = {}
    for row in records:
        year = _year(row.get('year'))
        if year not in requested:
            continue
        provenance = row.get('metric_provenance')
        provenance = provenance if isinstance(provenance, Mapping) else {}
        for metric in metric_keys:
            value = row.get(metric)
            if _valid_value(value, metric):
                cells.setdefault((year, metric), []).append(
                    (value, source_identity(provenance.get(metric), value=value, metric=metric)))
    counts = Counter({'builtin': 0, 'imported': 0, 'unknown': 0})
    for (_year_value, metric), facts in cells.items():
        kinds = {kind for _, kind in facts}
        coherent = all(_same_value(facts[0][0], value, metric) for value, _ in facts)
        counts[next(iter(kinds)) if len(kinds) == 1 and coherent else 'unknown'] += 1
    present_kinds = [kind for kind, count in counts.items() if count]
    kind = present_kinds[0] if len(present_kinds) == 1 else ('mixed' if present_kinds else 'unknown')
    covered_years = sorted({year for year, _metric in cells})
    missing = {year: [key for key in metric_keys if (year, key) not in cells] for year in requested}
    missing = {year: keys for year, keys in missing.items() if keys}
    gap_parts = []
    for year, keys in list(missing.items())[:3]:
        labels = '、'.join(METRIC_LABELS[key] for key in keys[:4])
        gap_parts.append(f'{year}年：{labels}' + (f'等{len(keys)}项' if len(keys) > 4 else ''))
    if len(missing) > 3:
        gap_parts.append(f'另有{len(missing) - 3}个年度存在缺项')
    notes = {
        'builtin': '本次展示使用随系统提供的样本指标；数据身份不代表原始财报已核验。',
        'imported': '本次展示使用已接入的用户资料；导入身份不代表原文或数值已核验。',
        'mixed': '本次展示包含不同数据身份，请逐项核对来源后比较。',
        'unknown': '现有逐指标记录不足以确认接入身份，来源仍待核验。',
    }
    return {
        'kind': kind, 'label': IDENTITY_LABELS[kind], 'counts': dict(counts),
        'metrics': list(metric_keys), 'years': covered_years, 'requested_years': requested,
        'coverage_text': '、'.join(map(str, covered_years)) + ' 年' if covered_years else '暂无有效年度',
        'metric_count': len(cells), 'missing_count': sum(map(len, missing.values())),
        'missing_by_year': missing,
        'missing_text': '；'.join(gap_parts) if gap_parts else ('当前范围内所选指标完整' if cells else '暂无可用指标'),
        'note': notes[kind],
    }


def identity_count_text(summary: Mapping) -> str:
    return ' · '.join(f'{IDENTITY_LABELS[kind]} {summary["counts"][kind]} 项'
                      for kind in ('builtin', 'imported', 'unknown') if summary['counts'].get(kind)) or '暂无有效指标'


def identity_report_lines(summary: Mapping) -> list[str]:
    """Small fixed-format introduction suitable for both text and PDF reports."""
    return [f'数据身份：{summary["label"]}（{identity_count_text(summary)}）。',
            f'有效指标覆盖年度：{summary["coverage_text"]}。',
            f'指标缺项：{summary["missing_text"]}。', summary['note']]
