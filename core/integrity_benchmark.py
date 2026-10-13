"""Offline regression cases that exercise the same production boundaries.

Each data-mutating case owns a fresh evaluation library. No monkeypatching,
provider calls, user data or process-global environment changes are used.
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pandas as pd
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen.canvas import Canvas

from core.analysis import compare_companies, compute_alerts, growth_details, rows_to_df, score_company
from core.db import (fetch_companies, fetch_company_metrics, fetch_metric_sources,
                     get_db_path, init_db, upsert_company, upsert_metric)
from core.llm import validate_enhancement
from core.qa_engine import answer_question
from core.retrieval import retrieve_documents
from core.seed import seed_sample_data
from core.service import prepare_import, commit_import
from core.storage import ROOT, workspace_context

BYD = '比亚迪股份有限公司'
CATL = '宁德时代新能源科技股份有限公司'


def _data_map():
    return {row['name']: rows_to_df(fetch_company_metrics(row['id'])) for row in fetch_companies()}


def _pdf_fixture() -> bytes:
    stream = io.BytesIO()
    pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
    canvas = Canvas(stream)
    canvas.setFont('STSong-Light', 12)
    lines = [
        '评测股份有限公司', '2024年年度报告', '主要会计数据和财务指标', '单位：亿元',
        '项目                             2024年',
        '营业收入                         100',
        '归母净利润                       10',
        '经营活动产生的现金流量净额         20',
        '审计意见：标准无保留意见',
    ]
    for index, text in enumerate(lines):
        canvas.drawString(60, 760 - index * 28, text)
    canvas.save()
    return stream.getvalue()


def execute_integrity_case(case: dict[str, Any], root: Path) -> dict[str, Any]:
    operation = case['operation']
    if operation == 'query_scope':
        data = _data_map()
        result = answer_question(case['question'], BYD, CATL, list(data), data)
        rows = result.get('sql_result', {}).get('rows') or []
        actual = {'status': result['status'], 'sql_status': result['sql_status']}
        if 'years' in case['expected']:
            actual['years'] = sorted({row['year'] for row in rows})
        if 'companies' in case['expected']:
            actual['companies'] = sorted({row['company'] for row in rows})
        if 'row_count' in case['expected']:
            actual['row_count'] = len(rows)
        if 'all_values_answered' in case['expected']:
            actual['all_values_answered'] = bool(rows) and all(f'{row["revenue"]:.2f}' in result['answer'] and row['company'] in result['answer'] for row in rows)
        return actual
    if operation == 'real_trace':
        data = _data_map()
        result = answer_question('比亚迪2024年营业收入是多少？', BYD, CATL, list(data), data)
        return {'sql_executed': result['sql_result']['status'] == 'success',
                'actual_durations': all('耗时ms' in item and item['耗时ms'] >= 0 for item in result['agent_trace']),
                'rag_executed': any(item['智能体'] == '文档检索' for item in result['agent_trace']),
                'static_called_label': any(item['状态'] == '已调用' for item in result['agent_trace'])}
    if operation in {'growth_complete', 'growth_gap'}:
        rows = [{'year': 2023, 'revenue': 100}, {'year': 2024, 'revenue': 120}, {'year': 2025, 'revenue': 144}]
        if operation == 'growth_gap':
            rows.pop(1)
        result = growth_details(pd.DataFrame(rows), 'revenue')
        return {key: (round(result[key], 6) if isinstance(result[key], float) else result[key]) for key in case['expected']}
    if operation == 'incomplete_score':
        result = score_company(pd.DataFrame([{'year': 2024, 'revenue': 100}]))
        return {'available': result['available'], 'score': result['score'], 'has_missing_reason': bool(result['missing'])}
    if operation == 'single_metric_compare':
        result = compare_companies('甲企业', pd.DataFrame([{'year': 2024, 'revenue': 100}]),
                                   '乙企业', pd.DataFrame([{'year': 2024, 'revenue': 200}]), metrics=['revenue'])
        return {'score_a': result['score_a']['score'], 'score_b': result['score_b']['score'],
                'recommended': result['recommended'], 'rows': len(result['table'])}
    if operation == 'empty_risk':
        alerts = compute_alerts(pd.DataFrame([{'year': 2024, 'revenue': None, 'net_profit': None}]))
        return {'unknown': any(a.get('level') == 'unknown' for a in alerts),
                'green': any(a.get('level') == 'green' for a in alerts),
                'coverage_complete': all(a.get('coverage_complete') for a in alerts)}
    if operation == 'cloud_fact_guard':
        return {'valid': validate_enhancement('比亚迪2024年营业收入为100亿元。', case['candidate'])['valid']}
    if operation in {'csv_yuan', 'csv_multi_company', 'csv_unknown_unit', 'csv_percent_ratio'}:
        batch = prepare_import(case['file_name'], case['csv'].encode('utf-8-sig'))
        actual = {'can_commit': batch['can_commit']}
        if 'values' in case['expected']:
            metric = case.get('metric', 'revenue')
            actual['values'] = [r.get(metric) for r in batch['records']]
        if 'companies' in case['expected']:
            actual['companies'] = batch['companies']
        return actual
    if operation == 'pdf_distinct_rows_audit':
        batch = prepare_import('评测2024年报.pdf', _pdf_fixture(), manual_company_name='评测股份有限公司')
        record = batch['records'][0] if batch['records'] else {}
        return {'can_commit': batch['can_commit'], 'revenue': record.get('revenue'),
                'net_profit': record.get('net_profit'), 'operating_cashflow': record.get('operating_cashflow'),
                'audit_opinion': record.get('audit_opinion')}
    if operation == 'official_pdf_values':
        path = ROOT / 'tests' / 'fixtures' / 'byd_2024_key_financials.pdf'
        batch = prepare_import('比亚迪2024年年度报告原文摘页.pdf', path.read_bytes(), manual_company_name=BYD, year_hint=2024)
        record = next((record for record in batch['records'] if record.get('year') == 2024), {})
        return {'can_commit': batch['can_commit'], **{metric: record.get(metric) for metric in ['revenue', 'net_profit', 'operating_cashflow']},
                'original_unit': record.get('_provenance', {}).get('net_profit', {}).get('raw_unit')}
    if operation == 'official_numeric_citation':
        path = ROOT / 'tests' / 'fixtures' / 'byd_2024_key_financials.pdf'
        report = {'id': 'official-byd2024', 'company_name': BYD, 'report_year': 2024,
                  'file_type': 'PDF', 'file_name': path.name, 'file_path': str(path)}
        result = retrieve_documents('比亚迪2024年年报净利润是多少', {'companies': [BYD], 'years': [2024], 'metrics': ['net_profit'],
            'structured_facts': [{'company': BYD, 'year': 2024, 'net_profit': 402.54}]}, [report], root / 'official-index')
        return {'status': result['status'], 'pages': [item['page'] for item in result['citations']],
                'supports_annual_value': any('40,254,346,000.00' in item['snippet'] for item in result['citations']),
                'fact_checked': result['numeric_fact_checked']}
    if operation == 'rag_wrong_year':
        path = root / 'integrity-cause.pdf'
        path.write_bytes(_pdf_fixture())
        report = {'company_name': BYD, 'report_year': 2023, 'file_type': 'PDF', 'file_path': str(path), 'file_name': path.name}
        result = retrieve_documents('2024年净利润原因', {'companies': [BYD], 'years': [2024], 'metrics': ['net_profit']}, [report], root / 'wrong-year-index')
        return {'status': result['status'], 'candidate_documents': result['candidate_documents'], 'citations': len(result['citations'])}
    # Mutations are deliberately isolated from both the application and other cases.
    with workspace_context('evaluation', root / case['id'], allow_writes=True):
        init_db()
        if operation == 'seed_preserves_import':
            seed_sample_data()
            cid = next(row['id'] for row in fetch_companies() if row['name'] == BYD)
            upsert_metric(cid, {'year': 2024, 'revenue': 123.45, '_provenance': {'revenue': {'file_name': 'user.csv', 'raw_unit': '亿元', 'import_id': 'user-v1'}}})
            seed_sample_data()
            row = next(row for row in fetch_company_metrics(cid) if row['year'] == 2024)
            source = fetch_metric_sources(cid, 2024, 'revenue')[0]
            return {'revenue': row['revenue'], 'file_name': source['file_name'], 'import_id': source['import_id']}
        if operation == 'per_metric_provenance':
            cid = upsert_company('来源评测企业')
            upsert_metric(cid, {'year': 2024, 'revenue': 100, 'net_profit': 10,
                               '_provenance': {key: {'file_name': 'original.csv', 'cell': cell, 'raw_unit': '亿元', 'import_id': 'v1'} for key, cell in [('revenue', 'B2'), ('net_profit', 'C2')]}})
            upsert_metric(cid, {'year': 2024, 'net_profit': 20, '_provenance': {'net_profit': {'file_name': 'profit.pdf', 'page': 5, 'raw_unit': '亿元', 'import_id': 'v2'}}})
            sources = {row['metric']: dict(row) for row in fetch_metric_sources(cid, 2024)}
            return {'revenue_file': sources['revenue']['file_name'], 'profit_file': sources['net_profit']['file_name'],
                    'profit_page': sources['net_profit']['page'], 'history_count': len(fetch_metric_sources(cid, 2024, include_history=True))}
        if operation == 'preview_before_write':
            batch = prepare_import('preview.csv', '公司,年份,营业收入（亿元）\n预览企业,2024,100\n'.encode('utf-8-sig'))
            denied = commit_import(batch, confirmed=False)
            before = len(fetch_companies())
            accepted = commit_import(batch, confirmed=True)
            return {'before_confirmation_count': before, 'denied': not denied['ok'], 'accepted': accepted['ok'],
                    'after_confirmation_count': len(fetch_companies())}
        if operation == 'read_only_deployment':
            with workspace_context('evaluation', root / 'readonly-evaluation', allow_writes=False):
                init_db()
                try:
                    upsert_company('禁止写入')
                except PermissionError:
                    return {'blocked': True, 'count': len(fetch_companies())}
                return {'blocked': False, 'count': len(fetch_companies())}
    raise ValueError(f'未知完整性评测操作：{operation}')
