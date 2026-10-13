"""Packaged company expansion must preserve real user values and provenance."""
from hashlib import sha256
import json
from pathlib import Path

import pytest

from core import builtin_catalog, db, seed
from core.data_identity import source_identity
from core.storage import workspace_context, resolve_stored_file


@pytest.fixture
def library(tmp_path):
    with workspace_context('evaluation', tmp_path / 'isolated', allow_writes=True):
        db.init_db()
        yield


def test_catalog_has_fifty_unique_companies_and_caller_owned_aliases():
    entries = builtin_catalog.get_builtin_companies()
    assert len(entries) == 50
    assert len({item['name'] for item in entries}) == len({item['stock_code'] for item in entries}) == 50
    assert len({item['file_name'] for item in entries}) == 50
    assert all(len(item['stock_code']) == 6 and item['stock_code'].isdigit() for item in entries)
    name = entries[0]['name']
    expected = builtin_catalog.builtin_aliases(name)
    entries[0]['aliases'].append('不得污染共享目录')
    assert builtin_catalog.builtin_aliases(name) == expected
    assert builtin_catalog.builtin_aliases('不存在的企业') == ()


def test_fifty_companies_seed_once_with_real_annual_records_and_readable_sources(library):
    seed.seed_sample_data()
    companies = db.fetch_companies()
    assert len(companies) == 50
    for company in companies:
        rows = db.fetch_company_metrics(company['id'])
        assert {row['year'] for row in rows} == {2023, 2024, 2025}
        assert all(any(row[field] is not None for field in db.METRIC_FIELDS) for row in rows)
        reports = db.fetch_report_files(company['id'])
        assert len(reports) == 1
        assert resolve_stored_file(reports[0]['file_path']).name == reports[0]['file_name']
        sources = db.fetch_metric_sources(company['id'])
        assert sources and all(source_identity(dict(row), value=json.loads(row['value_json']), metric=row['metric']) == 'builtin' for row in sources)
    path = db.get_db_path()
    before = sha256(path.read_bytes()).hexdigest()
    seed.seed_sample_data()
    seed.seed_sample_data(force=True)
    assert sha256(path.read_bytes()).hexdigest() == before
    assert len(db.fetch_report_files()) == 50


def test_seed_keeps_user_zero_negative_values_and_source_identity(library):
    entry = builtin_catalog.get_builtin_companies()[0]
    company_id = db.upsert_company(entry['name'], entry['stock_code'], industry='用户的行业分类', source='upload')
    db.upsert_metric(company_id, {'year': 2024, 'revenue': 0, 'net_profit': -7,
        'import_id': 'user-value', '_provenance': {
            key: {'file_name': '用户复核.csv', 'cell': key, 'import_id': 'user-value', 'source_note': '用户原始来源'}
            for key in ('revenue', 'net_profit')}})
    original = {row['metric']: dict(row) for row in db.fetch_metric_sources(company_id)}
    seed.seed_sample_data()
    rows = {row['year']: row for row in db.fetch_company_metrics(company_id)}
    assert rows[2024]['revenue'] == 0 and rows[2024]['net_profit'] == -7
    assert rows[2024]['operating_cashflow'] is not None
    assert len(db.fetch_companies()) == 50
    current = {row['metric']: dict(row) for row in db.fetch_metric_sources(company_id, 2024)}
    assert current['revenue'] == original['revenue']
    assert current['net_profit'] == original['net_profit']
    assert current['operating_cashflow']['import_id'] == 'builtin-v1'
    assert next(row for row in db.fetch_companies() if row['id'] == company_id)['source'] == 'upload'
    assert next(row for row in db.fetch_companies() if row['id'] == company_id)['industry'] == '用户的行业分类'


