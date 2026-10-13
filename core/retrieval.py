from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

from core.db import ROOT, fetch_report_files
from core.storage import get_data_root
from core.parsers import _document_report_year, _extract_company


INDEX_ROOT = ROOT / 'data' / 'rag_index'
INDEX_SCHEMA_VERSION = 5
NO_EVIDENCE = '当前未检索到足以支持该问题的可核验文档证据。'
METRIC_ALIASES = {
    'revenue': ['营业收入', '营收', '收入'],
    'net_profit': ['净利润', '归母净利润', '归属于上市公司股东的净利润'],
    'operating_cashflow': ['经营现金流', '经营活动产生的现金流量净额'],
    'roe': ['ROE', '净资产收益率'],
    'debt_ratio': ['资产负债率', '负债率'],
    'gross_margin': ['毛利率', '销售毛利率'],
    'eps': ['每股收益', 'EPS'],
}
EXPLANATION_QUERY_TERMS = {'为什么', '为何', '原因', '如何解释', '怎么解释', '主要原因', '业务因素', '归因'}
EXPLANATION_CUES = {'主要系', '主要由于', '原因是', '由于', '导致', '变化主要受到'}


def is_rag_eligible_report(report: dict[str, Any]) -> bool:
    suffix = Path(str(report.get('file_path') or report.get('file_name') or '')).suffix.lower()
    file_type = str(report.get('file_type') or '').strip().lower()
    return suffix in {'.pdf', '.docx'} or file_type in {'pdf', 'application/pdf', 'docx'}


def build_retrieval_query(question: str, resolved_context: dict[str, Any]) -> str:
    parts = [question]
    parts.extend(resolved_context.get('companies') or [])
    parts.extend(str(year) for year in resolved_context.get('years') or [])
    for metric in resolved_context.get('metrics') or []:
        parts.extend(METRIC_ALIASES.get(metric, [metric]))
    return ' '.join(part for part in parts if part)


def is_explanation_query(question: str) -> bool:
    return any(term in question for term in EXPLANATION_QUERY_TERMS)


def _content_query(question: str, context: dict[str, Any]) -> str:
    text = question
    for company in context.get('companies') or []:
        for alias in [company, company.replace('股份有限公司', '').replace('新能源科技', '').replace('有限公司', '')]:
            text = text.replace(alias, ' ')
    text = re.sub(r'20\d{2}\s*年?', ' ', text)
    for word in ['年度报告', '年报', '报告中', '报告里', '如何描述', '怎么描述', '如何解释', '怎么解释', '为什么', '主要原因', '是什么', '是多少', '多少', '披露', '原文', '请问', '提到']:
        text = text.replace(word, ' ')
    return text.strip()


def extract_pdf_pages(file_path: str | Path) -> list[dict[str, Any]]:
    path = Path(file_path)
    if not path.is_file():
        return []
    try:
        import fitz

        with fitz.open(path) as document:
            return [
                {'page': number + 1, 'text': page.get_text('text').strip()}
                for number, page in enumerate(document)
                if page.get_text('text').strip()
            ]
    except Exception:
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(path))
            return [
                {'page': number + 1, 'text': (page.extract_text() or '').strip()}
                for number, page in enumerate(reader.pages)
                if (page.extract_text() or '').strip()
            ]
        except Exception:
            return []


def chunk_pages(
    pages: list[dict[str, Any]], metadata: dict[str, Any], chunk_size: int = 800, overlap: int = 120,
) -> list[dict[str, Any]]:
    chunks = []
    step = max(1, chunk_size - overlap)
    for page in pages:
        text = re.sub(r'\s+', ' ', page.get('text', '')).strip()
        for offset in range(0, len(text), step):
            content = text[offset:offset + chunk_size].strip()
            if not content:
                continue
            chunks.append({
                'document_id': metadata.get('id'),
                'company': metadata.get('company_name') or metadata.get('company'),
                'report_year': metadata.get('report_year'),
                'file_name': metadata.get('file_name'),
                'file_path': str(metadata.get('file_path') or ''),
                'page': int(page['page']) if page.get('page') is not None else None,
                'location': page.get('location'), 'location_type': 'docx' if page.get('location') else 'pdf_page',
                'chunk_id': f'{metadata.get("id", "document")}-{page.get("location") or "p" + str(page.get("page"))}-{offset}',
                'text': content,
            })
            if offset + chunk_size >= len(text):
                break
    return chunks


def _local_path(file_path: str | Path) -> Path:
    path = Path(str(file_path))
    return path if path.is_absolute() else ROOT / path


