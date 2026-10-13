"""One-time compatibility migration from demo/personal into main.

Legacy databases are opened read-only. IDs are always rebuilt; main values that
already existed before migration are retained. Among legacy values, personal
wins over demo one metric at a time. Source directories remain intact.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

from core import db
from core.storage import ROOT, get_data_root, get_workspace, internal_writes

MIGRATION_KEY = 'unified_workspace_migration_v1'
SOURCE_COLUMNS = (
    'normalized_unit', 'file_name', 'file_path', 'page', 'cell', 'raw_value',
    'raw_unit', 'source_url', 'import_id', 'source_note', 'imported_at',
)


def _digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


class _Attachments:
    def __init__(self, target: Path):
        self.target = target.resolve()
        self.paths = {}
        self.hashes = {}
        self.created = []
        self.missing = []

    def copy(self, value: str | None, source_root: Path) -> tuple[str | None, str | None]:
        if not value:
            return None, None
        key = (str(source_root), str(value))
        if key in self.paths:
            return self.paths[key]
        raw = Path(str(value).replace('\\', '/'))
        candidates = [raw] if raw.is_absolute() else [source_root / raw, source_root.parent / raw, ROOT / raw]
        allowed = [source_root.resolve(), (ROOT / 'data').resolve(), (ROOT / 'uploads').resolve()]
        original = next((candidate.resolve() for candidate in candidates
                         if candidate.is_file() and any(candidate.resolve().is_relative_to(base) for base in allowed)), None)
        if original is None:
            self.missing.append(str(value))
            self.paths[key] = (str(value), None)
            return self.paths[key]
        digest = _digest(original)
        destination = self.hashes.get(digest)
        if destination is None:
            folder = self.target / 'uploads' / 'migrated' / digest
            # A stable content directory also makes a retry safe after an
            # interrupted process, including files with different legacy names.
            existing = sorted(folder.glob('*')) if folder.is_dir() else []
            destination = next((path for path in existing if path.is_file() and _digest(path) == digest), None)
            if destination is None:
                destination = folder / original.name
                folder.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    raise ValueError('迁移附件与内容哈希不一致：' + str(destination))
                try:
                    with original.open('rb') as source, destination.open('xb') as target:
                        shutil.copyfileobj(source, target)
                    self.created.append(destination)
                    if _digest(destination) != digest or _digest(original) != digest:
                        raise ValueError('迁移时附件发生变化，请暂停编辑后重试。')
                except Exception:
                    if destination.is_file() and destination not in self.created:
                        self.created.append(destination)
                    raise
            self.hashes[digest] = destination
        self.paths[key] = (str(destination), digest)
        return self.paths[key]

    def rollback(self):
        for path in reversed(self.created):
            resolved = path.resolve()
            if resolved.is_relative_to(self.target / 'uploads' / 'migrated'):
                resolved.unlink(missing_ok=True)


def _source_rows(source, table):
    exists = source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return [dict(row) for row in source.execute(f'SELECT * FROM {table} ORDER BY id')] if exists else []


def _insert_source(conn, company_id, year, metric, value, row, files, source_root, namespace):
    data = {key: row.get(key) for key in SOURCE_COLUMNS}
    data['file_path'], _ = files.copy(data['file_path'], source_root)
    data['normalized_unit'] = data['normalized_unit'] or db.METRIC_UNITS[metric]
    data['import_id'] = data['import_id'] or f'legacy-{namespace}-unverified'
    data['imported_at'] = data['imported_at'] or datetime.now(timezone.utc).isoformat()
    data['raw_value'] = data['raw_value'] if data['raw_value'] is not None else str(value)
    data['raw_unit'] = data['raw_unit'] or '旧版未记录'
    data['source_note'] = data['source_note'] or '旧版来源待复核'
    value_json = row.get('value_json') or json.dumps(value, ensure_ascii=False)
    # Deduplicate matching import provenance across both legacy databases.
    old = conn.execute('''SELECT id FROM metric_sources WHERE company_id=? AND year=? AND metric=?
        AND value_json=? AND import_id=? AND COALESCE(file_path,'')=COALESCE(?,'')
        AND COALESCE(page,-1)=COALESCE(?,-1) AND COALESCE(cell,'')=COALESCE(?,'')''',
        (company_id, year, metric, value_json, data['import_id'], data['file_path'], data['page'], data['cell'])).fetchone()
    if old:
        return old['id']
    columns = ','.join(SOURCE_COLUMNS)
    values = ','.join('?' for _ in SOURCE_COLUMNS)
    cursor = conn.execute(f'''INSERT INTO metric_sources(company_id,year,metric,value_json,{columns},is_current)
        VALUES(?,?,?,?,{values},0)''', (company_id, year, metric, value_json, *data.values()))
    return cursor.lastrowid


def _merge_source(conn, source_path, files, protected):
    source_root = source_path.parent.resolve()
    namespace = source_root.name
    with closing(sqlite3.connect(source_path.resolve().as_uri() + '?mode=ro', uri=True)) as source:
        source.row_factory = sqlite3.Row
        source.execute('PRAGMA query_only=ON')
        source.execute('BEGIN')
        if source.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('旧数据库完整性检查失败：' + str(source_path))
        tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'companies', 'financial_metrics'} <= tables:
            raise ValueError('旧数据库缺少企业或财务指标表：' + str(source_path))
        companies = _source_rows(source, 'companies')
        metrics = _source_rows(source, 'financial_metrics')
        sources = _source_rows(source, 'metric_sources')
        reports = _source_rows(source, 'report_files')
        company_ids = {}
        for company in companies:
            company_ids[company['id']] = db.upsert_company(
                company['name'], company.get('stock_code'), company.get('industry'), company.get('source') or 'upload')
        current_sources = {}
        for row in sources:
            metric = row.get('metric')
            if row.get('company_id') not in company_ids or metric not in db.METRIC_FIELDS:
                raise ValueError('旧数据库存在无法关联的指标来源。')
            cid = company_ids[row['company_id']]
            try:
                value = json.loads(row['value_json'])
            except (TypeError, ValueError):
                raise ValueError('旧数据库指标来源数值无效。')
            if not row.get('file_path') and not row.get('file_name') and 'unverified' in (row.get('import_id') or ''):
                annual = next((item for item in metrics if item['company_id'] == row['company_id']
                               and item['year'] == row['year'] and item.get(metric) == value), None)
                if annual:
                    row.update(db._legacy_report_source(annual, reports, namespace))
            sid = _insert_source(conn, cid, row['year'], metric, value, row, files, source_root, namespace)
            if row.get('is_current', 1):
                current_sources[(row['company_id'], row['year'], metric)] = (sid, value)
        for row in metrics:
            if row['company_id'] not in company_ids:
                raise ValueError('旧数据库存在无法关联的企业指标。')
            cid, year = company_ids[row['company_id']], int(row['year'])
            values = {key: row.get(key) for key in db.METRIC_FIELDS if row.get(key) is not None
                      and (cid, year, key) not in protected}
            if not values:
                continue
            conn.execute('INSERT OR IGNORE INTO financial_metrics(company_id,year) VALUES(?,?)', (cid, year))
            legacy_source = {'source_note': '旧版来源待复核：' + (row.get('raw_source') or '无逐指标来源'),
                             **db._legacy_report_source(row, reports, namespace)}
            for key, value in values.items():
                conn.execute(f'UPDATE financial_metrics SET {key}=? WHERE company_id=? AND year=?', (value, cid, year))
                winner = current_sources.get((row['company_id'], year, key))
                sid = winner[0] if winner and winner[1] == value else _insert_source(
                    conn, cid, year, key, value, legacy_source,
                    files, source_root, namespace)
                conn.execute('UPDATE metric_sources SET is_current=0 WHERE company_id=? AND year=? AND metric=?', (cid, year, key))
                conn.execute('UPDATE metric_sources SET is_current=1 WHERE id=?', (sid,))
            conn.execute('UPDATE financial_metrics SET raw_source=? WHERE company_id=? AND year=?', (row.get('raw_source'), cid, year))
        for row in reports:
            cid = company_ids.get(row.get('company_id'))
            if row.get('company_id') is not None and cid is None:
                raise ValueError('旧数据库存在无法关联的报告文件。')
            path, digest = files.copy(row.get('file_path'), source_root)
            import_id = row.get('import_id')
            if not import_id:
                linked = [item for item in sources if item.get('company_id') == row.get('company_id')
                          and item.get('file_path') == row.get('file_path') and item.get('import_id')]
                import_id = linked[-1]['import_id'] if linked else f'legacy-{namespace}-report-{row["id"]}'
            stable = digest or row.get('content_hash')
            duplicate = conn.execute('''SELECT id FROM report_files WHERE company_id IS ? AND report_year IS ?
                AND ((? IS NOT NULL AND content_hash=?) OR import_id=?) LIMIT 1''',
                (cid, row.get('report_year'), stable, stable, import_id)).fetchone()
            linked_sources = db._report_sources(conn, {**row, 'company_id': cid, 'file_path': path, 'import_id': import_id})
            valid_count = db._source_metric_count(conn, linked_sources)
            metric_count = row.get('metric_count') or valid_count
            source_url = row.get('source_url') or next((item.get('source_url') for item in linked_sources if item.get('source_url')), None)
            status = 'imported' if valid_count else (
                row.get('ingest_status') if row.get('ingest_status') in {'document_only', 'pending_metrics', 'failed'} else 'pending_metrics')
            legacy_type = 'builtin' if row.get('file_type') == 'builtin' else ('direct_url' if source_url else 'upload')
            metadata = dict(source_type=row.get('source_type') or legacy_type,
                            source_url=source_url, report_title=row.get('report_title') or row['file_name'],
                            report_type=row.get('report_type'), import_id=import_id, content_hash=stable,
                            ingest_status=status, metric_count=metric_count)
            if duplicate:
                conn.execute('''UPDATE report_files SET source_url=COALESCE(?,source_url),
                    report_title=COALESCE(?,report_title),metric_count=MAX(COALESCE(metric_count,0),?),
                    ingest_status=CASE WHEN ?>0 THEN 'imported' ELSE ingest_status END WHERE id=?''',
                    (source_url, metadata['report_title'], metric_count, valid_count, duplicate['id']))
                continue
            report_id = db.insert_report_file(cid, row['file_name'], row.get('report_year'), row.get('file_type'),
                path, row.get('parse_status'), row.get('note') or '', **metadata)
            if row.get('uploaded_at'):
                conn.execute('UPDATE report_files SET uploaded_at=? WHERE id=?', (row['uploaded_at'], report_id))
        # Preserve original document assets even if an older parser omitted a DB link.
        for path in source_root.rglob('*'):
            if path.is_file() and not path.is_symlink() and path.suffix.lower() in {'.pdf', '.csv', '.xls', '.xlsx'}:
                files.copy(str(path), source_root)


def migrate_legacy_workspaces() -> dict:
    """Merge sibling legacy libraries into the active main DB, atomically once."""
    if get_workspace() != 'main':
        return {'status': 'skipped', 'reason': 'internal_evaluation'}
    with closing(db.get_conn()) as conn:
        marker = conn.execute('SELECT value FROM app_meta WHERE key=?', (MIGRATION_KEY,)).fetchone()
        if marker and marker['value'] == 'done':
            return {'status': 'already_done'}
    target = get_data_root().resolve()
    sources = [target.parent / kind / 'investor_agent.db' for kind in ('demo', 'personal')]
    sources = [path for path in sources if path.is_file() and path.resolve() != db.get_db_path().resolve()]
    files = _Attachments(target)
    try:
        with internal_writes(), db.atomic_write():
            conn = db.get_conn()
            marker = conn.execute('SELECT value FROM app_meta WHERE key=?', (MIGRATION_KEY,)).fetchone()
            if marker and marker['value'] == 'done':
                return {'status': 'already_done'}
            # A pre-existing main database is authoritative. This set is fixed
            # before either legacy source is merged, allowing personal to replace demo.
            protected = {(row['company_id'], row['year'], key)
                         for row in conn.execute('SELECT * FROM financial_metrics')
                         for key in db.METRIC_FIELDS if row[key] is not None}
            for source in sources:
                _merge_source(conn, source, files, protected)
            if sources:
                for annual in conn.execute('SELECT company_id,year FROM financial_metrics').fetchall():
                    current = conn.execute('''SELECT metric,file_name,page,cell,source_note FROM metric_sources
                        WHERE company_id=? AND year=? AND is_current=1 ORDER BY metric''',
                        (annual['company_id'], annual['year'])).fetchall()
                    summary = '；'.join(f"{row['metric']}：{row['file_name'] or row['source_note'] or '来源未记录'}"
                                       + (f" 第{row['page']}页" if row['page'] else '')
                                       + (f" {row['cell']}" if row['cell'] else '') for row in current)
                    conn.execute('UPDATE financial_metrics SET raw_source=? WHERE company_id=? AND year=?',
                                 (summary, annual['company_id'], annual['year']))
            if conn.execute('PRAGMA foreign_key_check').fetchall():
                raise ValueError('迁移后企业、来源或报告关联检查失败。')
            details = {'sources': [str(path) for path in sources], 'copied_files': len(files.created),
                       'missing_files': sorted(set(files.missing)), 'completed_at': datetime.now(timezone.utc).isoformat()}
            conn.execute('INSERT OR REPLACE INTO app_meta(key,value) VALUES(?,?)', (MIGRATION_KEY, 'done'))
            conn.execute('INSERT OR REPLACE INTO app_meta(key,value) VALUES(?,?)',
                         (MIGRATION_KEY + '_details', json.dumps(details, ensure_ascii=False)))
        return {'status': 'done', **details}
    except Exception:
        files.rollback()
        raise
