"""Explicit, portable financial evidence exports and bounded offline replay.

No database, network, model or filesystem writes occur here. Only a fixed
allowlist of answer fields and referenced financial source documents is exported.
Hashes detect corruption, not authorship: an untrusted package is never treated
as a signed statement or as proof of the truth of its narrative.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
import io
import json
import math
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
from time import monotonic
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import zipfile
import zlib

from core.storage import ROOT, get_data_root, resolve_stored_file
from core.text_to_sql import validate_sql


SCHEMA_VERSION = 'qingmeng-evidence-v1'
MAX_PACKAGE_BYTES = 50 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_FILES = 100
MAX_ROWS = 50000
JSON_FILES = {'answer.json', 'snapshot.json', 'query.json', 'sources.json', 'rules.json'}
OPTIONAL_JSON_FILES = {'presentation.json'}
SOURCE_SUFFIXES = {'.pdf', '.csv', '.xlsx', '.xls', '.docx', '.txt'}
METRIC_UNITS = {
    'revenue': '亿元', 'net_profit': '亿元', 'operating_cashflow': '亿元',
    'roe': '%', 'debt_ratio': '%', 'gross_margin': '%', 'eps': '元/股',
    'consolidated_net_profit': '亿元',
    'total_assets': '亿元', 'total_liabilities': '亿元', 'total_equity': '亿元',
    'minority_profit': '亿元',
}
METRICS = tuple(METRIC_UNITS)
SCOPE_FIELDS = ('intent', 'companies', 'years', 'metrics', 'period', 'investor_profile')
SOURCE_FIELDS = ('company', 'company_name', 'year', 'report_year', 'metric',
                 'file_name', 'page', 'cell', 'raw_value', 'raw_unit',
                 'normalized_unit', 'value_json', 'import_id', 'source_note',
                 'source_url', 'number', 'snippet', 'document_id', 'report_type')
SOURCE_FIELDS += ('location', 'location_type', 'sheet', 'metric_definition', 'currency',
                  'period', 'statement_scope', 'consolidation_scope', 'accounting_policy',
                  'report_version', 'restatement_version', 'reviewed', 'metric_label', 'scope_hint',
                  'currency_hint', 'version_hint', 'metadata_basis', 'source_origin', 'source_type')
SECRET_QUERY_KEYS = {'key', 'api_key', 'apikey', 'token', 'access_token',
                     'password', 'secret', 'authorization', 'signature', 'sig'}


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if hasattr(value, 'item'):
        return _plain(value.item())
    return str(value)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(_plain(value), ensure_ascii=False, sort_keys=True,
                      allow_nan=False, indent=2).encode('utf-8')


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _safe_url(value: Any) -> str:
    try:
        url = urlsplit(str(value or ''))
        if url.scheme not in {'http', 'https'} or not url.hostname:
            return ''
        netloc = url.hostname + (f':{url.port}' if url.port else '')
        pairs = [(key, item) for key, item in parse_qsl(url.query)
                 if key.lower() not in SECRET_QUERY_KEYS and not re.search(
                     r'api.?key|secret|password|authorization|credential|token|signature', key, re.I)]
        return urlunsplit((url.scheme, netloc, url.path, urlencode(pairs), ''))
    except ValueError:
        return ''


def _source_label(value: Any) -> str:
    text = str(value or '')
    if re.match(r'^(?:[A-Za-z]:[\\/]|/)', text):
        return text.replace('\\', '/').rsplit('/', 1)[-1]
    return text


def _public_metadata(value: Any) -> Any:
    """Remove configuration credentials and host paths from optional metadata."""
    if isinstance(value, dict):
        return {str(key): (_source_label(item) if str(key).endswith('_path') and isinstance(item, str) else _public_metadata(item))
                for key, item in value.items()
                if not re.search(r'api.?key|secret|password|authorization|credential|token|llm_config|base_url', str(key), re.I)}
    if isinstance(value, (list, tuple)):
        return [_public_metadata(item) for item in value]
    if isinstance(value, str):
        if re.match(r'^(?:[A-Za-z]:[\\/]|/)', value):
            return _source_label(value)
        if value.startswith(('http://', 'https://')):
            return _safe_url(value)
    return _plain(value)


def _source_file(value: Any) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = resolve_stored_file(value)
    # One explicitly packaged, public annual-report test fixture is also allowed.
    fixture = (ROOT / 'tests' / 'fixtures' / 'byd_2024_key_financials.pdf').resolve()
    if path is None:
        candidate = Path(value)
        candidate = (candidate if candidate.is_absolute() else ROOT / candidate).resolve()
        if candidate == fixture and candidate.is_file():
            path = candidate
    if path is None or path.suffix.lower() not in SOURCE_SUFFIXES:
        return None
    if re.search(r'(?:^|[._-])(?:secrets?|credentials?|api[_-]?keys?|private[_-]?keys?)(?:[._-]|$)', path.name, re.I):
        return None
    # resolve_stored_file already resolves symlinks and enforces session isolation.
    if not (path.is_relative_to(get_data_root().resolve()) or
            path.parent == (ROOT / 'data').resolve() or path == fixture):
        return None
    return path


def _records(value: Any) -> list[dict]:
    if hasattr(value, 'to_dict'):
        value = value.to_dict('records')
    return [dict(item) for item in (value or []) if isinstance(item, dict)]


def _freeze_snapshot(result: dict, data_map: dict | None) -> tuple[dict, list[str]]:
    scope = result.get('parsed') or {}
    companies = scope.get('companies') or ([result['company']] if result.get('company') else [])
    years = set(scope.get('years') or [])
    warnings: list[str] = []
    raw = result.get('data_snapshot')
    probe = result.get('scope_probe') or {}
    if not isinstance(raw, dict) and probe.get('evidence_snapshot'):
        combined = {}
        for fact in probe['evidence_snapshot']:
            metric = 'net_profit' if fact.get('metric') == 'parent_net_profit' else fact.get('metric')
            if metric not in METRICS:
                continue
            company, year = fact.get('company_name'), fact.get('year')
            row = combined.setdefault((company, year), {'year': year, 'metric_provenance': {}})
            row[metric] = fact.get('value')
            row['raw_source'] = (fact.get('evidence') or {}).get('file_name', '')
            row['metric_provenance'][metric] = {
                **(fact.get('evidence') or {}),
                **{field: fact.get(field) for field in ('currency', 'period', 'statement_scope', 'report_version', 'import_id')},
            }
        raw = defaultdict(list)
        for (company, _year), row in combined.items():
            raw[company].append(row)
        condition = probe.get('condition_snapshot') or {}
        if condition.get('comparison') == 'growth' and condition.get('base_year'):
            years.add(condition['base_year'])
    if not isinstance(raw, dict):
        raw = {name: data_map[name] for name in companies if data_map and name in data_map}
        if raw:
            warnings.append('回答未保留原始数据快照；本包冻结的是导出时提供的数据，不能证明其与回答生成时完全相同。')
    if not raw:
        raw = defaultdict(list)
        for row in (result.get('sql_result') or {}).get('rows') or []:
            if row.get('company'):
                raw[row['company']].append(row)
    frozen: dict[str, list[dict]] = {}
    for company, items in raw.items():
        if companies and company not in companies:
            continue
        rows = []
        for item in _records(items):
            year = item.get('year')
            if isinstance(year, bool) or not str(year).isdigit() or not 1900 <= int(year) <= 2200:
                continue
            if years and int(year) not in years:
                continue
            row = {'year': int(year), 'raw_source': _source_label(item.get('raw_source'))}
            row.update({metric: _plain(item[metric]) for metric in METRICS if metric in item})
            if item.get('audit_opinion') is not None:
                row['audit_opinion'] = str(item['audit_opinion'])
            row['metric_provenance'] = {
                metric: {**{key: _public_metadata(source[key]) for key in SOURCE_FIELDS if key in source},
                         '_export_path': source.get('file_path', '')}
                for metric, source in (item.get('metric_provenance') or {}).items()
                if metric in METRICS and isinstance(source, dict)
            }
            rows.append(row)
        frozen[str(company)] = rows
    if sum(len(rows) for rows in frozen.values()) > MAX_ROWS:
        raise ValueError('证据包数据快照超过行数上限。')
    return frozen, warnings


def _presentation_values(presentation: dict) -> dict:
    """Replay exactly the UI's finite two-year visualization calculation."""
    metric = presentation['metric']
    rows = {row['year']: row for row in presentation['records']}
    current = _number(rows.get(presentation['year'], {}).get(metric))
    previous = _number(rows.get(presentation['base_year'], {}).get(metric))
    difference = current - previous if current is not None and previous is not None else None
    growth = difference / abs(previous) * 100 if difference is not None and previous not in (None, 0) else None
    calculation_errors = []
    if difference is not None and not math.isfinite(difference):
        difference = None
        calculation_errors.append('差额超出有限数值范围。')
    if growth is not None and not math.isfinite(growth):
        growth = None
        calculation_errors.append('同比超出有限数值范围。')
    return {'current_value': current, 'previous_value': previous,
            'difference': difference, 'calculation_errors': calculation_errors,
            'growth_rate': growth, 'growth_basis': '以前期绝对值为分母；基期为零或缺失不计算比例',
            'chart_rows': [{'year': year, 'value': _number(rows[year].get(metric)), 'unit': METRIC_UNITS[metric]}
                           for year in sorted(rows)],
            'missing_years': [year for year in (presentation['base_year'], presentation['year']) if year not in rows or _number(rows[year].get(metric)) is None]}


