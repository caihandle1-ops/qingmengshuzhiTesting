"""End-to-end checks through the distributed offline command-line interface."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def invoke(data_dir, *arguments, expected=0):
    env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1',
               FINANCIAL_DATA_DIR=str(data_dir.parent / 'must-not-be-used'), FINANCIAL_DEPLOYMENT='public')
    result = subprocess.run([sys.executable, str(ROOT / 'cli.py'), '--data-dir', str(data_dir), *arguments],
                            cwd=ROOT, env=env, capture_output=True, text=True, encoding='utf-8', timeout=90)
    assert result.returncode == expected, result.stdout + result.stderr
    decoded = json.loads(result.stdout)
    assert 'NaN' not in result.stdout and 'Infinity' not in result.stdout
    return decoded


@pytest.fixture(scope='module')
def runtime(tmp_path_factory):
    path = tmp_path_factory.mktemp('technical-cli') / 'isolated-library'
    result = invoke(path, 'init')
    assert result['counts']['companies'] == 50
    assert result['counts']['financial_metrics'] == 150
    assert not (path.parent / 'must-not-be-used').exists()
    return path


def test_init_is_repeatable_and_uses_explicit_directory(runtime):
    result = invoke(runtime, 'init')
    assert result['counts']['companies'] == 50
    assert result['counts']['report_files'] == 50
    assert Path(result['database']).parent == runtime


def test_company_catalog_and_ticker_lookup(runtime):
    assert invoke(runtime, 'companies')['count'] == 50
    company = invoke(runtime, 'companies', '--query', '600519')
    assert company['count'] == 1
    assert company['companies'][0]['name'] == '贵州茅台酒股份有限公司'


def test_real_question_and_upstream_source(runtime):
    output = invoke(runtime, 'ask', '贵州茅台2024年营业收入是多少？')
    result = output['turns'][0]['result']
    assert '1,708.99' in result['answer'] or '1708.99' in result['answer']
    assert result['parsed']['years'] == [2024]
    assert result['parsed']['companies'] == ['贵州茅台酒股份有限公司']
    assert result['agent_trace']
    source = invoke(runtime, 'sources', '600519', '--year', '2024', '--metric', 'revenue')
    assert source['count'] == 1
    location = source['sources'][0]
    assert location['file_name'] == 'sample_600519.csv'
    assert 'revenue' in location['cell']
    assert location['import_id'] == 'builtin-v1'
    assert 'eastmoney.com' in location['source_url']
    assert float(json.loads(location['value_json'])) == pytest.approx(1708.9915227634)


def test_multiturn_resolves_previous_company(runtime, tmp_path):
    questions = tmp_path / 'questions.json'
    questions.write_text(json.dumps(['贵州茅台2024年营业收入是多少？', '那2023年呢？'], ensure_ascii=False), encoding='utf-8')
    output = invoke(runtime, 'chat', '--input', str(questions))
    second = output['turns'][1]['result']
    assert second['parsed']['years'] == [2023]
    assert second['parsed']['companies'] == ['贵州茅台酒股份有限公司']
    assert '营业收入' in second['answer']


def test_comparison_and_four_dimension_scores(runtime):
    output = invoke(runtime, 'compare', '美的', '格力', '--year', '2025')
    assert len(output['companies']) == 2
    result = output['result']
    assert len(result['score_table']) == 5
    assert {row['年度'] for row in result['table']} == {2025}
    assert result['score_a']['available']
    assert result['score_b']['available']
    metrics = invoke(runtime, 'compare', '美的', '格力', '--year', '2024', '--metrics', 'revenue')['result']
    assert len(metrics['table']) == 1
    assert metrics['table'][0]['年度'] == 2024
    assert metrics['recommended'] is None


def test_risk_and_schema(runtime):
    risk = invoke(runtime, 'risk', '五粮液', '--year', '2024')
    assert risk['year'] == 2024
    assert isinstance(risk['alerts'], list)
    schema = invoke(runtime, 'schema')
    tables = {row['name']: row for row in schema['tables']}
    assert {'companies', 'financial_metrics', 'metric_sources'} <= set(tables)
    assert tables['financial_metrics']['foreign_keys']
    assert any(field['name'] == 'revenue' for field in tables['financial_metrics']['columns'])


def test_error_inputs_and_absent_period_are_not_silently_replaced(runtime, tmp_path):
    assert invoke(tmp_path / 'not-initialized', 'companies', expected=2)['status'] == 'error'
    assert invoke(runtime, 'compare', '不存在的公司', '格力', expected=2)['status'] == 'error'
    assert invoke(runtime, 'compare', '美的', '美的', expected=2)['status'] == 'error'
    assert invoke(runtime, 'risk', '美的', '--year', '2022', expected=2)['status'] == 'error'
    assert invoke(runtime, 'ask', '   ', expected=2)['status'] == 'error'
    missing = invoke(runtime, 'ask', '贵州茅台2022年营业收入是多少？')['turns'][0]['result']
    assert missing['status'] == 'missing_scope'
    bad = tmp_path / 'invalid.json'
    bad.write_text('{"questions": []}', encoding='utf-8')
    assert invoke(runtime, 'chat', '--input', str(bad), expected=2)['status'] == 'error'


def test_init_preserves_existing_zero_value(tmp_path):
    data_dir = tmp_path / 'zero'
    invoke(data_dir, 'init')
    conn = sqlite3.connect(data_dir / 'investor_agent.db')
    try:
        conn.execute('UPDATE financial_metrics SET revenue=0 WHERE id=(SELECT MIN(id) FROM financial_metrics)')
        conn.commit()
    finally:
        conn.close()
    invoke(data_dir, 'init')
    conn = sqlite3.connect(data_dir / 'investor_agent.db')
    try:
        assert conn.execute('SELECT revenue FROM financial_metrics ORDER BY id LIMIT 1').fetchone()[0] == 0
    finally:
        conn.close()


def test_offline_guard_blocks_network():
    code = 'import sys,socket; from cli import block_network; sys.addaudithook(block_network); socket.getaddrinfo("example.com",443)'
    output = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True,
                            text=True, encoding='utf-8', timeout=15)
    assert output.returncode != 0
    assert 'outbound networking is disabled' in output.stderr


def test_database_cannot_be_written_into_packaged_data():
    assert invoke(ROOT / 'data', 'init', expected=2)['status'] == 'error'
