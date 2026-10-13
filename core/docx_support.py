"""Safe DOCX text/tables with paragraph and cell locations, never fake pages."""
from __future__ import annotations

import csv
import io
import re
from xml.etree import ElementTree as ET
import zipfile

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'


def extract_docx_parts(content: bytes) -> list[dict]:
    if not content or len(content) > 30 * 1024 * 1024:
        raise ValueError('Word 文件为空或超过30 MB。')
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            members = archive.infolist()
            if len(members) > 2000 or sum(item.file_size for item in members) > 100 * 1024 * 1024:
                raise ValueError('Word 解压后超过100 MB或成员过多。')
            for item in members:
                if (item.file_size > 30 * 1024 * 1024 or
                    (item.file_size > 1024 * 1024 and item.file_size / max(1, item.compress_size) > 200)):
                    raise ValueError('Word 压缩展开比例或单项大小异常。')
                if 'vbaproject' in item.filename.lower() or item.filename.lower().endswith(('.bin', '.docm')):
                    raise ValueError('不接受包含宏或嵌入二进制程序的Word文件。')
                if item.filename.endswith('.xml'):
                    xml = archive.read(item)
                    if re.search(br'<!\s*(?:DOCTYPE|ENTITY)\b', xml.replace(b'\x00', b''), re.I):
                        raise ValueError('Word XML含实体或DTD声明，已阻止解析。')
                    if item.filename == '[Content_Types].xml' and b'macroenabled' in xml.lower():
                        raise ValueError('不接受启用宏的Word文件。')
            xml = archive.read('word/document.xml')
    except (zipfile.BadZipFile, KeyError) as error:
        raise ValueError('不是有效DOCX文件；旧版.doc请先转换为.docx。') from error
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as error:
        raise ValueError('Word正文XML损坏。') from error
    body = root.find(W + 'body')
    if body is None:
        return []
    parts, paragraph, table, cells = [], 0, 0, 0
    context = ''
    for element in body:
        if element.tag == W + 'p':
            paragraph += 1
            text = ''.join(node.text or '' for node in element.iter(W + 't')).strip()
            if text:
                parts.append({'page': None, 'location': f'段落{paragraph}', 'text': text, 'kind': 'paragraph'})
                context = (context + '\n' + text)[-2000:]
        elif element.tag == W + 'tbl':
            table += 1
            rows = []
            for row in element.findall(W + 'tr'):
                values = [''.join(node.text or '' for node in cell.iter(W + 't')).strip()
                          for cell in row.findall(W + 'tc')]
                cells += len(values)
                if cells > 200000 or len(rows) > 20000:
                    raise ValueError('Word表格超过单元格或行数限制。')
                rows.append(values)
            text = '\n'.join(' | '.join(row) for row in rows)
            if text:
                parts.append({'page': None, 'location': f'表格{table}', 'text': text, 'kind': 'table',
                              'rows': rows, 'table': table, 'context': context})
    if sum(len(part['text']) for part in parts) > 5_000_000:
        raise ValueError('Word正文文本超过500万字，请拆分。')
    return parts