def _freeze_presentation(result: dict) -> tuple[dict | None, list[str]]:
    incoming = result.get('presentation_snapshot')
    if incoming is None:
        return None, []
    if not isinstance(incoming, dict):
        raise ValueError('图表展示快照结构无效。')
    company, metric = incoming.get('company'), incoming.get('metric')
    year, base_year = incoming.get('year'), incoming.get('base_year')
    parsed = result.get('parsed') or {}
    if (not isinstance(company, str) or not company or metric not in METRICS
            or isinstance(year, bool) or not isinstance(year, int) or not 1900 <= year <= 2200
            or isinstance(base_year, bool) or not isinstance(base_year, int) or base_year != year - 1
            or (parsed.get('companies') and company not in parsed['companies'])
            or (parsed.get('metrics') and metric not in parsed['metrics'])):
        raise ValueError('图表展示快照企业、指标或年度不匹配；不扩展原问题范围。')
    selected = []
    seen = set()
    for record in _records(incoming.get('records')):
        if record.get('year') not in {year, base_year}:
            continue
        if isinstance(record.get('year'), bool) or not isinstance(record.get('year'), int) or record['year'] in seen:
            raise ValueError('图表展示快照年度无效或重复。')
        seen.add(record['year'])
        selected.append({'year': record['year'], metric: record.get(metric),
                         'raw_source': record.get('raw_source'),
                         'metric_provenance': {metric: (record.get('metric_provenance') or {}).get(metric, {})}})
    frozen, warnings = _freeze_snapshot({'parsed': {'companies': [company], 'years': [base_year, year]},
                                         'data_snapshot': {company: selected}}, None)
    presentation = {'company': company, 'metric': metric, 'year': year, 'base_year': base_year,
                    'records': frozen[company],
                    'scope_note': '仅供KPI、同比与图表的独立展示基期；原SQL及问题数据快照范围保持不变。'}
    presentation['calculation_snapshot'] = _presentation_values(presentation)
    recorded = [item for item in result.get('calculations') or []
                if isinstance(item, dict) and item.get('company') == company and item.get('metric') == metric]
    if recorded:
        presentation['recorded_calculation'] = {key: _plain(recorded[-1].get(key))
                                               for key in ('previous_value', 'current_value', 'growth_rate')}
    if presentation['calculation_snapshot']['missing_years'] or presentation['calculation_snapshot']['calculation_errors']:
        warnings.append('图表比较基期或目标年指标不足，展示快照不能形成完整年度同比；未从当前库补值。')
    return presentation, warnings


