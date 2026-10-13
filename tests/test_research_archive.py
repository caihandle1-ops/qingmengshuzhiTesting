"""Real CSV ingestion, isolated visitor restore, and hostile/failed imports."""
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from uuid import uuid4
import zipfile

import pytest

from core import db, service, research_archive as archive
from core.storage import configure_workspace, configure_public_session, get_data_root, resolve_stored_file

COMPANY = '档案测试科技股份有限公司'


@pytest.fixture
def public(tmp_path, monkeypatch):
    monkeypatch.setenv('FINANCIAL_DEPLOYMENT', 'public')
    monkeypatch.setenv('FINANCIAL_DATA_DIR', str(tmp_path))
    configure_workspace('main')
    configure_public_session(uuid4().hex)
    db.init_db()
    yield tmp_path
    configure_workspace('main')


def populate():
    data = ('公司,年份,营业收入（亿元）,归母净利润（亿元）\n'
            f'{COMPANY},2024,123,12\n{COMPANY},2023,100,10').encode()
    imported = service.commit_import(service.prepare_import('企业财务.csv', data), confirmed=True)
    assert imported['ok']
    company = db.fetch_companies()[0]
    source = dict(db.fetch_metric_sources(company['id'], 2024, 'revenue')[0])
    report = dict(db.fetch_report_files()[0])
    result = {'status': 'success', 'company': COMPANY, 'answer': '2024年营业收入123亿元。',
              'parsed': {'companies': [COMPANY], 'years': [2024], 'metrics': ['revenue'], 'intent': 'finance_query'},
              'data_snapshot': {COMPANY: [{'year': 2024, 'revenue': 123, 'metric_provenance': {'revenue': source}}]},
              'presentation_snapshot': {'company': COMPANY, 'year': 2024, 'base_year': 2023, 'metric': 'revenue',
                                        'records': [{'year': 2024, 'revenue': 123, 'metric_provenance': {'revenue': source}}]},
              'report_files_snapshot': [report],
              'audit_snapshot': {'status': 'conflict', 'message': '保留原有差异', 'paths': {'original': [{'status': 'pending'}]}},
              'delivery_state': {'status': 'blocked', 'deliverable': False},
              'retrieval_result': {'citations': [{'document_id': report['id'], 'snippet': '营业收入', 'file_name': report['file_name']}]}}
    message = {'question': '2024年营业收入是多少？', 'effective_question': '2024年营业收入是多少？',
               'applied_year': 2024, 'selected_compare': None, 'result': result}
    state = {'active_session_id': uuid4().hex, '_public_database_session': uuid4().hex,
             'session_title': '年度营收研究', 'selected_main': COMPANY, 'selected_cmp': None,
             'chat_messages': [message], 'conversation_context': {'primary_company': COMPANY, 'years': [2024], 'metrics': ['revenue']},
             'navigation_inputs': {'enterprise_chart_type': '折线图', 'enterprise_chart_metrics': ['revenue']},
             'scope_condition_record': {'scope_company': COMPANY, 'scope_year': 2024, 'scope_metric': 'revenue', 'scope_mode': '单条来源核验'},
             'scope_manual_signature': ('单条来源核验', COMPANY, 2024, 'revenue', None, None),
             'scope_manual_record': {'scope_manual_policy': '已查原件，政策差异待确认', 'scope_manual_confirmed': False},
             'counter_review_note': '请继续复核原始披露',
             'saved_sessions': [{'id': uuid4().hex, 'title': '营收', 'messages': [deepcopy(message)], 'context': {}, 'selected_main': COMPANY}],
             'llm_config': {'api_key': 'sk-private-ABC1234567890', 'base_url': 'https://private.example/api'},
             'llm_api_key': 'sk-private-ABC1234567890'}
    from core.review_queue import save_review_request
    save_review_request({'schema_version': 'countercheck/1', 'status': 'conflict', 'source': source}, note='人工备注保留')
    return state, data, imported


def new_session():
    configure_public_session(uuid4().hex)
    db.init_db()
    return get_data_root()


def restore(content, **kwargs):
    return archive.restore_research_archive(content, confirmed=True, expected_sha256=sha256(content).hexdigest(), **kwargs)


