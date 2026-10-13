from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import hashlib
import math
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

from core.storage import ROOT, get_data_root, get_workspace, require_write_access, is_session_workspace

DB_PATH = get_data_root() / 'investor_agent.db'
_INITIAL_DB_PATH = DB_PATH
_transaction = ContextVar('financial_database_transaction', default=None)
METRIC_FIELDS = ('revenue', 'net_profit', 'operating_cashflow', 'roe', 'debt_ratio', 'gross_margin', 'eps', 'audit_opinion', 'consolidated_net_profit', 'total_assets', 'total_liabilities', 'total_equity', 'minority_profit')
METRIC_UNITS = {'revenue': '亿元', 'net_profit': '亿元', 'operating_cashflow': '亿元', 'roe': '%', 'debt_ratio': '%', 'gross_margin': '%', 'eps': '元/股', 'audit_opinion': '文本', 'consolidated_net_profit': '亿元', 'total_assets': '亿元', 'total_liabilities': '亿元', 'total_equity': '亿元', 'minority_profit': '亿元'}


def get_db_path() -> Path:
    # A compatibility override must never redirect the isolated benchmark DB.
    if get_workspace() != 'evaluation' and not is_session_workspace() and DB_PATH != _INITIAL_DB_PATH:
        return Path(DB_PATH)
    return get_data_root() / 'investor_agent.db'


class _BorrowedConnection:
    def __init__(self, connection):
        self.connection = connection

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def commit(self):
        pass

    def close(self):
        pass


def get_conn() -> sqlite3.Connection:
    if _transaction.get() is not None:
        connection, transaction_path = _transaction.get()
        if get_db_path().resolve() != transaction_path:
            raise RuntimeError('事务内不能切换资料库；请先完成当前事务。')
        return _BorrowedConnection(connection)
    path = get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA busy_timeout=15000')
    return conn


@contextmanager
def atomic_write():
    require_write_access()
    if _transaction.get() is not None:
        get_conn()  # Validate that nested writes still target the same database.
        yield
        return
    conn = get_conn()
    conn.execute('BEGIN IMMEDIATE')
    token = _transaction.set((conn, get_db_path().resolve()))
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _transaction.reset(token)
        conn.close()