def _index_path(report: dict[str, Any], index_root: Path) -> Path:
    key = report.get('id') or re.sub(r'[^A-Za-z0-9_-]+', '_', report.get('file_name', 'document'))
    return index_root / f'report_{key}.json'


def _document_scope_check(pages: list[dict[str, Any]], report: dict[str, Any]) -> dict[str, Any]:
    """Check title/header facts already extracted from the PDF, never its filename.

    Comparative table years are not report years. Documents without a clear
    issuer/title are marked metadata-only rather than given invented certainty.
    """
    page_headers = [str(page.get('text', '')).splitlines()[:12] for page in pages[:4]]
    header_text = '\n'.join('\n'.join(lines)[:700] for lines in page_headers)
    document_year = _document_report_year(header_text)
    interim_title = bool(re.search(r'20\d{2}\s*年?\s*(?:半年度|上半年|下半年|第?[一二三四1234]季度)\s*(?:报告|财务报告)', header_text))
    document_company = None
    for lines in page_headers:
        for position, line in enumerate(lines):
            candidate = _extract_company(line)
            # A standalone issuer in the top header or an annual-report title
            # identifies the subject; a subsidiary mentioned in prose does not.
            if candidate and ((position < 4 and line.strip() == candidate) or
                              (_document_report_year(line) is not None and line.strip().startswith(candidate))):
                document_company = candidate
                break
        if document_company:
            break
    expected_year = report.get('report_year')
    expected_company = report.get('company_name') or report.get('company')
    conflicts = []
    if document_year is not None and expected_year is not None and str(document_year) != str(expected_year):
        conflicts.append(f'正文标题/页头报告年度为{document_year}年，来源记录却登记为{expected_year}年')

    def issuer_key(value: str) -> str:
        return re.sub(r'\s+', '', str(value)).replace('（', '(').replace('）', ')')

    if document_company and expected_company and issuer_key(document_company) != issuer_key(expected_company):
        conflicts.append(f'正文标题/页头主体为{document_company}，来源记录却登记为{expected_company}')
    verified = document_year is not None and document_company is not None
    return {'status': 'scope_mismatch' if conflicts else ('verified' if verified else 'partial' if document_year is not None or document_company else 'metadata_only'),
            'document_year': document_year, 'document_company': document_company,
            'interim_report': interim_title,
            'metadata_year': expected_year, 'metadata_company': expected_company,
            'error': '；'.join(conflicts),
            'note': ('正文范围与来源记录冲突，拒绝用于回答。' if conflicts else
                     '正文标题/页头企业与报告年已匹配。' if verified else
                     '正文未提供完整可识别的企业及报告年标题，未臆断；当前范围仍依赖导入元数据。')}


def build_document_index(report: dict[str, Any], index_root: Path | None = None) -> dict[str, Any]:
    index_root = Path(index_root) if index_root is not None else get_data_root() / 'rag_index'
    if not is_rag_eligible_report(report):
        return {'status': 'not_applicable', 'page_count': 0, 'chunks': []}
    path = _local_path(report.get('file_path', ''))
    if not path.is_file():
        return {'status': 'missing', 'page_count': 0, 'chunks': [], 'error': '文件不存在。'}
    stat = path.stat()
    index_path = _index_path(report, index_root)
    if index_path.is_file():
        try:
            cached = json.loads(index_path.read_text(encoding='utf-8'))
            if cached.get('schema_version') == INDEX_SCHEMA_VERSION and cached.get('file_path') == str(path) and cached.get('file_size') == stat.st_size and cached.get('mtime_ns') == stat.st_mtime_ns and cached.get('scope') == [report.get('company_name') or report.get('company'), report.get('report_year')]:
                return cached
        except Exception:
            pass
    if path.suffix.lower() == '.docx':
        from core.docx_support import extract_docx_parts
        try:
            pages = extract_docx_parts(path.read_bytes())
        except ValueError as error:
            return {'status': 'invalid_document', 'page_count': 0, 'chunks': [], 'error': str(error)}
    else:
        pages = extract_pdf_pages(path)
    verification = _document_scope_check(pages, report)
    chunks = [] if verification['status'] == 'scope_mismatch' else chunk_pages(pages, {**report, 'file_path': str(path)})
    payload = {
        'status': 'scope_mismatch' if verification['status'] == 'scope_mismatch' else 'ready' if chunks else 'no_text',
        'schema_version': INDEX_SCHEMA_VERSION,
        'report_file_id': report.get('id'), 'file_path': str(path),
        'file_size': stat.st_size, 'mtime_ns': stat.st_mtime_ns,
        'page_count': max((page['page'] for page in pages if page.get('page') is not None), default=0),
        'part_count': len(pages), 'location_type': 'docx' if path.suffix.lower() == '.docx' else 'pdf_page', 'chunks': chunks,
        'scope': [report.get('company_name') or report.get('company'), report.get('report_year')],
        'scope_verification': verification, 'error': verification['error'],
    }
    try:
        index_root.mkdir(parents=True, exist_ok=True)
        index_path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    except OSError:
        pass
    return payload


