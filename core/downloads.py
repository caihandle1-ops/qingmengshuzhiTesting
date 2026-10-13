"""Bounded official-disclosure downloads with per-hop validation and IP pinning.

Only exact operator-owned hostnames below are allowed. DNS results are checked
before a TLS connection to the selected *numeric IP*; Host, SNI and certificate
validation retain the official hostname. No proxy, redirect or retry fallback
may silently choose a different network destination.
"""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
from pathlib import PurePosixPath
from queue import Queue, Empty
import re
import socket
from threading import BoundedSemaphore, Thread
import time
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import certifi
import urllib3

ALLOWED_PDF_HOSTS = frozenset({
    'cninfo.com.cn', 'www.cninfo.com.cn', 'static.cninfo.com.cn',
    'sse.com.cn', 'www.sse.com.cn', 'static.sse.com.cn',
    'szse.cn', 'www.szse.cn', 'disc.static.szse.cn',
    'bse.cn', 'www.bse.cn', 'www.neeq.com.cn', 'static.neeq.com.cn',
})
MAX_PDF_BYTES = 30 * 1024 * 1024
MAX_TOTAL_SECONDS = 30.0
MAX_REDIRECTS = 3
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 5.0
DNS_TIMEOUT = 5.0
_DNS_SLOTS = BoundedSemaphore(4)
_UPLOAD_HINT = '请从官方披露网站自行下载原 PDF，再使用文件上传。'


class DownloadSafetyError(ValueError):
    pass


@dataclass(frozen=True)
class DownloadTarget:
    url: str
    host: str
    address: str
    request_path: str


def _error(message: str) -> DownloadSafetyError:
    return DownloadSafetyError(message + ' ' + _UPLOAD_HINT)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _error('下载超过总耗时上限，已停止。')
    return remaining