def rewrite(content, mutate=None, *, extra=None):
    with zipfile.ZipFile(BytesIO(content)) as z:
        parts = {name: z.read(name) for name in z.namelist()}
    manifest = json.loads(parts['manifest.json'])
    if mutate:
        workspace = json.loads(parts['workspace.json'])
        research = json.loads(parts['research.json'])
        mutate(workspace, research)
        parts['workspace.json'] = archive._json(workspace)
        parts['research.json'] = archive._json(research)
    for entry in manifest['entries']:
        entry.update(size=len(parts[entry['name']]), sha256=sha256(parts[entry['name']]).hexdigest())
    parts['manifest.json'] = archive._json(manifest)
    result = BytesIO()
    with zipfile.ZipFile(result, 'w') as z:
        for name, data in parts.items():
            z.writestr(name, data)
        if extra:
            z.writestr(*extra)
    return result.getvalue()


def test_real_upload_restores_into_new_visitor_with_sources_and_history(public):
    state, data, original = populate()
    old_root, old_session = get_data_root(), state['_public_database_session']
    packaged = archive.build_research_archive(state)
    with zipfile.ZipFile(BytesIO(packaged['zip_bytes'])) as z:
        texts = '\n'.join(z.read(name).decode() for name in z.namelist() if name.endswith('.json'))
    assert 'sk-private' not in texts and 'private.example' not in texts
    assert old_session not in texts and state['active_session_id'] not in texts
    assert str(old_root) not in texts and original['import_id'] not in texts
    new_root = new_session()
    restored = restore(packaged['zip_bytes'])['state']
    assert new_root != old_root and restored['active_session_id'] != state['active_session_id']
    assert '_public_database_session' not in restored and 'llm_config' not in restored
    company = db.fetch_companies()[0]
    assert company['name'] == COMPANY
    annual = dict(db.fetch_company_metrics(company['id'])[-1])
    assert annual['year'] == 2024 and annual['revenue'] == 123
    source = dict(db.fetch_metric_sources(company['id'], 2024, 'revenue')[0])
    path = resolve_stored_file(source['file_path'])
    assert path and path.is_relative_to(new_root) and path.read_bytes() == data
    assert source['import_id'] != original['import_id']
    assert resolve_stored_file(original['file_path']) is None
    snapshot = restored['chat_messages'][0]['result']
    frozen = snapshot['data_snapshot'][COMPANY][0]['metric_provenance']['revenue']
    assert frozen['file_path'] == source['file_path'] and frozen['import_id'] == source['import_id']
    assert snapshot['retrieval_result']['citations'][0]['file_path'] == source['file_path']
    assert snapshot['audit_snapshot']['status'] == 'conflict' and snapshot['delivery_state']['deliverable'] is False
    assert restored['enterprise_chart_type'] == '折线图'
    assert restored['scope_manual_signature'] == state['scope_manual_signature']
    assert restored['scope_manual_record'] == state['scope_manual_record']
    from core.review_queue import list_review_requests
    review = list_review_requests()[0]
    assert review['status'] == 'pending_review' and review['note'] == '人工备注保留'
    assert review['snapshot']['source']['file_path'] == source['file_path']
    new_session()
    assert not db.fetch_companies() and resolve_stored_file(str(path)) is None


def test_structure_only_export_has_explicit_missing_originals(public):
    state, _, _ = populate()
    package = archive.build_research_archive(state, include_originals=False)
    assert package['summary']['未附原件'] == 1 and package['warnings']
    new_session()
    result = restore(package['zip_bytes'])
    source = db.fetch_metric_sources(db.fetch_companies()[0]['id'])[0]
    assert source['file_path'] is None
    assert result['state']['chat_messages'][0]['result']['delivery_state']['status'] == 'blocked'


def test_samples_allowed_but_user_workspace_never_overwritten(public):
    state, _, _ = populate()
    package = archive.build_research_archive(state)['zip_bytes']
    before = db.get_db_path().read_bytes()
    with pytest.raises(archive.ArchiveError, match='已有用户'):
        restore(package)
    assert db.get_db_path().read_bytes() == before
    new_session()
    from core.seed import seed_sample_data
    seed_sample_data()
    from core.builtin_catalog import get_builtin_companies
    assert len(db.fetch_companies()) == len(get_builtin_companies())
    restore(package)
    assert [row['name'] for row in db.fetch_companies()] == [COMPANY]


