"""Synthetic text-only PDF input; contains no product UI or report styling."""
from io import BytesIO
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont


def pdf_bytes(*, document_only=False):
    pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(800, 700))
    pdf.setFont('STSong-Light', 12)
    lines = ['测试科技股份有限公司', '证券代码：688999', '2024年年度报告', '主要会计数据和财务指标', '单位：亿元']
    lines += (['公司业务主要围绕科技服务开展。'] if document_only else
              ['指标      2024年      2023年', '营业收入      100      90',
               '归母净利润      10      8', '经营现金流      20      15'])
    for index, line in enumerate(lines):
        pdf.drawString(30, 670 - index * 28, line)
    pdf.save()
    return buffer.getvalue()
