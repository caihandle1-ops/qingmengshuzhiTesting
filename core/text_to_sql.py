from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any
from time import perf_counter

from core.db import get_db_path


ALLOWED_TABLES = {'companies', 'financial_metrics', 'report_files'}
ALLOWED_METRICS = {
    'revenue', 'net_profit', 'operating_cashflow', 'roe',
    'debt_ratio', 'gross_margin', 'eps',
}
FORBIDDEN_KEYWORDS = {
    'INSERT', 'UPDATE', 'DELETE', 'DROP', 'ALTER', 'CREATE', 'REPLACE',
    'ATTACH', 'DETACH', 'PRAGMA', 'VACUUM', 'REINDEX', 'ANALYZE', 'TRIGGER',
}


@dataclass
class SQLPlan:
    sql: str
    params: list[Any]
    source: str = 'local'

    def as_dict(self) -> dict[str, Any]:
        return {'sql': self.sql, 'params': list(self.params), 'source': self.source}


def build_schema_context() -> str:
    return (
        'companies(id, name, stock_code, industry, source); '
        'financial_metrics(company_id, year, revenue, net_profit, operating_cashflow, '
        'roe, debt_ratio, gross_margin, eps, audit_opinion, raw_source); '
        'financial_metrics.company_id = companies.id'
    )


def generate_sql(resolved_context: dict[str, Any], source: str = 'local') -> SQLPlan:
    if resolved_context.get('period') not in {None, '', 'annual'}:
        raise ValueError('当前结构化数据仅提供年度指标，不能替代指定的半年或季度报告期。')
    companies = [name for name in resolved_context.get('companies') or [] if name]
    years = [int(year) for year in resolved_context.get('years') or []]
    intent = resolved_context.get('intent') or 'finance_query'
    metrics = [metric for metric in resolved_context.get('metrics') or [] if metric in ALLOWED_METRICS]
    if intent == 'company_compare' and not metrics:
        metrics = ['revenue', 'net_profit', 'operating_cashflow', 'roe', 'debt_ratio']
    if not metrics:
        metrics = ['revenue']
    if not companies:
        raise ValueError('Text-to-SQL 缺少企业条件。')

    columns = ['c.name AS company', 'fm.year'] + [f'fm.{metric}' for metric in metrics] + ['fm.raw_source']
    where = [f"c.name IN ({', '.join('?' for _ in companies)})"]
    params: list[Any] = list(companies)
    if years:
        where.append(f"fm.year IN ({', '.join('?' for _ in years)})")
        params.extend(years)
    order_by = 'ORDER BY fm.year DESC' if intent == 'trend_analysis' and len(companies) == 1 and not years else 'ORDER BY c.name, fm.year'
    sql = (
        'SELECT\n    ' + ',\n    '.join(columns) + '\n'
        'FROM financial_metrics fm\n'
        'JOIN companies c ON c.id = fm.company_id\n'
        'WHERE ' + '\n  AND '.join(where) + '\n' +
        order_by
    )
    if intent == 'trend_analysis' and len(companies) == 1 and not years:
        sql += '\nLIMIT 3'
    sql += ';'
    return SQLPlan(sql=sql, params=params, source=source)


def _strip_string_literals(sql: str) -> str:
    return re.sub(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"", "''", sql)


def _placeholder_count(sql: str) -> int:
    return _strip_string_literals(sql).count('?')


def validate_sql(sql: str, params: list[Any] | tuple[Any, ...] | None = None) -> dict[str, Any]:
    text = (sql or '').strip()
    errors: list[str] = []
    if not text:
        errors.append('SQL 为空。')
        return {'valid': False, 'errors': errors, 'tables': []}
    if '--' in text or '/*' in text or '*/' in text:
        errors.append('不允许 SQL 注释。')

    without_literals = _strip_string_literals(text)
    statements = [part.strip() for part in without_literals.split(';') if part.strip()]
    if len(statements) != 1:
        errors.append('只允许单条 SQL 语句。')
    normalized = re.sub(r'\s+', ' ', without_literals).strip().upper()
    if not (normalized.startswith('SELECT ') or normalized.startswith('WITH ')):
        errors.append('只允许 SELECT 或 WITH ... SELECT 查询。')
    if normalized.startswith('WITH ') and ' SELECT ' not in f' {normalized} ':
        errors.append('WITH 查询必须以 SELECT 返回结果。')
    tokens = set(re.findall(r'\b[A-Z_]+\b', normalized))
    blocked = sorted(tokens & FORBIDDEN_KEYWORDS)
    if blocked:
        errors.append('包含禁止关键字：' + '、'.join(blocked))

    tables = re.findall(r'\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)', without_literals, flags=re.I)
    cte_names = {
        name.lower() for name in re.findall(r'(?:\bWITH|,)\s*([A-Za-z_][A-Za-z0-9_]*)\s+AS\s*\(', without_literals, flags=re.I)
    }
    normalized_tables = [table.lower() for table in tables]
    if not normalized_tables:
        errors.append('未识别到查询表。')
    for table in normalized_tables:
        if table.startswith('sqlite_') or (table not in ALLOWED_TABLES and table not in cte_names):
            errors.append(f'不允许访问数据表：{table}')
    supplied = list(params or [])
    expected_params = _placeholder_count(text)
    if expected_params != len(supplied):
        errors.append(f'参数数量不匹配：SQL 需要 {expected_params} 个，实际 {len(supplied)} 个。')
    return {'valid': not errors, 'errors': errors, 'tables': normalized_tables}