def parse_docx(file_name: str, content: bytes, *, money_unit=None, percent_unit=None, year_hint=None):
    # Imported lazily to avoid circular parser dependencies.
    from core.parsers import (CH_ALIASES, _base_col, _document_report_year, _extract_company, _extract_year,
                              _find_col, _metric_label, _nearest_unit, _section_score, _year, parse_tabular)
    from core.import_validation import blank, detect_money_unit, detect_percent_unit, METRICS, MONEY_METRICS, normalize_metric
    parts = extract_docx_parts(content)
    text = '\n'.join(part['text'] for part in parts)
    company = _extract_company(text, file_name)
    report_year = _document_report_year(text) or _extract_year(text) or year_hint
    document_year = _document_report_year(text)
    errors, warnings, records = [], [], []
    if document_year and year_hint and document_year != year_hint:
        errors.append('Word正文报告年度与指定年度冲突，不能重标年份。')
    for part in parts:
        if part['kind'] != 'table' or not part['rows']:
            continue
        rows, context = part['rows'], part['context']
        if _section_score(context) < 0:
            warnings.append(f'表格{part["table"]}为母公司/非全年范围，未作为年度合并事实提取。')
            continue
        header_index = next((index for index, row in enumerate(rows[:12]) if _find_col(row, CH_ALIASES['year'])), None)
        if header_index is not None:
            # Reuse strict wide-table normalization; replace temporary CSV cell
            # locations with the actual DOCX table coordinates afterwards.
            out = io.StringIO()
            csv.writer(out).writerows(rows[header_index:])
            _, wide_records, wide_warnings = parse_tabular('Word解析临时.csv', out.getvalue().encode(),
                money_unit=money_unit or _nearest_unit(context), percent_unit=percent_unit)
            for record in wide_records:
                record['raw_source'] = file_name
                record['company_name'] = record.get('company_name') or company
                for source in record['_provenance'].values():
                    match = re.search(r'!([A-Z]+)(\d+)$', source['cell'])
                    column = 0
                    for char in match.group(1):
                        column = column * 26 + ord(char) - 64
                    source.update(file_name=file_name, page=None, location_type='docx_table',
                                  cell=f'表{part["table"]}!R{int(match.group(2)) + header_index}C{column}')
                    if not source.get('currency_hint') and '人民币' in context:
                        source['currency_hint'] = 'CNY'
                    if not source.get('scope_hint') and '合并' in context:
                        source['scope_hint'] = 'consolidated'
                records.append(record)
            warnings.extend(wide_warnings)
            continue
        header_years = {}
        year_records = {}
        for row_index, row in enumerate(rows, 1):
            years = {column: _year(value) for column, value in enumerate(row) if _year(value)}
            if years and (not row or not _metric_label(row[0])[0]):
                header_years = years
                continue
            metric, _ = _metric_label(row[0]) if row else (None, 0)
            if metric == 'net_profit' and _base_col(row[0]) == '净利润' and '合并利润表' in context:
                metric = 'consolidated_net_profit'
            if not metric:
                continue
            columns = header_years or ({1: report_year} if len(row) == 2 else {})
            for column, year in columns.items():
                if column >= len(row) or blank(row[column]):
                    continue
                record = year_records.setdefault(year, {'year': year, 'company_name': company,
                    'raw_source': file_name, '_provenance': {}, '_errors': []})
                cell = f'表{part["table"]}!R{row_index}C{column + 1}'
                try:
                    source_unit = (detect_money_unit(row[0]) or _nearest_unit(context)) if metric in MONEY_METRICS else detect_percent_unit(row[0])
                    value, raw_unit, normal_unit = normalize_metric(metric, row[column], source_unit, money_unit, percent_unit)
                    if metric in record and record[metric] != value:
                        raise ValueError('同一Word表存在同年度同指标冲突，不自动选择。')
                    record[metric] = value
                    record['_provenance'][metric] = {'file_name': file_name, 'page': None, 'cell': cell,
                        'location_type': 'docx_table', 'raw_value': str(row[column]), 'raw_unit': raw_unit,
                        'normalized_unit': normal_unit, 'metric_label': row[0],
                        'currency_hint': 'CNY' if '人民币' in context else None,
                        'scope_hint': 'consolidated' if '合并' in context else None,
                        'version_hint': 'restated' if '重述' in context else None}
                except ValueError as error:
                    record['_errors'].append(f'{cell}：{error}')
        records.extend(year_records.values())
    # Multiple financial tables may supplement the same company/year; they do
    # not become duplicate annual rows. Conflicting facts remain blockers.
    merged = {}
    for record in records:
        key = (record.get('company_name'), record.get('year'))
        target = merged.setdefault(key, {'company_name': key[0], 'year': key[1], 'raw_source': file_name,
                                        '_provenance': {}, '_errors': []})
        target['_errors'].extend(record['_errors'])
        for metric in METRICS:
            if record.get(metric) is None:
                continue
            if metric in target and target[metric] != record[metric]:
                target['_errors'].append(f'Word多个表格的{metric}数值冲突，请人工复核。')
                continue
            target[metric] = record[metric]
            target['_provenance'][metric] = record['_provenance'][metric]
    records = list(merged.values()) or [{'company_name': company, 'year': report_year,
                                        'raw_source': file_name, '_provenance': {}, '_errors': []}]
    for record in records:
        record['_errors'].extend(errors)
        record['_report_year'] = report_year
        record['_issuer_stock_codes'] = []
    warnings.append('Word来源定位为段落或表格单元格；不虚构打印页码。未解析数值可仅保存文档进入资料库。')
    return company, records, warnings, text[:1600]
