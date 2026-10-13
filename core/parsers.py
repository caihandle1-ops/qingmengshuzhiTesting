from __future__ import annotations

import io
import re
import zipfile
from xml.etree.ElementTree import iterparse
from pathlib import Path
from typing import Any

import pandas as pd
from pypdf import PdfReader
from core.import_validation import (METRICS, NUMERIC_METRICS, MONEY_METRICS, PERCENT_METRICS,
                                    blank, strict_number, detect_money_unit, detect_percent_unit, normalize_metric)

try:
    from pdfminer.high_level import extract_text as pdfminer_extract_text
except ImportError:
    pdfminer_extract_text = None
try:
    import fitz
except ImportError:
    fitz = None
try:
    import pdfplumber
except ImportError:
    pdfplumber = None

CH_ALIASES = {
    'year': ['年份', '年度', '报告年度', 'year', 'report_year'],
    'company': ['企业名称', '公司名称', '公司', '企业', 'company', '证券简称'],
    'revenue': ['营业收入', '营业总收入', '营收', '收入合计', 'revenue'],
    'net_profit': ['归母净利润', '归属于上市公司股东的净利润', '归属于母公司股东的净利润', '净利润', 'net_profit'],
    'consolidated_net_profit': ['合并净利润', '合并报表净利润', '净利润合计', 'consolidated_net_profit', 'total_net_profit'],
    'total_assets': ['资产总计', '资产总额', '总资产', '资产合计', 'total_assets'],
    'total_liabilities': ['负债合计', '总负债', '负债总计', '负债总额', 'total_liabilities'],
    'total_equity': ['所有者权益合计', '股东权益合计', '权益合计', 'total_equity'],
    'minority_profit': ['少数股东损益', 'minority_profit'],
    'operating_cashflow': ['经营现金流', '经营活动产生的现金流量净额', '经营活动现金流量净额', '经营现金流净额', 'operating_cashflow', '经营活动现金流'],
    'roe': ['ROE', '净资产收益率', '加权平均净资产收益率', 'roe'],
    'debt_ratio': ['资产负债率', 'debt_ratio'],
    'gross_margin': ['毛利率', '整体毛利率', 'gross_margin'],
    'eps': ['每股收益', '基本每股收益', 'eps'],
    'audit_opinion': ['审计意见', '审计意见类型', 'audit_opinion'],
}
COMPANY_PATTERNS = [r'([\u4e00-\u9fa5A-Za-z0-9（）()·]{2,40}股份有限公司)',
                    r'([\u4e00-\u9fa5A-Za-z0-9（）()·]{2,40}有限公司)']
NUMBER_RE = re.compile(r'(?<![\w.])(?:[-+−]?(?:\d[\d,，]*(?:\.\d+)?|\.\d+)|[（(]\d[\d,，]*(?:\.\d+)?[）)])\s*(?:亿元|百万元|万元|千元|元/股|元|[%％])?')


def _norm_col(value: str) -> str:
    return str(value).strip().replace('（', '(').replace('）', ')').replace('％', '%').replace('\n', '').replace(' ', '')


def _base_col(value: str) -> str:
    value = _norm_col(value)
    return re.sub(r'\([^)]*\)|\[[^]]*\]', '', value).strip(':：').lower()


def _find_col(cols: list[str], aliases: list[str]) -> str | None:
    # Exact normalized label only: 营业收入增长率 is not 营业收入.
    for alias in aliases:
        for col in cols:
            if _base_col(col) == _base_col(alias):
                return col
    return None


def _to_float(value: Any) -> float | None:
    try:
        return strict_number(value)
    except ValueError:
        return None


def _year(value: Any) -> int | None:
    text = str(value).strip()
    match = re.fullmatch(r'(19\d{2}|20\d{2}|2100)(?:\.0|年度|年)?', text)
    return int(match.group(1)) if match else None


