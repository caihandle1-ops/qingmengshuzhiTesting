"""Strict, reusable normalization for financial imports.

Money is stored in CNY 亿元; ratios are stored in percentage points.
Unknown units never silently receive a default.
"""
from __future__ import annotations

import math
import re
from typing import Any

MONEY_METRICS = ('revenue', 'net_profit', 'operating_cashflow', 'consolidated_net_profit', 'total_assets', 'total_liabilities', 'total_equity', 'minority_profit')
PERCENT_METRICS = ('roe', 'debt_ratio', 'gross_margin')
NUMERIC_METRICS = MONEY_METRICS + PERCENT_METRICS + ('eps',)
METRICS = NUMERIC_METRICS + ('audit_opinion',)
MONEY_FACTORS = {'亿元': 1.0, '百万元': .01, '万元': .0001, '千元': .00001, '元': .00000001}
MISSING = {'', '-', '--', '—', '–', '/', '不适用', 'nan', 'none', 'null', 'n/a'}


def blank(value: Any) -> bool:
    if value is None:
        return True
    try:
        if isinstance(value, (float, int)) and math.isnan(value):
            return True
    except (TypeError, ValueError):
        pass
    return str(value).strip().lower() in MISSING


def detect_money_unit(text: str) -> str | None:
    text = str(text).replace(' ', '').replace('人民币', '').replace('（', '(').replace('）', ')')
    # Currency conversion needs an exchange rate and is deliberately not guessed.
    if any(currency in text for currency in ('美元', '港元', '港币', '欧元', 'USD', 'HKD', 'EUR')):
        return '不支持币种'
    for unit in MONEY_FACTORS:
        if unit in text:
            return unit
    if '(亿)' in text:
        return '亿元'
    return None


def detect_percent_unit(text: str) -> str | None:
    text = str(text).replace('％', '%').lower()
    if '%' in text or '百分数' in text or '百分比' in text:
        return '百分数'
    if '比例' in text or 'ratio' in text or '0-1' in text or '0–1' in text:
        return '比例'
    return None


def strict_number(value: Any) -> float | None:
    if blank(value):
        return None
    if isinstance(value, bool):
        raise ValueError('布尔值不是财务数值')
    text = str(value).strip().replace('，', ',').replace('％', '%').replace('−', '-').replace('人民币', '')
    text = text.replace(',', '').replace(' ', '')
    negative = text.startswith(('(', '（')) and text.endswith((')', '）'))
    if negative:
        text = text[1:-1]
    text = re.sub(r'(?:亿元|百万元|万元|千元|元|%|元/股)$', '', text)
    if not re.fullmatch(r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?', text):
        raise ValueError(f'无法明确识别数值“{str(value)[:40]}”')
    number = float(text) * (-1 if negative else 1)
    if not math.isfinite(number):
        raise ValueError('数值必须是有限数字')
    return number


def normalize_metric(metric: str, value: Any, source_unit: str | None,
                     money_unit: str | None = None, percent_unit: str | None = None) -> tuple[float | None, str | None, str]:
    number = strict_number(value)
    normalized = '亿元' if metric in MONEY_METRICS else ('%' if metric in PERCENT_METRICS else '元/股')
    if number is None:
        return None, source_unit, normalized
    if metric in MONEY_METRICS:
        if '%' in str(value) or '％' in str(value):
            raise ValueError('金额列出现百分比，疑似误取增减幅列，请核对表头列位置')
        unit = detect_money_unit(str(value)) or source_unit or money_unit
        if unit not in MONEY_FACTORS:
            raise ValueError('金额单位未知或币种不支持，请选择原始金额单位（不根据数值大小猜测）')
        number *= MONEY_FACTORS[unit]
    elif metric in PERCENT_METRICS:
        unit = detect_percent_unit(str(value)) or source_unit or percent_unit
        unit = {'%': '百分数', '百分比': '百分数', '0-1': '比例', '小数比例': '比例'}.get(unit, unit)
        if unit not in ('百分数', '比例'):
            raise ValueError('百分比口径未知，请确认是百分数（如 12.5）还是比例（如 0.125）')
        if unit == '比例':
            number *= 100
    else:
        unit = source_unit or '元/股'
        if unit not in ('元/股', '元'):
            raise ValueError('每股收益仅接受元/股，不能沿用总金额单位')
    if metric == 'revenue' and number < 0:
        raise ValueError('营业收入为负数，需人工核对原文后重新导入')
    if metric in ('debt_ratio', 'gross_margin') and not -100 <= number <= 1000:
        raise ValueError('比率超出校验范围，请核对单位及百分比口径')
    return round(number, 10), unit, normalized


def validate_records(records: list[dict]) -> list[str]:
    """Revalidate at commit so a confirmation flag cannot bypass blockers."""
    errors: list[str] = []
    seen: set[tuple[str, int]] = set()
    if not records:
        return ['未提取到可核验的结构化财务指标，请上传规范表格或可解析的年报。']
    for index, record in enumerate(records, 1):
        prefix = f'第 {index} 条记录'
        errors.extend(f'{prefix}：{error}' for error in record.get('_errors', []))
        company = str(record.get('company_name') or '').strip()
        if not company:
            errors.append(f'{prefix}：缺少企业名称，请填写真实企业名称。')
        year = record.get('year')
        if isinstance(year, bool) or not isinstance(year, int) or not 1900 <= year <= 2100:
            errors.append(f'{prefix}：报告年度缺失或无效。')
        elif (company, year) in seen:
            errors.append(f'{prefix}：同一企业同一年度存在多行，请合并或核对重复数据。')
        else:
            seen.add((company, year))
        populated = [key for key in NUMERIC_METRICS if record.get(key) is not None]
        source_locations: set[tuple] = set()
        if not populated:
            errors.append(f'{prefix}：没有可核验的数值指标，不能创建空年度记录。')
        for key in populated:
            value = record[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                errors.append(f'{prefix}：{key} 不是有效有限数值。')
            source = record.get('_provenance', {}).get(key, {})
            if not source.get('raw_unit') or not source.get('normalized_unit'):
                errors.append(f'{prefix}：{key} 缺少可核验的原始单位。')
            location = (source.get('file_name'), source.get('page'), source.get('cell'))
            if source.get('cell'):
                if location in source_locations:
                    errors.append(f'{prefix}：多个指标指向同一原始单元格，疑似行列定位错误。')
                source_locations.add(location)
    return list(dict.fromkeys(errors))