def _formula_records(snapshot: dict, scope: dict, result: dict) -> list[dict]:
    formulas = []
    for company, rows in snapshot.items():
        for row in rows:
            for metric, source in row.get('metric_provenance', {}).items():
                expected = _number(row.get(metric))
                raw = str(source.get('raw_value') or '').strip().replace(',', '').replace('，', '')
                try:
                    raw_number = float(raw)
                except ValueError:
                    continue
                raw_unit, normalized_unit = source.get('raw_unit'), source.get('normalized_unit')
                factor = ({'元': 1e-8, '万元': 1e-4, '亿元': 1.0, '人民币元': 1e-8}.get(raw_unit)
                          if normalized_unit == '亿元' else
                          {'%': 1.0, '百分比': 1.0, '比例': 100.0}.get(raw_unit) if normalized_unit == '%' else
                          1.0 if raw_unit == normalized_unit and normalized_unit == '元/股' else None)
                if expected is not None and factor is not None and math.isfinite(raw_number):
                    formulas.append({'type': 'unit_conversion', 'company': company, 'metric': metric,
                                     'current_year': row['year'], 'expected': expected,
                                     'raw_value': raw_number, 'raw_unit': raw_unit,
                                     'unit': normalized_unit})
        ordered = sorted(rows, key=lambda row: row['year'])
        for previous, current in zip(ordered, ordered[1:]):
            if current['year'] != previous['year'] + 1:
                continue
            for metric in scope.get('metrics') or METRICS:
                base, end = _number(previous.get(metric)), _number(current.get(metric))
                if base is None or end is None:
                    continue
                formulas.append({'type': 'difference', 'company': company, 'metric': metric,
                                 'base_year': previous['year'], 'current_year': current['year'],
                                 'expected': end - base, 'unit': '个百分点' if METRIC_UNITS[metric] == '%' else METRIC_UNITS[metric]})
                if base > 0 and metric != 'gross_margin':
                    formulas.append({'type': 'growth_pct', 'company': company, 'metric': metric,
                                     'base_year': previous['year'], 'current_year': current['year'],
                                     'expected': (end / base - 1) * 100, 'unit': '%'})
    # Freeze explicitly recorded attribution claims; replay checks them against
    # independent snapshot values, and reports missing base-year facts honestly.
    attribution = result.get('attribution') or {}
    for node in [attribution.get('root'), *(attribution.get('drivers') or [])]:
        if not isinstance(node, dict) or node.get('metric') not in METRICS:
            continue
        for kind, field in [('difference', 'delta'), ('growth_abs_pct', 'growth_rate')]:
            if _number(node.get(field)) is not None:
                formulas.append({'type': kind, 'company': node.get('company') or attribution.get('company'),
                                 'metric': node['metric'], 'base_year': node.get('previous_year'),
                                 'current_year': node.get('current_year'), 'expected': node[field],
                                 'unit': node.get('delta_unit') if kind == 'difference' else '%'})
    if len(formulas) > MAX_ROWS:
        raise ValueError('证据包公式数量超过上限，请缩小导出范围。')
    return formulas