def _excel_col(index: int) -> str:
    result = ''
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _frames(file_name: str, content: bytes) -> list[tuple[str, pd.DataFrame, int, dict]]:
    if len(content) > 30 * 1024 * 1024:
        raise ValueError('表格超过 30 MB 解析限制，请拆分后导入。')
    if file_name.lower().endswith('.csv'):
        try:
            frame = pd.read_csv(io.BytesIO(content), dtype=object, nrows=20001)
        except UnicodeDecodeError:
            frame = pd.read_csv(io.BytesIO(content), encoding='gbk', dtype=object, nrows=20001)
        if len(frame) > 20000 or len(frame.columns) > 100:
            raise ValueError('表格超过 20,000 行或 100 列，请拆分后导入。')
        return [('CSV', frame, 1, {})]
    if file_name.lower().endswith('.xlsx'):
        _validate_xlsx_archive(content)
    result = []
    book = pd.ExcelFile(io.BytesIO(content))
    if len(book.sheet_names) > 30:
        book.close()
        raise ValueError('工作表超过 30 张，请拆分后导入。')
    format_book = None
    format_xls = None
    if file_name.lower().endswith('.xlsx'):
        from openpyxl import load_workbook
        format_book = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    elif file_name.lower().endswith('.xls'):
        import xlrd
        format_xls = xlrd.open_workbook(file_contents=content, formatting_info=True, on_demand=True)
    for sheet in book.sheet_names:
        raw = pd.read_excel(book, sheet_name=sheet, header=None, dtype=object, nrows=20002)
        if len(raw) > 20001 or len(raw.columns) > 100:
            book.close()
            if format_book is not None:
                format_book.close()
            raise ValueError('工作表超过 20,000 行或 100 列，请拆分后导入。')
        for header in range(min(12, len(raw))):
            cols = [str(value) for value in raw.iloc[header].tolist()]
            if _find_col(cols, CH_ALIASES['year']):
                frame = raw.iloc[header + 1:].copy()
                frame.columns = cols
                formats = {}
                if format_book is not None:
                    ws = format_book[sheet]
                    for row in ws.iter_rows(min_row=header + 2):
                        for cell in row:
                            formats[(cell.row, cell.column)] = cell.number_format
                elif format_xls is not None:
                    ws = format_xls.sheet_by_name(sheet)
                    for row_index in range(header + 1, min(ws.nrows, 20001)):
                        for column_index in range(min(ws.ncols, 100)):
                            xf = format_xls.xf_list[ws.cell_xf_index(row_index, column_index)]
                            formats[(row_index + 1, column_index + 1)] = format_xls.format_map[xf.format_key].format_str
                result.append((sheet, frame, header + 1, formats))
                break
    if format_book is not None:
        format_book.close()
    if format_xls is not None:
        format_xls.release_resources()
    book.close()
    if not result:
        raise ValueError('未识别到年份列；工作表须包含“年份”或“year”。')
    return result


