"""Portable, bounded research backups; restoring never approves a financial claim.

Only the five documented data tables and the UI fields below cross the boundary.
An archive is data, never Python/pickle/SQL. Host paths and storage identifiers are
replaced with archive-local references, then rebound to the receiving workspace.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
import math
from pathlib import Path, PurePosixPath
import re
import stat
from uuid import uuid4
import zipfile

from core import db
from core.storage import ROOT, get_data_root, require_write_access, resolve_stored_file

SCHEMA = 'qingmeng-research-archive/1'
MAX_ZIP = 112 * 1024 * 1024
MAX_FILES = 20
MAX_FILE = 30 * 1024 * 1024
MAX_TOTAL = 100 * 1024 * 1024
MAX_JSON = 12 * 1024 * 1024
MAX_REVIEW = 2 * 1024 * 1024
MAX_ROWS = 20000
SUFFIXES = {'.pdf', '.csv', '.xls', '.xlsx', '.docx'}
STATE_FIELDS = (
    'session_title', 'selected_main', 'selected_cmp', 'qa_period', 'research_period',
    'investor_profile', 'enterprise_chart_type', 'enterprise_chart_metrics',
    'enterprise_risk_signal', 'library_keyword', 'library_type', 'source_company',
    'source_history', 'counter_review_note', 'scope_manual_signature',
)
INPUT_FIELDS = ('qa_input', 'qa_period', 'workspace_question', 'workspace_year',
                'enterprise_chart_type', 'enterprise_chart_metrics', 'enterprise_risk_signal',
                'library_keyword', 'library_type', 'source_company', 'source_history')
MANUAL_FIELDS = ('scope_manual_currency', 'scope_manual_period', 'scope_manual_statement',
                 'scope_manual_consolidation', 'scope_manual_policy', 'scope_manual_version',
                 'scope_manual_confirmed')
SCOPE_FIELDS = ('scope_company', 'scope_year', 'scope_metric', 'scope_mode',
                'scope_base_year', 'scope_compare_company')
CONTEXT_FIELDS = ('primary_company', 'compare_company', 'years', 'metrics', 'intent',
                  'period', 'awaiting_clarification', 'clarification')
RESULT_FIELDS = (
    'status', 'message', 'reason', 'company', 'answer', 'parsed', 'analysis_intent',
    'data_snapshot', 'report_files_snapshot', 'presentation_snapshot', 'audit_snapshot',
    'comparability_snapshot', 'delivery_state', 'verification', 'attribution',
    'scope_probe', 'probe_request', 'research_task', 'tasks', 'rows', 'excluded',
    'report_text', 'calculations', 'sql_result', 'query_plan', 'retrieval_result',
    'agent_trace', 'trace', 'execution_budget', 'duration_ms', 'response_mode',
    'fallback_reason', 'warnings', 'errors', 'confidence', 'clarification',
)
TABLE_FIELDS = {
    'companies': ('name', 'stock_code', 'industry', 'source'),
    'financial_metrics': ('year', *db.METRIC_FIELDS, 'raw_source'),
    'report_files': ('file_name', 'report_year', 'file_type', 'parse_status', 'note',
                     'uploaded_at', 'source_type', 'source_url', 'report_title',
                     'report_type', 'content_hash', 'ingest_status', 'metric_count'),
    'metric_sources': ('year', 'metric', 'value_json', 'normalized_unit', 'file_name',
                       'page', 'cell', 'raw_value', 'raw_unit', 'source_url', 'source_note',
                       'imported_at', 'is_current', 'currency_hint', 'scope_hint',
                       'version_hint', 'metric_label', 'location_type'),
    'scope_probe_facts': ('year', 'metric', 'value', 'currency', 'period',
                          'statement_scope', 'report_version', 'created_at', 'is_current'),
}
SECRET_KEY = re.compile(r'api.?key|secret|password|authorization|credential|token|llm_config|llm_key|base_url|session.*id|workspace_root', re.I)
HOST_PATH = re.compile(r'(?<![\w:])(?:[A-Za-z]:[\\/][^\s\"<>]+|/(?:Users|users|home|mount|tmp|var|root|mnt)/[^\s\"<>]+)')


class ArchiveError(ValueError):
    """A safe, human-readable refusal before any restore writes."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(',', ':')).encode('utf-8')


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ArchiveError('档案 JSON 含重复字段。')
        result[key] = value
    return result


def _load(content):
    try:
        return json.loads(content, object_pairs_hook=_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(ArchiveError('档案含非有限数值。')))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ArchiveError('档案 JSON 结构无效。') from exc


def _bounded(value, depth=0, counter=None):
    counter = [0] if counter is None else counter
    counter[0] += 1
    if depth > 28 or counter[0] > 350000:
        raise ArchiveError('档案结构超过允许的深度或记录数量。')
    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, str) and len(value) > 200000:
            raise ArchiveError('档案中的单个文本字段过长。')
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    if isinstance(value, list):
        for item in value:
            _bounded(item, depth + 1, counter)
        return
    if isinstance(value, dict) and all(isinstance(key, str) and len(key) <= 500 for key in value):
        for item in value.values():
            _bounded(item, depth + 1, counter)
        return
    raise ArchiveError('档案包含不支持的数据类型。')


