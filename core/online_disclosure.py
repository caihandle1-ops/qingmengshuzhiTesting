from __future__ import annotations

import json
import re
import time
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin
from hashlib import sha256
from uuid import uuid4

from core.downloads import safe_download_pdf, validate_pdf_bytes
from core.storage import get_data_root, require_write_access
from core.builtin_catalog import get_builtin_companies
from core.company_identity import alias_pattern

import requests

try:
    from curl_cffi import requests as curl_requests  # browser-like TLS fingerprint, useful for SSE static PDFs
except Exception:  # pragma: no cover - optional dependency alternate_path
    curl_requests = None

ROOT = Path(__file__).resolve().parents[1]
ONLINE_CACHE_DIR = get_data_root() / 'online_cache'

USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36 InvestorAgent/1.0'

COMPANY_CODE_MAP = {
    # Built-in sample companies
    '比亚迪': '002594', '比亚迪股份有限公司': '002594',
    '宁德时代': '300750', '宁德时代新能源科技股份有限公司': '300750',
    '长安汽车': '000625', '长安汽车股份有限公司': '000625',
    '上汽集团': '600104', '上海汽车集团股份有限公司': '600104',
    '赣锋锂业': '002460', '江西赣锋锂业集团股份有限公司': '002460',
    '天齐锂业': '002466', '天齐锂业股份有限公司': '002466',
    '欣旺达': '300207', '欣旺达电子股份有限公司': '300207',
    '国轩高科': '002074', '国轩高科股份有限公司': '002074',
    '亿纬锂能': '300014', '惠州亿纬锂能股份有限公司': '300014',
    '华友钴业': '603799', '浙江华友钴业股份有限公司': '603799',
    # Common SSE test companies
    '贵州茅台': '600519', '贵州茅台酒股份有限公司': '600519',
    '中国平安': '601318', '中国平安保险（集团）股份有限公司': '601318',
    '招商银行': '600036', '招商银行股份有限公司': '600036',
    '工业富联': '601138', '富士康工业互联网股份有限公司': '601138',
    '中国中免': '601888', '中国旅游集团中免股份有限公司': '601888',
    # Common SZSE test companies for online-disclosure verification
    '美的集团': '000333', '美的集团股份有限公司': '000333',
    '格力电器': '000651', '珠海格力电器股份有限公司': '000651',
    '五粮液': '000858', '宜宾五粮液股份有限公司': '000858',
    '海康威视': '002415', '杭州海康威视数字技术股份有限公司': '002415',
    '立讯精密': '002475', '立讯精密工业股份有限公司': '002475',
    '牧原股份': '002714', '牧原食品股份有限公司': '002714',
    '东方财富': '300059', '东方财富信息股份有限公司': '300059',
    '迈瑞医疗': '300760', '深圳迈瑞生物医疗电子股份有限公司': '300760',
}

# The catalog is also the offline disclosure-search identity source. Keep the
# existing non-builtin lookup examples without implying they have local data.
for _company in get_builtin_companies():
    for _alias in (_company['name'], *_company['aliases']):
        COMPANY_CODE_MAP[_alias] = _company['stock_code']

CODE_COMPANY_MAP: dict[str, list[str]] = {}
for _name, _code in COMPANY_CODE_MAP.items():
    CODE_COMPANY_MAP.setdefault(_code, [])
    if _name not in CODE_COMPANY_MAP[_code]:
        CODE_COMPANY_MAP[_code].append(_name)


@dataclass
class DisclosureItem:
    title: str
    code: str | None
    company: str | None
    date: str | None
    source: str
    url: str
    file_type: str = 'pdf'

    def as_dict(self) -> dict[str, Any]:
        return {
            '公告标题': self.title,
            '股票代码': self.code or '',
            '公司名称': self.company or '',
            '披露日期': self.date or '',
            '来源': self.source,
            'PDF链接': self.url,
        }


def resolve_stock_code(query: str) -> str | None:
    q = (query or '').strip()
    codes = set(re.findall(r'(?<![A-Za-z0-9])\d{6}(?![A-Za-z0-9])', q))
    if codes:
        return next(iter(codes)) if len(codes) == 1 else None
    if q in COMPANY_CODE_MAP:
        return COMPANY_CODE_MAP[q]
    spans = []
    for name, code in sorted(COMPANY_CODE_MAP.items(), key=lambda item: -len(item[0])):
        for match in re.finditer(alias_pattern(name), q, re.IGNORECASE):
            if not any(match.start() < end and match.end() > start for start, end, _ in spans):
                spans.append((match.start(), match.end(), code))
    found = {code for _, _, code in spans}
    # A partial/ambiguous company query must not silently select the first code.
    return next(iter(found)) if len(found) == 1 else None


def resolve_company_names(query: str) -> list[str]:
    """Return possible company names for a code/name query.

    SZSE site-wide search is title-oriented: annual-report titles normally use
    the company abbreviation/full name, while the stock code appears in the PDF
    content. Therefore code-only searches like 000333 can miss the annual report
    unless we also search 美的集团 / 美的集团股份有限公司.
    """
    q = (query or '').strip()
    code = resolve_stock_code(q)
    names: list[str] = []
    if code:
        for name in CODE_COMPANY_MAP.get(code, []):
            if name not in names:
                names.append(name)
        # Fall back to the name->code map in case CODE_COMPANY_MAP was extended
        # dynamically or loaded from a previous import.
        for name, c in COMPANY_CODE_MAP.items():
            if c == code and name not in names:
                names.append(name)
    if q and not re.fullmatch(r'\d{6}', q) and q not in names:
        names.insert(0, q)
    names.sort(key=len)
    return names[:8]

def guess_exchange(query_or_code: str) -> str | None:
    code = resolve_stock_code(query_or_code) or (query_or_code or '').strip()
    if re.match(r'^(600|601|603|605|688)\d{3}$', code):
        return 'sse'
    if re.match(r'^(000|001|002|003|300|301)\d{3}$', code):
        return 'szse'
    return None


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        'User-Agent': USER_AGENT,
        'Accept': 'application/json,text/javascript,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Connection': 'close',
    })
    return s


