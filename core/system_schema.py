"""Read the current library's schema without fetching business rows or creating it."""
from __future__ import annotations

import re
import sqlite3
from typing import Any

from core.db import get_db_path


TABLE_PURPOSES = {
    'companies': '企业主体、股票代码与行业',
    'financial_metrics': '企业年度财务指标',
    'report_files': '导入文件与报告版本元信息',
    'metric_sources': '逐指标来源、位置与导入版本',
    'app_meta': '应用迁移与资料库元信息',
    'scope_probe_facts': '口径探针的独立事实与证据',
}
_IDENTIFIER = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,127}\Z')
_PRAGMAS = frozenset(('table_info', 'foreign_key_list', 'index_list', 'index_info', 'user_version'))


def _authorizer(action, first, second, database, trigger):
    if action == sqlite3.SQLITE_SELECT:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ and first == 'sqlite_master':
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and first in _PRAGMAS:
        # PRAGMA arguments are only server-owned table/index identifiers.
        if first == 'user_version' and second is not None:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_TRANSACTION and first in ('BEGIN', 'ROLLBACK'):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _identifier(value):
    return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else None


def _predicate(sql):
    """Only the partial-index condition; no full DDL or string default values."""
    match = re.search(r'\bWHERE\b(.*)\Z', sql or '', flags=re.I | re.S)
    if not match:
        return None
    condition = match.group(1).strip().rstrip(';').strip()
    # Project predicates use schema names and numeric flags. Redact arbitrary
    # string literals in customized databases; metadata downloads never include
    # secrets embedded into SQL strings or comments.
    condition = re.sub(r"'(?:''|[^'])*'", "'<文字常量已隐藏>'", condition)
    condition = re.sub(r'/\*.*?\*/|--[^\n]*', '', condition, flags=re.S)
    return condition[:500]


def read_system_schema() -> dict[str, Any]:
    """Read only the already-bound session DB. No path is included in results."""
    result = {'format': 'qingmeng-schema/1', 'status': 'not_initialized',
              'scope': '当前会话资料库结构', 'user_version': None,
              'tables': [], 'relations': [], 'read_only': True,
              'omitted': '业务记录、文件路径、连接配置、字段默认值和完整建表 SQL 均不导出。'}
    path = get_db_path()
    if not path.is_file():
        result['message'] = '当前会话资料库尚未初始化。此页面不会创建数据库。'
        return result
    connection = None
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        connection.set_authorizer(_authorizer)
        connection.execute('BEGIN')
        result['user_version'] = int(connection.execute('PRAGMA user_version').fetchone()[0])
        for table, purpose in TABLE_PURPOSES.items():
            present = connection.execute('SELECT name FROM sqlite_master WHERE type=? AND name=?', ('table', table)).fetchone()
            if present is None:
                continue
            fields = []
            for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall():
                name = _identifier(row['name'])
                if name is None:
                    continue
                fields.append({'name': name, 'type': str(row['type'])[:64],
                               'not_null': bool(row['notnull']), 'primary_key_order': int(row['pk'])})
            foreign_keys = []
            for row in connection.execute(f'PRAGMA foreign_key_list("{table}")').fetchall():
                if row['table'] not in TABLE_PURPOSES or not _identifier(row['from']) or not _identifier(row['to']):
                    continue
                key = {'from': row['from'], 'to_table': row['table'], 'to_field': row['to'],
                       'on_update': row['on_update'], 'on_delete': row['on_delete']}
                foreign_keys.append(key)
                result['relations'].append({'from_table': table, **key})
            indexes = []
            for row in connection.execute(f'PRAGMA index_list("{table}")').fetchall()[:64]:
                name = _identifier(row['name'])
                if name is None:
                    continue
                columns = [item['name'] if _identifier(item['name']) else '[表达式]'
                           for item in connection.execute(f'PRAGMA index_info("{name}")').fetchall()]
                definition = connection.execute('SELECT sql FROM sqlite_master WHERE type=? AND name=? AND tbl_name=?',
                                                ('index', name, table)).fetchone()
                indexes.append({'name': name, 'columns': columns, 'unique': bool(row['unique']),
                                'origin': row['origin'], 'partial': bool(row['partial']),
                                'predicate': _predicate(definition['sql']) if row['partial'] and definition else None})
            result['tables'].append({'name': table, 'purpose': purpose, 'fields': fields,
                                     'foreign_keys': foreign_keys, 'indexes': indexes})
        connection.execute('ROLLBACK')
        result['status'] = 'ready' if result['tables'] else 'not_initialized'
        result['message'] = ('结构来自当前会话资料库的只读实时查询。' if result['tables'] else
                             '资料库中尚未发现本系统的数据表。此页面不会建表或迁移。')
    except (sqlite3.Error, OSError, ValueError):
        result.update(status='unavailable', tables=[], relations=[], user_version=None,
                      message='暂时无法读取当前资料库结构。未执行建表、迁移或写入。')
    finally:
        if connection is not None:
            connection.close()
    return result
