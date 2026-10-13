"""Diagnostic sanitization: preserve factual diagnostics without exposing local paths."""
from __future__ import annotations

import re

NO_REPORTS = '当前尚未接入报告文件，请前往数据中心完成数据接入。'
NO_METRICS = '当前资料库暂无可用于分析的年度财务指标，请前往数据中心完成数据接入与复核。'
NO_SOURCE = '当前结果暂无可核验的来源信息。'
PARSE_COMPLETE = '解析完成：请核验企业、报告期及指标数据，确认无误后执行入库。'


def public_message(value, fallback='操作未完成，请核验输入条件后重试。') -> str:
    text = str(value or '').strip()
    if not text:
        return fallback
    if 'Traceback (most recent call last)' in text:
        return fallback
    text = re.sub(r'[A-Za-z]:[\\/][^\s\n\r<>\"\'，。；]+', '[本地文件]', text)
    text = re.sub(r'(?<![:/\w])/(?:Users|home|tmp|var|private|opt|workspace|app|mnt)/[^\s\n\r<>\"\'，。；]+', '[本地文件]', text)
    return text


def public_details(value):
    if isinstance(value, dict):
        return {key: public_details(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [public_details(item) for item in value]
    return public_message(value) if isinstance(value, str) and value.strip() else value

PAGE_TITLE = "擎梦数智技术测试报告"
REPORT_SUBTITLE = "财务分析结果与来源记录"
