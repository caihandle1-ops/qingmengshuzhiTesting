"""Request-local workspace selection and explicit write permissions."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import os
import re

ROOT = Path(__file__).resolve().parents[1]
_workspace = ContextVar('financial_workspace', default=None)
_data_root = ContextVar('financial_data_root', default=None)
_internal_write = ContextVar('financial_internal_write', default=False)
_session_root = ContextVar('financial_session_root', default=None)
WORKSPACES = {'main': '统一资料库', 'evaluation': '内部评测数据库'}


def is_public_deployment() -> bool:
    # Public writes require a separately bound session, never the shared library.
    return os.getenv('FINANCIAL_DEPLOYMENT', 'public').lower() != 'local'


def public_session_enabled() -> bool:
    return os.getenv('FINANCIAL_DEPLOYMENT', 'public').lower() == 'public'


def get_workspace() -> str:
    # Evaluation is available only through an explicit internal context. Old
    # environment defaults cannot select a different user-facing database.
    return _workspace.get() or 'main'


def configure_workspace(value: str) -> str:
    if value not in WORKSPACES:
        raise ValueError('未知资料库。')
    if value != 'main':
        raise ValueError('正常业务只能使用统一资料库。')
    _workspace.set(value)
    _session_root.set(None)
    return value


def configure_public_session(session_id: str) -> Path:
    """Bind a server-generated session ID on every UI run; never accept a path."""
    if not public_session_enabled() or not re.fullmatch(r'[0-9a-f]{32}', session_id):
        raise ValueError('无效的在线会话标识或部署模式。')
    base = Path(os.getenv('FINANCIAL_DATA_DIR', str(ROOT / 'data' / 'workspaces'))).expanduser().resolve()
    root = base / 'sessions' / session_id
    if root.resolve() != root or root.is_symlink():
        raise ValueError('在线会话目录无效。')
    _workspace.set('main')
    _session_root.set(root)
    return root


def is_session_workspace() -> bool:
    return _session_root.get() is not None and _data_root.get() is None


def get_data_root() -> Path:
    if _data_root.get() is not None:
        return Path(_data_root.get())
    if _session_root.get() is not None:
        return Path(_session_root.get())
    base = Path(os.getenv('FINANCIAL_DATA_DIR', str(ROOT / 'data' / 'workspaces'))).expanduser().resolve()
    return base / get_workspace()


def can_write() -> bool:
    return (bool(_internal_write.get()) or is_session_workspace()
            or (not is_public_deployment() and get_workspace() == 'main'))


def check_session_upload_capacity(content_size: int) -> None:
    """Bound retained public-session files without silently deleting data."""
    if not is_session_workspace():
        return
    folder = get_data_root() / 'uploads'
    files = [p for p in folder.rglob('*') if p.is_file()] if folder.exists() else []
    if len(files) >= 20 or sum(p.stat().st_size for p in files) + content_size > 100 * 1024 * 1024:
        raise ValueError('当前在线会话最多保存20个文件、合计100 MB，请删除不再需要的已导入资料后重试。')


def require_write_access() -> None:
    if not can_write():
        raise PermissionError('当前部署为只读模式，无法执行数据导入或删除操作。')


@contextmanager
def internal_writes():
    """Trusted bootstrap/migration only; never exposed as a UI option."""
    token = _internal_write.set(True)
    try:
        yield
    finally:
        _internal_write.reset(token)


@contextmanager
def workspace_context(kind: str, root: str | Path | None = None, *, allow_writes: bool = False):
    """Isolate evaluation/backup work without mutating process-wide DB globals."""
    if kind not in WORKSPACES:
        raise ValueError('未知资料库。')
    if kind == 'evaluation':
        target = Path(root).resolve() if root is not None else (
            Path(os.getenv('FINANCIAL_DATA_DIR', str(ROOT / 'data' / 'workspaces'))).expanduser().resolve() / 'evaluation')
        active = get_data_root().resolve()
        main_root = Path(os.getenv('FINANCIAL_DATA_DIR', str(ROOT / 'data' / 'workspaces'))).expanduser().resolve() / 'main'
        if target == main_root or (get_workspace() == 'main' and target == active):
            raise ValueError('评测数据库不能与统一资料库共用目录。')
    ws = _workspace.set(kind)
    session = _session_root.set(None)
    data = _data_root.set(Path(root).resolve() if root is not None else None)
    write = _internal_write.set(allow_writes)
    try:
        yield get_data_root()
    finally:
        _internal_write.reset(write)
        _data_root.reset(data)
        _workspace.reset(ws)
        _session_root.reset(session)


def resolve_stored_file(value: str) -> Path | None:
    """Only open files belonging to the current library or packaged samples."""
    raw = Path(value)
    candidates = [raw] if raw.is_absolute() else [get_data_root() / raw, ROOT / raw]
    current = get_data_root().resolve()
    from core.builtin_catalog import packaged_sample_paths
    packaged = packaged_sample_paths()
    for candidate in candidates:
        path = candidate.resolve()
        if path.is_file() and (path.is_relative_to(current) or path in packaged):
            return path
    return None
