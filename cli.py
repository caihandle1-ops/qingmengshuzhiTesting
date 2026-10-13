"""Offline JSON interface for the Qingmeng financial research engine.

This adapter deliberately contains no web application, visual theme or assets.
All database access is bound to the directory explicitly selected by the caller.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import redirect_stdout
from datetime import date, datetime
import json
import math
from pathlib import Path
import sys
from typing import Any


class InputError(ValueError):
    """A recoverable command-line or input validation error."""


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise InputError(message)


def json_value(value: Any) -> Any:
    """Convert engine results to strict JSON; unavailable numbers become null."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_value(item) for item in value]
    if isinstance(value, (Path, date, datetime)):
        return str(value)
    if hasattr(value, 'to_dict'):
        try:
            return json_value(value.to_dict(orient='records'))
        except TypeError:
            return json_value(value.to_dict())
    if hasattr(value, 'item'):
        return json_value(value.item())
    raise TypeError(f'Unsupported result type: {type(value).__name__}')


def emit(value: Any) -> None:
    print(json.dumps(json_value(value), ensure_ascii=False, allow_nan=False, indent=2))


def block_network(event: str, arguments: tuple) -> None:
    # No key, cloud LLM, remote document download or telemetry is needed by CLI.
    if event in {'socket.connect', 'socket.getaddrinfo'}:
        raise PermissionError('This testing CLI runs offline; outbound networking is disabled.')


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(description='擎梦数智技术测试 CLI（离线、JSON 输出）')
    parser.add_argument('--data-dir', type=Path, default=Path('./runtime'),
                        help='独立测试资料库目录，默认当前目录的 ./runtime；应写在子命令之前')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init', help='初始化独立 SQLite 资料库并补齐 50 家内置企业')
    companies = commands.add_parser('companies', help='列出企业；可按企业名、简称、代码筛选')
    companies.add_argument('--query', default='')
    ask = commands.add_parser('ask', help='使用真实本地问答引擎回答一个财务问题')
    ask.add_argument('question')
    ask.add_argument('--company', help='可选的当前企业；问题中的显式企业优先')
    ask.add_argument('--year', type=int, help='可选的当前年度；问题中的显式年度优先')
    chat = commands.add_parser('chat', help='读取 UTF-8 JSON 问题数组，按顺序测试多轮对话')
    chat.add_argument('--input', type=Path, required=True,
                      help='内容示例：["贵州茅台2024年营业收入是多少？", "那2023年呢？"]')
    chat.add_argument('--company')
    chat.add_argument('--year', type=int)
    compare = commands.add_parser('compare', help='按共同年度运行企业比较与四维规则评分')
    compare.add_argument('company_a')
    compare.add_argument('company_b')
    compare.add_argument('--year', type=int, help='限定比较到该年度；同比仅使用其相邻基期')
    compare.add_argument('--metrics', nargs='+', choices=[
        'revenue', 'net_profit', 'operating_cashflow', 'roe', 'debt_ratio', 'gross_margin', 'eps'],
        help='只比较指定指标时不执行综合评分')
    risk = commands.add_parser('risk', help='运行本地风险规则及数据覆盖检查')
    risk.add_argument('company')
    risk.add_argument('--year', type=int)
    sources = commands.add_parser('sources', help='定位指标对应的文件、行列和公开上游来源')
    sources.add_argument('company')
    sources.add_argument('--year', type=int)
    sources.add_argument('--metric', choices=[
        'revenue', 'net_profit', 'operating_cashflow', 'roe', 'debt_ratio', 'gross_margin', 'eps',
        'audit_opinion', 'consolidated_net_profit', 'total_assets', 'total_liabilities',
        'total_equity', 'minority_profit'])
    commands.add_parser('schema', help='输出当前数据库的表、字段、约束及索引')
    commands.add_parser('benchmark', help='在独立临时目录运行内置业务回归评测集，无需先 init')
    return parser


def workspace_path(value: Path) -> Path:
    root = Path(__file__).resolve().parent
    path = value.expanduser().resolve()
    if path == root or root.is_relative_to(path):
        raise InputError('请为 --data-dir 指定独立子目录，不能直接使用源码根目录或其父目录。')
    for reserved in ('core', 'data', 'tests', 'docs', 'tools', '.git'):
        folder = root / reserved
        if path == folder or path.is_relative_to(folder):
            raise InputError('--data-dir 不能写入随源码打包的业务模块、数据、测试或版本控制目录。')
    if path.exists() and not path.is_dir():
        raise InputError('--data-dir 必须是目录。')
    return path