def _safe_text(value, secrets=()):
    from core.evidence_package import _safe_url
    text = str(value)
    for secret in secrets:
        if len(secret) >= 6:
            text = text.replace(secret, '[已移除连接凭据]')
    text = re.sub(r'\bsk-[A-Za-z0-9_-]{10,}', '[已移除连接凭据]', text)
    def clean_url(match):
        original = match.group(0)
        address = original.rstrip('。，；！？）)]}')
        return _safe_url(address) + original[len(address):]
    text = re.sub(r'https?://[^\s<>\"\u3000]+', clean_url, text)
    text = HOST_PATH.sub('[本机路径已移除]', text)
    if re.match(r'^(?:[A-Za-z]:[\\/]|/|\\\\)', text):
        return '[本机路径已移除]'
    return text


def _secrets(state):
    found = []
    def visit(value, depth=0):
        if depth > 4 or not isinstance(value, dict):
            return
        for key, item in value.items():
            if SECRET_KEY.search(str(key)) and isinstance(item, str) and len(item) >= 6:
                found.append(item)
            elif isinstance(item, dict):
                visit(item, depth + 1)
    visit(dict(state))
    return found


def _builtins():
    # Only explicitly shipped seed files are eligible for the builtin identity.
    from core.seed import _sample_files
    return {path.name: (name, code, path.resolve(), rows)
            for name, code, _, path, rows in _sample_files()}


class _Encoder:
    def __init__(self, state, include_originals):
        self.secrets = _secrets(state)
        self.include_originals = include_originals
        self.files = []
        self.content = {}
        self.paths = {}
        self.imports = {}
        self.company_ids = {}
        self.report_ids = {}
        self.builtins = _builtins()
        self.total = 0

    def import_ref(self, value):
        if not value:
            return None
        if value == 'builtin-v1':
            return 'builtin-v1'
        unknown = str(value).startswith('legacy-') or 'unverified' in str(value)
        return self.imports.setdefault(str(value), f'{"u" if unknown else "i"}{len(self.imports) + 1}')

    def file_ref(self, value):
        if not value:
            return None
        original = str(value)
        if original in self.paths:
            return self.paths[original]
        path = resolve_stored_file(original)
        ref = f'f{len(self.files) + 1}'
        self.paths[original] = ref
        basename = original.replace('\\', '/').rsplit('/', 1)[-1]
        entry = {'ref': ref, 'name': _safe_text(basename, self.secrets), 'kind': 'missing',
                 'included': False, 'sha256': None, 'size': 0}
        if path and path.name in self.builtins and path == self.builtins[path.name][2]:
            raw = path.read_bytes()
            entry.update(kind='builtin', sha256=sha256(raw).hexdigest(), size=len(raw))
        elif path and path.suffix.lower() in SUFFIXES:
            size = path.stat().st_size
            if size > MAX_FILE:
                raise ArchiveError('单份研究原件超过30 MB，无法打包。')
            raw = path.read_bytes()
            entry.update(kind='original', sha256=sha256(raw).hexdigest(), size=len(raw))
            if self.include_originals:
                if len(self.content) >= MAX_FILES or self.total + len(raw) > MAX_TOTAL:
                    raise ArchiveError('原件最多20份、合计100 MB，请先整理资料。')
                member = f'originals/{ref}{path.suffix.lower()}'
                self.content[member] = raw
                self.total += len(raw)
                entry.update(included=True, member=member)
        self.files.append(entry)
        return ref

    def clean(self, value, depth=0):
        if depth > 28:
            raise ArchiveError('研究记录嵌套过深。')
        if isinstance(value, dict):
            output = {}
            for key, item in value.items():
                key = str(key)
                if key == 'file_path':
                    output['file_ref'] = self.file_ref(item)
                elif key == 'import_id':
                    output['import_ref'] = self.import_ref(item)
                elif key == 'company_id':
                    output['company_ref'] = self.company_ids.get(item)
                elif key == 'document_id':
                    report = self.report_ids.get(item)
                    if report:
                        output['file_ref'] = self.file_ref(report.get('file_path'))
                elif key in {'id', 'path', 'storage_id', 'candidate_source_id'} or key.endswith('_path') or SECRET_KEY.search(key):
                    continue
                else:
                    output[_safe_text(key, self.secrets)] = self.clean(item, depth + 1)
            return output
        if isinstance(value, (list, tuple)):
            return [self.clean(item, depth + 1) for item in value]
        if isinstance(value, str):
            return _safe_text(value, self.secrets)
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        if hasattr(value, 'item'):
            return self.clean(value.item(), depth + 1)
        return None