def build_evidence_package(question: str, result: dict, *, data_map: dict | None = None) -> dict:
    """Build ZIP bytes only when the caller explicitly requests an export.

    This does not walk the data directory or include the live database, sessions,
    configurations, API keys, source code or unrelated user files.
    """
    if not isinstance(result, dict) or not isinstance(question, str):
        raise ValueError('证据包需要当前问题及结构化回答。')
    scope = {key: _plain((result.get('parsed') or {}).get(key)) for key in SCOPE_FIELDS}
    snapshot, warnings = _freeze_snapshot(result, data_map)
    presentation, presentation_warnings = _freeze_presentation(result)
    warnings.extend(presentation_warnings)
    snapshot_groups = [('answer', snapshot)]
    if presentation is not None:
        snapshot_groups.append(('presentation', {presentation['company']: presentation['records']}))
    sources = []
    for source_scope, group in snapshot_groups:
        for company, rows in group.items():
            for row in rows:
                for metric, source in row['metric_provenance'].items():
                    sources.append({**source, 'company': company, 'year': row['year'], 'metric': metric,
                                    'source_scope': source_scope})
    for citation in (result.get('retrieval_result') or {}).get('citations') or []:
        if isinstance(citation, dict):
            citation_path = citation.get('file_path', '')
            if not citation_path:
                matches = [report for report in result.get('report_files_snapshot') or []
                           if report.get('file_name') == citation.get('file_name')
                           and report.get('company_name') == citation.get('company')
                           and report.get('report_year') == citation.get('report_year')]
                if len(matches) == 1:
                    citation_path = matches[0].get('file_path', '')
            sources.append({**{key: _public_metadata(citation[key]) for key in SOURCE_FIELDS if key in citation},
                            '_export_path': citation_path})
    attachments: dict[str, bytes] = {}
    by_content: dict[str, str] = {}
    for source in sources:
        requested_path = source.pop('_export_path', '')
        source['file_name'] = _source_label(source.get('file_name') or requested_path) or '未记录原文件'
        source['source_url'] = _safe_url(source.get('source_url'))
        path = _source_file(requested_path)
        content = None
        if path is not None:
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    raise OSError('source exceeds export bound')
                content = path.read_bytes()
            except OSError:
                pass
        if content is None:
            source.update(package_path=None, availability='missing_or_not_allowed')
            warnings.append(f'原始来源未冻结：{source["file_name"]}（缺失、超限或不属于当前资料库的允许文档）。')
            continue
        digest = sha256(content).hexdigest()
        attachment = by_content.get(digest)
        if attachment is None:
            attachment = f'attachments/{len(attachments) + 1:03d}-{digest[:16]}{path.suffix.lower()}'
            attachments[attachment] = content
            by_content[digest] = attachment
        source.update(package_path=attachment, availability='included', sha256=digest)
    # Provenance in the numerical snapshot shares the sanitized source entries.
    for source_scope, group in snapshot_groups:
        for company, rows in group.items():
            for row in rows:
                row['metric_provenance'] = {metric: next(
                    source for source in sources if source.get('company') == company and
                    source.get('year') == row['year'] and source.get('metric') == metric
                    and source.get('source_scope') == source_scope)
                    for metric in row['metric_provenance']}
    if not sources:
        warnings.append('当前回答没有逐指标原文件或文档引用；仅能重放结构化事实，不能声明完整原文证据可复演。')
    sql = result.get('sql_result') or {}
    query = {key: _public_metadata(sql.get(key)) for key in ('sql', 'params', 'columns', 'rows', 'status', 'source')}
    for row in query.get('rows') or []:
        if 'raw_source' in row:
            row['raw_source'] = _source_label(row['raw_source'])
    query['formulas'] = _formula_records(snapshot, scope, result)
    probe = result.get('scope_probe') or {}
    if probe.get('evidence_snapshot') and probe.get('condition_snapshot'):
        query['probe'] = _public_metadata(probe)
    packages = {}
    for name in ('pandas',):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = 'unavailable'
    rules = {'schema_version': SCHEMA_VERSION, 'metric_units': METRIC_UNITS,
             'numeric_tolerance': {'rel_tol': 1e-9, 'abs_tol': 1e-8},
             'formula_rules': {'difference': 'current-base', 'growth_pct': '(current/base-1)*100; base>0',
                               'growth_abs_pct': '(current-base)/abs(base)*100; base!=0',
                               'unit_conversion': 'CNY 元/1e8 or 万元/1e4 → 亿元; 比例*100 → %'},
             'dependencies': packages,
             'scope_metadata': _public_metadata(result.get('scope_fingerprint') or result.get('comparability_snapshot')
                                                or result.get('comparability') or probe.get('six_dimensions') or {}),
             'verification': _public_metadata(result.get('verification') or result.get('audit_snapshot') or {}),
             'replay_engine_sha256': sha256(Path(__file__).read_bytes()).hexdigest()}
    if query.get('probe'):
        rules['probe_engine_sha256'] = sha256((ROOT / 'core' / 'scope_probe.py').read_bytes()).hexdigest()
    delivery_state = result.get('delivery_state')
    if delivery_state is None:
        audit_status = (result.get('audit_snapshot') or {}).get('status')
        delivery_state = {'status': 'conflict' if audit_status == 'conflict' else 'pending' if audit_status == 'partial' else 'unverified',
                          'basis': '冻结原答案交付状态；复演只检查计算及文件完整性，不替代原文与口径审核。'}
    answer = {'question': question, 'scope': scope, 'answer': str(result.get('answer') or ''),
              'status': str(result.get('status') or ''), 'warnings': list(dict.fromkeys(warnings)),
              'delivery_state': _public_metadata(delivery_state),
              'claim_scope': '可重放结构化查询与确定性公式；原文文件哈希证明文件完整性，不等于验证自然语言解释或因果关系。'}
    files = {'answer.json': _json_bytes(answer), 'snapshot.json': _json_bytes(snapshot),
             'query.json': _json_bytes(query), 'sources.json': _json_bytes(sources),
             'rules.json': _json_bytes(rules), **attachments}
    if presentation is not None:
        files['presentation.json'] = _json_bytes(presentation)
    if len(files) + 1 > MAX_FILES or sum(map(len, files.values())) > MAX_TOTAL_BYTES or any(len(value) > MAX_FILE_BYTES for value in files.values()):
        raise ValueError('证据包文件数量或体积超过安全上限。')
    manifest = {'schema_version': SCHEMA_VERSION, 'created_at': datetime.now(timezone.utc).isoformat(),
                'files': {name: {'sha256': sha256(value).hexdigest(), 'size': len(value)} for name, value in files.items()},
                'source_count': len(sources), 'included_source_files': len(attachments),
                'authenticity': 'SHA256用于完整性检查；本包未经数字签名，不能证明提交者或数据真实性。'}
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json', _json_bytes(manifest))
        for name, value in files.items():
            archive.writestr(name, value)
    content = output.getvalue()
    if len(content) > MAX_PACKAGE_BYTES:
        raise ValueError('压缩后的证据包超过50 MB上限。')
    package_id = sha256(content).hexdigest()[:16]
    return {'status': 'partial' if warnings else 'ready', 'zip_bytes': content,
            'filename': f'financial-evidence-{package_id}.zip', 'package_id': package_id,
            'manifest': manifest, 'warnings': answer['warnings']}


def _safe_member(info: zipfile.ZipInfo) -> bool:
    name = info.filename
    path = PurePosixPath(name)
    return (bool(name) and '\\' not in name and '\x00' not in name and ':' not in name
            and not path.is_absolute() and all(part not in {'', '.', '..'} for part in name.split('/'))
            and not info.is_dir() and not stat.S_ISLNK(info.external_attr >> 16)
            and not (info.flag_bits & 1)
            and (name == 'manifest.json' or name in JSON_FILES or name in OPTIONAL_JSON_FILES or
                 (len(path.parts) == 2 and path.parts[0] == 'attachments' and path.suffix.lower() in SOURCE_SUFFIXES)))


