"""Durable, visitor-scoped checkpoints for the finite local screening workflow.

Recovery tokens grant a copy of exactly one frozen job, never a workspace login.
They are random capabilities stored only as hashes and expire after 24 hours.
No cloud model settings or credentials are accepted by this module.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import time
import uuid

import pandas as pd

from core.storage import get_data_root, is_session_workspace, require_write_access, resolve_stored_file
from core.task_decomposition import run_research_task

SCHEMA = 1
RECOVERY_TTL = 24 * 3600
MAX_JOBS = 20
MAX_FILES_BYTES = 100 * 1024 * 1024
MAX_WORKSPACE_BYTES = 200 * 1024 * 1024
MAX_STATE_BYTES = 20 * 1024 * 1024
_ID = re.compile(r'[0-9a-f]{32}')
_TOKEN = re.compile(r'[A-Za-z0-9_-]{43}')
_SECRET = re.compile(r'api.?key|password|secret|authorization|access.?token', re.I)
_PATH_KEYS = {'file_path', 'source_path', 'stored_path'}
_REPORT_KEYS = {'id', 'company_name', 'company', 'company_id', 'report_year', 'file_name',
                'file_path', 'file_type', 'report_type', 'report_title', 'report_period',
                'parse_status', 'note', 'content_sha256', 'content_hash', 'import_id', 'scope_status',
                'source_type', 'source_url', 'ingest_status', 'metric_count', 'uploaded_at'}


class JobBusy(RuntimeError):
    pass


def _clean(value):
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items() if not _SECRET.search(str(k))}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, 'item'):
        return _clean(value.item())
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    raise ValueError('研究快照含有不支持的数据类型。')


def _encoded(value):
    return json.dumps(_clean(value), ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _atomic(path, value):
    payload = _encoded(value)
    if len(payload) > MAX_STATE_BYTES:
        raise ValueError('研究检查点超过20 MB，请缩小筛选范围。')
    if path.name == 'job.json' and path.parent.parent == _root():
        previous = path.stat().st_size if path.is_file() else 0
        if _workspace_bytes() - previous + len(payload) > MAX_WORKSPACE_BYTES:
            raise ValueError('研究任务冻结资料已达到200 MB上限，上一个有效检查点仍保留。')
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _lock(path):
    """OS-owned lock released after process death; a timer cannot steal a job."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        # Do not read another runner's locked byte on Windows.
        if path.stat().st_size == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise JobBusy('该任务正在运行，请等待当前步骤完成。') from error
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _root():
    root = get_data_root().resolve() / 'research_jobs'
    if root.is_symlink():
        raise ValueError('研究任务目录无效。')
    return root


def _folder(identity):
    if not isinstance(identity, str) or not _ID.fullmatch(identity):
        raise ValueError('无效的研究任务。')
    folder = _root() / identity
    if folder.is_symlink() or folder.resolve().parent != _root():
        raise ValueError('研究任务目录无效。')
    return folder


def _load(identity):
    path = _folder(identity) / 'job.json'
    if not path.is_file() or path.stat().st_size > MAX_STATE_BYTES:
        raise ValueError('当前会话没有此研究任务。')
    job = json.loads(path.read_text(encoding='utf-8'))
    if job.get('schema') != SCHEMA or job.get('job_id') != identity:
        raise ValueError('研究任务版本或标识无效。')
    return job


def _summary(job, *, result=False):
    checkpoint = job.get('checkpoint') or {}
    output = {key: job.get(key) for key in ('job_id', 'question', 'status', 'created_at', 'updated_at',
              'source_fingerprint', 'error', 'restored', 'delivered')}
    output.update(completed=len(checkpoint.get('completed') or {}), total=checkpoint.get('total', 0),
                  screened=checkpoint.get('screened', 0))
    if result:
        output['result'] = deepcopy(job.get('result'))
    return output


def list_research_jobs():
    if not _root().exists():
        return []
    jobs = []
    for folder in _root().iterdir():
        if not _ID.fullmatch(folder.name) or folder.is_symlink():
            continue
        try:
            jobs.append(_summary(_load(folder.name)))
        except (ValueError, OSError, json.JSONDecodeError):
            continue
    return sorted(jobs, key=lambda job: job['updated_at'], reverse=True)


