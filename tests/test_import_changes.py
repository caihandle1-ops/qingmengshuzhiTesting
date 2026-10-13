"""Compare predicted changes with real committed values, sources and sessions."""
from copy import deepcopy
import json
from uuid import uuid4

import pytest

from core import db, service
from core.import_changes import build_import_change_preview, current_import_signature, affected_frozen_answers
from core.storage import workspace_context, configure_workspace, configure_public_session


COMPANY = '变化预览测试股份有限公司'


@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setenv('FINANCIAL_DEPLOYMENT', 'local')
    with workspace_context('evaluation', tmp_path / 'library', allow_writes=True) as root:
        db.init_db()
        yield root


def batch(values, *, filename='补充.csv', company=COMPANY, year=2024):
    labels = {'revenue': '营业收入（亿元）', 'net_profit': '归母净利润（亿元）',
              'operating_cashflow': '经营现金流（亿元）', 'audit_opinion': '审计意见'}
    header = ','.join(['公司', '年份', *(labels[key] for key in values)])
    line = ','.join([company, str(year), *(str(value) if value is not None else '' for value in values.values())])
    result = service.prepare_import(filename, (header + '\n' + line + '\n').encode())
    assert result['can_commit'], result['errors']
    return result


def commit(data, overwrite=False, preview=None):
    return service.commit_import(data, confirmed=True, overwrite_existing=overwrite,
        expected_current_signature=preview['current_signature'] if preview else None)


def rows(preview):
    return {row['metric']: row for row in preview['rows']}


def current():
    company = next(row for row in db.fetch_companies() if row['name'] == COMPANY)
    return dict(db.fetch_company_metrics(company['id'])[0])


def test_preview_is_read_only_and_new_year_is_not_a_missing_value(library):
    data = batch({'revenue': 0, 'net_profit': -2, 'audit_opinion': '标准无保留意见'})
    preview = build_import_change_preview(data, state={'llm_key_豆包': 'private-key-value'})
    assert preview['counts'] == {'new': 3}
    assert rows(preview)['revenue']['old_value'] is None
    assert rows(preview)['revenue']['incoming_value'] == 0
    assert rows(preview)['net_profit']['incoming_value'] == -2
    assert not db.fetch_companies() and not db.fetch_report_files()
    assert not (library / 'uploads').exists()
    assert 'private-key-value' not in json.dumps(preview)
    assert str(library) not in json.dumps(preview)
    assert commit(data, preview=preview)['ok']
    assert current()['revenue'] == 0 and current()['net_profit'] == -2
    assert current()['audit_opinion'] == '标准无保留意见'


def test_no_overwrite_keeps_existing_zero_and_negative_and_fills_only_missing(library):
    assert commit(batch({'revenue': 0, 'net_profit': -2, 'audit_opinion': '标准无保留意见'}, filename='旧表.csv'))['ok']
    old_sources = [dict(row) for row in db.fetch_metric_sources(db.fetch_companies()[0]['id'])]
    incoming = batch({'revenue': 0, 'net_profit': -3, 'operating_cashflow': 0})
    preview = build_import_change_preview(incoming)
    items = rows(preview)
    assert items['revenue']['action'] == 'unchanged'
    assert items['net_profit']['action'] == 'kept' and items['net_profit']['effective_value'] == -2
    assert items['operating_cashflow']['action'] == 'filled'
    assert items['audit_opinion']['action'] == 'omitted'
    result = commit(incoming, preview=preview)
    assert result['ok'] and result['inserted_metrics'] == 1 and result['updated_metrics'] == 0
    assert current()['net_profit'] == -2 and current()['operating_cashflow'] == 0
    assert current()['audit_opinion'] == '标准无保留意见'
    sources = {row['metric']: dict(row) for row in db.fetch_metric_sources(db.fetch_companies()[0]['id'])}
    assert all(sources[row['metric']]['id'] == row['id'] for row in old_sources)


