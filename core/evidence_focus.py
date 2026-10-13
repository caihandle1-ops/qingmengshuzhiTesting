"""Locate frozen excerpts on their recorded PDF page, without changing originals."""
from __future__ import annotations

from hashlib import sha256
import json
import math
import unicodedata

MAX_PAGE_PIXELS = 4_000_000
MAX_PAGE_EDGE = 2400
MAX_PAGE_TEXT = 120_000
MAX_HIGHLIGHT_RECTS = 64


def rasterize_pdf_page(page):
    """Bound real PDF MediaBoxes before allocating any raster, including rotation."""
    import fitz
    width, height = page.rect.width, page.rect.height
    if not all(math.isfinite(value) and value > 0 for value in (width, height)):
        raise ValueError('PDF 页面尺寸无效。')
    # The one-pixel edge and rounding allowance keep ceil-rounded pixmaps inside
    # the limits too; a tiny compressed PDF may describe a huge drawing surface.
    scale = min(1.6, (MAX_PAGE_EDGE - 1) / width, (MAX_PAGE_EDGE - 1) / height,
                math.sqrt((MAX_PAGE_PIXELS - 2 * MAX_PAGE_EDGE) / width / height))
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError('PDF 页面尺寸超出可预览范围。')
    return page.get_pixmap(matrix=fitz.Matrix(scale, scale)).tobytes('png')


def source_entry_identity(entry):
    """A chart and reader share only an exact frozen company/year/metric/source."""
    return sha256(json.dumps({field: entry.get(field) for field in
        ('company', 'year', 'metric', 'source')}, ensure_ascii=False,
        sort_keys=True, default=str).encode()).hexdigest()


def _normalized(text):
    return ''.join(character for character in unicodedata.normalize('NFC', str(text or ''))
                   if not character.isspace())


def _page_number(value):
    try:
        number = int(value)
        return number if not isinstance(value, bool) and number > 0 and float(value) == number else None
    except (ValueError, TypeError, OverflowError):
        return None


def pdf_evidence_focus(original: bytes, source, *, selected_page=None, render=False):
    """Read authorized bytes; whitespace-only normalization, no fuzzy/value search.

    Only the saved page is considered. A unique full snippet of sufficient text
    context identifies rectangles; this is text location, never a financial verdict.
    No annotation or file write is performed, even on the in-memory document.
    """
    import fitz
    with fitz.open(stream=original, filetype='pdf') as document:
        count = len(document)
        if not count:
            raise ValueError('PDF 没有可读取页面。')
        recorded = _page_number(source.get('page'))
        requested = _page_number(selected_page) or recorded or 1
        current = min(requested, count)
        page = document[current - 1]
        result = {'status': 'unlocated', 'page': current, 'pages': count,
                  'recorded_page': recorded, 'rectangles': []}
        if render:
            result['image'] = rasterize_pdf_page(page)
        def pending(reason):
            result['rectangles'] = []
            result['message'] = reason + ' 定位待复核；不会推断高亮位置。'
            return result
        if recorded is None or recorded > count:
            return pending('保存的引用页码缺失、无效或超出当前原件页数。')
        if current != recorded:
            result['status'] = 'browsing'
            result['message'] = f'当前为手动浏览第 {current} 页，保存的引用位于第 {recorded} 页；本页不显示该引用高亮。'
            return result
        snippet = _normalized(source.get('snippet'))
        if len(snippet) < 10 or sum(character.isalpha() for character in snippet) < 4:
            return pending('冻结引用未保存足够的文字上下文；页码或单个数值不足以确认页内位置。')
        if len(snippet) > 6000:
            return pending('冻结引用摘录过长，请结合保存页码人工核对。')
        if len(page.get_text('text')) > MAX_PAGE_TEXT:
            return pending('保存页文字层过于密集，超过精确定位的处理上限。')
        characters, positions = [], []
        extracted = page.get_text('rawdict', sort=True)
        for block in extracted.get('blocks', []):
            for line_index, line in enumerate(block.get('lines', [])):
                # A separate identity per actual PDF line keeps nearby columns
                # from being painted as one invented rectangle across the page.
                line_identity = (id(block), line_index)
                for span in line.get('spans', []):
                    for item in span.get('chars', []):
                        for character in _normalized(item.get('c')):
                            characters.append(character)
                            positions.append((line_identity, item.get('bbox')))
                            if len(characters) > MAX_PAGE_TEXT:
                                return pending('保存页文字层过于密集，超过精确定位的处理上限。')
        if not characters:
            return pending('保存页没有可检索的文字层，可能为扫描页；未进行 OCR 猜测。')
        text = ''.join(characters)
        start = text.find(snippet)
        if start < 0:
            return pending('冻结摘录与保存页的当前文字层不一致，可能存在缺字、页码或原件变化。')
        if text.find(snippet, start + 1) >= 0:
            return pending('冻结摘录在保存页出现多次，无法唯一确定引用位置。')
        grouped = {}
        for line, coordinates in positions[start:start + len(snippet)]:
            if not coordinates or not all(math.isfinite(value) for value in coordinates):
                return pending('引用文字的坐标不可用。')
            rectangle = fitz.Rect(coordinates)
            grouped[line] = (grouped[line] | rectangle) if line in grouped else rectangle
            if len(grouped) > MAX_HIGHLIGHT_RECTS:
                return pending('冻结摘录跨越过多文字区域，超过可靠展示上限。')
        # Extracted rectangles use the unrotated coordinate space. Pixmaps use
        # the page rotation, so rotate all four corners before overlaying.
        bounds = page.rect
        for rectangle in grouped.values():
            rectangle = (rectangle * page.rotation_matrix) & bounds
            if rectangle.is_empty or bounds.width <= 0 or bounds.height <= 0:
                return pending('引用文字的坐标超出可显示页面。')
            result['rectangles'].append({'x': 100 * rectangle.x0 / bounds.width,
                'y': 100 * rectangle.y0 / bounds.height,
                'width': 100 * rectangle.width / bounds.width,
                'height': 100 * rectangle.height / bounds.height})
        result.update(status='located', message='已按冻结摘录在保存页唯一定位。高亮只表示文字位置，不代表数值或口径核验通过。')
        return result