def test_builtins_roundtrip_does_not_relabel_samples(public):
    from core.seed import seed_sample_data
    seed_sample_data()
    package = archive.build_research_archive({})['zip_bytes']
    checked = archive.validate_research_archive(package)
    from core.builtin_catalog import get_builtin_companies
    assert checked['summary']['企业'] == len(get_builtin_companies()) and checked['summary']['原件'] == 0
    new_session()
    restore(package)
    assert all(row['import_id'] == 'builtin-v1' for company in db.fetch_companies() for row in db.fetch_metric_sources(company['id']))


@pytest.mark.parametrize('extra', [('../outside.csv', b'bad'), ('/outside.csv', b'bad'), ('originals\\bad.csv', b'bad'), ('research.json', b'{}')])
def test_malicious_zip_paths_and_duplicate_members_never_write(public, extra):
    state, _, _ = populate()
    package = rewrite(archive.build_research_archive(state)['zip_bytes'], extra=extra)
    root = new_session()
    before = db.get_db_path().read_bytes()
    with pytest.raises(archive.ArchiveError):
        restore(package)
    assert db.get_db_path().read_bytes() == before and not (root / 'uploads').exists()


@pytest.mark.parametrize('mutation', [
    lambda w, r: w['financial_metrics'][0].update(revenue=987),
    lambda w, r: w['metric_sources'][0].update(file_ref='f999'),
    lambda w, r: w['companies'][0].update(name=''),
    lambda w, r: r['settings'].update(llm_api_key='stolen-key'),
    lambda w, r: r['messages'][0]['result'].update(api_key='stolen-key'),
    lambda w, r: w['metric_sources'][0].update(import_ref='builtin-v1'),
    lambda w, r: r['settings'].update(selected_main={}),
    lambda w, r: r['messages'][0]['result'].update(presentation_snapshot='not-a-snapshot'),
    lambda w, r: r.update(sessions=[{'messages': [], 'context': {}}]),
    lambda w, r: r['sessions'][0].update(title={}),
    lambda w, r: r['messages'][0]['result'].update(parsed={'companies': [{}]}),
])
def test_invalid_schema_or_identity_never_writes(public, mutation):
    state, _, _ = populate()
    package = rewrite(archive.build_research_archive(state)['zip_bytes'], mutate=mutation)
    root = new_session()
    before = db.get_db_path().read_bytes()
    with pytest.raises(archive.ArchiveError):
        restore(package)
    assert db.get_db_path().read_bytes() == before and not (root / 'uploads').exists()


def test_mismatched_hash_unconfirmed_and_changed_preview_rejected(public):
    state, _, _ = populate()
    package = archive.build_research_archive(state)['zip_bytes']
    new_session()
    with pytest.raises(archive.ArchiveError, match='确认'):
        archive.restore_research_archive(package)
    with pytest.raises(archive.ArchiveError, match='变化'):
        archive.restore_research_archive(package, confirmed=True, expected_sha256='0' * 64)
    with zipfile.ZipFile(BytesIO(package)) as z:
        parts = {name: z.read(name) for name in z.namelist()}
    parts['research.json'] += b' '
    altered = BytesIO()
    with zipfile.ZipFile(altered, 'w') as z:
        for name, data in parts.items():
            z.writestr(name, data)
    with pytest.raises(archive.ArchiveError, match='完整性'):
        restore(altered.getvalue())
    assert not db.fetch_companies()


def test_compression_bomb_rejected_before_json_read(public):
    stream = BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr('research.json', b'0' * 1024 * 1024)
    with pytest.raises(archive.ArchiveError, match='压缩'):
        archive.validate_research_archive(stream.getvalue())