def test_overwrite_separates_same_value_source_update_and_text_change(library):
    assert commit(batch({'revenue': 10, 'net_profit': -2, 'audit_opinion': '标准无保留意见'}, filename='旧表.csv'))['ok']
    incoming = batch({'revenue': 10, 'net_profit': 0, 'audit_opinion': '保留意见'}, filename='新原件.csv')
    preview = build_import_change_preview(incoming, overwrite_existing=True)
    items = rows(preview)
    assert items['revenue']['action'] == 'source_replaced'
    assert items['revenue']['old_source'].startswith('旧表.csv') and items['revenue']['new_source'].startswith('新原件.csv')
    assert items['net_profit']['action'] == 'overwritten'
    assert items['audit_opinion']['action'] == 'text_updated'
    assert commit(incoming, True, preview)['ok']
    assert current()['net_profit'] == 0 and current()['audit_opinion'] == '保留意见'
    history = db.fetch_metric_sources(db.fetch_companies()[0]['id'], include_history=True)
    assert len(history) == 6 and sum(row['is_current'] for row in history) == 3


def test_audit_only_planned_change_is_not_promised_when_numeric_values_all_kept(library):
    assert commit(batch({'revenue': 10}))['ok']
    incoming = batch({'revenue': 10, 'audit_opinion': '保留意见'})
    preview = build_import_change_preview(incoming)
    assert rows(preview)['audit_opinion']['action'] == 'not_committed'
    assert not preview['write_count'] and not preview['can_commit']
    result = commit(incoming, preview=preview)
    assert result['status'] == 'unchanged' and current()['audit_opinion'] is None


@pytest.mark.parametrize('source_only', [False, True])
def test_stale_target_rejected_before_any_file_or_database_write(library, source_only):
    assert commit(batch({'revenue': 10}))['ok']
    incoming = batch({'revenue': 99})
    preview = build_import_change_preview(incoming, overwrite_existing=True)
    # A same-value source replacement invalidates approval just like an amount change.
    assert commit(batch({'revenue': 10 if source_only else 11}, filename='另一份资料.csv'), True)['ok']
    before_reports = [dict(row) for row in db.fetch_report_files()]
    before_files = list((library / 'uploads').rglob('*.csv'))
    result = commit(incoming, True, preview)
    assert result['status'] == 'preview_stale' and not result['committed']
    assert [dict(row) for row in db.fetch_report_files()] == before_reports
    assert list((library / 'uploads').rglob('*.csv')) == before_files
    assert current()['revenue'] == (10 if source_only else 11)
    new_preview = build_import_change_preview(incoming, overwrite_existing=True)
    assert commit(incoming, True, new_preview)['ok']


def test_other_company_change_does_not_invalidate_and_overwrite_choice_does(library):
    assert commit(batch({'revenue': 10}))['ok']
    incoming = batch({'revenue': 20})
    preview = build_import_change_preview(incoming)
    assert commit(batch({'revenue': 30}, company='其他股份有限公司'))['ok']
    assert current_import_signature(incoming) == preview['current_signature']
    assert commit(incoming, True, preview)['status'] == 'preview_stale'
    assert commit(incoming, preview=preview)['status'] == 'unchanged'


def test_double_click_remains_idempotent_with_a_precommit_signature(library):
    incoming = batch({'revenue': 20})
    preview = build_import_change_preview(incoming)
    assert commit(incoming, preview=preview)['ok']
    assert commit(incoming, preview=preview)['status'] == 'unchanged'
    assert len(db.fetch_report_files()) == 1


def test_document_only_preserves_the_existing_confirmed_flow(library):
    from pdf_fixture import pdf_bytes
    incoming = service.prepare_import('无结构化指标原文.pdf', pdf_bytes(document_only=True))
    assert incoming['can_save_document'] and not incoming['can_commit']
    preview = build_import_change_preview(incoming)
    assert not preview['write_count'] and not preview['affected_answers']
    result = service.commit_import(incoming, confirmed=True, document_only=True,
        expected_current_signature=preview['current_signature'])
    assert result['status'] == 'document_only' and result['committed'] and not result['ok']
    assert len(db.fetch_report_files()) == 1
    assert not db.fetch_company_metrics(db.fetch_companies()[0]['id'])


