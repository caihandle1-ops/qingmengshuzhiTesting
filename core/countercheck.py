"""Seek concrete counterexamples to a user-supplied financial answer.

The candidate answer is a claim, never a database update. Current structured
values, original file positions and accounting checks have separate statuses.
Missing proof cannot produce an automatic pass or an invented PDF page.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import re

from core.db import METRIC_FIELDS, METRIC_UNITS, get_conn
from core.import_validation import MONEY_METRICS, normalize_metric, strict_number
from core.storage import resolve_stored_file

METRIC_ALIASES = {'parent_net_profit': 'net_profit', '归母净利润': 'net_profit',
                  '合并净利润': 'consolidated_net_profit', '营业收入': 'revenue', '营收': 'revenue'}
LABELS = {'revenue': '营业收入', 'net_profit': '归母净利润', 'consolidated_net_profit': '合并净利润',
          'operating_cashflow': '经营现金流', 'total_assets': '总资产', 'total_liabilities': '总负债',
          'total_equity': '所有者权益合计', 'minority_profit': '少数股东损益'}


def _close(left, right):
    return left is not None and right is not None and math.isclose(float(left), float(right), rel_tol=1e-9, abs_tol=1e-8)


def _finite(value):
    try:
        return strict_number(value)
    except (ValueError, TypeError):
        return None


def _read_context(company, year, metric):
    conn = get_conn()
    try:
        issuer = conn.execute('SELECT * FROM companies WHERE name=?', (company,)).fetchone()
        if issuer is None:
            # The display name may be an exact, unambiguous abbreviation.
            candidates = [row for row in conn.execute('SELECT * FROM companies')
                          if row['name'].replace('股份有限公司', '').replace('有限公司', '') == company]
            if len(candidates) == 1:
                issuer = candidates[0]
        if issuer is None:
            return None, None, [], []
        row = conn.execute('SELECT * FROM financial_metrics WHERE company_id=? AND year=?', (issuer['id'], year)).fetchone()
        sources = [dict(source) for source in conn.execute('SELECT * FROM metric_sources WHERE company_id=? AND year=? ORDER BY imported_at DESC,id DESC',
                                                          (issuer['id'], year))]
        reports = [dict(report) for report in conn.execute('SELECT * FROM report_files WHERE company_id=?', (issuer['id'],))]
        return dict(issuer), dict(row) if row else None, sources, reports
    finally:
        conn.close()


def _same_position(old, fresh, metric):
    if old.get('page') != fresh.get('page'):
        return False
    if old.get('cell') == fresh.get('cell'):
        return True
    # Seed CSVs recorded a row+metric, rather than an Excel-style column. The
    # reparser still resolves the real column from the file's actual header.
    built_in = re.fullmatch(r'第([1-9]\d*)行\s*/\s*([A-Za-z_]+)', str(old.get('cell') or ''))
    position = re.search(r'!([A-Z]+)([1-9]\d*)$', str(fresh.get('cell') or ''))
    return bool(built_in and position and built_in[2] == metric and built_in[1] == position[2])


def _original_fact(source, company, year, metric, reports):
    """Reparse an allowed actual file and match its metric/year/cell exactly."""
    checked = {'status': 'pending', 'message': '', 'source': deepcopy(source), 'value': None,
               'raw_value': None, 'raw_unit': None, 'normalized_unit': METRIC_UNITS[metric],
               'file_sha256': None, 'hash_recorded': False}
    path = resolve_stored_file(str(source.get('file_path') or '')) if source.get('file_path') else None
    if path is None:
        checked['message'] = '原文件缺失或不属于当前资料库，不能把已存来源字段当作原文复核。'
        return checked
    if path.stat().st_size > 30 * 1024 * 1024:
        checked['message'] = '原文件超过30 MB核验限制。'
        return checked
    content = path.read_bytes()
    digest = sha256(content).hexdigest()
    checked['file_sha256'] = digest
    hashes = {report.get('content_hash') for report in reports if report.get('content_hash') and
              (report.get('import_id') == source.get('import_id') or report.get('file_path') == source.get('file_path'))}
    checked['hash_recorded'] = bool(hashes)
    if hashes and hashes != {digest}:
        checked.update(status='conflict', message='原文件哈希与已提交版本不一致，需人工确认文件变化；不据此自动替换答案。')
        return checked
    try:
        suffix = path.suffix.lower()
        if suffix in ('.csv', '.xlsx', '.xls'):
            from core.parsers import parse_tabular
            _, records, _ = parse_tabular(path.name, content,
                money_unit=source.get('raw_unit') if metric in MONEY_METRICS else None,
                percent_unit=source.get('raw_unit') if METRIC_UNITS[metric] == '%' else None)
        elif suffix == '.pdf':
            from core.parsers import parse_pdf
            _, records, _, _ = parse_pdf(path.name, content,
                money_unit=source.get('raw_unit') if metric in MONEY_METRICS else None,
                percent_unit=source.get('raw_unit') if METRIC_UNITS[metric] == '%' else None)
        elif suffix == '.docx':
            from core.docx_support import parse_docx
            _, records, _, _ = parse_docx(path.name, content,
                money_unit=source.get('raw_unit') if metric in MONEY_METRICS else None,
                percent_unit=source.get('raw_unit') if METRIC_UNITS[metric] == '%' else None)
        else:
            checked['message'] = '该原文件格式尚不支持独立数值核验。'
            return checked
        candidates = []
        for record in records:
            fresh = record.get('_provenance', {}).get(metric)
            if record.get('year') != year or not fresh or record.get(metric) is None:
                continue
            if record.get('company_name') and record['company_name'] != company:
                continue
            if _same_position(source, fresh, metric):
                candidates.append((record, fresh))
        if len(candidates) != 1:
            checked.update(status='conflict', message='原文件未在该企业、年度、指标的已记录位置找到唯一事实；不使用相邻列或任意同值替代。')
            return checked
        record, fresh = candidates[0]
        checked.update(status='passed', message='已重新解析实际原文件，并匹配企业/年度/指标及原始单元格或PDF页内位置。',
                       value=record[metric], raw_value=fresh['raw_value'], raw_unit=fresh['raw_unit'],
                       normalized_unit=fresh['normalized_unit'])
        checked['source'].update(verified_cell=fresh['cell'], page=fresh.get('page'),
                                  metric_label=fresh.get('metric_label'),
                                  verified_file_sha256=digest, verification_status='numeric_verified')
        try:
            recorded_value = json.loads(source.get('value_json', 'null'))
        except (ValueError, TypeError):
            recorded_value = None
        if not _close(record[metric], recorded_value):
            checked.update(status='conflict', message='原文件同位置数值与已提交来源版本数值不一致，需人工复核。')
        return checked
    except Exception as error:
        checked['message'] = '原文件独立解析未完成：' + type(error).__name__ + '：' + str(error)[:250]
        return checked


def _scope_check(company, year, metric, value, source, manual_conditions=None):
    from core.comparability import compare_financial_scopes
    return compare_financial_scopes({company: [{'year': year, metric: value,
        'metric_provenance': {metric: source or {}}}]}, metric,
        expected_years=[year], manual_conditions=manual_conditions)


def _accounting_check(row, sources, company, year, metric, normalized_claim, reports, manual_conditions=None):
    equations = {
        'total_assets': (['total_assets', 'total_liabilities', 'total_equity'], '资产=负债+权益'),
        'total_liabilities': (['total_assets', 'total_liabilities', 'total_equity'], '资产=负债+权益'),
        'total_equity': (['total_assets', 'total_liabilities', 'total_equity'], '资产=负债+权益'),
        'net_profit': (['consolidated_net_profit', 'net_profit', 'minority_profit'], '合并净利润=归母净利润+少数股东损益'),
        'consolidated_net_profit': (['consolidated_net_profit', 'net_profit', 'minority_profit'], '合并净利润=归母净利润+少数股东损益'),
        'minority_profit': (['consolidated_net_profit', 'net_profit', 'minority_profit'], '合并净利润=归母净利润+少数股东损益'),
    }
    if metric not in equations:
        return {'status': 'not_applicable', 'message': '当前指标没有已实现的适用会计等式；不将其他指标的等式当作该答案通过依据。', 'details': []}
    keys, formula = equations[metric]
    missing = [key for key in keys if _finite(row.get(key)) is None or key not in sources]
    if missing:
        return {'status': 'not_applicable', 'message': '会计关系缺少独立输入，当前不适用，不判通过。',
                'details': [{'formula': formula, 'missing': missing}]}
    facts = [_original_fact(sources[key], company, year, key, reports) for key in keys]
    if any(fact['status'] != 'passed' for fact in facts):
        return {'status': 'pending', 'message': '会计关系尚有输入未完成独立原文核验，不能判通过。', 'details': facts}
    scope_checks = [_scope_check(company, year, key, fact['value'], fact['source'], manual_conditions)
                    for key, fact in zip(keys, facts)]
    if any(check['status'] != 'passed' for check in scope_checks):
        return {'status': 'pending', 'message': '会计关系输入的六维口径存在未知或冲突，不能据此认定等式通过。',
                'details': [{'formula': formula, 'scope_checks': scope_checks}]}
    common_fields = ('currency', 'unit', 'period', 'statement_scope', 'consolidation_scope',
                     'accounting_policy', 'report_version')
    if any(len({check['fingerprints'][0]['fields'][field] for check in scope_checks}) != 1
           for field in common_fields):
        return {'status': 'pending', 'message': '会计关系输入的币种、范围、政策或版本不一致，不能跨口径拼接等式。',
                'details': [{'formula': formula, 'scope_checks': scope_checks}]}
    numbers = {key: fact['value'] for key, fact in zip(keys, facts)}
    left, first, second = (numbers[key] for key in keys)
    expected = first + second if metric == keys[0] else left - second if metric == keys[1] else left - first
    original_consistent = _close(left, first + second)
    matches = normalized_claim is not None and _close(expected, normalized_claim)
    return {'status': 'conflict' if not original_consistent or (normalized_claim is not None and not matches) else 'passed' if matches else 'pending',
            'message': '同口径会计关系发现反例。' if not original_consistent or (normalized_claim is not None and not matches) else
                       '已独立取数并按相同币种、合并范围和披露版本复算。' if matches else '候选单位或币种未确认，等式结果不能直接比对。',
            'details': [{'formula': formula, 'inputs': numbers, 'expected_claim_value': expected,
                         'normalized_claim': normalized_claim, 'original_consistent': original_consistent,
                         'sources': [fact['source'] for fact in facts]}]}


def run_countercheck(company: str, year: int, metric: str, *, candidate_value,
                     candidate_unit=None, candidate_year=None, candidate_currency=None,
                     candidate_source_id=None, manual_conditions=None) -> dict:
    """Check a candidate claim against current workspace facts and real sources."""
    metric = METRIC_ALIASES.get(metric, metric)
    now = datetime.now(timezone.utc).isoformat()
    claim = {'company': company, 'question_year': year, 'metric': metric, 'value': candidate_value,
             'unit': candidate_unit, 'citation_year': candidate_year, 'currency': candidate_currency,
             'source_id': candidate_source_id, 'is_candidate': True}
    checks = {name: {'status': 'pending', 'message': '尚未核验', 'details': []} for name in ('original', 'formula', 'accounting')}
    result = {'schema_version': 'countercheck/1', 'status': 'unsupported', 'message': '', 'claim': claim,
              'mismatches': [], 'corrected_answer': None, 'checks': checks, 'source': None,
              'evidence_snapshot': {}, 'warnings': [], 'created_at': now}
    if (not isinstance(company, str) or not company.strip() or isinstance(year, bool) or
        not isinstance(year, int) or not 1900 <= year <= 2100 or metric not in METRIC_FIELDS or metric == 'audit_opinion'):
        result['message'] = '请指定一家企业、完整年度和支持的数值指标。'
        return result
    candidate_number = _finite(candidate_value)
    if candidate_number is None:
        result['message'] = '候选答案必须包含有限数值，未写入任何财务事实。'
        return result
    issuer, row, sources, reports = _read_context(company.strip(), year, metric)
    if issuer is None or row is None or _finite(row.get(metric)) is None:
        result.update(status='insufficient_evidence', message='当前资料库没有该企业、年度、指标的可用事实，不能凭模型生成修正值。')
        checks['original']['message'] = result['message']
        checks['formula']['message'] = '缺少当前指标，无法进行原值换算复算。'
        checks['accounting'] = {'status': 'not_applicable', 'message': '缺少适用输入。', 'details': []}
        return result
    company = issuer['name']
    claim['resolved_company'] = company
    metric_sources = [source for source in sources if source['metric'] == metric]
    current = [source for source in metric_sources if source['is_current']]
    active_sources = {source['metric']: source for source in sources if source['is_current']}
    source = current[0] if len(current) == 1 else None
    result['source'] = deepcopy(source)
    def mismatch(code, field, actual, expected, message):
        result['mismatches'].append({'code': code, 'field': field, 'actual': actual, 'expected': expected, 'message': message})
    if candidate_year is None:
        result['warnings'].append('候选答案未声明引用年度，年度核验仍待补充。')
    elif isinstance(candidate_year, bool) or not isinstance(candidate_year, int) or candidate_year != year:
        mismatch('year_mismatch', 'citation_year', candidate_year, year, '候选答案引用年度与提问年度不一致。')
    currency = re.sub(r'\s+', '', str(candidate_currency or '')).upper()
    if not currency:
        result['warnings'].append('候选答案未声明币种，币种核验仍待补充。')
    elif metric in MONEY_METRICS and currency not in ('CNY', 'RMB', '人民币', '人民币元'):
        mismatch('currency_mismatch', 'currency', candidate_currency, 'CNY', '当前人民币指标不能与其他币种直接比较，未进行自动换汇。')
    normalized = None
    if not candidate_unit:
        result['warnings'].append('候选答案未声明单位，不能按数值大小猜测。')
    elif metric not in MONEY_METRICS or currency in ('CNY', 'RMB', '人民币', '人民币元'):
        try:
            normalized, _, _ = normalize_metric(metric, candidate_number, candidate_unit)
        except ValueError as error:
            mismatch('unit_conflict', 'unit', candidate_unit, METRIC_UNITS[metric], str(error))
    if source is None:
        mismatch('source_missing', 'source', None, '唯一的当前逐指标来源', '缺少唯一当前来源，不能给出已核验答案。')
        original = {'status': 'pending', 'message': '逐指标来源缺失或不唯一。', 'source': None, 'value': None}
    else:
        original = _original_fact(source, company, year, metric, reports)
        if original['status'] == 'conflict':
            mismatch('source_conflict', 'source', source.get('cell'), '当前原文件中的唯一同年度同指标位置', original['message'])
        elif original['status'] != 'passed':
            mismatch('source_missing', 'source', source.get('file_path'), '可读取且可定位的原文件', original['message'])
        elif not _close(original['value'], row[metric]):
            mismatch('stored_value_conflict', 'stored_value', row[metric], original['value'], '当前库值与原文件独立复算值不一致，需人工复核。')
    version_details = [{'id': item['id'], 'import_id': item['import_id'], 'year': item['year'],
                        'version': item.get('version_hint') or 'unknown', 'is_current': bool(item['is_current']),
                        'value': json.loads(item['value_json']), 'file_name': item.get('file_name')}
                       for item in metric_sources[:100]]
    if len(metric_sources) > 1:
        result['warnings'].append('该指标存在多个导入/披露版本；当前复算使用is_current来源，历史版本未自动视为同一版本。')
    if source and source.get('version_hint') in (None, '', 'unknown'):
        result['warnings'].append('当前来源披露版本未知，不宣称跨版本核验通过。')
    if candidate_source_id is not None:
        cited = next((item for item in sources if item['id'] == candidate_source_id), None)
        if cited is None or cited['metric'] != metric:
            mismatch('source_reference_mismatch', 'source_id', candidate_source_id, source.get('id') if source else None, '引用来源不属于当前企业、年度、指标。')
        elif not cited['is_current']:
            mismatch('source_version_mismatch', 'source_id', candidate_source_id, source.get('id') if source else None, '候选引用历史版本，当前来源版本已有变化。')
    checks['original'] = {'status': original['status'], 'message': original['message'],
                          'details': [deepcopy(original), {'source_versions': version_details}]}
    expected = original.get('value') if original['status'] == 'passed' else row[metric]
    formula_status = 'pending'
    if normalized is not None:
        formula_status = 'passed' if _close(normalized, expected) and original['status'] == 'passed' else 'conflict' if not _close(normalized, expected) and original['status'] == 'passed' else 'pending'
        if not _close(normalized, expected):
            code = ('unit_conflict' if _close(candidate_number, expected) and candidate_unit != METRIC_UNITS[metric] else 'value_conflict') if original['status'] == 'passed' else 'unverified_value_difference'
            mismatch(code, 'value', normalized, expected, '候选答案按其声明单位换算后，与当前年度独立来源数值不同。' if original['status'] == 'passed' else '候选与当前库值不同，但原件尚未复核，不能据此判定答案错误。')
    checks['formula'] = {'status': formula_status,
                         'message': '已按原始单位复算，并比对候选声明的单位换算。' if formula_status == 'passed' else
                                    '单位换算后发现数值反例。' if formula_status == 'conflict' else '原件或候选单位/币种缺少确认，当前只能展示复算条件。',
                         'details': [{'formula': '原始金额 × 已声明单位换算系数 → 标准单位',
                                      'raw_value': original.get('raw_value'), 'raw_unit': original.get('raw_unit'),
                                      'recomputed_value': original.get('value'), 'stored_value': row[metric],
                                      'candidate_value': candidate_number, 'candidate_unit': candidate_unit,
                                      'normalized_candidate': normalized, 'expected': expected, 'unit': METRIC_UNITS[metric]}]}
    scope_check = _scope_check(company, year, metric, expected, original.get('source') or source,
                               manual_conditions)
    result['scope_check'] = scope_check
    if scope_check['status'] == 'conflict':
        mismatch('financial_scope_conflict', 'source_scope', scope_check.get('conflicts'),
                 '本次指标对应的六维财务口径', scope_check['message'])
    elif scope_check['status'] != 'passed':
        result['warnings'].append('来源六维口径尚未确认完整；候选声明的币种或年度不能替代来源证明。')
    checks['accounting'] = _accounting_check(row, active_sources, company, year, metric, normalized, reports,
                                            manual_conditions)
    if checks['accounting']['status'] == 'conflict':
        mismatch('accounting_counterexample', 'accounting_relation', normalized, checks['accounting']['details'][0].get('expected_claim_value'), checks['accounting']['message'])
    proof_ok = original['status'] == 'passed' and _close(original.get('value'), row[metric])
    confirmed_currency = next((fingerprint['fields']['currency'] for fingerprint in scope_check.get('fingerprints', [])
        if str(fingerprint['fields']['currency']).lower() not in ('', 'unknown', 'none', 'null', '未知', '未确认', '未记录')), None)
    value_label = ('独立核验值' if scope_check['status'] == 'passed' else '原值数值已复核（口径待复核）') if proof_ok else '库值（原件待复核）'
    result['corrected_answer'] = {'company': company, 'year': year, 'metric': metric, 'value': expected,
        'unit': METRIC_UNITS[metric], 'currency': confirmed_currency if metric in MONEY_METRICS else None,
        'source': deepcopy(original.get('source') or source),
        'verification_status': 'verified' if proof_ok and scope_check['status'] == 'passed' else 'numeric_verified' if proof_ok else 'provisional',
        'text': f'{company}{year}年{LABELS.get(metric, metric)}的当前' + value_label +
                f'为{expected:g}{METRIC_UNITS[metric]}。'}
    result['evidence_snapshot'] = {'company': deepcopy(issuer), 'row': deepcopy(row), 'metric_sources': deepcopy(sources[:100]),
        'selected_source': deepcopy(source), 'original_check': deepcopy(original),
        'scope_check': deepcopy(scope_check), 'manual_conditions': deepcopy(manual_conditions),
        'candidate_claim': deepcopy(claim), 'scope': {'period': 'annual', 'company': company, 'year': year, 'metric': metric},
        'limitations': '核验当前资料库与所记录的原文件；会计关系只在输入齐全且同币种、同范围、同版本时适用。'}
    unresolved = candidate_year is None or not candidate_unit or (metric in MONEY_METRICS and not currency)
    hard_mismatches = [item for item in result['mismatches'] if item['code'] not in ('source_missing', 'unverified_value_difference')]
    if hard_mismatches:
        result.update(status='conflict', message='已发现候选答案或来源的反例，请按修正建议复核；候选答案未写入事实库。')
    elif any(item['code'] == 'source_missing' for item in result['mismatches']):
        result.update(status='insufficient_evidence', message='原件或定位证据缺失，当前不能确认或否定候选答案；修正值仅为待复核的库值。')
    elif unresolved or not proof_ok or checks['accounting']['status'] == 'pending' or scope_check['status'] != 'passed':
        result.update(status='pending_review', message='尚有声明条件或证据缺口，不能判定候选答案通过。')
    else:
        result.update(status='verified', message='在已列明的年度、指标、单位及实际原件范围内，未发现候选答案的反例。')
    signed = {key: value for key, value in result.items() if key not in ('created_at', 'snapshot_id')}
    result['snapshot_id'] = sha256(json.dumps(signed, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
    return result


def countercheck_to_json_bytes(result: dict) -> bytes:
    """A local downloadable snapshot; no external message or database change."""
    return json.dumps(deepcopy(result), ensure_ascii=False, indent=2, allow_nan=False, default=str).encode('utf-8')