def _validate_payload(payload: dict) -> None:
    snapshot = payload['snapshot']
    if not isinstance(snapshot, dict) or len(snapshot) > 1000:
        raise ValueError('数据快照结构或企业数量无效。')
    count = 0
    for company, rows in snapshot.items():
        if not isinstance(company, str) or not company or not isinstance(rows, list):
            raise ValueError('数据快照企业与行结构无效。')
        seen = set()
        for row in rows:
            count += 1
            if not isinstance(row, dict) or not isinstance(row.get('year'), int) or isinstance(row['year'], bool) or not 1900 <= row['year'] <= 2200 or row['year'] in seen:
                raise ValueError('数据快照年度无效或重复。')
            seen.add(row['year'])
            if set(row) - set(METRICS) - {'year', 'raw_source', 'metric_provenance', 'audit_opinion'}:
                raise ValueError('数据快照含未允许字段。')
            for metric in METRICS:
                if row.get(metric) is not None and _number(row[metric]) is None:
                    raise ValueError('数据快照数值必须为有限数字。')
            provenance = row.get('metric_provenance') or {}
            if not isinstance(provenance, dict) or any(key not in METRICS or not isinstance(value, dict) for key, value in provenance.items()):
                raise ValueError('逐指标来源结构无效。')
    if count > MAX_ROWS:
        raise ValueError('数据快照超过行数上限。')
    if not isinstance(payload['query'], dict) or not isinstance(payload['answer'], dict) or not isinstance(payload['rules'], dict) or not isinstance(payload['sources'], list):
        raise ValueError('证据包元数据结构无效。')
    query = payload['query']
    if query.get('sql') is not None and not isinstance(query['sql'], str):
        raise ValueError('SQL必须为文本。')
    if len(str(query.get('sql') or '')) > 20000 or not isinstance(query.get('params') or [], list) or len(query.get('params') or []) > 1000 or not isinstance(query.get('rows') or [], list) or len(query.get('rows') or []) > MAX_ROWS:
        raise ValueError('查询或参数超过重放上限。')
    if any(isinstance(value, (dict, list)) or (isinstance(value, float) and not math.isfinite(value)) for value in query.get('params') or []):
        raise ValueError('SQL参数类型不受支持。')
    if any(not isinstance(row, dict) for row in query.get('rows') or []):
        raise ValueError('SQL记录结果结构无效。')
    if not isinstance(query.get('formulas') or [], list) or len(query.get('formulas') or []) > MAX_ROWS:
        raise ValueError('公式结构或数量无效。')
    for formula in query.get('formulas') or []:
        if (not isinstance(formula, dict) or not isinstance(formula.get('company'), str)
                or formula.get('metric') not in METRICS
                or formula.get('type') not in {'difference', 'growth_pct', 'growth_abs_pct', 'unit_conversion'}
                or not isinstance(formula.get('current_year'), int)
                or _number(formula.get('expected')) is None):
            raise ValueError('公式元数据结构不受支持。')
    if query.get('probe'):
        probe = query['probe']
        if (not isinstance(probe, dict) or not isinstance(probe.get('condition_snapshot'), dict)
                or not isinstance(probe.get('evidence_snapshot'), list)
                or len(probe['evidence_snapshot']) > MAX_ROWS):
            raise ValueError('探针快照结构无效。')
    if payload['rules'].get('schema_version') != SCHEMA_VERSION or payload['rules'].get('metric_units') != METRIC_UNITS:
        raise ValueError('规则版本或单位定义不受支持；不得忽略版本差异重放。')
    for source in payload['sources']:
        if not isinstance(source, dict):
            raise ValueError('来源记录结构无效。')
        if not isinstance(source.get('file_name'), str):
            raise ValueError('来源文件名无效。')
        package_path = source.get('package_path')
        if package_path and (package_path not in payload['attachments'] or source.get('sha256') != sha256(payload['attachments'][package_path]).hexdigest()):
            raise ValueError('来源依赖文件缺失或指纹不一致。')
    if payload.get('presentation') is not None:
        presentation = payload['presentation']
        if (not isinstance(presentation, dict) or not isinstance(presentation.get('company'), str) or not presentation['company']
                or presentation.get('metric') not in METRICS
                or not isinstance(presentation.get('records'), list) or len(presentation['records']) > 2
                or not isinstance(presentation.get('year'), int) or isinstance(presentation['year'], bool)
                or not 1900 <= presentation['year'] <= 2200
                or not isinstance(presentation.get('base_year'), int) or isinstance(presentation['base_year'], bool)
                or presentation['base_year'] != presentation['year'] - 1
                or not isinstance(presentation.get('calculation_snapshot'), dict)
                or not isinstance(presentation.get('scope_note'), str)):
            raise ValueError('展示快照结构或期间无效。')
        context = payload['answer'].get('scope') or {}
        if not isinstance(context, dict):
            raise ValueError('冻结问题范围结构无效。')
        if ((context.get('companies') and presentation['company'] not in context['companies'])
                or (context.get('metrics') and presentation['metric'] not in context['metrics'])):
            raise ValueError('展示快照企业/指标不属于冻结的问题。')
        years = set()
        recorded = presentation.get('recorded_calculation')
        if recorded is not None and (not isinstance(recorded, dict)
                or set(recorded) != {'previous_value', 'current_value', 'growth_rate'}
                or any(value is not None and _number(value) is None for value in recorded.values())):
            raise ValueError('展示快照记录的计算值结构无效。')
        for row in presentation['records']:
            if (not isinstance(row, dict) or not isinstance(row.get('year'), int) or isinstance(row.get('year'), bool)
                    or row.get('year') not in {presentation['base_year'], presentation['year']} or row['year'] in years
                    or set(row) - {presentation['metric'], 'year', 'raw_source', 'metric_provenance'}
                    or (row.get(presentation['metric']) is not None and _number(row[presentation['metric']]) is None)
                    or not isinstance(row.get('metric_provenance') or {}, dict)):
                raise ValueError('展示快照记录无效、重复或含未允许字段。')
            years.add(row['year'])
            for metric, source in (row.get('metric_provenance') or {}).items():
                if metric != presentation['metric'] or not isinstance(source, dict):
                    raise ValueError('展示快照逐指标来源无效。')
                member = source.get('package_path')
                if member and (member not in payload['attachments'] or source.get('sha256') != sha256(payload['attachments'][member]).hexdigest()):
                    raise ValueError('展示快照原文件依赖缺失或错配。')


