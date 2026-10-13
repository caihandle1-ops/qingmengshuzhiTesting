"""Read-only, workspace-bound comparison of a reviewed import and current facts."""
from __future__ import annotations

from collections import Counter
from hashlib import sha256
import json
from pathlib import PurePosixPath

from core.db import get_conn, get_db_path, METRIC_UNITS
from core.import_validation import METRICS, NUMERIC_METRICS


METRIC_LABELS = {
    'revenue': '营业收入', 'net_profit': '归母净利润', 'operating_cashflow': '经营现金流',
    'roe': 'ROE', 'debt_ratio': '资产负债率', 'gross_margin': '毛利率', 'eps': '每股收益',
    'audit_opinion': '审计意见', 'consolidated_net_profit': '合并净利润',
    'total_assets': '资产总额', 'total_liabilities': '负债总额', 'total_equity': '所有者权益',
    'minority_profit': '少数股东损益',
}
ACTION_LABELS = {
    'new': '新增年度指标', 'filled': '补齐缺失', 'overwritten': '覆盖数值', 'text_updated': '更新审计意见',
    'source_replaced': '数值不变 · 更新来源', 'unchanged': '数值相同 · 保留来源',
    'kept': '保留已有值', 'omitted': '本次未提供 · 保留原值',
    'not_committed': '无数值入库 · 保留原值',
}
WRITE_ACTIONS = {'new', 'filled', 'overwritten', 'text_updated', 'source_replaced'}


def _digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), default=str).encode()).hexdigest()


def _target_snapshot(batch):
    """Use one database read transaction; do not open or return original files."""
    conn = get_conn()
    try:
        if not conn.in_transaction:
            conn.execute('BEGIN')
        companies = [dict(row) for row in conn.execute('SELECT id,name,stock_code FROM companies ORDER BY id')]
        result = []
        seen = set()
        for incoming in batch.get('records') or []:
            name, year = incoming.get('company_name'), incoming.get('year')
            if not isinstance(name, str) or not isinstance(year, int) or isinstance(year, bool):
                continue
            if (name, year) in seen:
                continue
            seen.add((name, year))
            code = batch.get('stock_code') if len(batch.get('companies') or []) == 1 else None
            company = next((item for item in companies if code and item['stock_code'] == code), None)
            if company is None:
                company = next((item for item in companies if item['name'] == name), None)
            record, sources = None, []
            if company:
                row = conn.execute('SELECT * FROM financial_metrics WHERE company_id=? AND year=?',
                                   (company['id'], year)).fetchone()
                record = dict(row) if row is not None else None
                # Include provenance and current typed facts in the concurrency
                # token. A source-only correction matters even at the same value.
                sources = [dict(row) for row in conn.execute('''SELECT * FROM metric_sources
                    WHERE company_id=? AND year=? AND is_current=1 ORDER BY metric,id''', (company['id'], year))]
                facts = [dict(row) for row in conn.execute('''SELECT * FROM scope_probe_facts
                    WHERE company_id=? AND year=? AND is_current=1 ORDER BY metric,id''', (company['id'], year))]
            else:
                facts = []
            result.append({'input_name': name, 'year': year, 'company': company,
                           'record': record, 'sources': sources, 'facts': facts})
        return result
    finally:
        conn.close()


def _signature(batch, targets, overwrite_existing):
    # The path is hashed only. An identical database in another visitor's
    # workspace must not accept a confirmation belonging to the first visitor.
    return _digest({'workspace': _digest(str(get_db_path().resolve())),
                    'batch': batch.get('_fingerprint'), 'overwrite': bool(overwrite_existing),
                    'targets': targets})


def current_import_signature(batch, *, overwrite_existing=False):
    """Recalculate inside commit_import's write transaction before any writes."""
    return _signature(batch, _target_snapshot(batch), overwrite_existing)


def _source_label(source):
    source = source or {}
    name = PurePosixPath(str(source.get('file_name') or '').replace('\\', '/')).name
    if not name:
        return '来源未记录'
    location = []
    if isinstance(source.get('page'), int) and source['page'] > 0:
        location.append(f'第 {source["page"]} 页')
    if source.get('cell'):
        # Spreadsheet addresses are useful; freeform paths/URLs are not.
        import re
        cell = str(source['cell'])
        if re.fullmatch(r'[\w\s\u4e00-\u9fff!:$.,()（）-]{1,80}', cell):
            location.append(cell)
    return name + (' · ' + ' / '.join(location) if location else '')


def _int_year(value):
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
        return number if 1900 <= number <= 2200 and str(value) in {str(number), f'{number}.0'} else None
    except (TypeError, ValueError):
        return None


