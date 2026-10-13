"""Parse in memory, review, then explicitly commit with per-metric provenance."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import re
from pathlib import Path
from uuid import uuid4

from core.db import insert_report_file, upsert_company, upsert_metric, get_conn, atomic_write
from core.import_validation import METRICS, NUMERIC_METRICS, validate_records
from core.parsers import parse_pdf, parse_tabular
from core.scope_probe_import import annotate_probe_records, persist_probe_facts
from core.docx_support import parse_docx
from core.online_disclosure import download_pdf, COMPANY_CODE_MAP
from core.storage import get_data_root, require_write_access, is_session_workspace, check_session_upload_capacity

ROOT = Path(__file__).resolve().parents[1]
UPLOAD_DIR = get_data_root() / 'uploads'
_INITIAL_UPLOAD_DIR = UPLOAD_DIR


def _upload_dir() -> Path:
    return Path(UPLOAD_DIR) if UPLOAD_DIR != _INITIAL_UPLOAD_DIR and not is_session_workspace() else get_data_root() / 'uploads'


def save_upload(file_name: str, content: bytes, import_id: str | None = None) -> Path:
    require_write_access()
    check_session_upload_capacity(len(content))
    safe_name = Path(str(file_name).replace('\\', '/')).name
    if safe_name in ('', '.', '..'):
        raise ValueError('文件名无效。')
    identifier = import_id or uuid4().hex
    if not identifier.isalnum() or len(identifier) > 64:
        raise ValueError('导入版本标识无效。')
    path = _upload_dir() / identifier / safe_name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as handle:
        handle.write(content)
    return path


def _fingerprint(batch: dict) -> str:
    fields = {key: batch.get(key) for key in ('file_name', 'records', 'errors', 'companies', 'import_id',
              'source_url', 'stock_code', 'expected_company', 'source_label', 'report_title', 'report_type',
              'report_year', 'content_sha256', 'document_errors', 'can_save_document', 'scope_probe_facts')}
    return sha256(json.dumps(fields, ensure_ascii=False, sort_keys=True, default=str).encode('utf-8')).hexdigest()


def prepare_import(file_name: str, content: bytes, manual_company_name: str | None = None,
                   money_unit: str | None = None, percent_unit: str | None = None,
                   year_hint: int | None = None, source_url: str | None = None,
                   stock_code: str | None = None, source_label: str = 'upload',
                   report_title: str | None = None, expected_company: str | None = None,
                   report_type: str | None = None, *, probe_currency: str | None = None,
                   probe_statement_scope: str | None = None,
                   probe_report_version: str | None = None) -> dict:
    """Return records/errors/warnings/can_commit and the original bytes without writing.

    Units in cells/headers take precedence. UI unit selections fill unknown units
    only. Show normalized values and provenance before ``commit_import``.
    """
    safe_name = Path(str(file_name).replace('\\', '/')).name
    batch = {'file_name': safe_name, 'content': content, 'content_sha256': sha256(content).hexdigest(),
             'import_id': uuid4().hex, 'records': [], 'companies': [], 'errors': [], 'warnings': [],
             'can_commit': False, 'snippet': '', 'source_url': source_url, 'stock_code': stock_code,
             'source_label': source_label, 'report_title': report_title, 'report_year': None,
             'expected_company': expected_company, 'report_type': report_type,
             'document_errors': [], 'can_save_document': False, 'scope_probe_facts': []}
    suffix = Path(safe_name).suffix.lower()
    if suffix == '.doc':
        batch['errors'].append('旧版.doc不支持直接解析，请先用Word另存为.docx。')
    elif suffix not in ('.pdf', '.docx', '.csv', '.xls', '.xlsx'):
        batch['errors'].append('仅支持 PDF、DOCX、CSV、XLS 和 XLSX 文件。')
    elif not content or len(content) > 30 * 1024 * 1024:
        batch['errors'].append('文件为空或超过 30 MB 的本地解析限制。')
    else:
        try:
            if suffix in ('.pdf', '.docx'):
                parser = parse_docx if suffix == '.docx' else parse_pdf
                company, records, warnings, snippet = parser(safe_name, content, money_unit=money_unit,
                    percent_unit=percent_unit, year_hint=year_hint)
                batch['snippet'] = snippet
                # Comparison columns have metric years; they are not separate reports.
                batch['report_year'] = records[0].get('_report_year') if records else None
                document_header = snippet[:1600]
                period_match = re.search(r'(20\d{2})\s*年\s*(半年度|上半年|下半年|第[一二三四1234]季度)', document_header)
                if period_match:
                    batch['report_type'] = '半年度报告' if period_match.group(2) in ('半年度', '上半年', '下半年') else '季度报告'
                    batch['report_year'] = int(period_match.group(1))
                elif not batch['report_type'] and batch['report_year']:
                    batch['report_type'] = '年度报告'
            else:
                company, records, warnings = parse_tabular(safe_name, content, money_unit=money_unit, percent_unit=percent_unit)
            batch['records'], batch['warnings'] = records, warnings
            explicit_companies = {str(record['company_name']).strip() for record in records if record.get('company_name')}
            identity_errors, identity_warnings = _validate_identity(explicit_companies, records, expected_company, stock_code)
            batch['errors'].extend(identity_errors)
            batch['warnings'].extend(identity_warnings)
            if suffix == '.pdf' and report_title:
                title_year = re.search(r'(20\d{2})\s*年?\s*(?:年度报告|年度财务报告|年报)', report_title)
                if title_year and batch['report_year'] and int(title_year.group(1)) != batch['report_year']:
                    batch['errors'].append(f'公告标题年度 {title_year.group(1)} 与 PDF 正文报告年度 {batch["report_year"]} 冲突，请核对所选原件。')
            manual_name = (manual_company_name or '').strip()
            if manual_name and explicit_companies and explicit_companies != {manual_name}:
                batch['errors'].append('手动企业名称与文件中的企业归属冲突；请清空手动名称或修正文件，不会将多家公司合并为一家。')
            for record in records:
                # A blank company cell is not a continuation of the first company.
                record['company_name'] = record.get('company_name') or manual_name or None
                if suffix not in ('.pdf', '.docx') and year_hint and record.get('year') and int(year_hint) != record['year']:
                    batch['errors'].append(f'表格年度 {record["year"]} 与指定年度 {year_hint} 冲突，不重写原始年份。')
                for source in record.get('_provenance', {}).values():
                    source.update({'import_id': batch['import_id'], 'source_url': source_url, 'file_name': safe_name})
                money_values = [record.get(key) for key in ('revenue', 'net_profit', 'operating_cashflow')]
                if all(value is not None for value in money_values) and len(set(money_values)) == 1:
                    batch['warnings'].append(f'{record.get("company_name") or "未确认企业"} {record.get("year")}：营收、利润和现金流数值完全相同，请重点核对三个不同原始单元格。')
                if any(record.get(key) is None for key in ('revenue', 'net_profit', 'operating_cashflow', 'roe', 'debt_ratio')):
                    batch['warnings'].append(f'{record.get("company_name") or "未确认企业"} {record.get("year") or "未知年度"}：指标不完整，仅导入已核验字段；缺失字段不会填零或使用其他数据补齐。')
            facts, probe_warnings, probe_errors = annotate_probe_records(records,
                currency=probe_currency, statement_scope=probe_statement_scope,
                report_version=probe_report_version)
            batch['scope_probe_facts'] = facts
            batch['warnings'].extend(probe_warnings)
            batch['errors'].extend(probe_errors)
            batch['errors'].extend(validate_records(records))
            if batch['report_type'] in ('半年度报告', '季度报告'):
                batch['errors'].append('该文件为半年/季度报告，当前年度指标库不支持直接作为全年入库。')
        except Exception as error:
            batch['errors'].append(f'解析未完成：{error}')
    batch['companies'] = sorted({record['company_name'] for record in batch['records'] if record.get('company_name')})
    batch['errors'] = list(dict.fromkeys(batch['errors']))
    batch['warnings'] = list(dict.fromkeys(batch['warnings']))
    batch['can_commit'] = bool(batch['records']) and not batch['errors']
    # A readable document can be retained without pretending its unsupported
    # period or absent numeric fields are annual financial facts. Other errors
    # (identity, units, conflicting values, broken files) remain blockers.
    batch['document_errors'] = [error for error in batch['errors']
        if '没有可核验的数值指标，不能创建空年度记录' not in error
        and '该文件为半年/季度报告，当前年度指标库不支持直接作为全年入库' not in error
        and not (batch['report_year'] and '报告年度缺失或无效。' in error)]
    numeric = any(row.get(key) is not None for row in batch['records'] for key in NUMERIC_METRICS)
    batch['can_save_document'] = bool(suffix in ('.pdf', '.docx') and batch['snippet'] and batch['companies']
        and batch['report_year'] and not batch['document_errors']
        and (not numeric or batch['report_type'] in ('半年度报告', '季度报告')))
    batch['_fingerprint'] = _fingerprint(batch)
    return batch


def _validate_identity(actual_names: set[str], records: list[dict], expected_company: str | None,
                       stock_code: str | None) -> tuple[list[str], list[str]]:
    """An exchange label is an expectation, never a replacement company name."""
    errors, warnings = [], []
    if not expected_company and not stock_code:
        return errors, warnings
    if len(actual_names) != 1:
        return ['无法从文件唯一识别公告主体，不能按公告元数据替换企业；请下载原件，从本地上传入口核对企业后导入。'], []
    actual = next(iter(actual_names))
    compact = lambda value: re.sub(r'\s+', '', str(value or ''))
    actual_code = COMPANY_CODE_MAP.get(actual)
    file_codes = {str(code) for record in records for code in record.get('_issuer_stock_codes', [])}
    if actual_code and file_codes and actual_code not in file_codes:
        errors.append(f'PDF 主体“{actual}”与文件所列股票代码冲突，请核对原件。')
    expected = compact(expected_company)
    expected_code = COMPANY_CODE_MAP.get(expected_company or '') or COMPANY_CODE_MAP.get(expected)
    if expected and compact(actual) != expected:
        if actual_code and expected_code:
            if actual_code != expected_code:
                errors.append(f'公告企业“{expected_company}”与 PDF 主体“{actual}”的股票代码不一致，已阻止入库。')
        elif expected in compact(actual):
            warnings.append(f'公告简称“{expected_company}”出现在 PDF 主体“{actual}”中，但缺少已知代码映射，请人工核对；入库名称仍使用 PDF 主体全称。')
        else:
            errors.append(f'无法确认公告企业“{expected_company}”与 PDF 主体“{actual}”一致；请下载原件，从本地上传入口核验后导入。')
    if stock_code:
        code = str(stock_code).strip()
        if not re.fullmatch(r'\d{6}', code):
            errors.append('公告股票代码不是明确的六位代码，不能写入企业身份。')
        elif actual_code:
            if code != actual_code:
                errors.append(f'指定股票代码 {code} 与 PDF 主体“{actual}”的已知代码 {actual_code} 不一致，已阻止入库。')
        else:
            if code not in file_codes:
                errors.append(f'无法在 PDF 主体资料中核实股票代码 {code}；请下载原件，通过本地上传入口核验企业后导入，系统不会附加未经证实的股票代码。')
        if expected_code and code != expected_code:
            errors.append('公告简称和公告股票代码不一致，已阻止入库。')
    return list(dict.fromkeys(errors)), list(dict.fromkeys(warnings))


def _source_type(batch: dict) -> str:
    if batch.get('source_label') == 'online_disclosure':
        return 'exchange'
    if batch.get('source_url') or batch.get('source_label') == 'online_pdf_url':
        return 'direct_url'
    return 'upload'


def _verify_committed_import(result: dict, targets: list[tuple], *, document_only: bool) -> None:
    """Use the active transaction before commit and a fresh connection afterward."""
    conn = get_conn()
    try:
        for cid, year, metric, value in targets:
            row = conn.execute(f'SELECT {metric} FROM financial_metrics WHERE company_id=? AND year=?',
                               (cid, year)).fetchone()
            source = conn.execute('''SELECT value_json,file_path,source_url FROM metric_sources
                WHERE company_id=? AND year=? AND metric=? AND import_id=? AND is_current=1''',
                (cid, year, metric, result['import_id'])).fetchone()
            if (row is None or row[metric] != value or source is None
                    or json.loads(source['value_json']) != value
                    or source['file_path'] != result['file_path']
                    or source['source_url'] != result['source_url']):
                raise RuntimeError('提交核验未通过：年度指标或逐指标来源缺失。')
        for report_id in result['report_file_ids']:
            report = conn.execute('''SELECT rf.*,c.name AS company_name FROM report_files rf
                JOIN companies c ON c.id=rf.company_id WHERE rf.id=? AND rf.import_id=?''',
                (report_id, result['import_id'])).fetchone()
            expected_status = 'document_only' if document_only else 'imported'
            if (report is None or report['ingest_status'] != expected_status
                    or report['company_name'] not in result['company_names']
                    or report['file_path'] != result['file_path']
                    or report['source_type'] != result['source_type']
                    or report['source_url'] != result['source_url']
                    or report['content_hash'] != result['content_hash']
                    or report['metric_count'] != sum(1 for target in targets
                        if target[0] == report['company_id'] and (report['file_type'] in ('pdf', 'docx') or target[1] == report['report_year']))):
                raise RuntimeError('提交核验未通过：企业或来源文件登记缺失。')
        if not result['report_file_ids'] or (not document_only and not any(t[2] in NUMERIC_METRICS for t in targets)):
            raise RuntimeError('提交核验未通过：缺少报告或可分析年度指标。')
    finally:
        conn.close()


def commit_import(batch: dict, confirmed: bool = False, overwrite_existing: bool = False,
                  *, document_only: bool = False, expected_current_signature: str | None = None) -> dict:
    """Commit companies, annual facts, sources and reports as one atomic unit.

    ok is true only for verified structured imports. An explicit document-only
    operation and an unchanged import have separate statuses. File cleanup is
    limited to a file created by this invocation, and never runs after commit.
    """
    result = {'ok': False, 'status': 'failed', 'company_names': [], 'companies': [],
              'imported_count': 0, 'imported_records': 0, 'inserted_metrics': 0, 'updated_metrics': 0,
              'report_file_id': None, 'report_file_ids': [], 'document_added': False, 'committed': False,
              'source_type': _source_type(batch), 'source_url': batch.get('source_url'),
              'error_message': '', 'skipped_metrics': [], 'warnings': list(batch.get('warnings', [])),
              'errors': [], 'file_path': None, 'import_id': batch.get('import_id'),
              'content_hash': batch.get('content_sha256'), 'probe_fact_count': 0}

    def reject(errors):
        result['errors'] = list(dict.fromkeys(str(error) for error in errors))
        result['error_message'] = '；'.join(result['errors'])
        return result

    if not confirmed:
        return reject(['请先查看解析预览，确认企业、年度、单位、数值和来源后再入库。'])
    errors = list(batch.get('document_errors' if document_only else 'errors', []))
    if document_only:
        if not batch.get('can_save_document'):
            errors.append('当前文件不满足仅文档保存条件，请核实企业、报告年份和文件内容。')
    else:
        errors.extend(validate_records(batch.get('records', [])))
        if batch.get('report_type') in ('半年度报告', '季度报告'):
            errors.append('半年/季度报告不能作为年度指标入库。')
    if _fingerprint(batch) != batch.get('_fingerprint'):
        errors.append('预览内容已改变，请重新解析并确认后入库。')
    if sha256(batch.get('content', b'')).hexdigest() != batch.get('content_sha256'):
        errors.append('原始文件与预览不一致，请重新解析。')
    if errors:
        return reject(errors)

    path = None
    targets: list[tuple] = []
    try:
        require_write_access()
        with atomic_write():
            conn = get_conn()
            # Double clicks and reruns reuse the import ID, not a second file.
            duplicate = conn.execute('SELECT id FROM report_files WHERE import_id=?', (batch['import_id'],)).fetchall()
            if duplicate:
                result['status'] = 'unchanged'
                result['warnings'].append('本次预览对应的文件已经提交，未重复写入。')
                return result
            if expected_current_signature is not None:
                from core.import_changes import current_import_signature
                current_signature = current_import_signature(batch, overwrite_existing=overwrite_existing)
                if current_signature != expected_current_signature:
                    result['status'] = 'preview_stale'
                    return reject(['当前资料已在预览后改变，本次未写入。请重新核对变化预览并确认。'])
            companies = list(conn.execute('SELECT id,name,stock_code FROM companies'))
            company_ids = {}
            planned = []
            for record in batch.get('records', []):
                name = record['company_name']
                stock_code = batch.get('stock_code') if len(batch['companies']) == 1 else None
                current_company = next((row for row in companies if stock_code and row['stock_code'] == stock_code), None)
                if current_company is None:
                    current_company = next((row for row in companies if row['name'] == name), None)
                company_ids[name] = current_company['id'] if current_company else None
                current = conn.execute('SELECT * FROM financial_metrics WHERE company_id=? AND year=?',
                    (current_company['id'], record['year'])).fetchone() if current_company else None
                selected = deepcopy(record)
                selected['_provenance'] = deepcopy(record.get('_provenance') or {})
                changes = {}
                for metric in METRICS:
                    value = selected.get(metric)
                    old_value = current[metric] if current is not None else None
                    if value is None or (not overwrite_existing and old_value is not None):
                        if value is not None:
                            result['skipped_metrics'].append(f'{name} {record["year"]} {metric}')
                        selected.pop(metric, None)
                        selected['_provenance'].pop(metric, None)
                    else:
                        changes[metric] = 'inserted' if old_value is None else 'updated'
                if changes:
                    planned.append((selected, changes))
            if not document_only and not any(record.get(key) is not None for record, _ in planned for key in NUMERIC_METRICS):
                result['status'] = 'unchanged'
                result['warnings'].append('已有指标全部保留，未写入新的年度指标或来源文件；如需更新请明确启用覆盖。')
                return result
            path = save_upload(batch['file_name'], batch['content'], batch['import_id'])
            result['file_path'] = str(path)
            relevant_names = batch['companies'] if document_only else list(dict.fromkeys(row['company_name'] for row, _ in planned))
            for name in relevant_names:
                cid = upsert_company(name, stock_code=batch.get('stock_code') if len(batch['companies']) == 1 else None,
                                     source=batch.get('source_label') or 'upload')
                company_ids[name] = cid
                canonical_name = conn.execute('SELECT name FROM companies WHERE id=?', (cid,)).fetchone()['name']
                if canonical_name not in result['company_names']:
                    result['company_names'].append(canonical_name)
            company_counts = {}
            record_counts = {}
            if not document_only:
                for record, changes in planned:
                    cid = company_ids[record['company_name']]
                    for metric in changes:
                        source = record['_provenance'].setdefault(metric, {})
                        source.update(file_path=str(path), file_name=batch['file_name'],
                                      import_id=batch['import_id'], source_url=batch.get('source_url'))
                    record['raw_source'] = batch['file_name']
                    record['import_id'] = batch['import_id']
                    upsert_metric(cid, record, only_missing=not overwrite_existing)
                    result['probe_fact_count'] += persist_probe_facts(cid, record)
                    for metric, operation in changes.items():
                        result[operation + '_metrics'] += 1
                        targets.append((cid, record['year'], metric, record[metric]))
                    result['imported_records'] += 1
                    company_counts[cid] = company_counts.get(cid, 0) + len(changes)
                    record_counts[(cid, record['year'])] = len(changes)
            result['imported_count'] = result['inserted_metrics'] + result['updated_metrics']
            file_type = 'pdf' if batch['file_name'].lower().endswith('.pdf') else 'docx' if batch['file_name'].lower().endswith('.docx') else 'tabular'
            note = f'导入版本：{batch["import_id"]}；已确认企业及来源。'
            if file_type in ('pdf', 'docx'):
                metric_years = '、'.join(str(year) for year in sorted({row['year'] for row in batch['records']}))
                note += f' 文档报告年度：{batch.get("report_year") or "正文未识别"}；表中指标年度：{metric_years}。比较年度列不作为其他年度年报登记。'
                report_groups = [(cid, batch.get('report_year'), company_counts.get(cid, 0))
                                 for cid in dict.fromkeys(company_ids[name] for name in relevant_names)]
            else:
                report_groups = [(cid, year, count) for (cid, year), count in record_counts.items()]
            for cid, report_year, metric_count in report_groups:
                report_id = insert_report_file(cid, batch['file_name'], report_year, file_type, str(path),
                    'document_only' if document_only else 'reviewed', note,
                    source_type=result['source_type'], source_url=batch.get('source_url'),
                    report_title=batch.get('report_title') or batch['file_name'],
                    report_type=batch.get('report_type') or ('年度指标表' if file_type == 'tabular' else None),
                    import_id=batch['import_id'], content_hash=batch['content_sha256'],
                    ingest_status='document_only' if document_only else 'imported', metric_count=metric_count)
                result['report_file_ids'].append(report_id)
            result['report_file_id'] = result['report_file_ids'][0]
            result['companies'] = list(result['company_names'])
            _verify_committed_import(result, targets, document_only=document_only)
        result['committed'] = True
        result['document_added'] = True
    except Exception as exc:
        if path is not None:
            path.unlink(missing_ok=True)
        result.update(company_names=[], companies=[], imported_count=0, imported_records=0,
                      inserted_metrics=0, updated_metrics=0, report_file_id=None, report_file_ids=[],
                      document_added=False, file_path=None, probe_fact_count=0)
        return reject([str(exc)])
    try:
        _verify_committed_import(result, targets, document_only=document_only)
    except Exception as exc:
        # A committed transaction cannot be described as rolled back just because
        # the independent read-back failed. Keep all saved files and report IDs.
        result['status'] = 'verification_failed'
        return reject([f'事务已提交，但结果复核未完成，请刷新数据中心确认：{exc}'])
    if result['skipped_metrics']:
        result['warnings'].append('默认保留已有指标及来源：' + '、'.join(result['skipped_metrics']))
    result['status'] = 'document_only' if document_only else 'success'
    result['ok'] = not document_only
    return result


def _legacy_commit(batch: dict, confirmed: bool, overwrite_existing: bool) -> dict:
    result = commit_import(batch, confirmed=confirmed, overwrite_existing=overwrite_existing)
    if not result['ok']:
        raise ValueError('；'.join(result['errors'] or result['warnings']))
    return result


def ingest_tabular_file(file_name: str, content: bytes, manual_company_name: str | None = None,
                        *, confirmed: bool = False, overwrite_existing: bool = False,
                        money_unit: str | None = None, percent_unit: str | None = None) -> tuple[str | None, list[str]]:
    result = _legacy_commit(prepare_import(file_name, content, manual_company_name, money_unit, percent_unit), confirmed, overwrite_existing)
    return '、'.join(result['companies']) or None, result['warnings']


def ingest_pdf_file(file_name: str, content: bytes, manual_company_name: str | None = None,
                    *, confirmed: bool = False, overwrite_existing: bool = False,
                    money_unit: str | None = None, percent_unit: str | None = None) -> tuple[str | None, list[str]]:
    result = _legacy_commit(prepare_import(file_name, content, manual_company_name, money_unit, percent_unit), confirmed, overwrite_existing)
    return '、'.join(result['companies']) or None, result['warnings']


def ingest_online_pdf_url(url: str, manual_company_name: str | None = None, stock_code: str | None = None,
    source_label: str = 'online_pdf_url', overwrite_existing: bool = False, year_hint: int | None = None,
    report_title: str | None = None, *, confirmed: bool = False,
    money_unit: str | None = None, percent_unit: str | None = None) -> tuple[str | None, list[str], str]:
    file_name, content = download_pdf(url)
    return ingest_online_pdf_bytes(file_name, content, manual_company_name, stock_code, source_label,
        overwrite_existing, year_hint, report_title, url, confirmed=confirmed, money_unit=money_unit,
        percent_unit=percent_unit)


def ingest_online_pdf_bytes(file_name: str, content: bytes, manual_company_name: str | None = None,
    stock_code: str | None = None, source_label: str = 'browser_uploaded_pdf', overwrite_existing: bool = False,
    year_hint: int | None = None, report_title: str | None = None, source_url: str | None = None,
    *, confirmed: bool = False, money_unit: str | None = None,
    percent_unit: str | None = None) -> tuple[str | None, list[str], str]:
    batch = prepare_import(file_name, content, manual_company_name, money_unit, percent_unit, year_hint,
                           source_url, stock_code, source_label, report_title)
    result = _legacy_commit(batch, confirmed, overwrite_existing)
    return '、'.join(result['companies']) or None, result['warnings'], str(result['file_path'] or '')