def _pick(record, fields):
    return {key: record[key] for key in fields if key in record}


def _messages(messages, encoder):
    if not isinstance(messages, list) or len(messages) > 250:
        raise ArchiveError('每份研究最多保存250轮问答。')
    result = []
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get('result'), dict):
            raise ArchiveError('问答记录结构不完整，无法保存。')
        result.append(encoder.clean({**_pick(message, ('question', 'effective_question', 'applied_year', 'selected_compare')),
                                     'result': _pick(message['result'], RESULT_FIELDS)}))
    return result


def _research(state, encoder):
    research = {'settings': encoder.clean(_pick(state, STATE_FIELDS)),
                'inputs': encoder.clean(_pick(state.get('navigation_inputs') or {}, INPUT_FIELDS)),
                'manual': encoder.clean(_pick(state.get('scope_manual_record') or {}, MANUAL_FIELDS)),
                'scope': encoder.clean(_pick(state.get('scope_condition_record') or {}, SCOPE_FIELDS)),
                'context': encoder.clean(_pick(state.get('conversation_context') or {}, CONTEXT_FIELDS)),
                'messages': _messages(state.get('chat_messages') or [], encoder), 'sessions': [], 'reviews': []}
    research['inputs'].update(encoder.clean(_pick(state, INPUT_FIELDS)))
    research['manual'].update(encoder.clean(_pick(state, MANUAL_FIELDS)))
    research['scope'].update(encoder.clean(_pick(state, SCOPE_FIELDS)))
    if len(state.get('saved_sessions') or []) > 10:
        raise ArchiveError('研究档案最多保存10个历史会话。')
    for session in state.get('saved_sessions') or []:
        research['sessions'].append({**encoder.clean(_pick(session, ('title', 'selected_main', 'selected_cmp', 'period', 'created_at', 'updated_at'))),
                                     'context': encoder.clean(_pick(session.get('context') or {}, CONTEXT_FIELDS)),
                                     'messages': _messages(session.get('messages') or [], encoder)})
    from core.review_queue import list_review_requests
    for review in list_review_requests(limit=500):
        research['reviews'].append(encoder.clean(_pick(review, ('schema_version', 'status', 'created_at', 'note', 'snapshot'))))
    return research