def get_index_status(report: dict[str, Any], index_root: Path | None = None) -> str:
    index_root = Path(index_root) if index_root is not None else get_data_root() / 'rag_index'
    if not is_rag_eligible_report(report):
        return '不适用'
    path = _local_path(report.get('file_path', ''))
    if not path.is_file():
        return '文件缺失'
    index_path = _index_path(report, index_root)
    if not index_path.is_file():
        return '待首次查询建立'
    try:
        cached = json.loads(index_path.read_text(encoding='utf-8'))
        stat = path.stat()
        if cached.get('schema_version') != INDEX_SCHEMA_VERSION or cached.get('file_path') != str(path) or cached.get('file_size') != stat.st_size or cached.get('mtime_ns') != stat.st_mtime_ns or cached.get('scope') != [report.get('company_name') or report.get('company'), report.get('report_year')]:
            return '待刷新'
        if cached.get('status') == 'scope_mismatch':
            return '范围冲突：' + str(cached.get('error') or '正文企业/报告年与来源记录不符')
        return '无可检索文本' if cached.get('status') == 'no_text' else '已建立'
    except Exception:
        return '待刷新'


def _terms(text: str) -> list[str]:
    normalized = re.sub(r'\s+', '', text.lower())
    chinese = ''.join(re.findall(r'[\u4e00-\u9fff]', normalized))
    grams = [chinese[i:i + size] for size in (2, 3) for i in range(max(0, len(chinese) - size + 1))]
    words = re.findall(r'[a-z]+|\d+(?:\.\d+)?', normalized)
    finance = [term for term in ['研发', '海外', '风险', '利润', '营收', '毛利率', '现金流', '产品结构', '政策', '汇率'] if term in normalized]
    return grams + words + finance


def rank_chunks(query: str, chunks: list[dict[str, Any]], top_k: int = 5) -> list[dict[str, Any]]:
    query_counts = Counter(_terms(query))
    if not query_counts:
        return []
    document_frequency = Counter()
    chunk_terms = []
    for chunk in chunks:
        terms = Counter(_terms(chunk['text']))
        chunk_terms.append(terms)
        document_frequency.update(terms.keys())
    scored = []
    total = max(1, len(chunks))
    for chunk, terms in zip(chunks, chunk_terms):
        score = sum(
            min(count, terms.get(term, 0)) * (math.log((total + 1) / (document_frequency[term] + 0.5)) + 1)
            for term, count in query_counts.items()
        )
        if score > 0:
            scored.append({**chunk, 'score': round(score, 4)})
    scored.sort(key=lambda item: item['score'], reverse=True)
    results, seen_pages = [], set()
    for item in scored:
        page_key = (item['document_id'], item.get('page'), item.get('location'))
        if page_key in seen_pages:
            continue
        results.append(item)
        seen_pages.add(page_key)
        if len(results) >= top_k:
            break
    return results


def _extract_snippet(query: str, text: str, limit: int = 220) -> str:
    sentences = [part.strip() for part in re.split(r'(?<=[。！？；])|\n+', text) if part.strip()]
    ranked = rank_chunks(
        query,
        [{'text': sentence, 'document_id': 0, 'page': 0} for sentence in sentences],
        top_k=1,
    )
    snippet = ranked[0]['text'] if ranked else (sentences[0] if sentences else text)
    if len(snippet) <= limit:
        return snippet
    terms = sorted(set(re.findall(r'[\u4e00-\u9fff]{2,}|[A-Za-z]{2,}', query)), key=len, reverse=True)
    positions = [snippet.lower().find(term.lower()) for term in terms if term.lower() in snippet.lower()]
    offset = max(0, positions[0] - 30) if positions else 0
    return snippet[offset:offset + limit]