def _answer_scope(result):
    """Return explicit financial dependencies, including adjacent-year charts."""
    scope = set()
    parsed = result.get('parsed') or {}
    companies = parsed.get('companies') or []
    metrics = [metric for metric in parsed.get('metrics') or [] if metric in METRICS]
    years = [_int_year(year) for year in parsed.get('years') or []]
    for company in companies:
        for year in years:
            if year:
                scope.update((company, year, metric) for metric in metrics)
    snapshot = result.get('data_snapshot') or {}
    if isinstance(snapshot, dict):
        for company, records in snapshot.items():
            if not isinstance(records, list):
                continue
            for row in records:
                if not isinstance(row, dict) or not _int_year(row.get('year')):
                    continue
                # Frozen data may contain unused fields. Use the query metrics
                # where available, then explicit calculation sources below.
                keys = metrics or [key for key in METRICS if key in row]
                scope.update((company, _int_year(row['year']), key) for key in keys)
    presentation = result.get('presentation_snapshot') or {}
    if isinstance(presentation, dict) and presentation.get('metric') in METRICS:
        for field in ('year', 'base_year'):
            year = _int_year(presentation.get(field))
            if year and presentation.get('company'):
                scope.add((presentation['company'], year, presentation['metric']))
    attribution = result.get('attribution') or {}
    if isinstance(attribution, dict):
        for node in [attribution.get('root'), *(attribution.get('drivers') or [])]:
            if not isinstance(node, dict):
                continue
            for source in node.get('quantitative_sources') or []:
                metric = source.get('metric') or node.get('metric') if isinstance(source, dict) else None
                if isinstance(source, dict) and metric in METRICS and _int_year(source.get('year')):
                    scope.add((source.get('company') or attribution.get('company'),
                               _int_year(source['year']), metric))
    probe = result.get('scope_probe') or {}
    if isinstance(probe, dict):
        for fact in probe.get('evidence_snapshot') or []:
            if not isinstance(fact, dict):
                continue
            metric = 'net_profit' if fact.get('metric') == 'parent_net_profit' else fact.get('metric')
            if metric in METRICS and _int_year(fact.get('year')):
                scope.add((fact.get('company_name') or fact.get('company'), _int_year(fact['year']), metric))
    request = result.get('probe_request') or {}
    if isinstance(request, dict) and _int_year(request.get('year')):
        # Missing probe evidence is still a dependency that a new import may fill.
        probe_metrics = ['net_profit', 'consolidated_net_profit'] if request.get('metric') in (None, 'net_profit') else [request['metric']]
        for company in request.get('company_names') or []:
            scope.update((company, _int_year(request['year']), metric) for metric in probe_metrics if metric in METRICS)
            condition = probe.get('condition_snapshot') or {} if isinstance(probe, dict) else {}
            if isinstance(condition, dict) and condition.get('comparison') == 'growth' and _int_year(condition.get('base_year')):
                scope.update((company, _int_year(condition['base_year']), metric) for metric in probe_metrics if metric in METRICS)
    return scope


def affected_frozen_answers(rows, state):
    """Inspect only this visitor's supplied snapshots; never rewrite a result."""
    from core.public_messages import public_message
    changes = {(row['company'], row['year'], row['metric']): row for row in rows if row['will_write']}
    if not changes:
        return []
    state = state or {}
    sessions = [(state.get('active_session_id', 'current'), '当前会话', state.get('chat_messages') or [])]
    sessions += [(item.get('id'), item.get('title') or '历史会话', item.get('messages') or [])
                 for item in state.get('saved_sessions') or [] if isinstance(item, dict)]
    seen, affected = set(), []
    for session_id, title, messages in sessions:
        for index, message in enumerate(messages):
            identity = (session_id, index)
            if identity in seen or not isinstance(message, dict) or not isinstance(message.get('result'), dict):
                continue
            seen.add(identity)
            hits = sorted(_answer_scope(message['result']).intersection(changes))
            if not hits:
                continue
            reasons = []
            for hit in hits:
                row = changes[hit]
                reason = ('来源更新' if row['action'] == 'source_replaced' else '缺失补齐'
                          if row['old_value'] is None else '审计意见变化' if row['metric'] == 'audit_opinion' else '数值变化')
                reasons.append(f'{hit[0]} · {hit[1]} · {METRIC_LABELS[hit[2]]}：{reason}')
            affected.append({'session': public_message(title), 'question': public_message(message.get('question'), '历史问答'),
                             'answer_number': index + 1, 'reasons': reasons})
    return affected


def build_import_change_preview(batch, *, overwrite_existing=False, state=None):
    """Predict commit_import behavior without changing data, sources or history."""
    targets = _target_snapshot(batch)
    by_target = {(item['input_name'], item['year']): item for item in targets}
    rows = []
    for incoming in batch.get('records') or []:
        target = by_target.get((incoming.get('company_name'), incoming.get('year')))
        if target is None:
            continue
        current = target['record']
        sources = {source['metric']: source for source in target['sources']}
        canonical = (target['company'] or {}).get('name') or target['input_name']
        for metric in METRICS:
            old = current.get(metric) if current else None
            new = incoming.get(metric)
            if old is None and new is None:
                continue
            if new is None:
                action = 'omitted'
            elif old is None:
                action = 'new' if current is None else 'filled'
            elif not overwrite_existing:
                action = 'unchanged' if old == new else 'kept'
            else:
                action = 'source_replaced' if old == new else 'overwritten'
                if action == 'overwritten' and metric == 'audit_opinion':
                    action = 'text_updated'
            source = {**(incoming.get('_provenance') or {}).get(metric, {}), 'file_name': batch.get('file_name')}
            rows.append({'company': canonical, 'year': target['year'], 'metric': metric,
                         'label': METRIC_LABELS[metric], 'unit': METRIC_UNITS[metric],
                         'old_value': old, 'incoming_value': new, 'action': action,
                         'old_source': _source_label(sources.get(metric)),
                         'new_source': _source_label(source) if new is not None else '本次未提供',
                         'will_write': action in WRITE_ACTIONS})
    # The service deliberately refuses an audit-text-only commit and a batch
    # rejected by validation. Do not promise changes it will never write.
    has_numeric = any(row['will_write'] and row['metric'] in NUMERIC_METRICS for row in rows)
    for row in rows:
        if row['will_write'] and (not has_numeric or not batch.get('can_commit')):
            row.update(action='not_committed', will_write=False)
        row['effective_value'] = row['incoming_value'] if row['will_write'] else row['old_value']
    return {'current_signature': _signature(batch, targets, overwrite_existing), 'rows': rows,
            'counts': dict(Counter(row['action'] for row in rows)),
            'write_count': sum(row['will_write'] for row in rows),
            'affected_answers': affected_frozen_answers(rows, state),
            'can_commit': bool(batch.get('can_commit') and has_numeric)}