def _validate_xlsx_archive(content: bytes) -> None:
    """Reject oversized expansion/cell counts before pandas builds a workbook."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            members = archive.infolist()
            if len(members) > 2000 or sum(member.file_size for member in members) > 100 * 1024 * 1024:
                raise ValueError('Excel 解压后超过 100 MB 或包含过多成员，请拆分文件。')
            if any(member.file_size > 30 * 1024 * 1024 or (member.file_size > 1024 * 1024 and member.file_size / max(1, member.compress_size) > 200)
                   for member in members):
                raise ValueError('Excel 压缩展开比例或单项大小异常，已阻止解析。')
            cell_count = 0
            for member in members:
                if member.filename.startswith('xl/worksheets/') and member.filename.endswith('.xml'):
                    with archive.open(member) as source:
                        for _, element in iterparse(source, events=('end',)):
                            tag = element.tag.rsplit('}', 1)[-1]
                            if tag == 'c':
                                cell_count += 1
                                if cell_count > 200000:
                                    raise ValueError('Excel 超过 200,000 个单元格，请拆分文件。')
                            if tag == 'row' and int(element.attrib.get('r', 0)) > 20001:
                                raise ValueError('Excel 超过 20,000 数据行，请拆分文件。')
                            element.clear()
    except zipfile.BadZipFile as error:
        raise ValueError('XLSX 文件结构无效，请重新保存为标准 Excel 文件。') from error


def _read_tabular(file_name: str, content: bytes) -> pd.DataFrame:
    return _frames(file_name, content)[0][1]


def parse_tabular(file_name: str, content: bytes, *, money_unit: str | None = None,
                  percent_unit: str | None = None) -> tuple[str | None, list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    records: list[dict] = []
    companies: set[str] = set()
    for sheet, frame, header_row, formats in _frames(file_name, content):
        frame.columns = [_norm_col(col) for col in frame.columns]
        cols = list(frame.columns)
        mapped = {key: _find_col(cols, aliases) for key, aliases in CH_ALIASES.items()}
        money_unit_col = _find_col(cols, ['金额单位', '单位', 'money_unit'])
        percent_unit_col = _find_col(cols, ['比率口径', '百分比口径', 'percent_unit'])
        period_col = _find_col(cols, ['报告期', '期间', 'period'])
        currency_col = _find_col(cols, ['币种', '货币', 'currency'])
        scope_col = _find_col(cols, ['报表范围', '报表口径', 'statement_scope'])
        version_col = _find_col(cols, ['列报版本', '披露版本', '数据版本', 'report_version'])
        if not mapped['year']:
            raise ValueError('未识别到年份列，请包含“年份”或“year”。')
        duplicate_cols = [col for col in set(cols) if cols.count(col) > 1]
        if duplicate_cols:
            raise ValueError('存在重复列名，无法确定数据归属：' + '、'.join(duplicate_cols))
        for offset, (_, row) in enumerate(frame.iterrows(), header_row + 1):
            if all(blank(value) for value in row):
                continue
            record: dict[str, Any] = {'year': _year(row[mapped['year']]), 'raw_source': file_name,
                                      '_provenance': {}, '_errors': []}
            if currency_col and not blank(row.get(currency_col)) and str(row[currency_col]).strip().upper() not in ('CNY', 'RMB', '人民币', '人民币元'):
                record['_errors'].append(f'{sheet}!{offset} 行币种不是人民币，当前亿元指标库不自动换汇。')
            period_value = row.get(period_col) if period_col else None
            if not blank(period_value) and str(period_value).strip() not in ('全年', '年度', 'annual', str(record['year']), f'{record["year"]}年'):
                record['_errors'].append(f'{sheet}!{offset} 行不是明确的完整年度记录，不能把其他期间归入全年。')
            if record['year'] is None:
                record['_errors'].append(f'{sheet}!{offset} 行年份无效或不是完整年度，不能把半年/季度记录作为全年导入。')
            company_value = row.get(mapped['company']) if mapped['company'] else None
            company = None if blank(company_value) else str(company_value).strip()
            record['company_name'] = company
            if company:
                companies.add(company)
            for key in METRICS:
                col = mapped.get(key)
                if not col:
                    continue
                raw_value = row[col]
                if blank(raw_value):
                    continue
                cell = f'{sheet}!{_excel_col(cols.index(col) + 1)}{offset}'
                source_unit = detect_money_unit(col) if key in MONEY_METRICS else detect_percent_unit(col)
                if source_unit is None and key in MONEY_METRICS and money_unit_col:
                    source_unit = detect_money_unit(str(row[money_unit_col]))
                if source_unit is None and key in PERCENT_METRICS and percent_unit_col:
                    source_unit = detect_percent_unit(str(row[percent_unit_col]))
                excel_format = formats.get((offset, cols.index(col) + 1), '')
                numeric_format = re.sub(r'"[^"]*"|\\.', '', excel_format)
                if key in PERCENT_METRICS and '%' in numeric_format:
                    # Excel stores a displayed 12.5% as the numeric fraction .125.
                    source_unit = '比例'
                if key == 'eps':
                    source_unit = '元/股'
                if key == 'audit_opinion':
                    value, source_unit, normalized_unit = _audit_opinion(str(raw_value)), '文本', '文本'
                else:
                    try:
                        value, source_unit, normalized_unit = normalize_metric(key, raw_value, source_unit, money_unit, percent_unit)
                    except ValueError as error:
                        record['_errors'].append(f'{cell} {col}：{error}')
                        continue
                record[key] = value
                record['_provenance'][key] = {'file_name': file_name, 'page': None, 'cell': cell,
                    'raw_value': str(raw_value), 'raw_unit': source_unit, 'normalized_unit': normalized_unit,
                    'metric_label': col,
                    'currency_hint': str(row[currency_col]) if currency_col and not blank(row[currency_col]) else ('CNY' if '人民币' in str(col) else None),
                    'scope_hint': str(row[scope_col]) if scope_col and not blank(row[scope_col]) else None,
                    'version_hint': str(row[version_col]) if version_col and not blank(row[version_col]) else None}
            records.append(record)
    if not companies:
        warnings.append('表格缺少企业名称，请在预览前填写；不会按文件名或首家公司猜测。')
    if len(companies) > 1:
        warnings.append(f'已识别 {len(companies)} 家企业，按各行企业名称分别导入。')
    if not records:
        raise ValueError('未读取到有效年度数据，请检查表格是否为标准行列表。')
    for record in records:
        warnings.extend(record['_errors'])
    return next(iter(companies)) if len(companies) == 1 else None, records, list(dict.fromkeys(warnings))


def extract_pdf_text(content: bytes, page_limit: int = 120) -> str:
    """Extract text from a PDF with stable alternate_paths.

    SSE/SZSE annual reports are sometimes large and contain object streams that
    pypdf may reject with errors such as ``Stream has ended unexpectedly`` even
    though the browser can open the file. For presentation stability we should not
    let such parser-level failures crash the app. Try pypdf first, then fall
    back to pdfminer.six; if both fail, return a clear Chinese error.
    """
    if not content or len(content) < 1024:
        raise ValueError('PDF 文件内容过小，可能未完整下载。请重试或复制浏览器中可打开的最终 PDF 链接导入。')
    header_pos = content[:8192].find(b'%PDF')
    if header_pos < 0:
        preview = content[:180].decode('utf-8', errors='ignore').replace('\n', ' ')
        raise ValueError('下载内容暂未识别为有效 PDF 文件。可能收到的是网页提示内容而不是年报 PDF。内容预览：' + preview)
    if header_pos > 0:
        content = content[header_pos:]

    errors: list[str] = []
    # pypdf fast path. strict=False is important for many exchange PDFs.
    try:
        reader = PdfReader(io.BytesIO(content), strict=False)
        texts = []
        for p in reader.pages[:page_limit]:
            try:
                texts.append(p.extract_text() or '')
            except Exception as e:
                errors.append(f'pypdf页面解析未完成：{e}')
                continue
        text = '\n'.join(texts).strip()
        if text:
            return text
        errors.append('pypdf 未提取到有效文本')
    except Exception as e:
        errors.append(f'pypdf整体解析未完成：{e}')

    # AlternatePath 1: PyMuPDF is often more tolerant for object streams.
    if fitz is not None:
        try:
            doc = fitz.open(stream=content, filetype='pdf')
            texts = []
            for i in range(min(page_limit, doc.page_count)):
                try:
                    texts.append(doc.load_page(i).get_text('text') or '')
                except Exception as e:
                    errors.append(f'PyMuPDF页面解析未完成：{e}')
            text = '\n'.join(texts).strip()
            if text:
                return text
            errors.append('PyMuPDF 未提取到有效文本')
        except Exception as e:
            errors.append(f'PyMuPDF解析未完成：{e}')

    # AlternatePath 2: pdfplumber can recover row/column tables that plain text
    # extraction sometimes misses, especially in bank/insurance annual reports.
    if pdfplumber is not None:
        try:
            texts = []
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                for page in pdf.pages[:page_limit]:
                    try:
                        t = page.extract_text() or ''
                        if t:
                            texts.append(t)
                        for table in page.extract_tables() or []:
                            for row in table:
                                line = ' '.join(str(cell or '').replace('\n', ' ') for cell in row)
                                if line.strip():
                                    texts.append(line)
                    except Exception as e:
                        errors.append(f'pdfplumber页面解析未完成：{e}')
            text = '\n'.join(texts).strip()
            if text:
                return text
            errors.append('pdfplumber 未提取到有效文本')
        except Exception as e:
            errors.append(f'pdfplumber解析未完成：{e}')

    # AlternatePath 3 for PDFs that the browser can open but pypdf cannot parse.
    if pdfminer_extract_text is not None:
        try:
            text = pdfminer_extract_text(io.BytesIO(content), maxpages=page_limit) or ''
            if text.strip():
                return text
            errors.append('pdfminer 未提取到有效文本')
        except Exception as e:
            errors.append(f'pdfminer解析未完成：{e}')

    # Last resort: keep the app alive and produce a user-understandable failure.
    joined = '；'.join(errors[-3:])
    raise ValueError('PDF 已下载，但文本解析未完成。可能原因：该年报 PDF 对象流较特殊、文本层异常或文件下载内容不完整。技术细节：' + joined)


def _extract_company(text: str, file_name: str = '') -> str | None:
    for source in [text[:3000], Path(file_name).stem]:
        for pattern in COMPANY_PATTERNS:
            match = re.search(pattern, source)
            if match and not any(bad in match.group(1) for bad in ('公司法', '本公司')):
                return match.group(1)
    return None


def _extract_year(text: str, file_name: str = '') -> int | None:
    for source in [text[:3000], Path(file_name).stem]:
        match = re.search(r'(20\d{2})\s*年?\s*(?:年度报告|年度财务报告|年报)', source)
        if match:
            return int(match.group(1))
    # A single visible year is a permissible unambiguous report-period hint.
    years = set(re.findall(r'(20\d{2})\s*年', text[:1500]))
    return int(next(iter(years))) if len(years) == 1 else None


def _document_report_year(text: str) -> int | None:
    """A document's report year comes from its title/header, never comparison columns."""
    match = re.search(r'(20\d{2})\s*年?\s*(?:年度报告|年度财务报告|年报)', text[:3000])
    return int(match.group(1)) if match else None