def _walk_paths(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _PATH_KEYS and isinstance(item, str) and item:
                yield item
            else:
                yield from _walk_paths(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_paths(item)


def _replace_paths(value, replacements):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in _PATH_KEYS and isinstance(item, str):
                result[key] = replacements.get(item, '')
                if item and not result[key]:
                    result['source_availability'] = '冻结时原件不可访问，来源待复核'
            else:
                result[key] = _replace_paths(item, replacements)
        return result
    if isinstance(value, list):
        return [_replace_paths(item, replacements) for item in value]
    return value


def _inputs(data_map, reports):
    if len(data_map) > 300 or sum(len(frame) for frame in data_map.values()) > 10000:
        raise ValueError('研究任务最多冻结300家企业、10000行年度数据。')
    if len(reports or []) > 100:
        raise ValueError('研究任务最多冻结100条来源记录。')
    data = _clean({name: frame.to_dict(orient='records') for name, frame in data_map.items()})
    sources = _clean([{key: value for key, value in dict(row).items() if key in _REPORT_KEYS}
                      for row in (reports or [])])
    paths = {}
    total = 0
    for raw in sorted(set(_walk_paths([data, sources]))):
        path = resolve_stored_file(raw)
        if path is None:
            paths[raw] = None
            continue
        size = path.stat().st_size
        total += size
        if total > MAX_FILES_BYTES:
            raise ValueError('此任务原件超过100 MB，请缩小资料范围。')
        digest = sha256(path.read_bytes()).hexdigest()
        paths[raw] = {'path': path, 'sha256': digest, 'size': size}
    aliases = {raw: item['sha256'] if item else '' for raw, item in paths.items()}
    fingerprint = sha256(_encoded(_replace_paths([data, sources], aliases))).hexdigest()
    return data, sources, paths, fingerprint


def research_input_fingerprint(data_map, reports=None):
    return _inputs(data_map, reports)[3]


def _capacity(extra):
    if len(list_research_jobs()) >= MAX_JOBS:
        raise ValueError('当前资料库已保留20个研究任务，请先导出并整理任务后再创建。')
    size = _workspace_bytes()
    if size + extra > MAX_WORKSPACE_BYTES:
        raise ValueError('研究任务冻结资料已达到200 MB上限，本次未新建任务。')


def _workspace_bytes():
    return sum(path.stat().st_size for path in _root().rglob('*') if path.is_file()) if _root().exists() else 0


def _discard_derived_index(folder):
    # This cache can be rebuilt from frozen originals and is never a checkpoint.
    # Discard it after each company, so repeated resumes cannot grow the cache.
    index = folder / 'index'
    if index.exists():
        if index.is_symlink() or index.resolve().parent != folder.resolve():
            raise ValueError('研究任务索引目录无效。')
        shutil.rmtree(index)


def create_research_job(question, data_map, *, reports=None):
    """Freeze inputs once. Repeated clicks with identical inputs reuse the job."""
    require_write_access()
    question = str(question).strip()
    if not question or len(question) > 4000:
        raise ValueError('研究问题应为1至4000个字符。')
    data, sources, paths, fingerprint = _inputs(data_map, reports)
    with _lock(_root() / 'create.lock'):
        for summary in list_research_jobs():
            if summary['question'] == question and summary['source_fingerprint'] == fingerprint:
                return _summary(_load(summary['job_id']), result=True)
        _capacity(sum(item['size'] for item in paths.values() if item))
        identity = uuid.uuid4().hex
        folder = _folder(identity)
        (folder / 'sources').mkdir(parents=True)
        replacements = {}
        try:
            for raw, item in paths.items():
                if item is None:
                    continue
                target = folder / 'sources' / (item['sha256'] + item['path'].suffix.lower())
                if not target.exists():
                    shutil.copyfile(item['path'], target)
                if sha256(target.read_bytes()).hexdigest() != item['sha256']:
                    raise ValueError('冻结期间原件发生变化，请重新开始。')
                replacements[raw] = str(target)
            now = time.time()
            job = {'schema': SCHEMA, 'job_id': identity, 'question': question, 'status': 'ready',
                   'created_at': now, 'updated_at': now, 'source_fingerprint': fingerprint,
                   'company_order': list(data),
                   'data': _replace_paths(data, replacements), 'reports': _replace_paths(sources, replacements),
                   'checkpoint': {'completed': {}}, 'result': None, 'error': '', 'delivered': False}
            _atomic(folder / 'job.json', job)
        except BaseException:
            # Only this newly allocated, validated job directory is discarded.
            if folder.resolve().parent == _root():
                shutil.rmtree(folder)
            raise
    return _summary(job, result=True)


def resume_research_job(identity, *, progress=None, check_interrupt=None):
    require_write_access()
    folder = _folder(identity)
    job = _load(identity)
    try:
        with _lock(folder / 'run.lock'):
            job = _load(identity)
            if job.get('result') is not None:
                return _summary(job, result=True)
            _discard_derived_index(folder)
            job.update(status='running', error='', updated_at=time.time())
            _atomic(folder / 'job.json', job)
            def save(checkpoint):
                _discard_derived_index(folder)
                candidate = {**job, 'checkpoint': _clean(checkpoint), 'updated_at': time.time()}
                _atomic(folder / 'job.json', candidate)
                job.clear()
                job.update(candidate)
            try:
                result = run_research_task(job['question'], {name: pd.DataFrame(job['data'][name]) for name in job.get('company_order', job['data'])},
                    reports=job['reports'], index_root=folder / 'index', progress=progress,
                    checkpoint=deepcopy(job['checkpoint']), save_checkpoint=save, check_interrupt=check_interrupt)
                result.update(research_job_id=identity, research_task=True, effective_question=job['question'],
                              report_files_snapshot=deepcopy(job['reports']), source_fingerprint=job['source_fingerprint'])
                _discard_derived_index(folder)
                candidate = {**job, 'status': 'completed', 'result': _clean(result), 'error': '', 'updated_at': time.time()}
                _atomic(folder / 'job.json', candidate)
                job = candidate
            except BaseException as error:
                _discard_derived_index(folder)
                # A full disk or oversized new retrieval must retain the last
                # successfully persisted checkpoint, never just its in-memory copy.
                job = _load(identity)
                job.update(status='paused', error=type(error).__name__)
                job['updated_at'] = time.time()
                try:
                    _atomic(folder / 'job.json', job)
                except (OSError, ValueError):
                    pass  # Existing atomic checkpoint is still resumable.
                if not isinstance(error, Exception):
                    raise
            return _summary(job, result=True)
    except JobBusy:
        result = _summary(_load(identity), result=True)
        result.update(status='busy')
        return result


def mark_research_job_delivered(identity):
    """Bookkeeping only: the caller deduplicates its own conversation by job ID."""
    require_write_access()
    with _lock(_folder(identity) / 'run.lock'):
        job = _load(identity)
        if job.get('result') is None:
            return False
        job.update(delivered=True, updated_at=time.time())
        _atomic(_folder(identity) / 'job.json', job)
        return True


def _recovery_root():
    active = get_data_root().resolve()
    base = active.parent.parent if is_session_workspace() else active.parent
    root = base / '_research_recovery'
    if root.is_symlink():
        raise ValueError('恢复索引无效。')
    return root


def issue_recovery_code(identity):
    require_write_access()
    _load(identity)
    root = _recovery_root()
    root.mkdir(parents=True, exist_ok=True)
    # One active mapping per job prevents repeated UI clicks filling the registry.
    with _lock(_folder(identity) / 'run.lock'):
        job = _load(identity)
        previous = job.get('recovery_hash')
        if previous and re.fullmatch(r'[0-9a-f]{64}', previous):
            (root / (previous + '.json')).unlink(missing_ok=True)
        code = secrets.token_urlsafe(32)
        digest = sha256(code.encode('ascii')).hexdigest()
        expires = time.time() + RECOVERY_TTL
        _atomic(root / (digest + '.json'), {'schema': SCHEMA, 'job_id': identity,
                'source': str(_folder(identity)), 'expires_at': expires})
        job['recovery_hash'] = digest
        _atomic(_folder(identity) / 'job.json', job)
    return {'code': code, 'expires_at': expires}


def restore_research_job(code):
    """Authorize one frozen copy by capability; never select another visitor root."""
    require_write_access()
    if not isinstance(code, str) or not _TOKEN.fullmatch(code.strip()):
        raise ValueError('恢复码无效或已过期。')
    digest = sha256(code.strip().encode('ascii')).hexdigest()
    registry = _recovery_root()
    try:
        path = registry / (digest + '.json')
        if path.is_symlink() or path.stat().st_size > 4096:
            raise ValueError()
        record = json.loads(path.read_text(encoding='utf-8'))
        if record.get('schema') != SCHEMA or record['expires_at'] < time.time() or not _ID.fullmatch(record['job_id']):
            raise ValueError()
        source = Path(record['source'])
        base = registry.parent.resolve()
        if source.is_symlink() or not source.resolve().is_relative_to(base) or source.name != record['job_id'] or source.parent.name != 'research_jobs':
            raise ValueError()
        if any(part.is_symlink() for part in [source, source.parent, source.parent.parent]):
            raise ValueError()
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError('恢复码无效或已过期。') from error
    identity = record['job_id']
    with _lock(_root() / 'create.lock'):
        destination = _folder(identity)
        if (destination / 'job.json').is_file():
            return _summary(_load(identity), result=True)
        try:
            with _lock(source / 'run.lock'):
                if (source / 'job.json').stat().st_size > MAX_STATE_BYTES:
                    raise ValueError('研究检查点过大。')
                job = json.loads((source / 'job.json').read_text(encoding='utf-8'))
                if job.get('job_id') != identity or job.get('recovery_hash') != digest:
                    raise ValueError('恢复码无效或已过期。')
                files = [file for file in (source / 'sources').iterdir() if file.is_file()]
                if any(file.is_symlink() or not file.resolve().is_relative_to(source.resolve()) for file in files):
                    raise ValueError('冻结原件无效。')
                _capacity(sum(file.stat().st_size for file in files))
                (destination / 'sources').mkdir(parents=True)
                replacements = {}
                for file in files:
                    target = destination / 'sources' / file.name
                    shutil.copyfile(file, target)
                    replacements[str(file)] = str(target)
                job = _replace_paths(job, replacements)
                job.update(restored=True, delivered=False, updated_at=time.time())
                job.pop('recovery_hash', None)
                if job['status'] == 'running':
                    job['status'] = 'paused'
                _atomic(destination / 'job.json', job)
        except JobBusy as error:
            raise ValueError('原任务正在保存检查点，请稍后再恢复。') from error
        except BaseException:
            if destination.exists() and destination.resolve().parent == _root():
                shutil.rmtree(destination)
            raise
    return _summary(job, result=True)
