"""Local workspace review requests; submitting never means review passed."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from core.storage import get_data_root, require_write_access


def _folder() -> Path:
    root = get_data_root().resolve()
    folder = root / 'review_requests'
    if not folder.resolve().is_relative_to(root) or folder.is_symlink():
        raise ValueError('复核目录不能越出当前资料库。')
    return folder


def save_review_request(result: dict, note: str = '') -> dict:
    """Save one immutable pending request via fsync + atomic replace."""
    require_write_access()
    if not isinstance(result, dict) or result.get('schema_version') != 'countercheck/1':
        raise ValueError('只保存由当前反证接口返回的核验快照。')
    if len(str(note)) > 4000:
        raise ValueError('复核说明最多4000字。')
    folder = _folder()
    folder.mkdir(parents=True, exist_ok=True)
    if len(list(folder.glob('*.json'))) >= 500:
        raise ValueError('当前资料库已保存500条复核请求，请先整理。')
    identifier = uuid4().hex
    path = folder / f'{identifier}.json'
    temporary = folder / f'.{identifier}.tmp'
    record = {'schema_version': 'review-request/1', 'id': identifier, 'status': 'pending_review',
              'created_at': datetime.now(timezone.utc).isoformat(), 'note': str(note),
              'snapshot': deepcopy(result), 'workspace_root': str(get_data_root().resolve())}
    payload = json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False, default=str).encode('utf-8')
    if len(payload) > 2 * 1024 * 1024:
        raise ValueError('复核快照超过2 MB，请缩小材料范围。')
    try:
        with temporary.open('xb') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return {**record, 'path': str(path)}


def list_review_requests(limit: int = 50) -> list[dict]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError('复核读取数量必须在1到500之间。')
    folder = _folder()
    if not folder.exists():
        return []
    records = []
    for path in folder.glob('*.json'):
        if not re.fullmatch(r'[0-9a-f]{32}\.json', path.name) or path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
            continue
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
            if record.get('schema_version') != 'review-request/1' or record.get('status') != 'pending_review':
                continue
            records.append({**record, 'path': str(path)})
        except (OSError, ValueError, TypeError):
            continue
    return sorted(records, key=lambda item: item.get('created_at', ''), reverse=True)[:limit]