def _issuer_stock_codes(text: str) -> list[str]:
    # Only explicitly labelled stock/security codes in the title/company-profile area.
    matches = re.findall(r'(?:股票代码|证券代码)[\s:：]{0,12}((?:\d\s*){6})(?!\d)', text[:15000])
    return sorted({re.sub(r'\s+', '', code) for code in matches})


def _audit_opinion(text: str) -> str:
    # Order matters: 无保留意见 contains 保留意见 as a substring.
    for opinion in ['带强调事项段的无保留意见', '带强调事项的无保留意见', '标准的无保留意见',
                    '标准无保留意见', '无保留意见', '无法表示意见', '否定意见', '保留意见']:
        if opinion in text:
            return opinion
    return text.strip()


def _str_from_context(text: str, labels: list[str]) -> str | None:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if any(label in line for label in labels):
            context = line + '\n' + (lines[index + 1] if index + 1 < len(lines) else '')
            if '意见' in context:
                result = _audit_opinion(context)
                if result != context.strip():
                    return result
    return None


def _metric_label(value: str) -> tuple[str | None, int]:
    normalized = _norm_col(value)
    normalized = re.sub(r'^[一二三四五六七八九十\d]+[、.．]', '', normalized)
    normalized = normalized.lstrip('其中:：')
    # Explicitly discard ratios/deltas/segment subrows before any prefix match.
    if any(word in normalized for word in ['增长', '同比', '占比', '占营业', '扣除非', '扣非']):
        return None, 0
    for metric in METRICS:
        aliases = sorted(CH_ALIASES[metric], key=len, reverse=True)
        for alias in aliases:
            if _base_col(normalized) == _base_col(alias):
                score = 20 if metric == 'net_profit' and ('归属于' in alias or '归母' in alias) else 10
                return metric, score
    return None, 0