def validate_evidence_package(content: bytes) -> dict:
    """Validate without extracting files or executing SQL; all reads are bounded."""
    errors = []
    manifest = None
    try:
        if not isinstance(content, bytes) or not content or len(content) > MAX_PACKAGE_BYTES:
            raise ValueError('证据包为空、格式错误或超过50 MB上限。')
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            names = [item.filename for item in entries]
            if len(entries) > MAX_FILES or len({name.casefold() for name in names}) != len(names):
                raise ValueError('证据包文件过多或存在重复名称。')
            if any(not _safe_member(item) for item in entries):
                raise ValueError('证据包含不允许的路径、文件类型、符号链接或加密条目。')
            if any(item.file_size > MAX_FILE_BYTES for item in entries) or sum(item.file_size for item in entries) > MAX_TOTAL_BYTES:
                raise ValueError('解压后文件体积超过上限。')
            if 'manifest.json' not in names or archive.getinfo('manifest.json').file_size > 1024 * 1024:
                raise ValueError('证据包清单缺失或超限。')
            manifest = json.loads(archive.read('manifest.json'))
            if not isinstance(manifest, dict) or manifest.get('schema_version') != SCHEMA_VERSION:
                raise ValueError('证据包版本不受支持。')
            declared = manifest.get('files')
            actual_names = set(names) - {'manifest.json'}
            if not isinstance(declared, dict) or set(declared) != actual_names or not JSON_FILES.issubset(actual_names):
                raise ValueError('清单与实际文件不一致或必需文件缺失。')
            files = {}
            for name in actual_names:
                metadata = declared[name]
                if not isinstance(metadata, dict) or not re.fullmatch(r'[0-9a-f]{64}', str(metadata.get('sha256', ''))):
                    raise ValueError('文件哈希清单无效。')
                data = archive.read(name)
                if len(data) != metadata.get('size') or sha256(data).hexdigest() != metadata['sha256']:
                    raise ValueError(f'文件哈希或体积不一致：{name}')
                files[name] = data
        payload = {name.removesuffix('.json'): json.loads(files[name]) for name in JSON_FILES}
        payload['presentation'] = json.loads(files['presentation.json']) if 'presentation.json' in files else None
        payload['attachments'] = {name: value for name, value in files.items() if name.startswith('attachments/')}
        _validate_payload(payload)
        return {'valid': True, 'errors': [], 'manifest': manifest, 'payload': payload,
                'warnings': payload['answer'].get('warnings') or []}
    except (ValueError, TypeError, KeyError, OSError, RuntimeError, RecursionError, EOFError, zlib.error, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        errors.append(str(exc))
    return {'valid': False, 'errors': errors, 'manifest': manifest, 'payload': None}


def _snapshot_connection(snapshot: dict) -> sqlite3.Connection:
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute('CREATE TABLE companies(id INTEGER PRIMARY KEY, name TEXT, stock_code TEXT, industry TEXT, source TEXT)')
    columns = ', '.join(f'{metric} REAL' for metric in METRICS)
    conn.execute(f'CREATE TABLE financial_metrics(id INTEGER PRIMARY KEY, company_id INTEGER, year INTEGER, {columns}, audit_opinion TEXT, raw_source TEXT)')
    conn.execute('CREATE TABLE report_files(id INTEGER PRIMARY KEY, company_id INTEGER, file_name TEXT, report_year INTEGER, file_type TEXT, file_path TEXT, parse_status TEXT, note TEXT)')
    for company_id, (company, rows) in enumerate(snapshot.items(), 1):
        conn.execute('INSERT INTO companies(id,name) VALUES(?,?)', (company_id, company))
        fields = ['company_id', 'year', *METRICS, 'audit_opinion', 'raw_source']
        for row in rows:
            conn.execute('INSERT INTO financial_metrics(' + ','.join(fields) + ') VALUES(' + ','.join('?' for _ in fields) + ')',
                         [company_id, row['year'], *(row.get(metric) for metric in METRICS), row.get('audit_opinion'), row.get('raw_source')])
    conn.commit()
    conn.execute('PRAGMA query_only=ON')
    allowed_functions = {'abs', 'round', 'count', 'sum', 'avg', 'min', 'max', 'coalesce', 'ifnull', 'nullif', 'lower', 'upper', 'length', 'substr', 'typeof'}
    def authorize(action, arg1, arg2, _database, _trigger):
        if action == sqlite3.SQLITE_READ:
            return sqlite3.SQLITE_OK if arg1 in {'companies', 'financial_metrics', 'report_files'} else sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION:
            return sqlite3.SQLITE_OK if str(arg2 or arg1).lower() in allowed_functions else sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK if action == sqlite3.SQLITE_SELECT else sqlite3.SQLITE_DENY
    conn.set_authorizer(authorize)
    deadline = monotonic() + 2
    conn.set_progress_handler(lambda: int(monotonic() > deadline), 1000)
    return conn


def _same(left: Any, right: Any) -> bool:
    a, b = _number(left), _number(right)
    if a is not None and b is not None:
        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-8)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    if isinstance(left, dict) and isinstance(right, dict):
        return set(left) == set(right) and all(_same(left[key], right[key]) for key in left)
    return type(left) is type(right) and left == right