def _supported_causal_sentences(text: str, metric_aliases: list[str]) -> list[str]:
    """Require the requested metric and a concrete causal clause in one sentence.

    A loose mention of '影响', a risk possibility, or another metric elsewhere
    on the page is not evidence of this metric's reported change.
    """
    supported = []
    for sentence in re.split(r'(?<=[。！？；])|\n+', text):
        sentence = sentence.strip()
        if not sentence or (metric_aliases and not any(alias in sentence.lower() for alias in metric_aliases)):
            continue
        if any(term in sentence for term in ['可能', '或将', '不一定', '尚不能', '无法判断', '未能确定']):
            continue
        has_change = bool(re.search(r'增长|下降|减少|增加|变动|变化|下滑|提升|降低', sentence))
        causal = bool(re.search(r'(?:主要系|主要由于|原因(?:为|是)|由于).{3,}|.{3,}导致.{2,}|(?:变化|变动|增长|下降)主要受到.{3,}影响', sentence))
        if has_change and causal:
            supported.append(sentence)
    return supported


def _numeric_fact_support(chunk: dict[str, Any], context: dict[str, Any]) -> str | None:
    """Locate a requested SQL fact next to its metric label before citing it.

    Annual reports also contain quarterly breakdowns. Matching just a report's
    year or the word 'profit' is insufficient to substantiate an annual number.
    """
    text = chunk.get('text', '')
    facts = [row for row in context.get('structured_facts') or []
             if row.get('company') == chunk.get('company') and str(row.get('year')) == str(chunk.get('report_year'))]
    for fact in facts:
        for metric in context.get('metrics') or []:
            expected = fact.get(metric)
            if not isinstance(expected, (int, float)):
                continue
            aliases = METRIC_ALIASES.get(metric, [metric])
            for alias in aliases:
                for found in re.finditer(re.escape(alias), text, flags=re.I):
                    # Stop before a different metric label so a neighbouring
                    # row's number cannot support the requested metric.
                    tail = text[found.end():found.end() + 160]
                    next_labels = [match.start() for key, names in METRIC_ALIASES.items() if key != metric for label in names
                                   if (match := re.search(re.escape(label), tail, flags=re.I))]
                    if next_labels:
                        tail = tail[:min(next_labels)]
                    for value in re.finditer(r'(?<![\d.])[-+]?\d[\d,，]*(?:\.\d+)?', tail):
                        number = float(value.group().replace(',', '').replace('，', ''))
                        suffix = tail[value.end():value.end() + 2]
                        if metric in {'revenue', 'net_profit', 'operating_cashflow'} and suffix.startswith(('%', '％')):
                            continue
                        scales = [1, .01, .0001, .00001, .00000001] if metric in {'revenue', 'net_profit', 'operating_cashflow'} else [1]
                        if any(round(number * scale, 2) == round(float(expected), 2) for scale in scales):
                            end = found.end() + value.end()
                            return text[max(0, found.start() - 25):min(len(text), end + 80)]
    return None