def _line_label(line: str) -> tuple[str | None, str, str, int]:
    normalized = _norm_col(line)
    normalized = re.sub(r'^[一二三四五六七八九十\d]+[、.．]', '', normalized).lstrip('其中:：')
    for metric in METRICS:
        for label in sorted(CH_ALIASES[metric], key=len, reverse=True):
            if normalized.lower().startswith(label.lower()):
                tail = normalized[len(label):]
                # Must be a row with numeric cells, never a paragraph containing the label.
                if metric != 'audit_opinion' and tail and not re.match(r'^[(:：\d+−\-（.%]|^(?:亿元|百万元|万元|千元|元)', tail):
                    continue
                # Slice original line by a flexible-space label, preserving numerical cell separation.
                label_match = re.search(r'\s*'.join(re.escape(ch) for ch in label), line, re.IGNORECASE)
                if label_match:
                    metric_score = 20 if metric == 'net_profit' and ('归属于' in label or '归母' in label) else 10
                    return metric, label, line[label_match.end():].strip(), metric_score
    return None, '', '', 0


def _nearest_unit(context: str) -> str | None:
    matches = list(re.finditer(r'单位\s*[:：]\s*(?:人民币)?\s*(亿元|百万元|万元|千元|元)(?!/股)', context))
    return matches[-1].group(1) if matches else None


