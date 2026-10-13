"""A bounded, evidence-backed financial conclusion-flip probe.

Version 1 compares two enterprises' annual CNY consolidated-statement profits.
Its only enumerated ambiguity is total vs attributable-to-parent net profit.
It does not claim universal stability, generate missing facts, or use model
confidence as a replacement for financial evidence.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
from itertools import combinations
import json
import math
import re
import sqlite3

from core.db import get_conn

METRIC_LABELS = {'consolidated_net_profit': '合并净利润', 'parent_net_profit': '归母净利润'}
METRIC_ALIASES = {'net_profit': 'parent_net_profit', '归母净利润': 'parent_net_profit',
                  '归母': 'parent_net_profit', '合并净利润': 'consolidated_net_profit'}
PROBE_SCOPE = '仅覆盖两家企业、全年、人民币亿元、合并报表范围下的合并净利润与归母净利润；会计政策及未知披露版本仍需人工核验。'


def load_scope_probe_facts(company_names=None) -> list[dict]:
    """Read reviewed typed facts; never manufacture them from legacy metrics."""
    conn = get_conn()
    try:
        rows = conn.execute('''SELECT f.*,c.name AS company_name FROM scope_probe_facts f
            JOIN companies c ON c.id=f.company_id WHERE f.is_current=1
            ORDER BY c.name,f.year,f.metric,f.id''').fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    wanted = set(company_names or [])
    return [{'company_name': row['company_name'], 'year': row['year'], 'metric': row['metric'],
             'value': row['value'], 'currency': row['currency'], 'period': row['period'],
             'statement_scope': row['statement_scope'], 'report_version': row['report_version'],
             'import_id': row['import_id'], 'evidence': json.loads(row['evidence_json'])}
            for row in rows if not wanted or row['company_name'] in wanted]


def _metric(value):
    return METRIC_ALIASES.get(value, value) if value else None


def _explicit_metric(question):
    parent = bool(re.search(r'归母|归属于(?:上市公司|母公司)(?:的)?(?:股东|所有者)', question))
    total = bool(re.search(r'合并(?:报表)?净利润|净利润合计', question))
    return 'ambiguous_explicit' if parent and total else 'parent_net_profit' if parent else 'consolidated_net_profit' if total else None


def _position(fact):
    evidence = fact.get('evidence') or {}
    return (fact.get('company_name'), fact.get('year'), evidence.get('import_id') or fact.get('import_id'),
            evidence.get('file_name'), evidence.get('page'), evidence.get('cell'))


def _fact_problem(fact):
    value, evidence = fact.get('value'), fact.get('evidence') or {}
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return '缺少有限数值'
    if fact.get('currency') != 'CNY':
        return '币种未确认或当前不支持'
    if fact.get('period') != 'annual':
        return '全年期间未确认'
    if fact.get('statement_scope') != 'consolidated':
        return '合并报表范围未确认'
    if (evidence.get('reviewed') is not True or not evidence.get('file_name') or not evidence.get('cell')
            or evidence.get('raw_value') in (None, '') or not evidence.get('raw_unit')
            or evidence.get('normalized_unit') != '亿元'):
        return '缺少已复核的原始值、单位或来源定位'
    from core.import_validation import normalize_metric
    try:
        raw_normalized, _, _ = normalize_metric('net_profit', evidence['raw_value'], evidence['raw_unit'])
    except ValueError:
        return '来源原始金额或单位无法复算'
    if not math.isclose(value, raw_normalized, rel_tol=1e-12, abs_tol=1e-10):
        return '事实值与原始金额单位换算不一致'
    label = re.sub(r'[\s（）()]', '', str(evidence.get('metric_label') or '')).lower()
    if fact.get('metric') == 'parent_net_profit':
        if not ('归母' in label or '归属于' in label or label.startswith(('net_profit', 'parent_net_profit'))):
            return '原始标签未证明归母利润定义'
    elif fact.get('metric') == 'consolidated_net_profit':
        if not (any(token in label for token in ('合并净利润', '合并报表净利润', '净利润合计',
                 'consolidated_net_profit', 'total_net_profit')) or
                 (label.startswith('净利润') and evidence.get('scope_hint') == 'consolidated')):
            return '原始标签未证明合并净利润定义'
    return None


def _candidate(metric, names, year, base_year, facts, comparison):
    item = {'metric': metric, 'label': METRIC_LABELS[metric], 'status': 'insufficient_evidence',
            'conditions': {'metric': metric, 'period': 'annual', 'currency': 'CNY',
                           'normalized_unit': '亿元', 'statement_scope': 'consolidated',
                           'year': year, 'base_year': base_year if comparison == 'growth' else None,
                           'comparison': comparison},
            'values': {}, 'growth_rates': {}, 'winner': None, 'ranking': [],
            'conclusion': '', 'evidence': [], 'missing': []}
    for name in names:
        years = [base_year, year] if comparison == 'growth' else [year]
        matched = []
        for fiscal_year in years:
            rows = [fact for fact in facts if fact.get('company_name') == name and
                    fact.get('year') == fiscal_year and fact.get('metric') == metric]
            if not rows:
                item['missing'].append(f'{name} {fiscal_year} {METRIC_LABELS[metric]}：缺少独立事实')
                continue
            if len(rows) != 1:
                item['missing'].append(f'{name} {fiscal_year}：存在多个事实版本，请先确认当前版本')
                continue
            fact = rows[0]
            problem = _fact_problem(fact)
            if problem:
                item['missing'].append(f'{name} {fiscal_year}：{problem}')
                continue
            matched.append(fact)
            item['evidence'].append(deepcopy(fact))
        if len(matched) == len(years):
            item['values'][name] = {str(fact['year']): fact['value'] for fact in matched}
            if comparison == 'growth':
                versions = {fact.get('report_version') for fact in matched if fact.get('report_version') not in (None, '', 'unknown')}
                if len(versions) > 1:
                    item['missing'].append(f'{name}：两年披露版本不同，需核验是否使用同一重述口径')
                elif matched[0]['value'] <= 0:
                    item['missing'].append(f'{name}：基年利润非正，当前不输出普通增长率排名')
                else:
                    item['growth_rates'][name] = (matched[1]['value'] - matched[0]['value']) / matched[0]['value'] * 100
    if item['missing']:
        item['conclusion'] = '该口径证据不足，无法用缺失数据证明结论稳定。'
        return item
    scores = item['growth_rates'] if comparison == 'growth' else {name: item['values'][name][str(year)] for name in names}
    if math.isclose(scores[names[0]], scores[names[1]], rel_tol=1e-12, abs_tol=1e-12):
        item['ranking'], item['winner'] = [list(names)], 'tie'
        item['conclusion'] = f'按{METRIC_LABELS[metric]}，两家企业的' + ('增长率' if comparison == 'growth' else '利润金额') + '相同。'
    else:
        ranking = sorted(names, key=lambda name: scores[name], reverse=True)
        item['ranking'], item['winner'] = ranking, ranking[0]
        item['conclusion'] = f'按{METRIC_LABELS[metric]}，{ranking[0]}的' + ('增长率' if comparison == 'growth' else '利润金额') + f'高于{ranking[1]}。'
    item['status'] = 'valid'
    return item


def _minimum_conditions(candidates):
    """Exact minimum separating set within this finite enumerated candidate set."""
    opposite_pairs = [(left, right) for index, left in enumerate(candidates) for right in candidates[index + 1:]
                      if left['status'] == right['status'] == 'valid' and left['winner'] != right['winner']]
    fields = ['metric']
    for size in range(len(fields) + 1):
        for selected in combinations(fields, size):
            if all(any(left['conditions'][field] != right['conditions'][field] for field in selected)
                   for left, right in opposite_pairs):
                return list(selected)
    return fields


def run_scope_probe(company_names, year, *, base_year=None, question='', metric=None,
                    output_mode='ranking', selections=None, facts=None, comparison=None) -> dict:
    """Compare independent profit definitions and return a JSON-ready snapshot.

    ``facts=result['evidence_snapshot']`` replays the captured data, independent
    of later database changes. ``selections={'metric': ...}`` narrows a question.
    An explicit metric in the question has priority over that selection.
    ``comparison`` is ``value`` or ``growth``; when omitted, growth/同比 words
    select growth and other questions compare the requested year's amount.
    """
    names = list(dict.fromkeys(str(name).strip() for name in company_names if str(name).strip()))
    question = str(question or '')
    if re.search(r'是多少|分别多少|具体(?:数值|金额)|精确(?:数值|金额)', question):
        output_mode = 'exact'
    comparison = comparison or ('growth' if re.search(r'增长|增速|同比', question) else 'value')
    result = {'schema_version': 'scope-probe/1', 'status': 'unsupported', 'message': '',
              'scope': PROBE_SCOPE, 'candidates': [], 'flipped': False, 'minimal_conditions': [],
              'clarification': None, 'evidence_snapshot': [], 'condition_snapshot': {},
              'six_dimensions': [], 'warnings': [], 'snapshot_id': None}
    if (len(names) != 2 or isinstance(year, bool) or not isinstance(year, int) or not 1900 <= year <= 2100
            or comparison not in ('value', 'growth') or output_mode not in ('ranking', 'exact')):
        result['message'] = '当前探针支持两家不同企业、明确全年年度、利润金额或增长率比较。'
        return result
    base_year = year - 1 if base_year is None else base_year
    if comparison == 'growth' and (isinstance(base_year, bool) or not isinstance(base_year, int) or base_year != year - 1):
        result['message'] = '当前增长率探针仅支持相邻两个完整年度。'
        return result
    if re.search(r'季度|半年度|上半年|下半年|母公司报表|母公司利润表|扣非', question):
        result['message'] = '该问题超出全年合并报表利润探针范围，请另行确认期间或指标定义。'
        return result
    explicit = _explicit_metric(question)
    selected = _metric(explicit or metric or (selections or {}).get('metric'))
    if selected == 'ambiguous_explicit' or (selected and selected not in METRIC_LABELS):
        result['message'] = '问题同时指定多种利润定义或指定了暂不支持的指标，请拆分问题。'
        return result
    if explicit and (selections or {}).get('metric') and selected != _metric(selections['metric']):
        result['warnings'].append('沿用问题中明确指定的指标，未采用与原问题冲突的按钮选择。')
    raw_facts = deepcopy(load_scope_probe_facts(names) if facts is None else facts)
    relevant = [fact for fact in raw_facts if isinstance(fact, dict) and fact.get('company_name') in names and
                fact.get('year') in ([base_year, year] if comparison == 'growth' else [year]) and fact.get('metric') in METRIC_LABELS]
    reused = {pos for pos in {_position(fact) for fact in relevant}
              if len({fact['metric'] for fact in relevant if _position(fact) == pos}) > 1}
    for fact in relevant:
        if _position(fact) in reused:
            fact.setdefault('evidence', {})['reviewed'] = False
    if reused:
        result['warnings'].append('同一原始单元格被重复标成两个指标，已拒绝作为独立事实。')
    result['evidence_snapshot'] = relevant
    allowed = [selected] if selected else list(METRIC_LABELS)
    candidates = [_candidate(key, names, year, base_year, relevant, comparison) for key in allowed]
    result['candidates'] = candidates
    valid = [candidate for candidate in candidates if candidate['status'] == 'valid']
    result['flipped'] = len({candidate['winner'] for candidate in valid}) > 1
    result['minimal_conditions'] = _minimum_conditions(candidates) if result['flipped'] else []
    options = [{'value': key, 'label': METRIC_LABELS[key]} for key in METRIC_LABELS]
    if len(valid) != len(candidates):
        result.update(status='insufficient_evidence', message='至少一个合法口径缺少可比较证据，当前不能证明结论稳定。')
    elif not selected and (result['flipped'] or output_mode == 'exact'):
        result.update(status='flipped' if result['flipped'] else 'needs_clarification',
                      message='合法利润口径改变了比较结论，请只确认决定性的利润定义。' if result['flipped'] else
                      '排序在已核验口径内一致，但精确金额依赖利润定义，仍需确认指标。')
    else:
        result.update(status='stable', message=valid[0]['conclusion'] if selected else
                      '两种已核验利润定义的排序一致；结论仅适用于本次证据和已列条件。')
    if not selected and (result['status'] != 'stable' or output_mode == 'exact'):
        result['clarification'] = {'field': 'metric', 'question': '本次比较采用合并净利润，还是归母净利润？', 'options': options}
    if output_mode == 'exact' and not selected:
        result['minimal_conditions'] = ['metric']
    versions = sorted({str(fact.get('report_version') or 'unknown') for fact in relevant}) or ['unknown']
    currency_known = bool(relevant) and all(fact.get('currency') == 'CNY' for fact in relevant)
    scope_known = bool(relevant) and all(fact.get('statement_scope') == 'consolidated' for fact in relevant)
    result['six_dimensions'] = [
        {'key': 'definition', 'label': '指标定义', 'status': 'confirmed' if selected else 'enumerated', 'value': METRIC_LABELS.get(selected, '合并净利润 / 归母净利润')},
        {'key': 'currency_unit', 'label': '币种单位', 'status': 'confirmed' if currency_known else 'unknown', 'value': 'CNY / 亿元' if currency_known else '未知待确认'},
        {'key': 'period', 'label': '期间', 'status': 'confirmed', 'value': f'{base_year}—{year}全年' if comparison == 'growth' else f'{year}全年'},
        {'key': 'consolidation_scope', 'label': '合并范围', 'status': 'confirmed' if scope_known else 'unknown', 'value': '合并报表' if scope_known else '未知待确认'},
        {'key': 'accounting_policy', 'label': '会计准则政策', 'status': 'unknown', 'value': '未自动核验，需人工复核'},
        {'key': 'restatement_version', 'label': '重述版本', 'status': 'unknown' if 'unknown' in versions else 'recorded', 'value': ' / '.join(versions)}]
    result['condition_snapshot'] = {'companies': names, 'year': year, 'base_year': base_year if comparison == 'growth' else None,
        'comparison': comparison, 'question': question, 'metric': selected, 'output_mode': output_mode,
        'enumerated_fields': ['metric'], 'candidate_metrics': allowed, 'six_dimensions': deepcopy(result['six_dimensions']),
        'limitations': PROBE_SCOPE, 'created_at': datetime.now(timezone.utc).isoformat()}
    fingerprint = {'facts': relevant, 'conditions': {key: value for key, value in result['condition_snapshot'].items() if key != 'created_at'}}
    result['snapshot_id'] = sha256(json.dumps(fingerprint, ensure_ascii=False, sort_keys=True, default=str).encode('utf-8')).hexdigest()
    if 'unknown' in versions:
        result['warnings'].append('披露/重述版本未确认，本次结果未证明跨版本或会计政策下稳定。')
    return result