def load_workspace():
    from core.analysis import rows_to_df
    from core.data_identity import attach_source_origins
    from core.db import get_conn

    conn = get_conn()
    try:
        conn.execute('BEGIN')
        companies = [dict(row) for row in conn.execute('SELECT * FROM companies ORDER BY name')]
        annuals = [dict(row) for row in conn.execute('SELECT * FROM financial_metrics ORDER BY company_id,year')]
        sources = [dict(row) for row in conn.execute(
            'SELECT * FROM metric_sources WHERE is_current=1 ORDER BY company_id,year,metric,id DESC')]
        reports = [dict(row) for row in conn.execute('SELECT * FROM report_files')]
    finally:
        conn.close()
    by_company, by_source, by_report = defaultdict(list), defaultdict(dict), defaultdict(list)
    for row in annuals:
        by_company[row['company_id']].append(row)
    for row in sources:
        by_source[(row['company_id'], row['year'])].setdefault(row['metric'], row)
    for row in reports:
        by_report[row['company_id']].append(row)
    frames = {}
    for company in companies:
        cid = company['id']
        frame = rows_to_df(by_company[cid])
        if not frame.empty:
            frame['metric_provenance'] = [by_source[(cid, int(year))] for year in frame['year']]
            enriched = attach_source_origins(frame, by_report[cid])
            frame['metric_provenance'] = [row['metric_provenance'] for row in enriched]
        frames[company['name']] = frame
    return companies, frames


def resolve_company(value: str, companies: list[dict]) -> str:
    from core.company_identity import company_aliases
    value = value.strip().casefold()
    names = [company['name'] for company in companies
             if value in {str(alias).casefold() for alias in
                          (*company_aliases(company['name']), company.get('stock_code') or '')}]
    if len(names) != 1:
        raise InputError('企业名称或代码不存在或不唯一，请先用 companies --query 检索并选择完整企业名。')
    return names[0]


def annual_scope(frame, year: int | None):
    if frame.empty:
        raise InputError('该企业没有年度财务指标。')
    if year is not None:
        if year not in set(frame['year']):
            raise InputError(f'缺少 {year} 年数据，不会改用其他年度。')
        return frame[frame['year'] <= year].copy()
    return frame.copy()


def run_questions(questions, args, companies, frames):
    from core.conversation import new_conversation_context, resolve_turn
    from core.qa_engine import answer_question

    names = [row['name'] for row in companies]
    selected = resolve_company(args.company, companies) if args.company else None
    context = new_conversation_context()
    outputs = []
    for question in questions:
        if not isinstance(question, str) or not question.strip() or len(question) > 10000:
            raise InputError('每个问题必须是 1—10000 字符的非空字符串。')
        turn = resolve_turn(context, question.strip(), names, page_company=selected, page_year=args.year)
        context = turn['context']
        if turn['status'] != 'ready':
            outputs.append({'question': question, 'result': turn, 'context': context})
            continue
        result = answer_question(question.strip(), selected or '', '', names, frames,
                                 llm_config=None, resolved_context=turn.get('resolved'))
        outputs.append({'question': question, 'result': result, 'context': context})
    return outputs


def schema_result() -> dict:
    from core.db import get_conn
    conn = get_conn()
    try:
        tables = []
        rows = conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
        for row in rows.fetchall():
            # Names come only from sqlite_master and are quoted as identifiers.
            identifier = '"' + row['name'].replace('"', '""') + '"'
            tables.append({'name': row['name'], 'sql': row['sql'],
                           'columns': [dict(item) for item in conn.execute(f'PRAGMA table_info({identifier})')],
                           'foreign_keys': [dict(item) for item in conn.execute(f'PRAGMA foreign_key_list({identifier})')],
                           'indexes': [dict(item) for item in conn.execute(f'PRAGMA index_list({identifier})')]})
        return {'schema_version': conn.execute('PRAGMA user_version').fetchone()[0], 'tables': tables}
    finally:
        conn.close()