def retrieve_documents(
    question: str,
    resolved_context: dict[str, Any],
    reports: list[dict[str, Any]] | None = None,
    index_root: Path | None = None,
    top_k: int = 5,
) -> dict[str, Any]:
    report_rows = [
        dict(row) for row in (reports if reports is not None else fetch_report_files())
        if is_rag_eligible_report(dict(row))
    ]
    companies = set(resolved_context.get('companies') or [])
    years = {int(year) for year in resolved_context.get('years') or []}
    company_reports = [row for row in report_rows if row.get('company_name') in companies]
    exact = [row for row in company_reports if str(row.get('report_year')) in {str(year) for year in years}] if years else company_reports
    selected = exact
    if resolved_context.get('period') not in {None, '', 'annual'}:
        selected = [row for row in selected if row.get('report_period') == resolved_context.get('period')]
    else:
        selected = [row for row in selected if not re.search(
            r'半年|季度|(?<![A-Za-z])[Qq][1-4](?!\d)',
            ' '.join(str(row.get(key) or '') for key in ('report_type', 'report_title', 'file_name')))]
    missing_scope = [{'company': company, 'year': year} for company in companies for year in years
                     if not any(row.get('company_name') == company and str(row.get('report_year')) == str(year) for row in selected)]
    all_chunks, indexed_documents = [], 0
    accepted_reports = []
    document_checks, rejected_documents = [], []
    for report in selected:
        try:
            index = build_document_index(report, index_root)
            check = {'document_id': report.get('id'), 'file_name': report.get('file_name'),
                     'status': index.get('status'), 'scope_verification': index.get('scope_verification'),
                     'reason': index.get('error') or (index.get('scope_verification') or {}).get('note', '')}
            document_checks.append(check)
            if (resolved_context.get('period') in {None, '', 'annual'}
                    and (index.get('scope_verification') or {}).get('interim_report')):
                check.update(status='scope_mismatch', reason='正文标题为半年或季度报告，不能作为年度问题依据。')
                rejected_documents.append(check)
                continue
            if index.get('status') == 'scope_mismatch':
                rejected_documents.append(check)
                continue
            accepted_reports.append(report)
            if index.get('chunks'):
                indexed_documents += 1
                all_chunks.extend(index['chunks'])
        except Exception as exc:
            document_checks.append({'document_id': report.get('id'), 'file_name': report.get('file_name'),
                                    'status': 'failed', 'reason': f'{type(exc).__name__}：{str(exc)[:160]}'})
            continue
    missing_scope = [{'company': company, 'year': year} for company in companies for year in years
                     if not any(row.get('company_name') == company and str(row.get('report_year')) == str(year) for row in accepted_reports)]
    retrieval_query = build_retrieval_query(question, resolved_context)
    content_query = _content_query(question, resolved_context)
    metric_aliases = [
        alias.lower()
        for metric in resolved_context.get('metrics') or []
        for alias in METRIC_ALIASES.get(metric, [metric])
    ]
    if metric_aliases:
        all_chunks = [
            chunk for chunk in all_chunks
            if any(alias in chunk.get('text', '').lower() for alias in metric_aliases)
        ]
    else:
        raw_matches = rank_chunks(content_query, all_chunks, top_k=max(1, len(all_chunks)))
        matched_chunks = {item.get('chunk_id') for item in raw_matches}
        all_chunks = [chunk for chunk in all_chunks if chunk.get('chunk_id') in matched_chunks]
    if is_explanation_query(question):
        supported_chunks = []
        for chunk in all_chunks:
            sentences = _supported_causal_sentences(chunk.get('text', ''), metric_aliases)
            if sentences:
                supported_chunks.append({**chunk, 'supported_sentences': sentences})
        all_chunks = supported_chunks
    verify_numeric = bool(resolved_context.get('structured_facts')) and not is_explanation_query(question) and any(term in question for term in ['多少', '数值', '数据'])
    if verify_numeric:
        supporting_chunks = []
        for chunk in all_chunks:
            support = _numeric_fact_support(chunk, resolved_context)
            if support:
                supporting_chunks.append({**chunk, 'fact_support_text': support})
        all_chunks = supporting_chunks
    ranking_query = content_query + ' ' + ' '.join(metric_aliases)
    hits = rank_chunks(ranking_query, all_chunks, top_k)
    citations = []
    for number, hit in enumerate(hits, 1):
        citations.append({
            'number': number, 'document_id': hit.get('document_id'),
            'company': hit.get('company'), 'report_year': hit.get('report_year'),
            'file_name': hit.get('file_name'), 'file_path': hit.get('file_path'),
            'page': hit.get('page'), 'location': hit.get('location'), 'location_type': hit.get('location_type'),
            'snippet': (hit.get('fact_support_text') or (hit['supported_sentences'][0][:400] if hit.get('supported_sentences') else _extract_snippet(ranking_query, hit.get('text', '')))),
            'chunk_id': hit.get('chunk_id'), 'score': hit.get('score'),
        })
    answer = NO_EVIDENCE
    if citations:
        answer = '年报原文要点：\n' + '\n'.join(
            f'[{item["number"]}] {item["snippet"]}' for item in citations
        )
    return {
        'status': 'success' if citations else 'no_evidence', 'answer': answer,
        'hits': hits, 'citations': citations, 'candidate_documents': len(selected) - len(rejected_documents),
        'metadata_candidates': len(selected), 'document_checks': document_checks, 'rejected_documents': rejected_documents,
        'indexed_documents': indexed_documents, 'chunk_count': len(all_chunks),
        'hit_count': len(hits), 'exact_year_match': bool(accepted_reports),
        'retrieval_query': retrieval_query,
        'content_query': content_query,
        'numeric_fact_checked': verify_numeric,
        'missing_scope': missing_scope,
        'reason': ('已拒绝正文与来源记录范围不符的文档：' + '；'.join(f'{item["file_name"]}：{item["reason"]}' for item in rejected_documents) if rejected_documents else
                   '缺少目标企业或年份的报告，不会退回其他年度。' if missing_scope else
                   ('没有同一段落直接支持所问原因的可核验证据。' if is_explanation_query(question) and not citations else ('' if citations else '未找到匹配问题的原文证据。'))),
    }