def _readonly_authorizer(action, _arg1, _arg2, _database, _trigger):
    allowed = {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
    }
    if hasattr(sqlite3, 'SQLITE_RECURSIVE'):
        allowed.add(sqlite3.SQLITE_RECURSIVE)
    return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY


def get_readonly_connection() -> sqlite3.Connection:
    uri = f'{get_db_path().resolve().as_uri()}?mode=ro'
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    conn.set_authorizer(_readonly_authorizer)
    return conn


def execute_readonly_sql(sql: str, params: list[Any] | None = None) -> dict[str, Any]:
    validation = validate_sql(sql, params)
    if not validation['valid']:
        raise ValueError('；'.join(validation['errors']))
    conn = get_readonly_connection()
    try:
        cursor = conn.execute(sql, list(params or []))
        rows = [dict(row) for row in cursor.fetchall()]
        return {'columns': [item[0] for item in cursor.description or []], 'rows': rows, 'row_count': len(rows)}
    finally:
        conn.close()


def validate_query_result(result: dict[str, Any], resolved_context: dict[str, Any]) -> dict[str, Any]:
    rows = result.get('rows') or []
    columns = set(result.get('columns') or [])
    errors: list[str] = []
    expected_companies = set(resolved_context.get('companies') or [])
    expected_years = {int(year) for year in resolved_context.get('years') or []}
    expected_metrics = {
        metric for metric in resolved_context.get('metrics') or [] if metric in ALLOWED_METRICS
    }
    if resolved_context.get('intent') == 'company_compare' and not expected_metrics:
        expected_metrics = {'revenue', 'net_profit', 'operating_cashflow', 'roe', 'debt_ratio'}
    if not rows:
        errors.append('查询结果为空。')
    if not {'company', 'year'}.issubset(columns):
        errors.append('结果缺少企业或年份列。')
    missing_metrics = sorted(expected_metrics - columns)
    if missing_metrics:
        errors.append('结果缺少指标列：' + '、'.join(missing_metrics))
    actual_companies = {row.get('company') for row in rows}
    if rows and expected_companies and expected_companies != actual_companies:
        errors.append('结果企业与查询条件不一致。')
    actual_years = {int(row['year']) for row in rows if row.get('year') is not None}
    if rows and expected_years and actual_years != expected_years:
        errors.append('结果年份与查询条件不一致。')
    actual_pairs = {(row.get('company'), int(row['year'])) for row in rows if row.get('year') is not None}
    expected_pairs = {(company, year) for company in expected_companies for year in expected_years}
    if expected_pairs and not expected_pairs.issubset(actual_pairs):
        errors.append('结果缺少部分企业与年份组合。')
    if rows and expected_metrics:
        if any(row.get(metric) is None for row in rows for metric in expected_metrics):
            errors.append('部分预期数值指标为空，不能作为完整结果。')
    return {'valid': not errors, 'errors': errors}


def repair_sql(_failed_plan: SQLPlan, _error: str, resolved_context: dict[str, Any]) -> SQLPlan:
    return generate_sql(resolved_context, source='local_repair')


def run_text_to_sql(
    resolved_context: dict[str, Any],
    initial_plan: SQLPlan | dict[str, Any] | None = None,
) -> dict[str, Any]:
    plan = initial_plan or generate_sql(resolved_context)
    if isinstance(plan, dict):
        plan = SQLPlan(plan.get('sql', ''), list(plan.get('params') or []), plan.get('source', 'local'))
    attempts = []
    corrected = False
    # Initial execution plus at most two repairs. Every attempt passes through
    # the same read-only SQL validator before it may touch the database.
    for attempt_number in [1, 2, 3]:
        attempt_started = perf_counter()
        validation = validate_sql(plan.sql, plan.params)
        attempt = {
            'attempt': attempt_number,
            'sql': plan.sql,
            'params': list(plan.params),
            'source': plan.source,
            'safety_status': 'passed' if validation['valid'] else 'rejected',
        }
        try:
            if not validation['valid']:
                raise ValueError('；'.join(validation['errors']))
            execution = execute_readonly_sql(plan.sql, plan.params)
            result_validation = validate_query_result(execution, resolved_context)
            if not result_validation['valid']:
                raise ValueError('；'.join(result_validation['errors']))
            attempt.update({'status': 'success', 'rows': execution['row_count'], 'duration_ms': round((perf_counter() - attempt_started) * 1000, 3)})
            attempts.append(attempt)
            return {
                'status': 'success', 'sql_status': 'success', 'sql': plan.sql,
                'params': list(plan.params), 'source': plan.source,
                'safety_status': 'passed', 'execution_status': 'success',
                'row_count': execution['row_count'], 'columns': execution['columns'],
                'rows': execution['rows'], 'corrected': corrected, 'attempts': attempts,
                'schema': build_schema_context(),
            }
        except Exception as exc:
            attempt.update({'status': 'failed', 'error': str(exc), 'rows': 0, 'duration_ms': round((perf_counter() - attempt_started) * 1000, 3)})
            attempts.append(attempt)
            if attempt_number == 3:
                break
            plan = repair_sql(plan, str(exc), resolved_context)
            corrected = True
    return {
        'status': 'failed', 'sql_status': 'fallback', 'sql': plan.sql,
        'params': list(plan.params), 'source': plan.source,
        'safety_status': attempts[-1].get('safety_status'), 'execution_status': 'failed',
        'row_count': 0, 'columns': [], 'rows': [], 'corrected': corrected,
        'attempts': attempts, 'schema': build_schema_context(),
    }