def _safe_json_from_text(text: str) -> Any:
    text = text.strip()
    if not text:
        return None
    # JSONP wrapper support
    m = re.search(r'^[\w$]+\((.*)\)\s*;?$', text, flags=re.S)
    if m:
        text = m.group(1)
    try:
        return json.loads(text)
    except Exception:
        # extract first JSON object if response has noise
        m = re.search(r'(\{.*\})', text, flags=re.S)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                return None
    return None


def _validated_search_json(text: str) -> Any:
    """An HTTP 200/error page is not a completed disclosure search."""
    data = _safe_json_from_text(text)
    if isinstance(data, dict):
        if data.get('success') is False or data.get('error'):
            raise ValueError('交易所接口返回错误：' + str(data.get('message') or data.get('error'))[:100])
        code = data.get('code')
        if code is not None and str(code).lower() not in {'0', '200', '0000', 'success'}:
            raise ValueError('交易所接口返回错误代码：' + str(code)[:40])
    def has_rows(value):
        if isinstance(value, list):
            return all(isinstance(row, dict) or (isinstance(row, list) and has_rows(row)) for row in value)
        if isinstance(value, dict):
            return any(has_rows(value[key]) for key in ('data', 'result', 'announcements', 'list', 'rows', 'pageHelp') if key in value)
        return False
    if not has_rows(data):
        raise ValueError('响应解析失败：未返回有效的披露结果列表。')
    return data


def _normalize_pdf_url(raw_url: str, base: str) -> str:
    if not raw_url:
        return ''
    raw_url = raw_url.replace('\\/', '/').strip()
    if raw_url.startswith('//'):
        raw_url = 'https:' + raw_url
    else:
        raw_url = urljoin(base, raw_url)
    # SZSE search API often returns http://disc.static.szse.cn/download/disc/...
    # while the browser's final PDF viewer uses https://disc.static.szse.cn/disc/...
    # Normalize this pattern to improve download success.
    raw_url = raw_url.replace('http://disc.static.szse.cn/', 'https://disc.static.szse.cn/')
    raw_url = raw_url.replace('https://disc.static.szse.cn/download/disc/', 'https://disc.static.szse.cn/disc/')
    return raw_url


def _clean_html_text(text: Any) -> str:
    if text is None:
        return ''
    text = str(text)
    text = re.sub(r'<[^>]+>', '', text)
    return (text.replace('&nbsp;', ' ').replace('&amp;', '&')
                .replace('&lt;', '<').replace('&gt;', '>').strip())


def _format_szse_time(value: Any) -> str | None:
    if value is None or value == '':
        return None
    if isinstance(value, (int, float)):
        try:
            # SZSE docpubtime is Unix milliseconds.
            return datetime.fromtimestamp(float(value) / 1000).strftime('%Y-%m-%d')
        except Exception:
            return str(value)[:10]
    text = str(value).strip()
    if re.fullmatch(r'\d{13}', text):
        try:
            return datetime.fromtimestamp(int(text) / 1000).strftime('%Y-%m-%d')
        except Exception:
            return text[:10]
    return text[:10]


def _filter(items: list[DisclosureItem], query: str, report_type: str = '年度报告', year: int | None = None, limit: int = 20) -> list[DisclosureItem]:
    """Final disclosure filtering used by the UI.

    Important competition behavior:
    - A selected year means the *report year* in the title, not the publish date.
      For example, 比亚迪 2024 年年度报告 may be published in 2025, but its title
      must contain 2024 年度报告.
    - For stock-code queries, do not blindly keep rows whose code was only filled
      by our alternate_path. The candidate itself should carry the code/company name.
    """
    q = (query or '').strip()
    q_code = resolve_stock_code(q)
    q_names = resolve_company_names(q)
    out: list[DisclosureItem] = []
    seen: set[str] = set()
    for item in items:
        title = item.title or ''
        company = item.company or ''
        code = item.code or ''
        text = ' '.join([title, code, company])

        # Company/stock relevance. For code queries, the row code should match or
        # the title/company should contain the known company name. This prevents
        # unrelated fund/company annual reports from being mislabeled as 002594.
        if q_code:
            if code != q_code and not any(n and n in text for n in q_names):
                continue
        elif q:
            if q not in text and not any(n and n in text for n in q_names):
                continue

        # Report type filtering. Avoid abstracts when the user requests full annual report.
        if report_type and report_type != '全部公告':
            if report_type == '年度报告':
                if '年度报告' not in title or '摘要' in title:
                    continue
                # Exclude related but non-annual-report documents.
                bad_words = ['审计报告', '内部控制', '内部控制审计', '说明会', '股东会', '可持续发展', '社会责任', 'H股公告', '业绩预告', '业绩快报', '权益归属', '持股计划', '产销快报']
                if any(w in title for w in bad_words):
                    continue
            elif report_type not in title:
                continue

        # Year is the reporting year in the title. Do NOT use publish date for this
        # check because annual reports are commonly disclosed in the following year.
        if year and str(year) not in title:
            continue

        if item.url in seen:
            continue
        seen.add(item.url)
        out.append(item)

    # Prefer exact full annual reports; then newer publish date.
    def rank(x: DisclosureItem):
        t = x.title or ''
        exact = 0 if (report_type == '年度报告' and year and re.search(fr'{year}\s*年?\s*年度报告$', t)) else 1
        return (exact, x.date or '')
    out.sort(key=rank, reverse=False)
    return out[:limit]