def init_db(*, migrate_legacy: bool = True) -> None:
    migrate_legacy = migrate_legacy and not is_session_workspace()
    conn = get_conn()
    if conn.execute('PRAGMA user_version').fetchone()[0] >= 6:
        conn.close()
        if migrate_legacy and get_workspace() == 'main':
            from core.unified_migration import migrate_legacy_workspaces
            migrate_legacy_workspaces()
        return
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL,
            stock_code TEXT, industry TEXT, source TEXT DEFAULT 'upload'
        );
        CREATE TABLE IF NOT EXISTS financial_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER NOT NULL,
            year INTEGER NOT NULL, revenue REAL, net_profit REAL, operating_cashflow REAL,
            roe REAL, debt_ratio REAL, gross_margin REAL, eps REAL, audit_opinion TEXT,
            raw_source TEXT, UNIQUE(company_id, year),
            FOREIGN KEY(company_id) REFERENCES companies(id)
        );
        CREATE TABLE IF NOT EXISTS report_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER, file_name TEXT NOT NULL,
            report_year INTEGER, file_type TEXT, file_path TEXT, parse_status TEXT, note TEXT,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(company_id) REFERENCES companies(id)
        );
        CREATE TABLE IF NOT EXISTS metric_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER NOT NULL, year INTEGER NOT NULL,
            metric TEXT NOT NULL, value_json TEXT NOT NULL, normalized_unit TEXT,
            file_name TEXT, file_path TEXT, page INTEGER, cell TEXT,
            raw_value TEXT, raw_unit TEXT, source_url TEXT, import_id TEXT NOT NULL,
            source_note TEXT, imported_at TEXT NOT NULL, is_current INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY(company_id) REFERENCES companies(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS metric_sources_current
        ON metric_sources(company_id,year,metric) WHERE is_current=1;
        CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS scope_probe_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER NOT NULL,
            year INTEGER NOT NULL, metric TEXT NOT NULL, value REAL NOT NULL,
            currency TEXT, period TEXT NOT NULL DEFAULT 'annual', statement_scope TEXT,
            report_version TEXT, evidence_json TEXT NOT NULL, import_id TEXT NOT NULL,
            created_at TEXT NOT NULL, is_current INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY(company_id) REFERENCES companies(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS scope_probe_facts_lookup
            ON scope_probe_facts(company_id,year,metric,is_current);
    ''')
    # Preserve net_profit's historical meaning (attributable-to-parent profit).
    # A second economic metric is nullable and never backfilled from net_profit.
    financial_columns = {row['name'] for row in conn.execute('PRAGMA table_info(financial_metrics)')}
    for field in ('consolidated_net_profit', 'total_assets', 'total_liabilities', 'total_equity', 'minority_profit'):
        if field not in financial_columns:
            conn.execute(f'ALTER TABLE financial_metrics ADD COLUMN {field} REAL')
    source_columns = {row['name'] for row in conn.execute('PRAGMA table_info(metric_sources)')}
    for field in ('currency_hint', 'scope_hint', 'version_hint', 'metric_label', 'location_type'):
        if field not in source_columns:
            conn.execute(f'ALTER TABLE metric_sources ADD COLUMN {field} TEXT')
    if conn.execute("SELECT value FROM app_meta WHERE key='provenance_v1'").fetchone() is None:
        for row in conn.execute('SELECT * FROM financial_metrics').fetchall():
            for key in METRIC_FIELDS:
                if row[key] is None:
                    continue
                conn.execute('''INSERT OR IGNORE INTO metric_sources
                    (company_id,year,metric,value_json,normalized_unit,raw_value,raw_unit,import_id,source_note,imported_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)''', (
                        row['company_id'], row['year'], key, json.dumps(row[key], ensure_ascii=False),
                        METRIC_UNITS[key], str(row[key]), '旧版未记录', 'legacy-unverified',
                        '旧版来源待复核：' + (row['raw_source'] or '无逐指标来源'), datetime.now(timezone.utc).isoformat(),
                    ))
        conn.execute("INSERT INTO app_meta(key,value) VALUES('provenance_v1','1')")
    columns = {row['name'] for row in conn.execute('PRAGMA table_info(report_files)')}
    for column, declaration in {
        'source_type': 'TEXT', 'source_url': 'TEXT', 'report_title': 'TEXT',
        'report_type': 'TEXT', 'import_id': 'TEXT', 'content_hash': 'TEXT',
        'ingest_status': 'TEXT', 'metric_count': 'INTEGER DEFAULT 0',
    }.items():
        if column not in columns:
            conn.execute(f'ALTER TABLE report_files ADD COLUMN {column} {declaration}')
    conn.execute('CREATE INDEX IF NOT EXISTS report_files_import ON report_files(import_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS report_files_content ON report_files(content_hash)')
    _backfill_legacy_report_metadata(conn)
    conn.execute('PRAGMA user_version=6')
    conn.commit()
    conn.close()
    if migrate_legacy and get_workspace() == 'main':
        from core.unified_migration import migrate_legacy_workspaces
        migrate_legacy_workspaces()


def _legacy_report_source(annual: dict, reports: list[dict], namespace: str) -> dict:
    """Associate old aggregate provenance only when identity is exact and unique."""
    raw = str(annual.get('raw_source') or '').strip().replace('\\', '/')
    if not raw:
        return {}
    matches = [report for report in reports
               if report.get('company_id') == annual.get('company_id')
               and report.get('report_year') == annual.get('year')
               and raw in {str(report.get('file_name') or '').replace('\\', '/'),
                           str(report.get('file_path') or '').replace('\\', '/')}]
    if len(matches) != 1:
        return {}
    report = matches[0]
    return {'file_name': report.get('file_name'), 'file_path': report.get('file_path'),
            'import_id': report.get('import_id') or f'legacy-{namespace}-report-{report["id"]}'}


def _report_sources(conn, report: dict) -> list[dict]:
    if report.get('company_id') is None:
        return []
    is_pdf = str(report.get('file_type')).lower() == 'pdf' or str(report.get('file_name')).lower().endswith('.pdf')
    sources = [dict(row) for row in conn.execute('SELECT * FROM metric_sources WHERE company_id=?', (report['company_id'],))]
    return [source for source in sources if
            ((report.get('file_path') and source.get('file_path') == report['file_path']) or
             (report.get('import_id') and source.get('import_id') == report['import_id'])) and
            (is_pdf or report.get('report_year') is None or source['year'] == report['report_year'])]


def _source_metric_count(conn, sources: list[dict]) -> int:
    valid = set()
    for source in sources:
        if source['metric'] not in METRIC_FIELDS or source['metric'] == 'audit_opinion':
            continue
        try:
            value = json.loads(source['value_json'])
            if isinstance(value, bool) or value is None or not math.isfinite(float(value)):
                continue
            annual = conn.execute('SELECT * FROM financial_metrics WHERE company_id=? AND year=?',
                                  (source['company_id'], source['year'])).fetchone()
            actual = annual[source['metric']] if annual else None
            if actual is None or isinstance(actual, bool) or not math.isfinite(float(actual)):
                continue
        except (TypeError, ValueError):
            continue
        valid.add((source['year'], source['metric']))
    return len(valid)


def _backfill_legacy_report_metadata(conn) -> None:
    """Upgrade actual stored provenance once; never infer metadata from UI notes."""
    reports = [dict(row) for row in conn.execute('SELECT * FROM report_files')]
    namespace = get_data_root().name
    for annual in conn.execute('SELECT * FROM financial_metrics').fetchall():
        matched = _legacy_report_source(dict(annual), reports, namespace)
        for metric in METRIC_FIELDS:
            if annual[metric] is None:
                continue
            source = conn.execute('''SELECT * FROM metric_sources WHERE company_id=? AND year=? AND metric=? AND is_current=1''',
                                  (annual['company_id'], annual['year'], metric)).fetchone()
            if source is None:
                conn.execute('''INSERT INTO metric_sources(company_id,year,metric,value_json,normalized_unit,
                    file_name,file_path,raw_value,raw_unit,import_id,source_note,imported_at,is_current)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1)''', (
                        annual['company_id'], annual['year'], metric, json.dumps(annual[metric], ensure_ascii=False),
                        METRIC_UNITS[metric], matched.get('file_name'), matched.get('file_path'), str(annual[metric]),
                        '旧版未记录', matched.get('import_id') or 'legacy-unverified',
                        '旧版来源待复核：' + (annual['raw_source'] or '无逐指标来源'), datetime.now(timezone.utc).isoformat()))
            elif matched and not source['file_path'] and not source['file_name'] and 'unverified' in source['import_id']:
                conn.execute('UPDATE metric_sources SET file_name=?,file_path=?,import_id=? WHERE id=?',
                             (matched['file_name'], matched['file_path'], matched['import_id'], source['id']))
    for report in reports:
        # Existing structured import results are authoritative. Only rows from
        # before the schema upgrade need this one-time reconstruction.
        if report.get('ingest_status'):
            continue
        sources = _report_sources(conn, report)
        import_ids = {source['import_id'] for source in sources if source.get('import_id')}
        import_id = report.get('import_id') or (next(iter(import_ids)) if len(import_ids) == 1 else
                                               f'legacy-{namespace}-report-{report["id"]}')
        count = _source_metric_count(conn, sources)
        source_url = report.get('source_url') or next((source['source_url'] for source in sources if source.get('source_url')), None)
        source_type = report.get('source_type') or ('builtin' if report.get('file_type') == 'builtin' else
                                                  ('direct_url' if source_url else 'upload'))
        digest = report.get('content_hash')
        if not digest and report.get('file_path'):
            from core.storage import resolve_stored_file
            path = resolve_stored_file(report['file_path'])
            if path:
                hasher = hashlib.sha256()
                try:
                    with path.open('rb') as handle:
                        for block in iter(lambda: handle.read(1024 * 1024), b''):
                            hasher.update(block)
                    digest = hasher.hexdigest()
                except OSError:
                    # An unavailable old attachment must not discard stored
                    # financial values or prevent upgrading the database.
                    digest = None
        conn.execute('''UPDATE report_files SET source_type=?,source_url=?,report_title=?,import_id=?,content_hash=?,
            ingest_status=?,metric_count=? WHERE id=?''', (
                source_type, source_url, report.get('report_title') or report['file_name'], import_id, digest,
                'imported' if count else 'pending_metrics', count, report['id']))


def upsert_company(name: str, stock_code: str | None = None, industry: str | None = None, source: str = 'upload') -> int:
    require_write_access()
    name = (name or '').strip()
    if not name:
        raise ValueError('企业名称缺失，不能入库。')
    conn = get_conn()
    stock_code = str(stock_code).strip().upper() if stock_code else None
    if stock_code:
        existing = conn.execute('SELECT id FROM companies WHERE UPPER(TRIM(stock_code))=? ORDER BY id LIMIT 1', (stock_code,)).fetchone()
        if existing:
            conn.execute('UPDATE companies SET industry=COALESCE(?,industry) WHERE id=?', (industry, existing['id']))
            conn.commit()
            conn.close()
            return int(existing['id'])
    conn.execute('''INSERT INTO companies(name,stock_code,industry,source) VALUES(?,?,?,?)
        ON CONFLICT(name) DO UPDATE SET
        stock_code=COALESCE(excluded.stock_code,companies.stock_code),
        industry=COALESCE(excluded.industry,companies.industry)''', (name, stock_code, industry, source))
    cid = conn.execute('SELECT id FROM companies WHERE name=?', (name,)).fetchone()['id']
    conn.commit()
    conn.close()
    return int(cid)


def upsert_metric(company_id: int, metric: dict[str, Any], *, only_missing: bool = False) -> None:
    with atomic_write():
        _upsert_metric(company_id, metric, only_missing=only_missing)


def _upsert_metric(company_id: int, metric: dict[str, Any], *, only_missing: bool = False) -> None:
    require_write_access()
    if metric.get('period') not in (None, '', 'annual', '全年', '年度'):
        raise ValueError('当前指标库仅支持年度数据，不能将中期或季度数据写入全年记录。')
    year = metric.get('year')
    if isinstance(year, bool) or year is None or int(year) != float(year) or not 1900 <= int(year) <= 2100:
        raise ValueError('报告年份缺失或无效。')
    year = int(year)
    values = {}
    for key in METRIC_FIELDS:
        value = metric.get(key)
        if value is None or (isinstance(value, float) and math.isnan(value)):
            continue
        if key != 'audit_opinion':
            if isinstance(value, bool) or not math.isfinite(float(value)):
                raise ValueError(f'{key} 不是有效财务数值。')
            value = float(value)
        else:
            value = str(value).strip()
            if not value:
                continue
        values[key] = value
    if not values:
        raise ValueError('没有有效指标，不能创建空的年度记录。')
    conn = get_conn()
    old = conn.execute('SELECT * FROM financial_metrics WHERE company_id=? AND year=?', (company_id, year)).fetchone()
    if old and only_missing:
        values = {key: value for key, value in values.items() if old[key] is None}
    if not values:
        conn.close()
        return
    if old is None:
        conn.execute('INSERT INTO financial_metrics(company_id,year) VALUES(?,?)', (company_id, year))
    sql_set = ', '.join(f'{key}=?' for key in values)
    conn.execute(f'UPDATE financial_metrics SET {sql_set} WHERE company_id=? AND year=?', (*values.values(), company_id, year))
    provenance = metric.get('_provenance') or {}
    import_id = metric.get('import_id') or uuid4().hex
    for key, value in values.items():
        source = provenance.get(key) or {}
        if key in ('net_profit', 'consolidated_net_profit'):
            # A newer ambiguous/legacy import must not leave an older typed
            # fact marked current. The reviewed import service reactivates a
            # fresh independently labelled fact in this same transaction.
            conn.execute('UPDATE scope_probe_facts SET is_current=0 WHERE company_id=? AND year=? AND metric=?',
                         (company_id, year, 'parent_net_profit' if key == 'net_profit' else key))
        conn.execute('UPDATE metric_sources SET is_current=0 WHERE company_id=? AND year=? AND metric=? AND is_current=1', (company_id, year, key))
        conn.execute('''INSERT INTO metric_sources
            (company_id,year,metric,value_json,normalized_unit,file_name,file_path,page,cell,
             raw_value,raw_unit,source_url,import_id,source_note,imported_at,
             currency_hint,scope_hint,version_hint,metric_label,location_type)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
                company_id, year, key, json.dumps(value, ensure_ascii=False), source.get('normalized_unit', METRIC_UNITS[key]),
                source.get('file_name'), source.get('file_path'), source.get('page'), source.get('cell'),
                str(source.get('raw_value', value)), source.get('raw_unit', '未记录'), source.get('source_url'),
                source.get('import_id') or import_id, source.get('source_note') or metric.get('raw_source', ''),
                datetime.now(timezone.utc).isoformat(),
                source.get('currency_hint'), source.get('scope_hint'), source.get('version_hint'),
                source.get('metric_label'), source.get('location_type'),
            ))
    current_sources = conn.execute('SELECT metric,file_name,page,cell,source_note FROM metric_sources WHERE company_id=? AND year=? AND is_current=1 ORDER BY metric', (company_id, year)).fetchall()
    summary = '；'.join(f"{row['metric']}：{row['file_name'] or row['source_note'] or '来源未记录'}" + (f" 第{row['page']}页" if row['page'] else '') + (f" {row['cell']}" if row['cell'] else '') for row in current_sources)
    conn.execute('UPDATE financial_metrics SET raw_source=? WHERE company_id=? AND year=?', (summary, company_id, year))
    conn.commit()
    conn.close()