def test_failure_after_db_inserts_rolls_back_database_and_created_files(public, monkeypatch):
    state, _, _ = populate()
    package = archive.build_research_archive(state)['zip_bytes']
    root = new_session()
    from core.seed import seed_sample_data
    seed_sample_data()
    before = [dict(row) for row in db.fetch_companies()]
    original_write = archive._write_file
    def fail_review(path, content):
        if path.parent.name == 'review_requests':
            raise OSError('simulated disk write failure')
        original_write(path, content)
    monkeypatch.setattr(archive, '_write_file', fail_review)
    with pytest.raises(OSError, match='simulated'):
        restore(package)
    assert [dict(row) for row in db.fetch_companies()] == before
    assert not list((root / 'uploads').rglob('*')) and not (root / 'review_requests').exists()


def test_existing_answers_or_manual_records_block_restore(public):
    state, _, _ = populate()
    package = archive.build_research_archive(state)['zip_bytes']
    new_session()
    with pytest.raises(archive.ArchiveError, match='问答或人工备注'):
        restore(package, state={'chat_messages': state['chat_messages']})
    assert not db.fetch_companies()




def test_actual_provider_key_fields_and_embedded_url_secrets_are_removed(public):
    state, _, _ = populate()
    state['llm_key_豆包'] = 'synthetic-doubao-private-value'
    state['llm_key_DeepSeek'] = 'synthetic-deepseek-private-value'
    state['counter_review_note'] = ('连接排查 synthetic-doubao-private-value synthetic-deepseek-private-value；'
                                    '资料地址 https://finance.example/report.pdf?year=2024&access_token=synthetic-query-private-value')
    package = archive.build_research_archive(state)['zip_bytes']
    with zipfile.ZipFile(BytesIO(package)) as z:
        all_json = '\n'.join(z.read(name).decode() for name in z.namelist() if name.endswith('.json'))
    assert 'synthetic-doubao-private-value' not in all_json
    assert 'synthetic-deepseek-private-value' not in all_json
    assert 'synthetic-query-private-value' not in all_json
    assert 'https://finance.example/report.pdf?year=2024' in all_json
    hostile = rewrite(package, mutate=lambda w, r: r['settings'].update(
        counter_review_note='资料地址 https://finance.example/report.pdf?access_token=synthetic-query-private-value'))
    with pytest.raises(archive.ArchiveError, match='连接信息'):
        archive.validate_research_archive(hostile)


def test_oversize_review_is_rejected_before_restore(public):
    state, _, _ = populate()
    package = archive.build_research_archive(state)['zip_bytes']
    hostile = rewrite(package, mutate=lambda w, r: r['reviews'][0]['snapshot'].update(details=['a' * 150000] * 16))
    root = new_session()
    with pytest.raises(archive.ArchiveError, match='2 MB'):
        restore(hostile)
    assert not db.fetch_companies() and not (root / 'uploads').exists()


def test_review_rebinding_size_limit_rolls_back_near_threshold(public):
    state, _, _ = populate()
    package = archive.build_research_archive(state)['zip_bytes']
    def near_limit(workspace, research):
        review = research['reviews'][0]
        review['snapshot'] = {'schema_version': 'countercheck/1', 'details': ['a' * 100000] * 20, 'tail': ''}
        padding = archive.MAX_REVIEW - len(archive._json(review)) - 40
        assert 0 < padding < 200000
        review['snapshot']['tail'] = 'b' * padding
    near = rewrite(package, mutate=near_limit)
    archive.validate_research_archive(near)
    root = new_session()
    with pytest.raises(archive.ArchiveError, match='恢复后.*2 MB'):
        restore(near)
    assert not db.fetch_companies() and not (root / 'uploads').exists() and not (root / 'review_requests').exists()


def test_legacy_unverified_origin_is_never_promoted_by_restore(public):
    populate()
    conn = db.get_conn()
    conn.execute("UPDATE metric_sources SET import_id='legacy-unverified'")
    conn.execute("UPDATE report_files SET import_id='legacy-unverified'")
    conn.commit()
    conn.close()
    package = archive.build_research_archive({})['zip_bytes']
    new_session()
    restore(package)
    from core.data_identity import source_identity
    source = dict(db.fetch_metric_sources(db.fetch_companies()[0]['id'])[0])
    assert source['import_id'].startswith('legacy-archive-unverified-')
    assert source_identity(dict(source, source_origin='imported'), value=json.loads(source['value_json']), metric=source['metric']) == 'unknown'