def _section_score(context: str) -> int:
    # Parent-only statements must never beat consolidated/key annual-data tables.
    if '母公司' in context[-180:]:
        return -50
    if any(word in context[-800:] for word in ('分季度', '半年度', '季度报告', '按产品', '按行业')):
        return -50
    return 40 if any(word in context[-800:] for word in ('主要会计数据', '主要财务指标', '合并利润表', '合并现金流量表')) else 0


def _pdf_pages(content: bytes, text: str) -> list[dict]:
    pages: list[dict] = []
    if pdfplumber is not None:
        try:
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                for number, page in enumerate(pdf.pages[:120], 1):
                    page_text = page.extract_text(x_tolerance=2, y_tolerance=3) or ''
                    tables = []
                    # Table geometry is expensive: run it only on candidate financial pages.
                    interesting = sum(label in page_text.replace('\n', '') for label in ('营业收入', '净利润', '现金流', '净资产收益率', '资产负债率')) >= 2
                    for table in page.find_tables() if interesting else []:
                        context = page.crop((0, 0, page.width, max(1, table.bbox[1]))).extract_text() or ''
                        tables.append({'rows': table.extract(), 'context': context, 'bbox': table.bbox})
                    pages.append({'page': number, 'text': page_text, 'tables': tables, 'total_pages': len(pdf.pages)})
            if any(page['text'] for page in pages):
                return pages
        except Exception:
            pass
    # Keep per-page source locations when the table engine cannot read a PDF.
    try:
        reader = PdfReader(io.BytesIO(content), strict=False)
        pages = [{'page': index, 'text': page.extract_text(extraction_mode='layout') or '', 'tables': []}
                 for index, page in enumerate(reader.pages[:120], 1)]
        if any(page['text'] for page in pages):
            return pages
    except Exception:
        pass
    return [{'page': None, 'text': text, 'tables': []}]