def _items_from_any_json(data: Any, source: str, base_url: str) -> list[DisclosureItem]:
    rows: list[Any] = []
    if isinstance(data, dict):
        for key in ['result', 'data', 'announcements', 'list', 'rows']:
            val = data.get(key)
            if isinstance(val, list):
                rows.extend(val)
            elif isinstance(val, dict):
                for key2 in ['data', 'list', 'rows']:
                    if isinstance(val.get(key2), list):
                        rows.extend(val.get(key2))
        if not rows and isinstance(data.get('pageHelp'), dict):
            ph = data['pageHelp']
            for key in ['data', 'list']:
                if isinstance(ph.get(key), list):
                    rows.extend(ph[key])
    elif isinstance(data, list):
        rows = data

    items: list[DisclosureItem] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = row.get('title') or row.get('TITLE') or row.get('doctitle') or row.get('docTitle') or row.get('announcementTitle') or row.get('bulletinTitle') or row.get('annTitle') or row.get('discTitle') or ''
        url = row.get('url') or row.get('URL') or row.get('docpuburl') or row.get('docpubjsonurl') or row.get('adjunctUrl') or row.get('attachUrl') or row.get('bulletinUrl') or row.get('annUrl') or row.get('fileUrl') or row.get('attachPath') or ''
        code = row.get('stockCode') or row.get('SECURITY_CODE') or row.get('securityCode') or row.get('secCode') or row.get('productId') or row.get('code')
        company = row.get('companyName') or row.get('SECURITY_NAME') or row.get('securityName') or row.get('secName') or row.get('stockName') or row.get('companyAbbr')
        content = str(row.get('doccontent') or row.get('content') or '')
        if not code:
            m = re.search(r'证券代码[:：]\s*(\d{6})', content)
            if m:
                code = m.group(1)
        date = row.get('publishDate') or row.get('SSEDATE') or row.get('date') or row.get('announcementTime') or row.get('publishTime') or row.get('annDate') or row.get('docpubtime')
        date = _format_szse_time(date)
        if url and '.pdf' in url.lower():
            items.append(DisclosureItem(_clean_html_text(title), str(code) if code else None, str(company) if company else None, date, source, _normalize_pdf_url(url, base_url)))
    return items


def _items_from_html(html: str, source: str, base_url: str) -> list[DisclosureItem]:
    items: list[DisclosureItem] = []
    for m in re.finditer(r'<a[^>]+href=["\']([^"\']+\.pdf[^"\']*)["\'][^>]*>(.*?)</a>', html, flags=re.I | re.S):
        url = _normalize_pdf_url(m.group(1), base_url)
        title = re.sub(r'<.*?>', '', m.group(2)).strip() or Path(url).name
        items.append(DisclosureItem(title=title, code=None, company=None, date=None, source=source, url=url))
    # Some pages embed pdf links in JSON strings without <a>
    for m in re.finditer(r'["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']', html, flags=re.I):
        url = _normalize_pdf_url(m.group(1), base_url)
        if not any(x.url == url for x in items):
            items.append(DisclosureItem(title=Path(url.split('?')[0]).name, code=None, company=None, date=None, source=source, url=url))
    return items