def _replay_formulas(snapshot: dict, formulas: list) -> list[dict]:
    checks = []
    for formula in formulas[:MAX_ROWS]:
        if not isinstance(formula, dict):
            checks.append({'status': 'failed', 'reason': '公式结构不受支持。'})
            continue
        rows = {row['year']: row for row in snapshot.get(formula.get('company'), [])}
        base = _number(rows.get(formula.get('base_year'), {}).get(formula.get('metric')))
        current = _number(rows.get(formula.get('current_year'), {}).get(formula.get('metric')))
        output = {**formula, 'actual': None, 'status': 'not_replayable'}
        if formula.get('type') == 'unit_conversion':
            source = rows.get(formula.get('current_year'), {}).get('metric_provenance', {}).get(formula.get('metric')) or {}
            try:
                raw = float(str(source.get('raw_value') or '').replace(',', '').replace('，', ''))
            except (TypeError, ValueError):
                raw = None
            factor = ({'元': 1e-8, '万元': 1e-4, '亿元': 1.0, '人民币元': 1e-8}.get(source.get('raw_unit'))
                      if formula.get('unit') == '亿元' else
                      {'%': 1.0, '百分比': 1.0, '比例': 100.0}.get(source.get('raw_unit')) if formula.get('unit') == '%' else
                      1.0 if source.get('raw_unit') == formula.get('unit') == '元/股' else None)
            actual = raw * factor if raw is not None and factor is not None and math.isfinite(raw) else None
            match = actual is not None and _same(actual, formula.get('expected')) and _same(actual, current)
            output.update(actual=actual, status='passed' if match else 'failed',
                          reason='按冻结的逐指标原始数值及单位复算；原文取数是否正确仍需页级复核。')
        elif base is None or current is None:
            output['reason'] = '冻结快照缺少公式所需的比较基期或指标，未用已记录结论代替独立事实。'
        else:
            kind = formula.get('type')
            actual = current - base if kind == 'difference' else ((current / base - 1) * 100 if kind == 'growth_pct' and base > 0 else
                     ((current - base) / abs(base) * 100 if kind == 'growth_abs_pct' and base != 0 else None))
            output.update(actual=actual, status='passed' if actual is not None and _same(actual, formula.get('expected')) else 'failed')
            if output['status'] == 'failed':
                output['reason'] = '公式不受支持、基期不适用或重算值与记录不一致。'
        checks.append(output)
    return checks


def replay_evidence_package(content: bytes) -> dict:
    """Rebuild a bounded private in-memory DB, then replay frozen read-only SQL."""
    validated = validate_evidence_package(content)
    if not validated['valid']:
        return {'status': 'invalid', 'verified': False, 'replayable': False,
                'errors': validated['errors'], 'checks': [], 'recomputed': []}
    payload = validated['payload']
    query, snapshot = payload['query'], payload['snapshot']
    checks, rows, errors = [], [], []
    if query.get('sql') and query.get('status') == 'success':
        safety = validate_sql(query['sql'], query.get('params') or [])
        if not safety['valid']:
            errors.extend(safety['errors'])
            checks.append({'kind': 'sql', 'status': 'failed', 'reason': '冻结SQL未通过只读安全校验。'})
        else:
            conn = _snapshot_connection(snapshot)
            try:
                cursor = conn.execute(query['sql'], query.get('params') or [])
                raw_rows = cursor.fetchmany(MAX_ROWS + 1)
                if len(raw_rows) > MAX_ROWS:
                    raise ValueError('SQL重放结果超过行数上限。')
                rows = [dict(row) for row in raw_rows]
                columns = [column[0] for column in cursor.description or []]
                match = columns == (query.get('columns') or columns) and _same(rows, query.get('rows') or [])
                checks.append({'kind': 'sql', 'status': 'passed' if match else 'failed', 'row_count': len(rows),
                               'reason': '冻结SQL结果与原记录一致。' if match else '冻结SQL结果与原记录存在差异。'})
            except (sqlite3.Error, ValueError, TypeError) as exc:
                errors.append(str(exc))
                checks.append({'kind': 'sql', 'status': 'failed', 'reason': str(exc)})
            finally:
                conn.close()
    elif query.get('probe'):
        checks.append({'kind': 'sql', 'status': 'not_applicable', 'reason': '探针使用冻结的已复核事实，无SQL查询可重放。'})
    else:
        checks.append({'kind': 'sql', 'status': 'not_replayable', 'reason': '本回答没有成功执行的SQL，不能声明已重放查询。'})
    probe_result = None
    if query.get('probe'):
        probe = query['probe']
        condition = probe['condition_snapshot']
        try:
            from core.scope_probe import run_scope_probe
            probe_result = run_scope_probe(
                condition.get('companies') or [], condition.get('year'),
                base_year=condition.get('base_year'), question=condition.get('question', ''),
                metric=condition.get('metric'), output_mode=condition.get('output_mode', 'ranking'),
                comparison=condition.get('comparison'), facts=probe['evidence_snapshot'])
            def numerical_claims(item):
                return {'status': item.get('status'), 'flipped': item.get('flipped'),
                        'minimal_conditions': item.get('minimal_conditions'),
                        'candidates': [{key: candidate.get(key) for key in ('metric', 'status', 'winner', 'values', 'growth_rates')}
                                       for candidate in item.get('candidates') or []]}
            match = _same(numerical_claims(probe), numerical_claims(probe_result))
            eligible = probe_result.get('status') in {'stable', 'flipped', 'needs_clarification'} and bool(probe_result.get('candidates')) and all(
                candidate.get('status') == 'valid' for candidate in probe_result['candidates'])
            checks.append({'kind': 'probe', 'status': 'passed' if match and eligible else 'not_replayable' if match else 'failed',
                           'reason': ('从冻结的独立口径事实重放排序与翻转探针。' if match and eligible else
                                      '已复现证据不足状态；不能把复现失败条件当作结论核验通过。' if match else
                                      '冻结探针重放结果与原记录不同。')})
        except (ImportError, ValueError, TypeError, KeyError, AttributeError) as exc:
            checks.append({'kind': 'probe', 'status': 'failed', 'reason': str(exc)})
    formula_checks = _replay_formulas(snapshot, query.get('formulas') or [])
    checks.extend({'kind': 'formula', **item} for item in formula_checks)
    presentation_result = None
    if payload.get('presentation') is not None:
        presentation = payload['presentation']
        recomputed = _presentation_values(presentation)
        match = _same(recomputed, presentation['calculation_snapshot'])
        if presentation.get('recorded_calculation') is not None:
            recorded_fields = {key: recomputed[key] for key in ('previous_value', 'current_value', 'growth_rate')}
            match = match and _same(recorded_fields, presentation['recorded_calculation'])
        state = 'failed' if not match else 'not_replayable' if recomputed['missing_years'] or recomputed['calculation_errors'] else 'passed'
        checks.append({'kind': 'presentation', 'status': state,
                       'reason': '独立复算展示快照中的KPI、同比与图表；未扩展原SQL范围。' if state == 'passed' else
                                 '展示基期或目标指标缺失，不能声明完整同比重放。' if state == 'not_replayable' else
                                 '展示快照复算与保存的KPI/同比/图表记录存在差异。'})
        presentation_result = {'company': presentation['company'], 'metric': presentation['metric'],
                               'year': presentation['year'], 'base_year': presentation['base_year'],
                               'status': state, **recomputed, 'scope_note': presentation['scope_note']}
    missing = [source['file_name'] for source in payload['sources'] if not source.get('package_path')]
    full_sources = bool(payload['sources']) and not missing
    checks.append({'kind': 'sources', 'status': 'passed' if full_sources else 'missing',
                   'reason': '引用原文件均在包内且哈希一致。' if full_sources else '引用原文件不完整；不能声明完整原文证据可复演。',
                   'missing': missing})
    engine_match = payload['rules'].get('replay_engine_sha256') == sha256(Path(__file__).read_bytes()).hexdigest()
    if query.get('probe'):
        probe_file = ROOT / 'core' / 'scope_probe.py'
        engine_match = engine_match and probe_file.is_file() and payload['rules'].get('probe_engine_sha256') == sha256(probe_file.read_bytes()).hexdigest()
    checks.append({'kind': 'rule_version', 'status': 'passed' if engine_match else 'changed',
                   'reason': '重放引擎版本一致。' if engine_match else '重放引擎文件版本变化；本次只执行受支持的冻结规则，未假定原环境完全一致。'})
    quantitative = [check for check in checks if check['kind'] in {'sql', 'formula', 'probe', 'presentation'} and check['status'] != 'not_applicable']
    failed = any(check['status'] == 'failed' for check in quantitative)
    replayable = any(check['status'] == 'passed' for check in quantitative)
    complete = replayable and not failed and full_sources and engine_match and all(check['status'] == 'passed' for check in quantitative)
    status = 'mismatch' if failed else 'replayed' if complete else 'partial' if replayable else 'not_replayable'
    return {'status': status, 'verified': complete, 'replayable': replayable, 'full_evidence': full_sources,
            'checks': checks, 'recomputed': rows, 'formulas': formula_checks, 'probe': probe_result,
            'presentation': presentation_result,
            'delivery_state': payload['answer'].get('delivery_state') or {'status': 'unverified'},
            'answer_approved': False, 'verified_scope': '结构化复演与文件完整性；不自动批准原答案', 'errors': errors,
            'warnings': validated.get('warnings') or [], 'manifest': validated['manifest'],
            'claim_scope': '仅校验冻结结构化查询、确定性公式与文件完整性；未证明叙述真实性、原文语义支持或因果关系。'}