def insert_report_file(company_id: int | None, file_name: str, report_year: int | None, file_type: str, file_path: str, parse_status: str, note: str = '', *,
                       source_type: str | None = None, source_url: str | None = None,
                       report_title: str | None = None, report_type: str | None = None,
                       import_id: str | None = None, content_hash: str | None = None,
                       ingest_status: str | None = None, metric_count: int = 0) -> int:
    require_write_access()
    conn = get_conn()
    cur = conn.execute('''INSERT INTO report_files(company_id,file_name,report_year,file_type,file_path,parse_status,note,
        source_type,source_url,report_title,report_type,import_id,content_hash,ingest_status,metric_count)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (company_id, file_name, report_year, file_type, file_path, parse_status, note,
        source_type, source_url, report_title, report_type, import_id, content_hash, ingest_status, int(metric_count)))
    report_id = cur.lastrowid
    conn.commit()
    conn.close()
    return report_id


def fetch_companies() -> list[sqlite3.Row]:
    conn = get_conn()
    rows = conn.execute('SELECT * FROM companies ORDER BY CASE WHEN source="builtin" THEN 0 ELSE 1 END,name').fetchall()
    conn.close()
    return rows


def fetch_company_metrics(company_id: int) -> list[sqlite3.Row]:
    conn = get_conn()
    rows = conn.execute('SELECT * FROM financial_metrics WHERE company_id=? ORDER BY year', (company_id,)).fetchall()
    conn.close()
    return rows


def fetch_metric_sources(company_id: int, year: int | None = None, metric: str | None = None, *, include_history: bool = False) -> list[sqlite3.Row]:
    clauses, params = ['company_id=?'], [company_id]
    if year is not None:
        clauses.append('year=?')
        params.append(int(year))
    if metric:
        clauses.append('metric=?')
        params.append(metric)
    if not include_history:
        clauses.append('is_current=1')
    conn = get_conn()
    rows = conn.execute('SELECT * FROM metric_sources WHERE ' + ' AND '.join(clauses) + ' ORDER BY year,metric,id DESC', params).fetchall()
    conn.close()
    return rows


def fetch_report_files(company_id: int | None = None) -> list[sqlite3.Row]:
    conn = get_conn()
    sql = 'SELECT rf.*,c.name AS company_name,c.stock_code AS stock_code FROM report_files rf LEFT JOIN companies c ON c.id=rf.company_id'
    rows = conn.execute(sql + (' WHERE rf.company_id=?' if company_id is not None else '') + ' ORDER BY uploaded_at DESC,rf.id DESC', (company_id,) if company_id is not None else ()).fetchall()
    conn.close()
    return rows


def delete_uploaded_company(company_id: int) -> bool:
    require_write_access()
    with atomic_write():
        conn = get_conn()
        row = conn.execute('SELECT source FROM companies WHERE id=?', (company_id,)).fetchone()
        if not row or row['source'] == 'builtin':
            return False
        conn.execute('DELETE FROM metric_sources WHERE company_id=?', (company_id,))
        conn.execute('DELETE FROM financial_metrics WHERE company_id=?', (company_id,))
        conn.execute('DELETE FROM report_files WHERE company_id=?', (company_id,))
        conn.execute('DELETE FROM companies WHERE id=?', (company_id,))
    return True


def delete_report_file(report_id: int) -> bool:
    """Remove a report's provenance and restore surviving historical metric values."""
    require_write_access()
    with atomic_write():
        conn = get_conn()
        report = conn.execute('SELECT * FROM report_files WHERE id=?', (report_id,)).fetchone()
        if report is None:
            return False
        company_id = report['company_id']
        is_pdf = str(report['file_type']).lower() in ('pdf', 'docx') or str(report['file_name']).lower().endswith(('.pdf', '.docx'))
        clauses, params = ['company_id=?'], [company_id]
        identity, identity_params = [], []
        if report['import_id']:
            identity.append('import_id=?')
            identity_params.append(report['import_id'])
        if report['file_path']:
            identity.append('file_path=?')
            identity_params.append(report['file_path'])
        if not identity:
            conn.execute('DELETE FROM report_files WHERE id=?', (report_id,))
            return True
        clauses.append('(' + ' OR '.join(identity) + ')')
        params.extend(identity_params)
        if not is_pdf and report['report_year'] is not None:
            clauses.append('year=?')
            params.append(report['report_year'])
        candidates = conn.execute('SELECT * FROM metric_sources WHERE ' + ' AND '.join(clauses), params).fetchall()
        conn.execute('DELETE FROM report_files WHERE id=?', (report_id,))
        survivors = conn.execute('SELECT * FROM report_files WHERE company_id=?', (company_id,)).fetchall()
        affected = set()
        for source in candidates:
            still_linked = any(
                ((other['file_path'] and other['file_path'] == source['file_path']) or
                 (other['import_id'] and other['import_id'] == source['import_id'])) and
                (other['report_year'] is None or other['report_year'] == source['year'] or
                 str(other['file_type']).lower() in ('pdf', 'docx') or str(other['file_name']).lower().endswith(('.pdf', '.docx')))
                for other in survivors)
            if still_linked:
                continue
            if source['is_current']:
                affected.add((source['year'], source['metric']))
            conn.execute('DELETE FROM metric_sources WHERE id=?', (source['id'],))
            conn.execute('DELETE FROM scope_probe_facts WHERE company_id=? AND year=? AND import_id=? AND metric=?',
                         (company_id, source['year'], source['import_id'],
                          'parent_net_profit' if source['metric'] == 'net_profit' else source['metric']))
        for year, metric in affected:
            if metric not in METRIC_FIELDS:
                continue
            previous = conn.execute('''SELECT * FROM metric_sources WHERE company_id=? AND year=? AND metric=?
                ORDER BY imported_at DESC,id DESC LIMIT 1''', (company_id, year, metric)).fetchone()
            conn.execute('UPDATE metric_sources SET is_current=0 WHERE company_id=? AND year=? AND metric=?', (company_id, year, metric))
            conn.execute('UPDATE scope_probe_facts SET is_current=0 WHERE company_id=? AND year=? AND metric=?',
                         (company_id, year, 'parent_net_profit' if metric == 'net_profit' else metric))
            value = json.loads(previous['value_json']) if previous else None
            if previous:
                conn.execute('UPDATE metric_sources SET is_current=1 WHERE id=?', (previous['id'],))
                conn.execute('UPDATE scope_probe_facts SET is_current=1 WHERE company_id=? AND year=? AND import_id=? AND metric=?',
                             (company_id, year, previous['import_id'],
                              'parent_net_profit' if metric == 'net_profit' else metric))
            conn.execute(f'UPDATE financial_metrics SET {metric}=? WHERE company_id=? AND year=?', (value, company_id, year))
        for year in {year for year, _ in affected}:
            current = conn.execute('SELECT metric,file_name,source_note FROM metric_sources WHERE company_id=? AND year=? AND is_current=1', (company_id, year)).fetchall()
            summary = '；'.join(f"{row['metric']}：{row['file_name'] or row['source_note'] or '来源未记录'}" for row in current)
            conn.execute('UPDATE financial_metrics SET raw_source=? WHERE company_id=? AND year=?', (summary, company_id, year))
        empty = ' AND '.join(f'{metric} IS NULL' for metric in METRIC_FIELDS)
        conn.execute(f'DELETE FROM financial_metrics WHERE company_id=? AND {empty}', (company_id,))
    return True