def build_research_archive(state, *, include_originals=True):
    """Read the current bound workspace and return bytes plus a human preview."""
    encoder = _Encoder(state, include_originals)
    conn = db.get_conn()
    try:
        raw = {table: [dict(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY id')]
               for table in TABLE_FIELDS}
    finally:
        conn.close()
    encoder.company_ids = {row['id']: f'c{index + 1}' for index, row in enumerate(raw['companies'])}
    encoder.report_ids = {row['id']: row for row in raw['report_files']}
    # Freeform historical notes may also contain a batch's old storage ID.
    for table in ('report_files', 'metric_sources', 'scope_probe_facts'):
        for row in raw[table]:
            identifier = row.get('import_id')
            encoder.import_ref(identifier)
            if identifier and identifier != 'builtin-v1':
                encoder.secrets.append(identifier)
    workspace = {}
    for table, fields in TABLE_FIELDS.items():
        records = []
        for row in raw[table]:
            record = _pick(row, fields)
            if table == 'companies':
                record['ref'] = encoder.company_ids[row['id']]
            else:
                record['company_ref'] = encoder.company_ids.get(row['company_id'])
            if table in {'report_files', 'metric_sources', 'scope_probe_facts'}:
                record['import_id'] = row.get('import_id')
            if table in {'report_files', 'metric_sources'}:
                record['file_path'] = row.get('file_path')
            if table == 'scope_probe_facts':
                record['evidence'] = _load(row['evidence_json'])
            records.append(encoder.clean(record))
        workspace[table] = records
    research = _research(state, encoder)
    content = {'workspace.json': _json(workspace), 'research.json': _json(research), **encoder.content}
    if any(len(content[name]) > MAX_JSON for name in ('workspace.json', 'research.json')):
        raise ArchiveError('研究记录超过12 MB，请减少历史会话。')
    manifest = {'schema_version': SCHEMA, 'created_at': datetime.now(timezone.utc).isoformat(),
                'files': encoder.files,
                'entries': [{'name': name, 'size': len(data), 'sha256': sha256(data).hexdigest()} for name, data in content.items()]}
    stream = BytesIO()
    # Stored entries avoid making legitimate highly repetitive tables look like
    # a compression bomb when the same strict validator reads our own export.
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_STORED) as archive:
        archive.writestr('manifest.json', _json(manifest))
        for name, data in content.items():
            archive.writestr(name, data)
    result = validate_research_archive(stream.getvalue())
    return {'zip_bytes': stream.getvalue(), 'summary': result['summary'], 'warnings': result['warnings']}


def _shape(value, fields, *, required=()):
    if not isinstance(value, dict) or set(value) - set(fields) or set(required) - set(value):
        raise ArchiveError('档案字段与当前版本不兼容。')


def _check_refs(value, files, imports, companies, depth=0):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == 'file_ref' and item is not None and item not in files:
                raise ArchiveError('档案原件引用不存在。')
            if key == 'import_ref' and item is not None:
                if not isinstance(item, str) or not re.fullmatch(r'[iu][1-9]\d*|builtin-v1', item):
                    raise ArchiveError('档案导入批次引用无效。')
                imports.add(item)
            if key == 'company_ref' and item is not None and item not in companies:
                raise ArchiveError('档案企业引用不存在。')
            if key in {'id', 'file_path', 'company_id', 'import_id', 'document_id', 'storage_id', 'path'} or key.endswith('_path') or SECRET_KEY.search(key):
                raise ArchiveError('档案含连接配置、存储标识或本机路径。')
            _check_refs(item, files, imports, companies, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _check_refs(item, files, imports, companies, depth + 1)
    elif isinstance(value, str) and _safe_text(value) != value:
        raise ArchiveError('档案含不安全的路径或连接信息。')


def _validate_records(workspace, research, files):
    _shape(workspace, TABLE_FIELDS, required=TABLE_FIELDS)
    companies = {}
    for table, rows in workspace.items():
        if not isinstance(rows, list) or len(rows) > MAX_ROWS:
            raise ArchiveError('档案数据表超限。')
        for row in rows:
            extra = ('ref',) if table == 'companies' else ('company_ref',)
            if table in {'report_files', 'metric_sources', 'scope_probe_facts'}:
                extra += ('import_ref',)
            if table in {'report_files', 'metric_sources'}:
                extra += ('file_ref',)
            if table == 'scope_probe_facts':
                extra += ('evidence',)
            _shape(row, (*TABLE_FIELDS[table], *extra), required=extra)
            if table == 'companies':
                if (not isinstance(row.get('ref'), str) or not re.fullmatch(r'c[1-9]\d*', row['ref'])
                        or row['ref'] in companies or not isinstance(row.get('name'), str)
                        or not row['name'].strip() or len(row['name']) > 300):
                    raise ArchiveError('档案企业身份无效或重复。')
                companies[row['ref']] = row
            for key in TABLE_FIELDS[table]:
                value = row.get(key)
                if value is not None and not isinstance(value, (str, int, float)):
                    raise ArchiveError('档案数据库字段类型无效。')
    if len({r['name'] for r in companies.values()}) != len(companies):
        raise ArchiveError('档案企业名称重复。')
    codes = [r['stock_code'] for r in companies.values() if r.get('stock_code')]
    if len(set(codes)) != len(codes):
        raise ArchiveError('档案股票代码归属重复。')
    imports = set()
    _check_refs(workspace, files, imports, companies)
    _check_refs(research, files, imports, companies)
    annuals = {}
    for row in workspace['financial_metrics']:
        key = (row['company_ref'], row.get('year'))
        if key in annuals or row['company_ref'] is None or type(row.get('year')) is not int or not 1900 <= row['year'] <= 2100:
            raise ArchiveError('档案年度记录无效或重复。')
        annuals[key] = row
        for metric in db.METRIC_FIELDS:
            value = row.get(metric)
            if value is not None and (not isinstance(value, str) if metric == 'audit_opinion' else
                                       type(value) not in (int, float) or not math.isfinite(value)):
                raise ArchiveError('档案财务指标格式无效。')
    current = set()
    builtins = _builtins()
    for source in workspace['metric_sources']:
        key = (source['company_ref'], source.get('year'), source.get('metric'))
        if (source.get('metric') not in db.METRIC_FIELDS or source.get('is_current') not in (0, 1)
                or type(source.get('year')) is not int or not 1900 <= source['year'] <= 2100):
            raise ArchiveError('档案指标来源字段无效。')
        value = _load(source.get('value_json') or 'null')
        if source['is_current']:
            if key in current or key[:2] not in annuals or annuals[key[:2]].get(key[2]) != value:
                raise ArchiveError('档案当前指标与来源数值不一致。')
            current.add(key)
        if source.get('import_ref') == 'builtin-v1':
            entry = files.get(source.get('file_ref'), {})
            if entry.get('kind') != 'builtin':
                raise ArchiveError('内置样本来源身份不成立。')
            name, code, _, records = builtins[entry['name']]
            company = companies[source['company_ref']]
            expected = next((r.get(source['metric']) for r in records if int(r['year']) == source['year']), None)
            if company['name'] != name or company.get('stock_code') != code or value != expected:
                raise ArchiveError('内置样本记录与随应用提供的文件不符。')
    for row in workspace['scope_probe_facts']:
        if (row.get('metric') not in {'parent_net_profit', 'consolidated_net_profit'}
                or type(row.get('year')) is not int or not 1900 <= row['year'] <= 2100
                or type(row.get('value')) not in (int, float) or not math.isfinite(row['value'])
                or row.get('is_current') not in (0, 1) or not isinstance(row.get('evidence'), dict)):
            raise ArchiveError('档案口径事实无效。')
    for report in workspace['report_files']:
        if not isinstance(report.get('file_name'), str) or not report['file_name']:
            raise ArchiveError('档案文档名称缺失。')
        if report.get('report_year') is not None and (type(report['report_year']) is not int or not 1900 <= report['report_year'] <= 2100):
            raise ArchiveError('档案文档年度无效。')
        if report.get('source_type') == 'builtin' or report.get('file_type') == 'builtin':
            entry = files.get(report.get('file_ref'), {})
            if entry.get('kind') != 'builtin' or report.get('import_ref') != 'builtin-v1':
                raise ArchiveError('内置文档引用无效。')
        file = files.get(report.get('file_ref'), {})
        if report.get('content_hash') and file.get('sha256') and report['content_hash'] != file['sha256']:
            raise ArchiveError('文档登记哈希与原件不符。')
    _shape(research, ('settings', 'inputs', 'manual', 'scope', 'context', 'messages', 'sessions', 'reviews'),
           required=('settings', 'inputs', 'manual', 'scope', 'context', 'messages', 'sessions', 'reviews'))
    _shape(research['settings'], STATE_FIELDS)
    _shape(research['inputs'], INPUT_FIELDS)
    _shape(research['manual'], MANUAL_FIELDS)
    _shape(research['scope'], SCOPE_FIELDS)
    _shape(research['context'], CONTEXT_FIELDS)
    for group in ('settings', 'inputs', 'manual', 'scope'):
        for key, value in research[group].items():
            if key == 'scope_manual_signature':
                if not isinstance(value, list) or len(value) != 6 or any(isinstance(item, (list, dict)) for item in value):
                    raise ArchiveError('人工口径条件签名格式无效。')
            elif key == 'enterprise_chart_metrics':
                if not isinstance(value, list) or any(item not in {'revenue', 'net_profit', 'operating_cashflow', 'roe', 'debt_ratio', 'gross_margin', 'eps'} for item in value):
                    raise ArchiveError('图表指标筛选无效。')
            elif key in {'source_history', 'scope_manual_confirmed'}:
                if type(value) is not bool:
                    raise ArchiveError('研究开关值格式无效。')
            elif value is not None and type(value) not in (str, int):
                raise ArchiveError('研究筛选或备注格式无效。')
    if research['settings'].get('investor_profile') not in (None, '稳健型', '平衡型', '成长型'):
        raise ArchiveError('研究分析偏好无效。')
    for group in ('settings', 'inputs'):
        if research[group].get('enterprise_chart_type') not in (None, '折线图', '柱形图', '饼图'):
            raise ArchiveError('研究图表类型无效。')
    if research['scope'].get('scope_mode') not in (None, '单条来源核验', '同企业跨年比较', '跨企业同期比较'):
        raise ArchiveError('人工口径核验方式无效。')
    if research['scope'].get('scope_metric') not in (None, *[metric for metric in db.METRIC_FIELDS if metric != 'audit_opinion']):
        raise ArchiveError('人工口径指标无效。')
    for field, allowed in {'scope_manual_currency': ('不补充', 'CNY', 'USD'),
                           'scope_manual_period': ('不补充', 'annual'),
                           'scope_manual_statement': ('不补充', 'consolidated', 'parent')}.items():
        if research['manual'].get(field) not in (None, *allowed):
            raise ArchiveError('人工口径选项无效。')
    def messages(items):
        if not isinstance(items, list) or len(items) > 250:
            raise ArchiveError('档案问答数量超限。')
        for item in items:
            _shape(item, ('question', 'effective_question', 'applied_year', 'selected_compare', 'result'), required=('question', 'result'))
            if not isinstance(item['question'], str):
                raise ArchiveError('档案问题格式错误。')
            _shape(item['result'], RESULT_FIELDS)
            result = item['result']
            parsed = result.get('parsed') or {}
            if isinstance(parsed, dict):
                for field in ('companies', 'years', 'metrics'):
                    items = parsed.get(field) or []
                    if not isinstance(items, list) or any(type(value) is not (int if field == 'years' else str) for value in items):
                        raise ArchiveError('档案问题条件格式无效。')
            for field in ('parsed', 'data_snapshot', 'presentation_snapshot', 'audit_snapshot',
                          'comparability_snapshot', 'delivery_state', 'attribution', 'scope_probe',
                          'probe_request', 'sql_result', 'retrieval_result', 'execution_budget'):
                if result.get(field) is not None and not isinstance(result[field], dict):
                    raise ArchiveError('档案回答快照格式无效。')
            for field in ('report_files_snapshot', 'rows', 'excluded', 'tasks', 'calculations'):
                if result.get(field) is not None and not isinstance(result[field], list):
                    raise ArchiveError('档案回答记录格式无效。')
            for name, rows in (result.get('data_snapshot') or {}).items():
                if not isinstance(name, str) or not isinstance(rows, list) or not all(isinstance(row, dict) and type(row.get('year')) is int for row in rows):
                    raise ArchiveError('档案企业年度快照格式无效。')
            presentation = result.get('presentation_snapshot')
            if presentation:
                if (not {'company', 'metric', 'year', 'base_year', 'records'}.issubset(presentation)
                        or not isinstance(presentation['company'], str) or presentation['metric'] not in db.METRIC_FIELDS
                        or type(presentation['year']) is not int or type(presentation['base_year']) is not int
                        or not isinstance(presentation['records'], list)
                        or not all(isinstance(row, dict) and type(row.get('year')) is int for row in presentation['records'])):
                    raise ArchiveError('档案展示快照格式无效。')
    messages(research['messages'])
    if not isinstance(research['sessions'], list) or len(research['sessions']) > 10:
        raise ArchiveError('档案历史会话超限。')
    for session in research['sessions']:
        _shape(session, ('title', 'selected_main', 'selected_cmp', 'period', 'created_at', 'updated_at', 'context', 'messages'), required=('title', 'messages', 'context'))
        if not isinstance(session['title'], str) or not session['title'].strip() or len(session['title']) > 300:
            raise ArchiveError('档案历史会话标题无效。')
        for field in ('selected_main', 'selected_cmp', 'period', 'created_at', 'updated_at'):
            if session.get(field) is not None and not isinstance(session[field], str):
                raise ArchiveError('档案历史会话条件格式无效。')
        _shape(session['context'], CONTEXT_FIELDS)
        messages(session['messages'])
    if not isinstance(research['reviews'], list) or len(research['reviews']) > 500:
        raise ArchiveError('档案人工复核记录超限。')
    for review in research['reviews']:
        _shape(review, ('schema_version', 'status', 'created_at', 'note', 'snapshot'), required=('schema_version', 'status', 'snapshot'))
        if (review['schema_version'] != 'review-request/1' or review['status'] != 'pending_review'
                or not isinstance(review['snapshot'], dict) or review['snapshot'].get('schema_version') != 'countercheck/1'):
            raise ArchiveError('档案人工复核状态无效。')
        if not isinstance(review.get('note', ''), str) or len(review.get('note', '')) > 4000:
            raise ArchiveError('人工复核备注最多4000字。')
        if len(_json(review)) > MAX_REVIEW:
            raise ArchiveError('单条人工复核记录超过2 MB。')
    return imports


def validate_research_archive(content):
    """Fully validate in memory. This function does not touch the active DB."""
    if not isinstance(content, bytes) or not content or len(content) > MAX_ZIP:
        raise ArchiveError('档案为空或超过112 MB。')
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(infos) > MAX_FILES + 3 or len(names) != len(set(names)):
                raise ArchiveError('档案成员重复或数量超限。')
            if sum(info.file_size for info in infos) > MAX_TOTAL + MAX_JSON * 2 + 1024 * 1024:
                raise ArchiveError('档案解压后总大小超限。')
            for info in infos:
                path = PurePosixPath(info.filename)
                mode = info.external_attr >> 16
                if (info.is_dir() or path.is_absolute() or '..' in path.parts or '\\' in info.filename
                        or ':' in info.filename or str(path) != info.filename or info.flag_bits & 1
                        or stat.S_ISLNK(mode) or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                        or info.file_size > MAX_FILE or info.file_size > max(info.compress_size, 1) * 200):
                    raise ArchiveError('档案含不安全路径、加密成员或异常压缩内容。')
                if info.filename.endswith('.json') and info.file_size > MAX_JSON:
                    raise ArchiveError('档案 JSON 超限。')
            if not {'manifest.json', 'workspace.json', 'research.json'}.issubset(names):
                raise ArchiveError('档案缺少清单或研究记录。')
            manifest = _load(archive.read('manifest.json'))
            _bounded(manifest)
            _shape(manifest, ('schema_version', 'created_at', 'files', 'entries'), required=('schema_version', 'files', 'entries'))
            if manifest['schema_version'] != SCHEMA or not isinstance(manifest['entries'], list) or not isinstance(manifest['files'], list):
                raise ArchiveError('不支持此研究档案版本。')
            entries = {}
            for entry in manifest['entries']:
                _shape(entry, ('name', 'size', 'sha256'), required=('name', 'size', 'sha256'))
                if not isinstance(entry['name'], str) or entry['name'] in entries:
                    raise ArchiveError('档案清单成员重复。')
                entries[entry['name']] = entry
            if set(entries) != set(names) - {'manifest.json'}:
                raise ArchiveError('档案成员与清单不一致。')
            parts = {}
            for name, entry in entries.items():
                data = archive.read(name)
                if type(entry['size']) is not int or len(data) != entry['size'] or sha256(data).hexdigest() != entry['sha256']:
                    raise ArchiveError('档案成员完整性检查失败。')
                parts[name] = data
            files, original_members = {}, set()
            builtin = _builtins()
            for item in manifest['files']:
                _shape(item, ('ref', 'name', 'kind', 'included', 'sha256', 'size', 'member'), required=('ref', 'name', 'kind', 'included', 'sha256', 'size'))
                if (not isinstance(item['ref'], str) or not re.fullmatch(r'f[1-9]\d*', item['ref']) or item['ref'] in files
                        or not isinstance(item['name'], str) or len(item['name']) > 300
                        or item['kind'] not in {'builtin', 'original', 'missing'} or type(item['included']) is not bool
                        or type(item['size']) is not int or not 0 <= item['size'] <= MAX_FILE):
                    raise ArchiveError('档案原件清单无效。')
                if item['kind'] == 'builtin':
                    if (item['name'] not in builtin or item['included'] or 'member' in item
                            or sha256(builtin[item['name']][2].read_bytes()).hexdigest() != item['sha256']):
                        raise ArchiveError('内置样本版本不一致，请使用匹配版本恢复。')
                elif item['included']:
                    member = item.get('member')
                    if (item['kind'] != 'original' or not isinstance(member, str)
                            or not re.fullmatch(r'originals/f[1-9]\d*\.(pdf|csv|xls|xlsx|docx)', member)
                            or member in original_members or member not in parts
                            or item['size'] != len(parts[member]) or sha256(parts[member]).hexdigest() != item['sha256']):
                        raise ArchiveError('档案原件引用或哈希无效。')
                    original_members.add(member)
                elif 'member' in item:
                    raise ArchiveError('缺失原件不能携带可写入路径。')
                files[item['ref']] = item
            if set(parts) != {'workspace.json', 'research.json'} | original_members:
                raise ArchiveError('档案含未声明的额外文件。')
            if len(original_members) > MAX_FILES or sum(len(parts[name]) for name in original_members) > MAX_TOTAL:
                raise ArchiveError('档案原件超过20份或100 MB。')
            workspace, research = _load(parts['workspace.json']), _load(parts['research.json'])
            _bounded(workspace)
            _bounded(research)
            imports = _validate_records(workspace, research, files)
            missing = sum(item['kind'] != 'builtin' and not item['included'] for item in files.values())
            return {'manifest': manifest, 'workspace': workspace, 'research': research, 'parts': parts,
                    'imports': imports, 'sha256': sha256(content).hexdigest(),
                    'summary': {'企业': len(workspace['companies']), '年度记录': len(workspace['financial_metrics']),
                                '逐项来源': len(workspace['metric_sources']), '原件': len(original_members),
                                '未附原件': missing, '当前问答': len(research['messages']),
                                '历史会话': len(research['sessions']), '人工复核': len(research['reviews'])},
                    'warnings': ([f'{missing}份原件未随档案保存；恢复后相关入口显示原件缺失。'] if missing else [])}
    except ArchiveError:
        raise
    except (zipfile.BadZipFile, KeyError, TypeError, ValueError, OSError, RuntimeError, RecursionError) as exc:
        raise ArchiveError('研究档案格式无效或已损坏。') from exc


def restore_block_reason(state=None):
    """Refuse overwrite; bundled sample records are the only allowed baseline."""
    state = state or {}
    if state.get('chat_messages') or state.get('saved_sessions') or state.get('counter_review_note') or state.get('scope_manual_record'):
        return '当前已有问答或人工备注。请先保存当前研究，再在新的浏览器会话中恢复。'
    conn = db.get_conn()
    try:
        for table in ('metric_sources', 'report_files', 'scope_probe_facts'):
            if conn.execute(f"SELECT 1 FROM {table} WHERE COALESCE(import_id,'') != 'builtin-v1' LIMIT 1").fetchone():
                return '当前已有用户资料。请先导出保存，再在新的浏览器会话中恢复，系统不会覆盖当前研究。'
        if conn.execute("SELECT 1 FROM companies WHERE COALESCE(source,'') != 'builtin' LIMIT 1").fetchone():
            return '当前已有用户企业资料，请在新的浏览器会话中恢复。'
    finally:
        conn.close()
    root = get_data_root()
    if any(folder.exists() and any(path.is_file() for path in folder.rglob('*'))
           for folder in (root / 'uploads', root / 'review_requests')):
        return '当前保留了用户原件或复核记录，请在新的浏览器会话中恢复。'
    return ''


def _write_file(path, content):
    with path.open('xb') as handle:
        handle.write(content)


def restore_research_archive(content, *, confirmed=False, expected_sha256=None, state=None):
    """Validate again, then atomically write the library and return UI state.

    Callers apply ``state`` on the next full UI run, before creating widgets.
    Existing approved/conflicting/pending snapshots are copied, never recomputed.
    """
    if not confirmed:
        raise ArchiveError('请先校验预览并明确确认恢复。')
    archive = validate_research_archive(content)
    if not expected_sha256 or archive['sha256'] != expected_sha256:
        raise ArchiveError('档案已变化，请重新校验预览。')
    require_write_access()
    reason = restore_block_reason(state)
    if reason:
        raise ArchiveError(reason)
    root = get_data_root().resolve()
    nonce = uuid4().hex
    folder = root / 'uploads' / f'archive_{nonce}'
    review_folder = root / 'review_requests'
    for target in (folder, review_folder):
        if not target.resolve().is_relative_to(root) or target.is_symlink() or target.parent.is_symlink():
            raise ArchiveError('恢复目录不属于当前资料库。')
    file_paths = {}
    imports = {ref: 'builtin-v1' if ref == 'builtin-v1' else
               ('legacy-archive-unverified-' if ref.startswith('u') else '') + uuid4().hex
               for ref in archive['imports']}
    companies = {}
    created, directories = [], []
    def rebind(value):
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if key == 'file_ref':
                    result['file_path'] = file_paths.get(item)
                elif key == 'import_ref':
                    result['import_id'] = imports.get(item)
                elif key == 'company_ref':
                    result['company_id'] = companies.get(item)
                else:
                    result[key] = rebind(item)
            return result
        return [rebind(item) for item in value] if isinstance(value, list) else value
    try:
        with db.atomic_write():
            # Recheck under the same transaction before replacing sample rows.
            reason = restore_block_reason(state)
            if reason:
                raise ArchiveError(reason)
            for entry in archive['manifest']['files']:
                if entry['kind'] == 'builtin':
                    file_paths[entry['ref']] = str(_builtins()[entry['name']][2])
                elif entry['included']:
                    if not folder.exists():
                        if not folder.parent.exists():
                            folder.parent.mkdir()
                            directories.append(folder.parent)
                        folder.mkdir()
                        directories.append(folder)
                    path = folder / Path(entry['member']).name
                    created.append(path)
                    _write_file(path, archive['parts'][entry['member']])
                    file_paths[entry['ref']] = str(path)
                else:
                    file_paths[entry['ref']] = None
            conn = db.get_conn()
            for table in ('scope_probe_facts', 'metric_sources', 'financial_metrics', 'report_files', 'companies'):
                conn.execute(f'DELETE FROM {table}')
            for company in archive['workspace']['companies']:
                fields = TABLE_FIELDS['companies']
                row = _pick(company, fields)
                # Archive labels never confer a new verified origin.
                cursor = conn.execute(f"INSERT INTO companies({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
                                      [row.get(field) for field in fields])
                companies[company['ref']] = cursor.lastrowid
            for table in ('financial_metrics', 'report_files', 'metric_sources', 'scope_probe_facts'):
                for original in archive['workspace'][table]:
                    row = rebind(original)
                    if table == 'scope_probe_facts':
                        row['evidence_json'] = _json(row.pop('evidence')).decode('utf-8')
                    fields = tuple(row)
                    conn.execute(f"INSERT INTO {table}({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
                                 [row[field] for field in fields])
            research = rebind(deepcopy(archive['research']))
            for review in research['reviews']:
                if not review_folder.exists():
                    review_folder.mkdir()
                    directories.append(review_folder)
                identifier = uuid4().hex
                path = review_folder / f'{identifier}.json'
                payload = _json({**review, 'id': identifier, 'workspace_root': str(root)})
                if len(payload) > MAX_REVIEW:
                    raise ArchiveError('恢复后的单条人工复核记录超过2 MB，已取消本次恢复。')
                created.append(path)
                _write_file(path, payload)
            restored_state = {**research['settings'], **research['inputs'], **research['manual'], **research['scope'],
                              'navigation_inputs': research['inputs'], 'scope_manual_record': research['manual'],
                              'scope_condition_record': research['scope'],
                              'conversation_context': research['context'], 'chat_messages': research['messages'],
                              'active_session_id': uuid4().hex}
            restored_state['saved_sessions'] = [dict(session, id=uuid4().hex) for session in research['sessions']]
            restored_state['recent_sessions'] = [session.get('title', '历史会话') for session in research['sessions']]
            restored_state['qa_main'] = restored_state.get('selected_main')
            restored_state['enterprise_main'] = restored_state.get('selected_main')
            restored_state['enterprise_cmp'] = restored_state.get('selected_cmp')
            if isinstance(restored_state.get('scope_manual_signature'), list):
                restored_state['scope_manual_signature'] = tuple(restored_state['scope_manual_signature'])
    except Exception:
        for path in reversed(created):
            if path.resolve().is_relative_to(root):
                path.unlink(missing_ok=True)
        for directory in reversed(directories):
            if directory.resolve().is_relative_to(root):
                try:
                    directory.rmdir()
                except OSError:
                    pass
        raise
    return {'state': restored_state, 'summary': archive['summary'], 'warnings': archive['warnings']}