def compare_package_to_current(content: bytes, data_map: dict) -> dict:
    """Compare frozen values, units and provenance with caller-supplied facts."""
    validated = validate_evidence_package(content)
    if not validated['valid']:
        return {'status': 'invalid', 'differences': [], 'errors': validated['errors']}
    differences = []
    groups = [('answer', validated['payload']['snapshot'])]
    presentation = validated['payload'].get('presentation')
    if presentation is not None:
        groups.append(('presentation', {presentation['company']: presentation['records']}))
    for comparison_scope, group in groups:
        for company, rows in group.items():
            current_rows = {row.get('year'): row for row in _records(data_map.get(company))}
            for frozen in rows:
                year = frozen['year']
                current = current_rows.get(year)
                location = {'scope': comparison_scope, 'company': company, 'year': year}
                if current is None:
                    differences.append({**location, 'field': 'record', 'frozen': '存在', 'current': '缺失', 'kind': 'missing'})
                    continue
                for metric in METRICS:
                    if metric not in frozen:
                        continue
                    if not _same(frozen.get(metric), _plain(current.get(metric))):
                        differences.append({**location, 'field': metric, 'frozen': frozen.get(metric), 'current': _plain(current.get(metric)), 'kind': 'value'})
                    old_source = frozen.get('metric_provenance', {}).get(metric) or {}
                    new_source = current.get('metric_provenance', {}).get(metric) or {}
                    for field in ('normalized_unit', 'raw_unit', 'import_id', 'page', 'cell', 'file_name',
                                  'currency_hint', 'scope_hint', 'version_hint', 'metric_label', 'location_type', 'metadata_basis',
                                  'currency', 'period', 'statement_scope', 'consolidation_scope', 'accounting_policy',
                                  'report_version', 'restatement_version', 'metric_definition'):
                        old, new = old_source.get(field), _public_metadata(new_source.get(field))
                        if old != new:
                            differences.append({**location, 'field': f'{metric}.{field}', 'frozen': old, 'current': new, 'kind': 'source_or_version'})
                    path = _source_file(new_source.get('file_path'))
                    if old_source.get('sha256'):
                        try:
                            if path is None or path.stat().st_size > MAX_FILE_BYTES:
                                raise OSError('source unavailable within current workspace bounds')
                            digest = sha256(path.read_bytes()).hexdigest()
                        except OSError:
                            differences.append({**location, 'field': f'{metric}.source_availability',
                                                'frozen': 'included', 'current': 'missing_or_not_allowed', 'kind': 'missing'})
                        else:
                            if digest != old_source['sha256']:
                                differences.append({**location, 'field': f'{metric}.source_sha256', 'frozen': old_source['sha256'], 'current': digest, 'kind': 'source_content'})
    return {'status': 'changed' if differences else 'unchanged', 'differences': differences,
            'summary': f'在证据包冻结范围内发现 {len(differences)} 项差异。', 'errors': [],
            'scope_note': '分别比较问题快照与独立展示基期；当前资料库其他新增范围不算旧结论差异。'}