def test_public_snapshot_preserves_raw_units_zero_negative_and_source_url():
    row = {'year': 2025, 'revenue': 0, 'net_profit': -2.5, 'roe': -3, 'eps': 0,
           'raw_revenue': 0, 'raw_net_profit': -250000000, 'raw_roe': -3, 'raw_eps': 0,
           'source_provider': 'Eastmoney F10', 'source_currency': 'CNY',
           'source_notice_date': '2026-03-28', 'source_retrieved_at': '2026-10-13',
           'source_snapshot': 'provenance/000001.json', 'source_url': 'https://example.org/public-data',
           'source_net_profit_field': 'PARENT_NETPROFIT', 'raw_source': '2025年度公开数据'}
    amount = seed._metric_source(Path('sample_000001.csv'), 2, row, 'net_profit', '亿元')
    assert amount['raw_value'] == -2.5 and amount['raw_unit'] == '亿元'
    assert '上游原值：-250000000 元（CSV raw_net_profit列）' in amount['source_note']
    assert amount['normalized_unit'] == '亿元' and amount['currency_hint'] == 'CNY'
    assert amount['source_url'] == row['source_url'] and amount['cell'] == '第4行 / net_profit'
    assert 'PARENT_NETPROFIT' in amount['source_note'] and '尚未逐份核对' in amount['source_note']
    assert seed._metric_source(Path('a.csv'), 0, row, 'revenue', '亿元')['raw_value'] == 0
    assert seed._metric_source(Path('a.csv'), 0, row, 'roe', '%')['raw_unit'] == '%'
    assert seed._metric_source(Path('a.csv'), 0, row, 'eps', '元/股')['raw_unit'] == '元/股'
    legacy = seed._metric_source(Path('sample_byd.csv'), 0, {'revenue': 5}, 'revenue', '亿元')
    assert legacy['raw_value'] == 5 and legacy['raw_unit'] == '亿元'


def test_new_seed_rows_have_public_source_urls_and_empty_unknown_audit_opinions(library):
    seed.seed_sample_data()
    company_by_code = {row['stock_code']: row for row in db.fetch_companies()}
    added = [entry for entry in builtin_catalog.get_builtin_companies()
             if entry['file_name'] == f"sample_{entry['stock_code']}.csv"]
    assert len(added) == 40
    for entry in added:
        company_id = company_by_code[entry['stock_code']]['id']
        assert all(row['audit_opinion'] is None for row in db.fetch_company_metrics(company_id))
        assert all(row['source_url'] and '尚未逐份核对' in row['source_note']
                   for row in db.fetch_metric_sources(company_id))


@pytest.mark.parametrize('code,metric', [('600519', 'revenue'), ('601012', 'net_profit'),
                                         ('600519', 'operating_cashflow')])
def test_new_builtin_money_can_be_reparsed_without_a_second_unit_conversion(library, code, metric):
    from core.countercheck import _original_fact
    seed.seed_sample_data()
    company = next(row for row in db.fetch_companies() if row['stock_code'] == code)
    source = dict(db.fetch_metric_sources(company['id'], 2025, metric)[0])
    checked = _original_fact(source, company['name'], 2025, metric,
                             [dict(row) for row in db.fetch_report_files(company['id'])])
    assert checked['status'] == 'passed', checked['message']
    assert checked['value'] == pytest.approx(json.loads(source['value_json']))
    if code == '601012':
        assert checked['value'] < 0


def test_catalog_rejects_path_escape(tmp_path, monkeypatch):
    catalog = tmp_path / 'catalog.json'
    catalog.write_text(json.dumps({'schema_version': 1, 'companies': [
        {'name': '测试公司', 'stock_code': '000001', 'industry': '测试',
         'file_name': '../sample_escape.csv', 'aliases': []}]}), encoding='utf-8')
    monkeypatch.setattr(builtin_catalog, 'CATALOG_PATH', catalog)
    builtin_catalog._read_catalog.cache_clear()
    try:
        with pytest.raises(ValueError, match='明确列出'):
            builtin_catalog.get_builtin_companies()
    finally:
        builtin_catalog._read_catalog.cache_clear()