def _resolve_addresses(host: str, deadline: float) -> list[str]:
    # The socket resolver has no portable timeout argument. A bounded daemon
    # worker limits caller wait and prevents unbounded abandoned DNS threads.
    if not _DNS_SLOTS.acquire(blocking=False):
        raise _error('域名解析正在繁忙，已停止本次下载。')
    result: Queue = Queue(maxsize=1)

    def resolve():
        try:
            result.put((True, socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
        except Exception as exc:
            result.put((False, exc))
        finally:
            _DNS_SLOTS.release()

    Thread(target=resolve, name='official-pdf-dns', daemon=True).start()
    try:
        ok, addresses = result.get(timeout=min(DNS_TIMEOUT, _remaining(deadline)))
    except Empty as exc:
        raise _error('域名解析超时，已停止下载。') from exc
    if not ok:
        raise _error('无法解析官方披露域名。') from addresses
    ips = sorted({str(item[4][0]) for item in addresses}, key=lambda value: (':' in value, value))
    if not ips:
        raise _error('域名没有可用公网地址。')
    for value in ips:
        try:
            ip = ipaddress.ip_address(value)
        except ValueError as exc:
            raise _error('域名解析结果不是有效 IP 地址。') from exc
        if (not ip.is_global or ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_reserved or ip.is_unspecified or getattr(ip, 'ipv4_mapped', None) is not None):
            raise _error('域名解析包含非公网、保留或本地网络地址，已拦截。')
    _remaining(deadline)
    return ips


def validate_download_target(url: str, deadline: float | None = None) -> DownloadTarget:
    deadline = deadline if deadline is not None else time.monotonic() + MAX_TOTAL_SECONDS
    if not isinstance(url, str) or len(url) > 4096 or not url or any(ch.isspace() or ord(ch) < 32 for ch in url) or '\\' in url:
        raise _error('下载链接格式无效。')
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').lower()
        port = parsed.port
    except ValueError as exc:
        raise _error('下载链接格式无效。') from exc
    if parsed.scheme.lower() != 'https':
        raise _error('仅允许官方披露网站的 HTTPS 直达 PDF 链接。')
    if parsed.username is not None or parsed.password is not None or port not in (None, 443):
        raise _error('链接不得包含用户凭证或非标准 HTTPS 端口。')
    if host not in ALLOWED_PDF_HOSTS or parsed.netloc.lower() not in {host, host + ':443'}:
        raise _error('下载目标不在官方披露域名白名单内。')
    ips = _resolve_addresses(host, deadline)
    canonical = urlunsplit(('https', host, parsed.path or '/', parsed.query, ''))
    path = urlunsplit(('', '', parsed.path or '/', parsed.query, ''))
    return DownloadTarget(canonical, host, ips[0], path)


def _open_pinned(target: DownloadTarget, remaining: float):
    # urllib3's documented custom-SNI interface allows direct IP connections
    # while checking the original host certificate. Numeric pool.host prevents
    # a second hostname lookup and thus a DNS rebinding check/use race.
    pool = urllib3.HTTPSConnectionPool(
        target.address, port=443, server_hostname=target.host,
        assert_hostname=target.host, cert_reqs='CERT_REQUIRED', ca_certs=certifi.where(),
        retries=False, maxsize=1,
    )
    try:
        response = pool.request(
            'GET', target.request_path,
            headers={'Host': target.host, 'User-Agent': 'FinancialReportReader/2.0',
                     'Accept': 'application/pdf,application/octet-stream;q=0.8',
                     'Accept-Encoding': 'identity', 'Connection': 'close'},
            preload_content=False, decode_content=False, redirect=False, retries=False,
            timeout=urllib3.Timeout(total=remaining, connect=min(CONNECT_TIMEOUT, remaining),
                                    read=min(READ_TIMEOUT, remaining)),
        )
        return response, pool
    except Exception:
        pool.close()
        raise


def validate_pdf_bytes(content: bytes) -> None:
    if not isinstance(content, bytes) or not content or len(content) > MAX_PDF_BYTES:
        raise _error('PDF 为空或超过 30 MB 文件大小上限。')
    # Do not accept HTML containing an embedded "%PDF" string or strip an
    # arbitrary HTML preamble as the older downloader did.
    if not re.match(rb'^%PDF-[12]\.\d(?:\r|\n|\s)', content[:16]):
        raise _error('返回内容不是以有效 PDF 文件头开头的文件。')
    if b'%%EOF' not in content[-4096:]:
        raise _error('PDF 缺少完整结束标记，可能下载不完整。')


def safe_download_pdf(url: str, file_name: str | None = None, timeout: int | float = 30) -> tuple[str, bytes]:
    """Return a verified official PDF; all redirects share one time/byte budget."""
    try:
        budget = min(float(timeout), MAX_TOTAL_SECONDS)
    except (ValueError, TypeError) as exc:
        raise _error('下载时限无效。') from exc
    if not 0 < budget <= MAX_TOTAL_SECONDS:
        raise _error('下载时限无效。')
    deadline = time.monotonic() + budget
    current = url
    try:
        for hop in range(MAX_REDIRECTS + 1):
            target = validate_download_target(current, deadline)
            response, pool = _open_pinned(target, _remaining(deadline))
            try:
                _remaining(deadline)
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get('Location')
                    if not location or hop >= MAX_REDIRECTS:
                        raise _error('重定向缺少目标或超过允许次数，已停止下载。')
                    if any(ch.isspace() or ord(ch) < 32 for ch in location) or '\\' in location:
                        raise _error('重定向地址格式无效，已停止下载。')
                    current = urljoin(target.url, location)
                    continue
                if response.status != 200:
                    raise _error(f'官方披露服务返回 HTTP {response.status}，未取得 PDF。')
                encoding = str(response.headers.get('Content-Encoding') or 'identity').strip().lower()
                if encoding not in {'', 'identity'}:
                    raise _error('服务器返回压缩内容，已拒绝以保证下载大小限制。')
                content_type = str(response.headers.get('Content-Type') or '').lower()
                if 'text/html' in content_type or 'application/json' in content_type:
                    raise _error('服务器返回网页或访问校验内容，未递归跟随网页链接。')
                expected = response.headers.get('Content-Length')
                if expected is not None:
                    if not re.fullmatch(r'\d+', str(expected)) or int(expected) > MAX_PDF_BYTES:
                        raise _error('文件长度无效或超过 30 MB 上限。')
                    expected = int(expected)
                content = bytearray()
                while True:
                    remaining = _remaining(deadline)
                    connection = getattr(response, 'connection', None)
                    sock = getattr(connection, 'sock', None)
                    if sock is not None:
                        sock.settimeout(min(READ_TIMEOUT, remaining))
                    # read1 performs at most one raw read. Regular read(n) can
                    # wait indefinitely while a peer drips bytes below n.
                    chunk = response.read1(64 * 1024, decode_content=False)
                    _remaining(deadline)
                    if not chunk:
                        break
                    if len(content) + len(chunk) > MAX_PDF_BYTES:
                        raise _error('下载内容超过 30 MB，已中止传输。')
                    content.extend(chunk)
                if expected is not None and len(content) != expected:
                    raise _error('实际下载字节数与声明长度不符。')
                result = bytes(content)
                validate_pdf_bytes(result)
                candidate = file_name or unquote(PurePosixPath(urlsplit(target.url).path).name) or 'annual-report.pdf'
                name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', str(candidate)).strip(' .')[:160] or 'annual-report.pdf'
                if not name.lower().endswith('.pdf'):
                    name += '.pdf'
                return name, result
            finally:
                response.close()
                pool.close()
    except DownloadSafetyError:
        raise
    except Exception as exc:
        # No requests/curl impersonation, HTML recursion or HTTP fallback.
        raise _error('安全下载未完成（网络、TLS 或读取超时）。') from exc
    raise _error('未取得可用 PDF。')