def parse_pdf(file_name: str, content: bytes, *, money_unit: str | None = None,
              percent_unit: str | None = None, year_hint: int | None = None) -> tuple[str | None, list[dict[str, Any]], list[str], str]:
    text = extract_pdf_text(content)
    company = _extract_company(text, file_name)
    document_year = _document_report_year(text)
    issuer_codes = _issuer_stock_codes(text)
    report_year = _extract_year(text, file_name)
    warnings: list[str] = []
    errors: list[str] = []
    problems: list[tuple[int | None, str, int, str]] = []
    if report_year and year_hint and int(year_hint) != report_year:
        errors.append(f'报告正文年度 {report_year} 与指定年度 {year_hint} 冲突，不能重标年份。')
    file_year = re.search(r'(20\d{2})\s*年?\s*(?:年度报告|年度财务报告|年报)', Path(file_name).stem)
    if file_year and report_year and int(file_year.group(1)) != report_year:
        errors.append(f'文件名年度 {file_year.group(1)} 与报告正文年度 {report_year} 不一致，请核对原文件。')
    report_year = report_year or year_hint
    if re.search(r'20\d{2}\s*年\s*(?:半年度|上半年|下半年|第[一二三四1234]季度)', text[:1800] + file_name):
        errors.append('该文件为半年/季度报告，当前年度指标库不支持直接作为全年入库。')
    candidates: dict[tuple[int | None, str], list[dict]] = {}

    def add(metric: str, raw: Any, year: int | None, unit_context: str, page: int | None, cell: str,
            score: int, table_unit: str | None = None, scope_context: str = ''):
        if blank(raw) or score < 0:
            return
        unit = detect_money_unit(unit_context) if metric in MONEY_METRICS else detect_percent_unit(unit_context)
        unit = unit or (table_unit if metric in MONEY_METRICS else None)
        if metric == 'eps':
            unit = '元/股'
        try:
            if metric == 'audit_opinion':
                value, unit, normalized = _audit_opinion(str(raw)), '文本', '文本'
            else:
                value, unit, normalized = normalize_metric(metric, raw, unit, money_unit, percent_unit)
        except ValueError as error:
            problems.append((year, metric, score, f'PDF 第 {page or "未知"} 页 {cell} {metric}：{error}'))
            return
        source = {'file_name': file_name, 'page': page, 'cell': cell, 'raw_value': str(raw),
                  'raw_unit': unit, 'normalized_unit': normalized, 'metric_label': unit_context,
                  'currency_hint': 'CNY' if '人民币' in scope_context else None,
                  'scope_hint': 'consolidated' if '合并' in scope_context else None,
                  'version_hint': 'restated' if '重述' in scope_context else None}
        candidates.setdefault((year, metric), []).append({'value': value, 'score': score, 'source': source})

    pages = _pdf_pages(content, text)
    if pages and pages[0].get('total_pages', 0) > 120:
        warnings.append(f'本文件共 {pages[0]["total_pages"]} 页，本次解析前 120 页；后续页未解析，缺失指标请另行提供相应财务表页。')
    continued_header: list[int | None] = []
    continued_context = ''
    for page in pages:
        table_metrics: set[str] = set()
        for table_index, table in enumerate(page['tables'], 1):
            header_years: dict[int, int] = {}
            context = table['context']
            for row_index, row in enumerate(table['rows'], 1):
                values = [str(cell or '').strip() for cell in row]
                year_cells = {index: _year(value) for index, value in enumerate(values) if _year(value)}
                if year_cells and not _metric_label(values[0])[0]:
                    header_years = year_cells
                    continue
                metric, score = _metric_label(values[0]) if values else (None, 0)
                if metric == 'net_profit' and _base_col(values[0]) == '净利润' and '合并利润表' in context:
                    metric = 'consolidated_net_profit'
                if not metric:
                    context += '\n' + ' '.join(values)
                    continue
                score += _section_score(context)
                if score < 0:
                    continue
                table_metrics.add(metric)
                if header_years:
                    for column, year in header_years.items():
                        if column < len(values):
                            add(metric, values[column], year, values[0], page['page'],
                                f'表{table_index}!R{row_index}C{column + 1}', score + 20, _nearest_unit(context), context)
                elif len([value for value in values[1:] if not blank(value)]) == 1:
                    column = next(index for index in range(1, len(values)) if not blank(values[index]))
                    add(metric, values[column], report_year, values[0], page['page'],
                        f'表{table_index}!R{row_index}C{column + 1}', score + 20, _nearest_unit(context), context)
                else:
                    problems.append((report_year, metric, score + 20, f'PDF 第 {page["page"]} 页 {metric} 存在多列但无法确定年份表头，请人工复核。'))
        lines = page['text'].splitlines()
        preceding = continued_context
        header_years = list(continued_header)
        last_metric_line = 0
        for line_index, line in enumerate(lines, 1):
            metric, label, tail, score = _line_label(line)
            if metric == 'net_profit' and _base_col(label) == '净利润' and '合并利润表' in preceding:
                metric = 'consolidated_net_profit'
            # Explicit year headers bind following row cells, and are reset by a new table section.
            if not metric:
                years = re.findall(r'(?<!\d)(20\d{2})(?:\s*年)?(?!\d)', line)
                if years and (len(years) > 1 or re.match(r'^\s*(?:项目|指标|20\d{2})', line)):
                    # Preserve positions of percentage/delta columns between fiscal years.
                    cells = re.findall(r'20\d{2}\s*年?|(?:本年|本期)[^\s]*(?:增减|增长|变动)[^\s]*|同比[^\s]*', line)
                    header_years = [int(re.search(r'20\d{2}', cell).group()) if re.search(r'20\d{2}', cell) else None for cell in cells]
                if re.match(r'^\s*[一二三四五六七八九十]+[、.]', line) and not years:
                    header_years = []
                    preceding = ''
                preceding = (preceding + '\n' + line)[-1800:]
                continue
            if metric in table_metrics:
                continue
            score += _section_score(preceding)
            if score < 0:
                continue
            last_metric_line = line_index
            if metric == 'audit_opinion':
                audit_text = tail or (lines[line_index] if line_index < len(lines) else '')
                add(metric, audit_text, report_year, '', page['page'], f'文本行{line_index}', score)
                continue
            # Values belong to this physical row only; never scan neighbouring metric rows.
            tail = re.sub(r'^\s*[（(][^）)]*(?:元|%|％|比例)[^）)]*[）)]', '', tail).lstrip(':： ')
            tokens = [match.group(0).strip() for match in NUMBER_RE.finditer(tail)]
            if not tokens:
                continue
            # Some annual balance-sheet headers wrap the delta label around
            # the fiscal-year row. Keep fiscal values only when the header
            # explicitly identifies that extra percentage column.
            if (metric in MONEY_METRICS and header_years and len(tokens) > len(header_years)
                    and re.search(r'本年末比上年末增\s*', preceding[-400:])):
                fiscal_tokens = [token for token in tokens if not token.endswith(('%', '％'))]
                if len(fiscal_tokens) == len(header_years):
                    tokens = fiscal_tokens
            unit_context = line[:len(line) - len(tail)]
            if header_years:
                if len(tokens) < len(header_years):
                    problems.append((report_year, metric, score, f'PDF 第 {page["page"]} 页 {metric} 数据列与年份表头不一致，请人工复核。'))
                    continue
                for column, year in enumerate(header_years):
                    if year is None:
                        continue
                    add(metric, tokens[column], year, unit_context, page['page'],
                        f'文本行{line_index}列{column + 1}', score, _nearest_unit(preceding), preceding)
            elif len(tokens) == 1:
                add(metric, tokens[0], report_year, unit_context, page['page'],
                    f'文本行{line_index}列1', score, _nearest_unit(preceding), preceding)
            else:
                problems.append((report_year, metric, score, f'PDF 第 {page["page"]} 页 {metric} 的多列数值缺少明确年度表头，请人工复核。'))
        # Carry a split table only when a metric row is actually at the page end.
        continued_header = header_years if last_metric_line and len(lines) - last_metric_line <= 3 else []
        continued_context = preceding[-800:] if continued_header else ''
    for year, metric, score, error in problems:
        better = [option for option in candidates.get((year, metric), []) if option['score'] > score]
        if better:
            warnings.append(error + ' 已跳过该候选，仅使用更高优先级的年度合并/主要会计数据表。')
        else:
            errors.append(error)
    records_by_year: dict[int | None, dict] = {}
    for (year, metric), options in candidates.items():
        highest = max(option['score'] for option in options)
        preferred = [option for option in options if option['score'] == highest]
        values = {option['value'] for option in preferred}
        if len(values) != 1:
            errors.append(f'{year} 年 {metric} 在多个同级表格中数值冲突，不自动选择。')
            continue
        chosen = preferred[0]
        record = records_by_year.setdefault(year, {'year': year, 'company_name': company, 'raw_source': file_name,
                                                   '_provenance': {}, '_errors': []})
        record[metric] = chosen['value']
        record['_provenance'][metric] = chosen['source']
    records = list(records_by_year.values())
    if not records:
        records = [{'year': report_year, 'company_name': company, 'raw_source': file_name,
                    '_provenance': {}, '_errors': []}]
        warnings.append('未提取到可核验数值；不会使用其他数据补齐，也不会创建空年度记录。')
    for record in records:
        record['_errors'] = list(dict.fromkeys(errors))
        record['_report_year'] = document_year
        record['_issuer_stock_codes'] = issuer_codes
    if document_year is None:
        warnings.append('正文标题/页头未识别到文档的报告年度；指标年度仍按表头复核，文档不会冒充指定年度年报。')
    if not company:
        warnings.append('PDF 标题未明确识别企业名称，请填写真实企业名称。')
    if not report_year:
        warnings.append('PDF 报告年度未明确识别，请核对正文及表头。')
    warnings.extend(errors)
    missing = [key for key in NUMERIC_METRICS if not any(record.get(key) is not None for record in records)]
    if missing:
        warnings.append('以下字段没有可信解析结果，保留缺失：' + '、'.join(missing))
    warnings.append('PDF 仅提取可定位表格/文本行；扫描件、跨页合并表和复杂重述需人工复核。')
    return company, records, list(dict.fromkeys(warnings)), text[:1600]


def _num_from_context(text: str, labels: list[str], prefer_percent: bool = False, metric: str = '') -> float | None:
    """Compatibility helper with row-local extraction; no wide-context guessing."""
    for line in text.splitlines():
        for label in sorted(labels, key=len, reverse=True):
            if line.strip().startswith(label):
                tail = line.strip()[len(label):]
                tokens = [match.group(0).strip() for match in NUMBER_RE.finditer(tail)]
                if len(tokens) == 1:
                    key = metric or ('roe' if prefer_percent else 'revenue')
                    unit = detect_percent_unit(line) if prefer_percent else detect_money_unit(line)
                    try:
                        return normalize_metric(key, tokens[0], unit)[0]
                    except ValueError:
                        return None
    return None