def execute(args) -> dict:
    from core.db import get_db_path, init_db
    from core.storage import workspace_context

    root = workspace_path(args.data_dir)
    if getattr(args, 'year', None) is not None and not 1900 <= args.year <= 2200:
        raise InputError('年度必须在 1900—2200 之间。')
    with workspace_context('main', root=root, allow_writes=args.command == 'init'):
        if args.command == 'benchmark':
            from core.evaluation import run_benchmark
            result = run_benchmark()
            return {'status': 'failed' if result.get('failed') else 'ok',
                    'network': 'disabled', 'result': result}
        if args.command == 'init':
            from core.seed import seed_sample_data
            init_db(migrate_legacy=False)
            seed_sample_data()
            from core.db import get_conn
            conn = get_conn()
            try:
                counts = {table: conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                          for table in ('companies', 'financial_metrics', 'metric_sources', 'report_files')}
            finally:
                conn.close()
            return {'status': 'ok', 'command': 'init', 'database': str(get_db_path()), 'counts': counts,
                    'network': 'disabled', 'note': '已有非空值不会被内置数据覆盖。'}
        if not get_db_path().is_file():
            raise InputError('测试资料库尚未初始化；请使用相同 --data-dir 先运行 init。')
        if args.command == 'schema':
            return {'status': 'ok', **schema_result()}
        companies, frames = load_workspace()
        if args.command == 'companies':
            from core.company_identity import company_search_matches
            rows = [row for row in companies if not args.query or company_search_matches(args.query, row['name'])
                    or args.query == row.get('stock_code')]
            return {'status': 'ok', 'count': len(rows), 'companies': rows}
        if args.command in {'ask', 'chat'}:
            if args.command == 'ask':
                questions = [args.question]
            else:
                if args.input.stat().st_size > 1_000_000:
                    raise InputError('chat 输入文件不得超过 1 MB。')
                questions = json.loads(args.input.read_text(encoding='utf-8-sig'))
                if not isinstance(questions, list) or not 1 <= len(questions) <= 100:
                    raise InputError('chat 输入必须是含 1—100 个问题的 JSON 数组。')
            # Validate the entire batch before running its first question.
            if any(not isinstance(q, str) or not q.strip() or len(q) > 10000 for q in questions):
                raise InputError('每个问题必须是 1—10000 字符的非空字符串。')
            turns = run_questions(questions, args, companies, frames)
            return {'status': 'ok', 'network': 'disabled', 'turns': turns}
        if args.command == 'compare':
            from core.analysis import compare_companies
            first, second = (resolve_company(value, companies) for value in (args.company_a, args.company_b))
            if first == second:
                raise InputError('请选择两家不同企业。')
            left, right = annual_scope(frames[first], args.year), annual_scope(frames[second], args.year)
            if args.metrics and args.year is not None:
                left, right = left[left['year'] == args.year], right[right['year'] == args.year]
            return {'status': 'ok', 'companies': [first, second],
                    'result': compare_companies(first, left, second, right, metrics=args.metrics)}
        company = resolve_company(args.company, companies)
        if args.command == 'risk':
            from core.analysis import compute_alerts, risk_dashboard
            frame = annual_scope(frames[company], args.year)
            alerts = compute_alerts(frame)
            return {'status': 'ok', 'company': company, 'year': int(frame['year'].max()),
                    'alerts': alerts, 'summary': risk_dashboard(alerts)}
        if args.command == 'sources':
            from core.db import fetch_metric_sources
            cid = next(row['id'] for row in companies if row['name'] == company)
            rows = [dict(row) for row in fetch_metric_sources(cid, args.year, args.metric)]
            return {'status': 'ok', 'company': company, 'count': len(rows), 'sources': rows}
        raise InputError('未知命令。')


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    sys.addaudithook(block_network)
    try:
        args = build_parser().parse_args(argv)
        # Some optional PDF libraries print import notices. Keep stdout a single
        # parseable JSON document and retain those diagnostics on stderr.
        with redirect_stdout(sys.stderr):
            result = execute(args)
        emit(result)
        return 1 if result.get('status') == 'failed' else 0
    except (InputError, ValueError, OSError) as exc:
        emit({'status': 'error', 'error': type(exc).__name__, 'message': str(exc)})
        return 2
    except Exception as exc:
        emit({'status': 'error', 'error': type(exc).__name__,
              'message': '执行失败，请核对依赖版本与输入资料；此 CLI 未调用远程模型。'})
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