def _walk_json(obj: Any):
    """Yield every dict/list/string node from a nested JSON-like object."""
    yield obj
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_json(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_json(v)


def _first_field(row: dict[str, Any], names: list[str]) -> str:
    for name in names:
        val = row.get(name)
        if val is not None and str(val).strip():
            return str(val).strip()
    return ''


def _pdf_links_from_text(text: str, base_url: str) -> list[str]:
    links: list[str] = []
    for m in re.finditer(r'(https?:\\?/\\?/[^\s"\']+?\.PDF|https?:\\?/\\?/[^\s"\']+?\.pdf|/[^\s"\']+?\.PDF|/[^\s"\']+?\.pdf)', text, flags=re.I):
        url = _normalize_pdf_url(m.group(1), base_url)
        if url and url not in links:
            links.append(url)
    return links


def _resolve_pdf_from_detail(url: str, source: str, timeout: int = 12) -> str | None:
    """Open a detail/search-result page and extract the first PDF URL."""
    if not url or '.pdf' in url.lower():
        return url
    try:
        s = _session()
        r = s.get(url, timeout=timeout, headers={'Referer': 'https://www.szse.cn/'})
        if r.status_code != 200:
            return None
        found = _items_from_html(r.text, source, url)
        if found:
            return found[0].url
        links = _pdf_links_from_text(r.text, url)
        return links[0] if links else None
    except Exception:
        return None


def _items_from_szse_search_json(data: Any, query: str, source: str = '深交所', base_url: str = 'https://www.szse.cn/') -> list[DisclosureItem]:
    """Parse SZSE site-wide search results from `/api/search/content`.

    The search endpoint often returns a detail-page URL instead of the final
    PDF. We therefore keep detail-page candidates; `download_pdf()` resolves
    them to the embedded PDF at import time.
    """
    items: list[DisclosureItem] = []
    resolved_code = resolve_stock_code(query) or (query if re.fullmatch(r'\d{6}', str(query).strip()) else '')
    url_keys = ['docpuburl', 'docpubjsonurl', 'url', 'href', 'link', 'docUrl', 'pageUrl', 'contentUrl', 'fileUrl', 'attachUrl', 'adjunctUrl', 'path', 'urlPath', 'linkUrl']
    title_keys = ['doctitle', 'docTitle', 'title', 'name', 'contentTitle', 'headline', 'articletitle']

    def add_row(node: dict[str, Any]) -> None:
        text_blob = json.dumps(node, ensure_ascii=False)
        title = _first_field(node, title_keys)
        if not title:
            title = _first_field(node, ['doccontent', 'content', 'summary', 'description'])[:120]
        title = _clean_html_text(title)
        code = _first_field(node, ['stockCode', 'code', 'secCode', 'zqdm'])
        company = _first_field(node, ['companyName', 'secName', 'stockName', 'zqjc'])
        content_for_code = str(node.get('doccontent') or node.get('content') or '')
        clean_content = _clean_html_text(content_for_code)
        if not code:
            m_code = re.search(r'证券代码[:：]\s*(\d{6})', clean_content)
            if m_code:
                code = m_code.group(1)
        if not company:
            m_name = re.search(r'证券简称[:：]\s*([\u4e00-\u9fa5A-Za-z0-9（）()·]+)', clean_content)
            if m_name:
                company = m_name.group(1).strip()
        if not company and '：' in title:
            company = title.split('：', 1)[0].strip()
        # For stock-code searches, ignore rows that do not really belong to that code.
        # Do not fill every row with resolved_code before filtering; otherwise unrelated
        # reports returned by full-site search may appear as 002594.
        if resolved_code:
            row_text = _clean_html_text(title + ' ' + content_for_code + ' ' + company)
            known_names = resolve_company_names(resolved_code)
            if code and code != resolved_code:
                return
            if not code and not any(n and n in row_text for n in known_names):
                return
            if not code:
                code = resolved_code
        date = _format_szse_time(_first_field(node, ['publishDate', 'date', 'pubDate', 'time', 'publishTime', 'docpubtime'])) or ''
        urls: list[str] = []
        # Prefer explicit row URL fields so the row title/date/code are preserved.
        for key in url_keys:
            val = node.get(key)
            if val is None:
                continue
            raw = str(val).strip()
            if not raw:
                continue
            detail = _normalize_pdf_url(raw, base_url)
            if detail not in urls:
                urls.append(detail)
        for url in _pdf_links_from_text(text_blob, base_url):
            if url not in urls:
                urls.append(url)
        for url in urls:
            if not url or any(x.url == url for x in items):
                continue
            item_title = title or Path(url.split('?')[0]).name
            items.append(DisclosureItem(item_title, code or None, company or None, date or None, source, url))

    # The real SZSE response from /api/search/content uses {totalSize, data:[...]}
    # with doctitle/doccontent/docpuburl/docpubtime. Parse these rows directly
    # before doing a recursive alternate_path; otherwise parent JSON blobs may create
    # filename-only candidates and hide the row metadata.
    if isinstance(data, dict) and isinstance(data.get('data'), list):
        for row in data['data']:
            if isinstance(row, dict):
                add_row(row)
        if items:
            return items
    for node in _walk_json(data):
        if isinstance(node, str):
            for url in _pdf_links_from_text(node, base_url):
                if not any(x.url == url for x in items):
                    items.append(DisclosureItem(Path(url.split('?')[0]).name, resolved_code or None, None, None, source, url))
            continue
        if not isinstance(node, dict):
            continue
        if not any(k in node for k in (url_keys + title_keys + ['doccontent', 'docpubtime'])):
            continue
        add_row(node)
    return items




class SearchDetails:
    """Lightweight detail collector for online-disclosure search."""
    def __init__(self):
        self.steps: list[str] = []
        self.requests: list[dict[str, Any]] = []
        self.started = time.time()

    def add(self, msg: str) -> None:
        self.steps.append(msg)

    def req(self, keyword: str, range_value: str, page: str, status: str, size: int = 0, parsed: int = 0, error: str = '') -> None:
        self.requests.append({
            'keyword': keyword,
            'range': range_value,
            'page': page,
            'status': status,
            'bytes': size,
            'parsed': parsed,
            'error': error[:120] if error else '',
            'success': str(status) == '200' and not error,
        })

    def as_dict(self) -> dict[str, Any]:
        return {'steps': self.steps, 'requests': self.requests, 'elapsed_sec': round(time.time() - self.started, 2)}


def _stock_code_from_item_text(item: DisclosureItem) -> str:
    return (item.code or '').strip()


def _exact_annual_title(title: str, report_type: str, year: int | None) -> bool:
    title = title or ''
    if not year or report_type != '年度报告':
        return True
    # Full annual report only; exclude summary / audit / sustainable etc in _filter.
    return bool(re.search(fr'{year}\s*年\s*年度报告', title)) and '摘要' not in title


def _search_szse_content_once(
    session: requests.Session,
    headers: dict[str, str],
    keyword: str,
    query: str,
    range_value: str = 'title',
    page: str = '1',
    page_size: str = '50',
    timeout: int = 5,
    diag: SearchDetails | None = None,
) -> list[DisclosureItem]:
    payload = {
        'keyword': keyword,
        'range': range_value,
        'time': '0',
        'orderby': 'score',
        'currentPage': page,
        'pageSize': page_size,
        'openChange': 'true',
        'searchtype': '1',
    }
    try:
        r = session.post('https://www.szse.cn/api/search/content', data=payload, headers=headers, timeout=timeout)
        size = len(r.text or '')
        if r.status_code != 200:
            if diag:
                diag.req(keyword, range_value, page, f'HTTP {r.status_code}', size, 0)
            return []
        data = _validated_search_json(r.text)
        items = _items_from_szse_search_json(data, query, '深交所', 'https://www.szse.cn/')
        if diag:
            diag.req(keyword, range_value, page, '200', size, len(items))
        return items
    except Exception as e:
        if diag:
            diag.req(keyword, range_value, page, 'EXCEPTION', 0, 0, repr(e))
        return []


def _normalize_sse_pdf_url(raw_url: str) -> str:
    """Normalize SSE disclosure PDF relative paths to the static host."""
    if not raw_url:
        return ''
    raw_url = raw_url.replace('\\/', '/').strip()
    if raw_url.startswith('http://') or raw_url.startswith('https://'):
        url = raw_url
    elif raw_url.startswith('//'):
        url = 'https:' + raw_url
    elif raw_url.startswith('/'):
        url = 'https://static.sse.com.cn' + raw_url
    else:
        url = 'https://static.sse.com.cn/' + raw_url
    url = url.replace('http://static.sse.com.cn/', 'https://static.sse.com.cn/')
    return url


def _flatten_sse_rows(obj: Any) -> list[dict[str, Any]]:
    """Flatten SSE pageHelp.data/result rows.

    SSE returns nested arrays: pageHelp.data = [[main_doc, attach_doc...], ...].
    We need every dict row because the full annual report is often an ORG_FILE_TYPE=1
    attachment within the annual-report group.
    """
    rows: list[dict[str, Any]] = []
    def walk(x: Any) -> None:
        if isinstance(x, dict):
            if any(k in x for k in ['TITLE', 'URL', 'SECURITY_CODE', 'SECURITY_NAME', 'SSEDATE']):
                rows.append(x)
            else:
                for v in x.values():
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    if isinstance(obj, dict):
        ph = obj.get('pageHelp')
        if isinstance(ph, dict) and isinstance(ph.get('data'), list):
            walk(ph.get('data'))
        elif isinstance(obj.get('result'), list):
            walk(obj.get('result'))
        else:
            walk(obj)
    else:
        walk(obj)
    # de-duplicate by URL/title
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for r in rows:
        key = (str(r.get('URL') or ''), str(r.get('TITLE') or ''))
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _items_from_sse_json(data: Any, source: str = '上交所') -> list[DisclosureItem]:
    items: list[DisclosureItem] = []
    for row in _flatten_sse_rows(data):
        title = _clean_html_text(row.get('TITLE') or row.get('title') or '')
        url = row.get('URL') or row.get('url') or ''
        if not url or '.pdf' not in str(url).lower():
            continue
        code = str(row.get('SECURITY_CODE') or row.get('securityCode') or '').strip() or None
        company = _clean_html_text(row.get('SECURITY_NAME') or row.get('securityName') or '') or None
        date = str(row.get('SSEDATE') or row.get('sseDate') or row.get('SSE_DATE') or '').strip() or None
        items.append(DisclosureItem(
            title=title,
            code=code,
            company=company,
            date=date,
            source=source,
            url=_normalize_sse_pdf_url(str(url)),
        ))
    return items


def _sse_date_window(report_type: str, year: int | None) -> tuple[str, str]:
    """Return SSE publish-date window for a requested report year.

    Annual reports for report year N are usually disclosed in N+1. To reduce
    noisy results while keeping tolerance for corrections, use Jan 1 to Dec 31 of
    N+1 for annual reports; for other reports, use N to N+1.
    """
    if not year:
        # broad but bounded range for generic search
        current = datetime.now().year
        return f'{current-3}-01-01', f'{current}-12-31'
    if report_type == '年度报告':
        return f'{year+1}-01-01', f'{year+1}-12-31'
    return f'{year}-01-01', f'{year+1}-12-31'


def _search_sse_impl(query: str, report_type: str = '年度报告', year: int | None = None, limit: int = 20, timeout: int = 6, diag: SearchDetails | None = None) -> list[DisclosureItem]:
    """Search SSE disclosures with details and SSE's real JSONP endpoint."""
    q = (query or '').strip()
    code = resolve_stock_code(q) or (q if re.fullmatch(r'\d{6}', q) else '')
    if not code:
        # SSE endpoint is code-oriented. Try mapping by company name first.
        code = resolve_stock_code(q) or ''
    if diag:
        diag.add(f'识别股票代码：{code or "未识别"}；上交所接口按 SECURITY_CODE 检索。')
    if not code:
        if diag:
            diag.add('上交所检索需要股票代码；当前公司名未在映射表中。可输入 600104 这类股票代码。')
        return []

    start_date, end_date = _sse_date_window(report_type, year)
    s = _session()
    url = 'https://query.sse.com.cn/security/stock/queryCompanyBulletinNew.do'
    headers = {
        'Referer': 'https://www.sse.com.cn/',
        'Accept': '*/*',
        'Connection': 'keep-alive',
    }
    candidates: list[DisclosureItem] = []
    max_pages = 4 if report_type == '年度报告' else 2
    page_size = 25

    for page_no in range(1, max_pages + 1):
        cb = f'jsonpCallback{int(time.time()*1000)%100000000}'
        params = {
            'jsonCallBack': cb,
            'isPagination': 'true',
            'pageHelp.pageSize': str(page_size),
            'pageHelp.cacheSize': '1',
            'START_DATE': start_date,
            'END_DATE': end_date,
            'SECURITY_CODE': code,
            'TITLE': '',  # use post-filtering; TITLE can be too strict on SSE
            'BULLETIN_TYPE': '',
            'stockType': '',
            'pageHelp.pageNo': str(page_no),
            'pageHelp.beginPage': str(page_no),
            'pageHelp.endPage': str(page_no),
            '_': str(int(time.time() * 1000)),
        }
        try:
            r = s.get(url, params=params, headers=headers, timeout=timeout)
            status = str(r.status_code)
            parsed = 0
            if r.status_code == 200:
                data = _validated_search_json(r.text)
                items = _items_from_sse_json(data, '上交所')
                parsed = len(items)
                for it in items:
                    if it.url and not any(x.url == it.url for x in candidates):
                        candidates.append(it)
                if diag:
                    diag.req(code, 'queryCompanyBulletinNew', str(page_no), status, len(r.text or ''), parsed)
                filtered_now = _filter(candidates, q or code, report_type, year, limit)
                if filtered_now:
                    if diag:
                        diag.add(f'上交所第 {page_no} 页已命中 {len(filtered_now)} 条有效候选，停止继续翻页。')
                    return filtered_now
                # Stop early if last page reached.
                if isinstance(data, dict):
                    ph = data.get('pageHelp')
                    if isinstance(ph, dict):
                        page_count = ph.get('pageCount') or ph.get('endPage')
                        try:
                            if page_count and int(page_no) >= int(page_count):
                                break
                        except Exception:
                            pass
            else:
                if diag:
                    diag.req(code, 'queryCompanyBulletinNew', str(page_no), status, len(r.text or ''), 0)
        except Exception as e:
            if diag:
                diag.req(code, 'queryCompanyBulletinNew', str(page_no), 'EXCEPTION', 0, 0, repr(e))
            break

    filtered = _filter(candidates, q or code, report_type, year, limit)
    if diag:
        diag.add(f'上交所原始候选 {len(candidates)} 条；最终过滤后 {len(filtered)} 条。')
        if not filtered and candidates:
            sample_titles = '；'.join([x.title for x in candidates[:5]])
            diag.add(f'样例标题：{sample_titles}')
    return filtered


def search_sse(query: str, report_type: str = '年度报告', year: int | None = None, limit: int = 20, timeout: int = 6) -> list[DisclosureItem]:
    return _search_sse_impl(query, report_type, year, limit, timeout, None)


def search_sse_with_details(query: str, report_type: str = '年度报告', year: int | None = None, limit: int = 20, timeout: int = 6) -> tuple[list[DisclosureItem], dict[str, Any]]:
    diag = SearchDetails()
    items = _search_sse_impl(query, report_type, year, limit, timeout, diag)
    return items, diag.as_dict()

def _search_szse_impl(query: str, report_type: str = '年度报告', year: int | None = None, limit: int = 20, timeout: int = 5, diag: SearchDetails | None = None) -> list[DisclosureItem]:
    """Fast, bounded SZSE search.

    The previous version could issue dozens of slow network requests, causing the
    Streamlit page to appear stuck. This implementation:
    - tries precise annual-report keywords first;
    - stops early once valid candidates are found;
    - uses short per-request timeouts;
    - records detail information for the UI.
    """
    q = str(query or '').strip()
    code = resolve_stock_code(q) or (q if re.fullmatch(r'\d{6}', q) else '')
    s = _session()
    base = 'https://www.szse.cn/'
    search_referer = f'https://www.szse.cn/application/search/index.html?keyword={quote(q)}'
    search_headers = {
        'Origin': 'https://www.szse.cn',
        'Referer': search_referer,
        'Content-Type': 'application/x-www-form-urlencoded',
        'X-Request-Type': 'ajax',
        'X-Requested-With': 'XMLHttpRequest',
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'Accept-Language': 'zh-CN,zh;q=0.9',
    }
    names = resolve_company_names(q)
    if diag:
        diag.add(f'识别股票代码：{code or "未识别"}；候选公司名：{"、".join(names) if names else "无"}')

    candidates: list[DisclosureItem] = []
    tried: set[tuple[str, str, str]] = set()

    # 1) Precise keywords first. These should find the exact report when available.
    keywords: list[str] = []
    if report_type and report_type != '全部公告' and year:
        for nm in names:
            keywords.extend([f'{nm}{year}年{report_type}', f'{nm} {year}年{report_type}'])
        if code:
            keywords.extend([f'{code}{year}年{report_type}', f'{code} {year}年{report_type}'])
    for nm in names:
        if report_type and report_type != '全部公告':
            keywords.append(f'{nm} {report_type}')
        keywords.append(nm)
    if code:
        if report_type and report_type != '全部公告':
            keywords.append(f'{code} {report_type}')
        keywords.append(code)
    keywords = [k for k in dict.fromkeys([x.strip() for x in keywords if x and x.strip()])]

    def add_items(new_items: list[DisclosureItem]) -> None:
        for it in new_items:
            if it.url and not any(x.url == it.url for x in candidates):
                candidates.append(it)

    # Fast phase: precise title searches only.
    for kw in keywords[:10]:
        for rv in ['title', 'all']:
            key = (kw, rv, '1')
            if key in tried:
                continue
            tried.add(key)
            add_items(_search_szse_content_once(s, search_headers, kw, q, rv, '1', '50', timeout, diag))
        filtered_now = _filter(candidates, q, report_type, year, limit)
        if filtered_now:
            if diag:
                diag.add(f'快速检索已命中 {len(filtered_now)} 条有效候选，停止继续请求。')
            return filtered_now

    # Learn company name from code search results, then retry one precise query.
    learned_names: list[str] = []
    for item in candidates:
        for nm in [item.company, item.title.split('：', 1)[0].strip() if item.title and '：' in item.title else None]:
            if nm and len(nm) <= 20 and nm not in learned_names:
                learned_names.append(nm)
    for nm in learned_names[:4]:
        if nm in names:
            continue
        if report_type and report_type != '全部公告' and year:
            kw = f'{nm}{year}年{report_type}'
        elif report_type and report_type != '全部公告':
            kw = f'{nm} {report_type}'
        else:
            kw = nm
        for rv in ['title', 'all']:
            key = (kw, rv, '1')
            if key in tried:
                continue
            tried.add(key)
            add_items(_search_szse_content_once(s, search_headers, kw, q, rv, '1', '50', timeout, diag))
        filtered_now = _filter(candidates, q, report_type, year, limit)
        if filtered_now:
            if diag:
                diag.add(f'根据搜索结果学习到公司名“{nm}”，已命中 {len(filtered_now)} 条有效候选。')
            return filtered_now

    # Broader phase: only two pages, bounded.
    broad_keywords = []
    for nm in names[:3]:
        if report_type and report_type != '全部公告':
            broad_keywords.append(f'{nm} {report_type}')
        broad_keywords.append(nm)
    if code:
        broad_keywords.append(code)
    for kw in dict.fromkeys([x for x in broad_keywords if x]):
        for page in ['1', '2']:
            key = (kw, 'all', page)
            if key in tried:
                continue
            tried.add(key)
            add_items(_search_szse_content_once(s, search_headers, kw, q, 'all', page, '50', timeout, diag))
        filtered_now = _filter(candidates, q, report_type, year, limit)
        if filtered_now:
            if diag:
                diag.add(f'扩展检索已命中 {len(filtered_now)} 条有效候选。')
            return filtered_now

    # Final alternate_path to old annList, with short timeout.
    referer = 'https://www.szse.cn/disclosure/listed/notice/index.html'
    headers = {'Referer': referer}
    date_range = f'{year}-01-01~{year + 1}-12-31' if year else ''
    if diag:
        diag.add('全站搜索未命中，尝试深交所公告列表接口备用导入。')
    for params in [
        {'random': str(time.time()), 'pageSize': str(limit), 'pageNum': '1', 'plateCode': '', 'stock': code, 'searchkey': report_type if report_type != '全部公告' else '', 'secid': '', 'categoryId': '', 'trade': '', 'seDate': date_range},
        {'random': str(time.time()), 'pageSize': str(limit), 'pageNum': '1', 'channelCode': 'listedNotice_disc', 'stock': code, 'searchkey': report_type if report_type != '全部公告' else '', 'seDate': date_range},
    ]:
        try:
            r = s.get('https://www.szse.cn/api/disc/announcement/annList', params=params, headers=headers, timeout=timeout)
            if r.status_code == 200:
                items = _items_from_any_json(_validated_search_json(r.text), '深交所', base)
                add_items(items)
                if diag:
                    diag.req('annList', 'api', params.get('pageNum','1'), '200', len(r.text or ''), len(items))
            elif diag:
                diag.req('annList', 'api', params.get('pageNum', '1'), f'HTTP {r.status_code}', len(r.text or ''), 0)
        except Exception as e:
            if diag:
                diag.req('annList', 'api', params.get('pageNum','1'), 'EXCEPTION', 0, 0, repr(e))
    filtered = _filter(candidates, q, report_type, year, limit)
    if diag:
        diag.add(f'原始候选 {len(candidates)} 条；最终过滤后 {len(filtered)} 条。')
    return filtered


def search_szse(query: str, report_type: str = '年度报告', year: int | None = None, limit: int = 20, timeout: int = 5) -> list[DisclosureItem]:
    return _search_szse_impl(query, report_type, year, limit, timeout, None)


def search_szse_with_details(query: str, report_type: str = '年度报告', year: int | None = None, limit: int = 20, timeout: int = 5) -> tuple[list[DisclosureItem], dict[str, Any]]:
    diag = SearchDetails()
    items = _search_szse_impl(query, report_type, year, limit, timeout, diag)
    return items, diag.as_dict()


def search_disclosures(query: str, source: str = '自动判断', report_type: str = '年度报告', year: int | None = None, limit: int = 20) -> list[DisclosureItem]:
    q = (query or '').strip()
    if not q:
        return []
    source_norm = source.lower()
    if source in ('自动判断', 'auto'):
        ex = guess_exchange(q)
        if ex == 'sse':
            return search_sse(q, report_type, year, limit)
        if ex == 'szse':
            return search_szse(q, report_type, year, limit)
        # Unknown: try both and merge
        return (search_sse(q, report_type, year, limit // 2 or 10) + search_szse(q, report_type, year, limit // 2 or 10))[:limit]
    if source in ('上交所', 'sse'):
        return search_sse(q, report_type, year, limit)
    if source in ('深交所', 'szse'):
        return search_szse(q, report_type, year, limit)
    return []


def search_disclosures_with_details(query: str, source: str = '自动判断', report_type: str = '年度报告', year: int | None = None, limit: int = 20) -> dict[str, Any]:
    """Separate actual empty responses from transport, API and parse failures.

    Candidates remain DisclosureItem objects; diagnostics keep the previous
    steps/requests shape so the detailed UI can display every attempted request.
    """
    started = time.monotonic()
    q = (query or '').strip()
    diag = SearchDetails()
    candidates: list[DisclosureItem] = []
    exchange = {'上交所': 'sse', '深交所': 'szse'}.get(source, source.lower())
    if exchange in ('自动判断', 'auto'):
        exchange = guess_exchange(q) or 'both'
    if not q:
        diag.add('请输入公司名称或股票代码。')
    elif exchange not in ('sse', 'szse', 'both'):
        diag.add('不支持的交易所检索条件。')
    else:
        routes = [('sse', search_sse_with_details, 6), ('szse', search_szse_with_details, 5)]
        for name, search, timeout in routes:
            if exchange not in (name, 'both'):
                continue
            try:
                items, details = search(q, report_type, year, limit, timeout=timeout)
                requests_log = details.get('requests') or []
                diag.steps.extend(details.get('steps') or [])
                diag.requests.extend(dict(row, exchange=name) for row in requests_log)
                if not requests_log:
                    diag.req(q, name, '1', 'NO_RESPONSE', error='没有有效请求响应。')
                for item in items:
                    if not isinstance(item, DisclosureItem) or not item.url:
                        raise ValueError('候选披露结构无效。')
                    if not any(existing.url == item.url for existing in candidates):
                        candidates.append(item)
            except Exception as exc:
                diag.req(q, name, '1', 'EXCEPTION', error=str(exc))
                diag.add(f'{name} 检索失败：{str(exc)[:160]}')
    candidates = candidates[:max(0, limit)]
    succeeded = sum(bool(row.get('success', str(row.get('status')) == '200' and not row.get('error'))) for row in diag.requests)
    failed = len(diag.requests) - succeeded
    status = 'success' if candidates else ('empty' if succeeded else 'failed')
    if status == 'success':
        message = f'检索完成：共获取 {len(candidates)} 条符合条件的公开披露。'
    elif status == 'empty':
        message = '检索完成，但未找到符合条件的公开披露。请检查公司名称或股票代码、交易所、报告类型和年份。'
    else:
        reason = next((row.get('error') or str(row.get('status')) for row in diag.requests if not row.get('success')), None)
        reason = reason or (diag.steps[-1] if diag.steps else '没有任何有效响应')
        message = f'检索失败：{str(reason).rstrip("。")[:160]}。请查看检索详情后重试。'
    elapsed = round(time.monotonic() - started, 3)
    diagnostics = diag.as_dict()
    diagnostics['elapsed_sec'] = elapsed
    return {'status': status, 'candidates': candidates, 'candidate_count': len(candidates),
            'message': message, 'query': q, 'exchange': exchange, 'report_type': report_type,
            'year': year, 'request_success_count': succeeded, 'request_failure_count': failed,
            'elapsed_sec': elapsed, 'diagnostics': diagnostics}


def _pdf_request_headers(url: str) -> dict[str, str]:
    """Headers that make direct exchange PDF downloads closer to a browser."""
    headers = {
        'User-Agent': USER_AGENT,
        'Accept': 'application/pdf,application/octet-stream,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Accept-Encoding': 'identity',
        'Connection': 'close',
    }
    if 'static.sse.com.cn' in url or 'sse.com.cn' in url:
        # SSE PDF files should be fetched from the static host. Using www.sse.com.cn
        # for the same path often returns an anti-bot HTML/JS page (var arg1=...), not a PDF.
        headers.update({
            'Referer': 'https://www.sse.com.cn/disclosure/listedinfo/announcement/',
            'Origin': 'https://www.sse.com.cn',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'same-site',
            'Upgrade-Insecure-Requests': '1',
        })
    elif 'disc.static.szse.cn' in url or 'szse.cn' in url:
        headers['Referer'] = 'https://www.szse.cn/'
    return headers


def _pdf_header_pos(content: bytes) -> int:
    """Return the position of the real PDF header, or -1 if absent."""
    if not content:
        return -1
    return content[:8192].find(b'%PDF')


def _is_strict_pdf(content: bytes) -> bool:
    pos = _pdf_header_pos(content)
    return 0 <= pos < 8192


def _response_preview(content: bytes, max_len: int = 300) -> str:
    head = content[:max_len]
    try:
        return head.decode('utf-8', errors='ignore').replace('\n', ' ').replace('\r', ' ')
    except Exception:
        return repr(head[:100])


def _canonical_sse_static_url(url: str) -> str:
    """Convert SSE disclosure PDF paths to the static announcement host.

    The static host is used for direct announcement PDF access.
    """
    if not url:
        return url
    u = url.replace('\\/', '/').strip()
    if u.startswith('/disclosure/listedinfo/announcement/'):
        return 'https://static.sse.com.cn' + u
    if u.startswith('http://www.sse.com.cn/disclosure/listedinfo/announcement/'):
        return u.replace('http://www.sse.com.cn', 'https://static.sse.com.cn', 1)
    if u.startswith('https://www.sse.com.cn/disclosure/listedinfo/announcement/'):
        return u.replace('https://www.sse.com.cn', 'https://static.sse.com.cn', 1)
    if u.startswith('http://static.sse.com.cn/'):
        return u.replace('http://static.sse.com.cn/', 'https://static.sse.com.cn/', 1)
    return u


def _sse_pdf_url_variants(url: str) -> list[str]:
    """Return safe SSE PDF URL variants for direct announcement PDF access."""
    canonical = _canonical_sse_static_url(url)
    variants = [canonical]
    if canonical.startswith('https://static.sse.com.cn/'):
        variants.append(canonical.replace('https://static.sse.com.cn/', 'http://static.sse.com.cn/', 1))
    # de-duplicate
    out: list[str] = []
    for x in variants:
        if x and x not in out:
            out.append(x)
    return out


def _validate_downloaded_pdf(content: bytes, headers: dict[str, str], final_url: str) -> bytes:
    ctype = headers.get('Content-Type', '') or headers.get('content-type', '')
    pos = _pdf_header_pos(content)
    if pos < 0:
        preview = _response_preview(content)
        if 'var arg1=' in preview or 'function(name,arg1)' in preview or 'text/html' in ctype:
            raise ValueError(f'返回内容暂未识别为 PDF，而是上交所网页/访问校验页面：Content-Type={ctype or "未知"}，bytes={len(content)}，final_url={final_url}，预览={preview}')
        raise ValueError(f'返回内容暂未识别为 PDF：Content-Type={ctype or "未知"}，bytes={len(content)}，final_url={final_url}，预览={preview}')
    if pos > 0:
        content = content[pos:]
    if len(content) < 1024:
        raise ValueError(f'PDF 内容过小：{len(content)} 字节')
    return content


def _download_binary_once(url: str, timeout: int = 30) -> tuple[bytes, dict[str, str], str]:
    """Download binary content once.

    For SSE static PDFs, first try curl_cffi when available because plain
    requests can be identified by the exchange as a non-browser client and may
    receive an HTML JS challenge instead of the PDF. If curl_cffi is unavailable
    or fails, fall back to requests.
    """
    headers = _pdf_request_headers(url)
    last_error: Exception | None = None

    if ('static.sse.com.cn' in url or 'sse.com.cn' in url) and curl_requests is not None:
        try:
            r = curl_requests.get(url, timeout=timeout, headers=headers, impersonate='chrome120', allow_redirects=True)
            if getattr(r, 'status_code', 0) >= 400:
                raise ValueError(f'HTTP {r.status_code}')
            content = bytes(r.content or b'')
            expected = r.headers.get('Content-Length') if hasattr(r, 'headers') else None
            if expected and str(expected).isdigit() and len(content) < int(expected) * 0.95:
                raise ValueError(f'文件下载内容不完整：应约 {expected} 字节，实际 {len(content)} 字节')
            if len(content) < 1024:
                raise ValueError(f'下载内容过小：{len(content)} 字节')
            return content, dict(r.headers), str(getattr(r, 'url', url))
        except Exception as e:
            last_error = e

    try:
        s = _session()
        # For SSE, seed the session by touching the announcement page so any normal
        # cookies set by the website can be carried into the static PDF request.
        if 'static.sse.com.cn' in url:
            try:
                s.get('https://www.sse.com.cn/disclosure/listedinfo/announcement/', headers={'User-Agent': USER_AGENT}, timeout=(5, 8))
            except Exception:
                pass
        r = s.get(url, timeout=(8, timeout), stream=True, headers=headers, allow_redirects=True)
        r.raise_for_status()
        chunks: list[bytes] = []
        for chunk in r.iter_content(chunk_size=1024 * 256):
            if chunk:
                chunks.append(chunk)
        content = b''.join(chunks)
        expected = r.headers.get('Content-Length')
        if expected and expected.isdigit() and len(content) < int(expected) * 0.95:
            raise ValueError(f'文件下载内容不完整：应约 {expected} 字节，实际 {len(content)} 字节')
        if len(content) < 1024:
            raise ValueError(f'下载内容过小：{len(content)} 字节')
        return content, dict(r.headers), str(r.url)
    except Exception as e:
        if last_error:
            raise ValueError(f'curl_cffi下载未完成：{last_error}；requests下载未完成：{e}')
        raise


def _download_pdf_bytes_strict(url: str, timeout: int = 30, retries: int = 3) -> tuple[bytes, dict[str, str], str, list[str]]:
    """Download and validate a real PDF; never trust a .pdf suffix alone."""
    notes: list[str] = []
    last_error: Exception | None = None
    variants = _sse_pdf_url_variants(url) if 'sse.com.cn' in url or url.startswith('/disclosure/listedinfo/announcement/') else [url]
    for variant in variants:
        for attempt in range(1, retries + 1):
            try:
                content, headers, final_url = _download_binary_once(variant, timeout=timeout)
                content = _validate_downloaded_pdf(content, headers, final_url)
                return content, headers, final_url, notes
            except Exception as e:
                last_error = e
                notes.append(f'{variant} 第 {attempt} 次下载未完成：{e}')
                time.sleep(0.6 * attempt)
    detail = '；'.join(notes)
    raise ValueError(f'PDF 下载暂未返回可解析文件，系统已自动重试。原因：{last_error}。运行详情：{detail}')


def download_pdf(url: str, file_name: str | None = None, timeout: int = 30) -> tuple[str, bytes]:
    """Use the single bounded downloader; never fall back to legacy fetchers."""
    return safe_download_pdf(url, file_name=file_name, timeout=timeout)


def cache_pdf(content: bytes, file_name: str) -> Path:
    require_write_access()
    validate_pdf_bytes(content)
    safe_name = re.sub(r'[^\w\-.\u4e00-\u9fa5]+', '_', Path(str(file_name).replace('\\', '/')).name).strip(' .')[:120] or 'report.pdf'
    if not safe_name.lower().endswith('.pdf'):
        safe_name += '.pdf'
    cache_root = get_data_root() / 'online_cache'
    cache_root.mkdir(parents=True, exist_ok=True)
    path = cache_root / f'{sha256(content).hexdigest()[:16]}_{uuid4().hex}_{safe_name}'
    with path.open('xb') as handle:
        handle.write(content)
    return path