def test_history_impact_includes_same_value_source_and_base_year_without_mutation(library):
    assert commit(batch({'revenue': 10}))['ok']
    incoming = batch({'revenue': 10}, filename='修订原文.csv')
    message = {'question': '营收是多少？', 'result': {'parsed': {'companies': [COMPANY], 'years': [2024], 'metrics': ['revenue']},
                'answer': '旧答案', 'audit_snapshot': {'status': 'passed'}, 'data_snapshot': {COMPANY: [{'year': 2024, 'revenue': 10}]}}}
    base = {'question': '2025同比？', 'result': {'parsed': {'companies': [COMPANY], 'years': [2025], 'metrics': ['revenue']},
            'presentation_snapshot': {'company': COMPANY, 'metric': 'revenue', 'year': 2025, 'base_year': 2024}}}
    unrelated = {'question': '利润是多少？', 'result': {'parsed': {'companies': [COMPANY], 'years': [2024], 'metrics': ['net_profit']},
                 'data_snapshot': {COMPANY: [{'year': 2024, 'revenue': 10, 'net_profit': 5}]}}}
    state = {'active_session_id': 'current', 'chat_messages': [message, unrelated],
             'saved_sessions': [{'id': 'current', 'title': '同一会话', 'messages': [deepcopy(message), deepcopy(unrelated)]},
                                {'id': 'old', 'title': '年度比较', 'messages': [base]}], 'llm_key_DeepSeek': 'do-not-copy'}
    before = deepcopy(state)
    preview = build_import_change_preview(incoming, overwrite_existing=True, state=state)
    assert len(preview['affected_answers']) == 2
    assert all('来源更新' in item['reasons'][0] for item in preview['affected_answers'])
    assert state == before
    assert 'do-not-copy' not in json.dumps(preview, ensure_ascii=False)
    assert not build_import_change_preview(incoming, state=state)['affected_answers']
    assert commit(incoming, True, preview)['ok']
    assert state == before


def test_attribution_explicit_driver_and_missing_probe_base_year_are_dependencies(library):
    incoming = batch({'net_profit': -1, 'operating_cashflow': 4}, year=2023)
    state = {'chat_messages': [
        {'question': '利润归因', 'result': {'parsed': {'companies': [COMPANY], 'years': [2024], 'metrics': ['net_profit']},
         'attribution': {'company': COMPANY, 'drivers': [{'metric': 'operating_cashflow',
                           'quantitative_sources': [{'year': 2023}]}]}}},
        {'question': '不同利润口径增幅', 'result': {'probe_request': {'company_names': [COMPANY], 'year': 2024, 'metric': 'net_profit'},
         'scope_probe': {'evidence_snapshot': [], 'condition_snapshot': {'comparison': 'growth', 'base_year': 2023}}}},
    ]}
    preview = build_import_change_preview(incoming, state=state)
    assert len(preview['affected_answers']) == 2
    assert '经营现金流' in preview['affected_answers'][0]['reasons'][0]
    assert '归母净利润' in preview['affected_answers'][1]['reasons'][0]


def test_public_visitors_have_isolated_previews_and_confirmation_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv('FINANCIAL_DEPLOYMENT', 'public')
    monkeypatch.setenv('FINANCIAL_DATA_DIR', str(tmp_path))
    first, second = uuid4().hex, uuid4().hex
    configure_workspace('main')
    try:
        configure_public_session(first)
        db.init_db()
        assert commit(batch({'revenue': 10}))['ok']
        incoming = batch({'revenue': 20})
        preview_first = build_import_change_preview(incoming, overwrite_existing=True)
        configure_public_session(second)
        db.init_db()
        preview_second = build_import_change_preview(incoming, overwrite_existing=True)
        assert rows(preview_second)['revenue']['old_value'] is None
        assert preview_first['current_signature'] != preview_second['current_signature']
        assert commit(incoming, True, preview_first)['status'] == 'preview_stale'
        assert not db.fetch_companies()
        assert commit(incoming, True, preview_second)['ok']
        configure_public_session(first)
        assert current()['revenue'] == 10
    finally:
        configure_workspace('main')


