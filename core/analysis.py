from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import math

import pandas as pd
from core.public_messages import PAGE_TITLE, REPORT_SUBTITLE
from core.public_messages import NO_METRICS, NO_SOURCE, public_message
from core.data_identity import summarize_data_identity, identity_report_lines, identity_count_text

METRIC_LABELS = {
    'revenue': '营业收入',
    'net_profit': '归母净利润',
    'operating_cashflow': '经营现金流',
    'roe': 'ROE',
    'debt_ratio': '资产负债率',
    'gross_margin': '毛利率',
    'eps': '每股收益',
}

METRIC_UNITS = {
    'revenue': '亿元',
    'net_profit': '亿元',
    'operating_cashflow': '亿元',
    'roe': '%',
    'debt_ratio': '%',
    'gross_margin': '%',
    'eps': '元/股',
}

PROFILE_RULES = {
    '稳健型': '更重视现金流质量、资产负债率和利润稳定性。',
    '平衡型': '综合关注盈利能力、现金流质量、偿债稳健性与成长能力。',
    '成长型': '更重视营收增长、盈利弹性、长期成长空间和产业链优势。',
}


NUMERIC_COLUMNS = [
    'year', 'revenue', 'net_profit', 'operating_cashflow',
    'roe', 'debt_ratio', 'gross_margin', 'eps'
]


def _normalize_numeric_value(value: Any) -> Any:
    """Convert common financial strings to numbers without breaking text fields.

    Uploaded Excel/PDF extracts may contain values like '24.72%',
    '1,332.20亿元', '--' or empty strings. Plotly and scoring functions need
    pure numeric columns, so we normalize these values at the data-frame edge.
    """
    if value is None or pd.isna(value):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    text = str(value).strip()
    if text in {'', '-', '--', '—', '暂无数据', 'nan', 'None'}:
        return None
    for token in [',', '，', '%', '％', '亿元', '亿', '元/股', '元', '倍']:
        text = text.replace(token, '')
    try:
        return float(text)
    except Exception:
        return None


def clean_financial_df(df: pd.DataFrame) -> pd.DataFrame:
    """Return a safe financial DataFrame for charts, scoring and QA.

    This prevents failures after users import third-party spreadsheets whose
    columns are read as object/string types. Missing optional numeric columns
    stay as NaN and will be skipped or displayed as unavailable.
    """
    if df.empty:
        return df
    work = df.copy()
    for col in NUMERIC_COLUMNS:
        if col in work.columns:
            work[col] = work[col].map(_normalize_numeric_value)
            work[col] = pd.to_numeric(work[col], errors='coerce')
    if 'year' in work.columns:
        work = work.dropna(subset=['year'])
        if not work.empty:
            work['year'] = work['year'].astype(int)
            work = work.sort_values('year').reset_index(drop=True)
    return work


def rows_to_df(rows: list[Any]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    if isinstance(rows[0], dict):
        df = pd.DataFrame(rows)
    else:
        df = pd.DataFrame([dict(r) for r in rows])
    return clean_financial_df(df)


def safe_num(v: Any) -> float | None:
    try:
        if v is None or pd.isna(v):
            return None
        value = float(v)
        return value if math.isfinite(value) else None
    except Exception:
        return None


def fmt_value(key: str, value: Any) -> str:
    val = safe_num(value)
    if val is None:
        return '暂无数据'
    unit = METRIC_UNITS.get(key, '')
    if key == 'eps':
        return f'{val:.2f}{unit}'
    return f'{val:.2f}{unit}'


def latest_row(df: pd.DataFrame) -> pd.Series | None:
    if df.empty:
        return None
    return df.sort_values('year').iloc[-1]


def previous_row(df: pd.DataFrame) -> pd.Series | None:
    if df.empty or len(df) < 2:
        return None
    return df.sort_values('year').iloc[-2]


def get_source_for_metric(df: pd.DataFrame, year: int | None = None, metric_key: str | None = None) -> str:
    if df.empty:
        return '暂无来源记录'
    work = df.sort_values('year')
    if year is not None and 'year' in work.columns:
        one = work[work['year'] == year]
        if one.empty:
            return f'未记录{year}年来源，不使用其他年度替代'
        work = one
    row = work.iloc[-1]
    provenance = row.get('metric_provenance')
    if isinstance(provenance, dict):
        sources = [provenance.get(metric_key)] if metric_key else list(provenance.values())
        labels = []
        for source in sources:
            if not isinstance(source, dict):
                continue
            label = str(source.get('file_name') or source.get('source_note') or '来源记录')
            if source.get('page') is not None:
                label += f'，第{source["page"]}页'
            if source.get('cell'):
                label += f'，单元格{source["cell"]}'
            if source.get('raw_unit'):
                label += f'，原始单位{source["raw_unit"]}'
            if source.get('import_id'):
                label += f'，导入版本{source["import_id"]}'
            if label not in labels:
                labels.append(label)
        if labels:
            return '；'.join(labels)
        if metric_key:
            return '该指标来源未记录，不使用同年其他指标的来源替代'
    src = row.get('raw_source')
    return (str(src) + '（旧版行级来源，指标级位置未记录）') if pd.notna(src) and str(src).strip() else '指标来源未记录'


def compute_alerts(df: pd.DataFrame) -> list[dict[str, Any]]:
    """Report actual rule coverage; absence of evidence is not low risk."""
    work = clean_financial_df(df)
    latest = latest_row(work)
    prior = previous_row(work)
    year = int(latest['year']) if latest is not None else None
    source = get_source_for_metric(work, year)
    alerts: list[dict[str, Any]] = []
    checked: list[str] = []
    missing: list[str] = []
    same_period = (latest is not None and prior is not None
                   and int(latest['year']) - int(prior['year']) == 1)

    def add(level, title, message, rule, basis):
        rule_metrics = {'R01': ['net_profit'], 'R02': ['revenue', 'net_profit'],
                        'R03': ['operating_cashflow'], 'R04': ['roe'],
                        'R05': ['debt_ratio'], 'R06': ['audit_opinion']}
        keys = rule_metrics.get(rule[:3], [])
        used_years = [year] if year is not None else []
        if keys and rule[:3] in {'R01', 'R02', 'R03', 'R04'} and prior is not None:
            used_years.insert(0, int(prior['year']))
        exact_sources = [f'{y}年{METRIC_LABELS.get(key, key)}：{get_source_for_metric(work, y, key)}'
                         for y in used_years for key in keys]
        alerts.append(dict(level=level, title=title, message=message,
                           rule=rule, basis=basis, source='；'.join(exact_sources) or source))

    # Ratio changes are defined only for adjacent years and positive bases.
    profit_growth = calc_growth(work, 'net_profit')
    revenue_growth = calc_growth(work, 'revenue')
    cash_growth = calc_growth(work, 'operating_cashflow')
    if profit_growth is None:
        missing.append('R01 净利润同比（需相邻年度及正基数）')
    else:
        checked.append('R01')
        if profit_growth < -10:
            add('orange', '净利润下滑预警', f'{year}年归母净利润同比下降 {abs(profit_growth):.2f}%。',
                'R01：相邻年度归母净利润同比下降超过10%。',
                f'{int(prior["year"])}年{fmt_value("net_profit", prior.get("net_profit"))}；{year}年{fmt_value("net_profit", latest.get("net_profit"))}。')
    if revenue_growth is None or profit_growth is None:
        missing.append('R02 营收与利润同比匹配')
    else:
        checked.append('R02')
        if revenue_growth > 0 and profit_growth < 0:
            add('yellow', '增收不增利', f'{year}年营收同比增长 {revenue_growth:.2f}%，归母净利润同比下降 {abs(profit_growth):.2f}%。',
                'R02：同一相邻年度区间收入增长但利润下降。',
                f'{year}年营收{fmt_value("revenue", latest.get("revenue"))}，归母净利润{fmt_value("net_profit", latest.get("net_profit"))}。')
    if cash_growth is None:
        missing.append('R03 经营现金流同比（需相邻年度及正基数）')
    else:
        checked.append('R03')
        if cash_growth < -30:
            add('red', '经营现金流恶化', f'{year}年经营现金流同比下降 {abs(cash_growth):.2f}%。',
                'R03：相邻年度经营现金流同比下降超过30%。',
                f'{int(prior["year"])}年{fmt_value("operating_cashflow", prior.get("operating_cashflow"))}；{year}年{fmt_value("operating_cashflow", latest.get("operating_cashflow"))}。')
    if same_period and all(safe_num(row.get('roe')) is not None for row in [latest, prior]):
        checked.append('R04')
        drop = float(prior['roe']) - float(latest['roe'])
        if drop > 5:
            add('orange', 'ROE大幅下滑', f'ROE下降 {drop:.2f} 个百分点。',
                'R04：相邻年度ROE下降超过5个百分点。',
                f'{int(prior["year"])}年{fmt_value("roe", prior.get("roe"))}；{year}年{fmt_value("roe", latest.get("roe"))}。')
    else:
        missing.append('R04 ROE变化')
    debt = safe_num(latest.get('debt_ratio')) if latest is not None else None
    if debt is None:
        missing.append('R05 资产负债率')
    else:
        checked.append('R05')
        if debt > 70:
            add('yellow', '资产负债率偏高', f'{year}年资产负债率为 {debt:.2f}%。',
                'R05：资产负债率高于70%。', f'{year}年资产负债率 {debt:.2f}%。')
    raw_audit = latest.get('audit_opinion') if latest is not None else None
    audit = '' if raw_audit is None or pd.isna(raw_audit) else str(raw_audit).strip()
    if audit in {'', 'nan', 'None', '未知', '暂无', '暂无数据', '--'}:
        missing.append('R06 审计意见')
    else:
        checked.append('R06')
        normal = audit.replace(' ', '').replace('。', '') in {'无保留意见', '标准无保留意见', '标准的无保留意见'}
        if not normal:
            add('red', '审计意见关注', f'{year}年审计意见为“{audit}”。',
                'R06：非标准无保留意见需要进一步核查；无保留意见本身不触发。', f'{year}年审计意见：{audit}。')
    if missing:
        add('unknown', '数据不足，无法完整评估风险',
            '部分规则缺少可用数据；不得据此判断低风险或相对平稳。',
            '仅对具有完整数据的规则执行检查。', '未完成：' + '；'.join(missing))
    elif not alerts:
        add('green', '主要规则未触发', '已执行的六项规则未触发预警；不代表不存在投资风险。',
            '已检查净利润、营收利润匹配、经营现金流、ROE、资产负债率和审计意见。', f'检查年度：{year}年。')
    for alert in alerts:
        alert['checked_rules'] = list(checked)
        alert['missing_rules'] = list(missing)
        alert['coverage_complete'] = not missing
    return alerts


def calc_growth(df: pd.DataFrame, key: str) -> float | None:
    """Latest annual YoY, never a multi-year interval or a negative-base ratio."""
    work = clean_financial_df(df)
    if work.empty or 'year' not in work.columns or len(work) < 2 or key not in work.columns:
        return None
    latest, prior = work.iloc[-1], work.iloc[-2]
    if int(latest['year']) - int(prior['year']) != 1:
        return None
    current, base = safe_num(latest.get(key)), safe_num(prior.get(key))
    if current is None or base is None or base <= 0:
        return None
    return (current / base - 1) * 100


def growth_details(df: pd.DataFrame, key: str) -> dict[str, Any]:
    """Expose separate interval/YoY/CAGR facts plus the actual missing years."""
    work = clean_financial_df(df)
    result = dict(yoy=None, interval_growth=None, cagr=None, start_year=None,
                  end_year=None, missing_years=[], reason='数据不足')
    if work.empty or key not in work.columns or 'year' not in work.columns:
        return result
    result['start_year'], result['end_year'] = int(work.iloc[0]['year']), int(work.iloc[-1]['year'])
    years = {int(row['year']) for _, row in work.iterrows() if safe_num(row.get(key)) is not None}
    result['missing_years'] = [y for y in range(result['start_year'], result['end_year'] + 1) if y not in years]
    result['yoy'] = calc_growth(work, key)
    if len(work) < 2:
        result['reason'] = '仅有一个年度，无法计算增长率'
        return result
    base, last = safe_num(work.iloc[0].get(key)), safe_num(work.iloc[-1].get(key))
    span = result['end_year'] - result['start_year']
    if base is None or last is None:
        result['reason'] = '区间起止年度指标缺失，不以其他年度替代'
    elif base <= 0:
        result['reason'] = '基期为零或负值，不计算百分比增长率及年化增长'
    elif span <= 0:
        result['reason'] = '缺少有效年度区间'
    else:
        result['interval_growth'] = (last / base - 1) * 100
        result['cagr'] = ((last / base) ** (1 / span) - 1) * 100 if last > 0 else None
        result['reason'] = ('期末值非正，不计算年化增长' if last <= 0 else '')
    return result


def score_breakdown(df: pd.DataFrame) -> dict[str, Any]:
    df = clean_financial_df(df)
    missing = []
    if df.empty:
        missing.append('无可用财务指标')
    else:
        latest = df.iloc[-1]
        for key in ['revenue', 'net_profit', 'operating_cashflow', 'roe', 'debt_ratio']:
            if safe_num(latest.get(key)) is None:
                missing.append(f'{int(latest["year"])}年{METRIC_LABELS[key]}缺失')
        if safe_num(latest.get('revenue')) is not None and safe_num(latest.get('revenue')) <= 0:
            missing.append('营业收入非正，净利率无法按常规口径评分')
        if safe_num(latest.get('net_profit')) == 0:
            missing.append('净利润为零，现金流/利润比无法评分')
        for key in ['revenue', 'net_profit']:
            if calc_growth(df, key) is None:
                missing.append(f'{METRIC_LABELS[key]}缺少相邻年度有效值或同比基期非正')
    if missing:
        return {'total': None, 'dimensions': {}, 'basis': [], 'available': False,
                'missing': missing, 'reason': '；'.join(missing), 'source': get_source_for_metric(df)}
    df = df.sort_values('year').reset_index(drop=True)
    latest = df.iloc[-1]
    source = get_source_for_metric(df, int(latest.get('year')))
    dims: dict[str, float] = {}
    basis: list[str] = []

    roe = safe_num(latest.get('roe'))
    net_margin_hint = None
    revenue = safe_num(latest.get('revenue'))
    net_profit = safe_num(latest.get('net_profit'))
    if revenue not in (None, 0) and net_profit is not None:
        net_margin_hint = net_profit / revenue * 100
    profitability = 12.0
    if roe is not None:
        profitability += min(max(roe / 25 * 14, 0), 14)
        basis.append(f'盈利能力：ROE为{roe:.2f}%。')
    if net_margin_hint is not None:
        profitability += min(max(net_margin_hint / 10 * 4, 0), 4)
        basis.append(f'净利率估算约{net_margin_hint:.2f}%。')
    dims['盈利能力'] = round(min(profitability, 30), 1)

    cfo = safe_num(latest.get('operating_cashflow'))
    cash_quality = 8.0
    if cfo is not None and net_profit not in (None, 0):
        ratio = cfo / abs(net_profit)
        cash_quality += min(max(ratio * 12, 0), 17)
        basis.append(f'现金流质量：经营现金流/归母净利润绝对值约为{ratio:.2f}。')
    elif cfo is not None and cfo > 0:
        cash_quality += 8
    dims['现金流质量'] = round(min(cash_quality, 25), 1)

    debt = safe_num(latest.get('debt_ratio'))
    solvency = 10.0
    if debt is not None:
        if debt <= 55:
            solvency += 15
        elif debt <= 65:
            solvency += 12
        elif debt <= 75:
            solvency += 7
        else:
            solvency += 3
        basis.append(f'偿债稳健性：资产负债率为{debt:.2f}%。')
    dims['偿债稳健性'] = round(min(solvency, 25), 1)

    rev_growth = calc_growth(df, 'revenue')
    profit_growth = calc_growth(df, 'net_profit')
    growth = 8.0
    if rev_growth is not None:
        growth += min(max((rev_growth + 5) / 25 * 7, 0), 7)
        basis.append(f'成长能力：营收同比变化{rev_growth:.2f}%。')
    if profit_growth is not None:
        growth += min(max((profit_growth + 10) / 40 * 5, 0), 5)
        basis.append(f'归母净利润同比变化{profit_growth:.2f}%。')
    dims['成长能力'] = round(min(growth, 20), 1)

    total = round(sum(dims.values()), 1)
    return {'total': total, 'dimensions': dims, 'basis': basis, 'source': source, 'available': True, 'missing': [], 'reason': ''}


def score_company(df: pd.DataFrame) -> dict[str, Any]:
    s = score_breakdown(df)
    return {'score': s['total'], 'detail': s['dimensions'], 'basis': s.get('basis', []),
            'source': s.get('source', ''), 'available': s['available'],
            'missing': s.get('missing', []), 'reason': s.get('reason', '')}


def risk_dashboard(alerts: list[dict[str, Any]]) -> dict[str, str]:
    levels = {a.get('level') for a in alerts}
    covered = set(rule for a in alerts for rule in a.get('checked_rules', []))
    incomplete = not alerts or 'unknown' in levels or any(a.get('coverage_complete') is False for a in alerts)
    overall = ('高关注' if 'red' in levels else '中高关注' if 'orange' in levels
               else '中等关注' if 'yellow' in levels else '数据不足，无法评估' if incomplete
               else '已检规则未触发')
    if incomplete and any(level in levels for level in ['red', 'orange', 'yellow']):
        overall += '（数据不全）'
    names = ''.join(a.get('title', '') for a in alerts if a.get('level') != 'unknown')
    def dimension(rules, tokens):
        if any(token in names for token in tokens):
            return '需关注' + ('（数据不全）' if not set(rules) <= covered else '')
        return '已检规则未触发' if set(rules) <= covered else '数据不足，无法评估'
    return {'综合风险': overall,
            '盈利能力': dimension(['R01', 'R02', 'R04'], ['净利润', '增收不增利', 'ROE']),
            '现金流质量': dimension(['R03'], ['经营现金流']),
            '偿债能力': dimension(['R05'], ['资产负债率'])}


def build_summary(company_name: str, df: pd.DataFrame, alerts: list[dict[str, str]], profile: str = '平衡型') -> str:
    if df.empty:
        return f'{company_name} 暂无可用财务数据。'
    df = df.sort_values('year').reset_index(drop=True)
    latest = df.iloc[-1]
    year = int(latest['year'])
    parts = [f'{company_name} 最新可用年度为 {year} 年']
    for key in ['revenue', 'net_profit', 'operating_cashflow', 'roe', 'debt_ratio']:
        if key in df.columns and pd.notna(latest.get(key)):
            parts.append(f'{METRIC_LABELS[key]} {fmt_value(key, latest.get(key))}')
    top_alert = alerts[0]['title'] if alerts else '数据不足，无法评估风险'
    profile_text = PROFILE_RULES.get(profile, PROFILE_RULES['平衡型'])
    return '，'.join(parts) + f'。当前重点关注：{top_alert}。按{profile}投资者视角，{profile_text}本结论仅供研究参考，不构成投资建议。'


def compare_companies(name_a: str, df_a: pd.DataFrame, name_b: str, df_b: pd.DataFrame,
                      profile: str = '平衡型', metrics: list[str] | None = None) -> dict[str, Any]:
    df_a, df_b = clean_financial_df(df_a), clean_financial_df(df_b)
    years_a = set(df_a['year']) if 'year' in df_a else set()
    years_b = set(df_b['year']) if 'year' in df_b else set()
    common_years = sorted(years_a & years_b)
    # Never compare the latest rows from different report years implicitly.
    a = df_a[df_a['year'].isin(common_years)] if 'year' in df_a else df_a
    b = df_b[df_b['year'].isin(common_years)] if 'year' in df_b else df_b
    keys = metrics or ['revenue', 'net_profit', 'operating_cashflow', 'roe', 'debt_ratio']
    table_rows = []
    display_years = sorted(years_a | years_b) if metrics else common_years[-1:]
    for year in display_years:
        la = df_a[df_a['year'] == year].iloc[-1] if year in years_a else pd.Series(dtype=object)
        lb = df_b[df_b['year'] == year].iloc[-1] if year in years_b else pd.Series(dtype=object)
        for key in keys:
            table_rows.append({'年度': int(year), '指标': f'{METRIC_LABELS.get(key, key)}({METRIC_UNITS.get(key, "")})',
                               name_a: safe_num(la.get(key)), name_b: safe_num(lb.get(key))})
    table = pd.DataFrame(table_rows)
    unavailable = dict(score=None, detail={}, basis=[], source='', available=False,
                       missing=[], reason='仅比较选定指标，不执行综合评分')
    score_a = dict(unavailable) if metrics else score_company(a)
    score_b = dict(unavailable) if metrics else score_company(b)
    recommended = None
    if metrics:
        reasoning = '按相同年度逐项比较已选指标；缺失值保留为空，不据单项数据给出综合评分或投资倾向。'
    elif not common_years:
        reasoning = f'{name_a}与{name_b}没有共同可用年度，不能跨报告期形成综合比较或倾向。'
    elif not score_a['available'] or not score_b['available']:
        reasons = '；'.join(f'{name}：{score["reason"]}' for name, score in [(name_a, score_a), (name_b, score_b)] if not score['available'])
        reasoning = '综合评分所需数据不足，不给出综合倾向。' + reasons
    else:
        if score_a['score'] != score_b['score']:
            recommended = name_a if score_a['score'] > score_b['score'] else name_b
        reasoning = (f'比较年度为{int(common_years[-1])}年。{name_a}综合评分{score_a["score"]}分，'
                     f'{name_b}综合评分{score_b["score"]}分。'
                     + (f'规则评分较高的是{recommended}。' if recommended else '两家公司规则评分相同。')
                     + '评分仅描述所用指标，不构成投资建议。')
    dimension_rows = []
    if not metrics:
        for dim in ['盈利能力', '现金流质量', '偿债稳健性', '成长能力']:
            dimension_rows.append({'评分维度': dim, name_a: score_a['detail'].get(dim), name_b: score_b['detail'].get(dim)})
        dimension_rows.append({'评分维度': '综合得分', name_a: score_a['score'], name_b: score_b['score']})
    # Keep companies separate and identify only facts actually shown or used.
    # Comprehensive scores use five latest-year values plus revenue/profit in
    # the adjacent common base year; unrelated older cells must not change the
    # source label of this comparison.
    identity_blocks = {}
    for name, original, comparable in ((name_a, df_a, a), (name_b, df_b, b)):
        if metrics:
            blocks = [{'scope': '选定指标与展示年度',
                       'summary': summarize_data_identity(original, metrics=keys, years=display_years)}]
        else:
            target = common_years[-1] if common_years else None
            blocks = [{'scope': '共同年度内的当期对比指标',
                       'summary': summarize_data_identity(comparable, metrics=keys,
                                                          years=[target] if target is not None else [])}]
            if target is not None:
                blocks.append({'scope': '共同年度内的同比基期',
                               'summary': summarize_data_identity(comparable, metrics=['revenue', 'net_profit'],
                                                                  years=[target - 1])})
        identity_blocks[name] = blocks
    return {'recommended': recommended, 'reasoning': reasoning, 'table': table,
            'score_table': pd.DataFrame(dimension_rows), 'score_a': score_a, 'score_b': score_b,
            'data_identity_blocks': identity_blocks,
            'basis': {name_a: score_a.get('basis', []), name_b: score_b.get('basis', [])}}


def metric_query(company_name: str, df: pd.DataFrame, metric_key: str, year: int | None = None) -> str:
    if df.empty:
        return f'{company_name} 暂无可用财务数据。'
    work = df.sort_values('year')
    if year is not None:
        one = work[work['year'] == year]
        if one.empty:
            years = '、'.join(str(int(x)) for x in work['year'].tolist())
            return f'系统当前没有检索到{company_name}{year}年的数据。当前可用年度包括：{years}。'
        row = one.iloc[-1]
    else:
        row = work.iloc[-1]
        year = int(row['year'])
    if metric_key not in row or pd.isna(row.get(metric_key)):
        return f'系统当前没有检索到{company_name}{year}年的{METRIC_LABELS.get(metric_key, metric_key)}数据。'
    source = get_source_for_metric(work, int(year), metric_key)
    return f'{company_name}{int(year)}年{METRIC_LABELS.get(metric_key, metric_key)}为 {fmt_value(metric_key, row.get(metric_key))}。数据来源：{source}。'


def trend_text(company_name: str, df: pd.DataFrame, metric_key: str = 'revenue') -> str:
    work = clean_financial_df(df)
    if work.empty or metric_key not in work.columns:
        return f'{company_name} 暂无可用趋势数据。'
    points = [f'{int(row["year"])}年{fmt_value(metric_key, row.get(metric_key))}' for _, row in work.iterrows()]
    info = growth_details(work, metric_key)
    statements = []
    if METRIC_UNITS.get(metric_key) == '%':
        if len(work) >= 2:
            first, last = safe_num(work.iloc[0].get(metric_key)), safe_num(work.iloc[-1].get(metric_key))
            if first is not None and last is not None:
                statements.append(f'{info["start_year"]}—{info["end_year"]}年变化 {last-first:.2f} 个百分点')
        if not statements:
            statements.append('数据不足，暂不计算百分点变化')
    else:
        if info['yoy'] is not None:
            statements.append(f'{info["end_year"]}年同比变化 {info["yoy"]:.2f}%')
        else:
            statements.append('缺少相邻年度有效值或同比基期非正，暂不计算最近一年同比')
        if info['interval_growth'] is not None and info['end_year'] - info['start_year'] > 1:
            statements.append(f'{info["start_year"]}—{info["end_year"]}年区间累计增长 {info["interval_growth"]:.2f}%')
            if info['cagr'] is not None:
                statements.append(f'该{info["end_year"]-info["start_year"]}年区间年化增长率（CAGR）{info["cagr"]:.2f}%')
        if info['reason']:
            statements.append(info['reason'])
    if info['missing_years']:
        statements.append('缺少' + '、'.join(str(y) for y in info['missing_years']) + '年指标，不能据此描述完整逐年趋势')
    return f'{company_name}{METRIC_LABELS.get(metric_key, metric_key)}趋势：' + '，'.join(points) + '。' + '；'.join(statements) + '。'


def explain_metric(metric_key: str) -> str:
    explanations = {
        'roe': 'ROE即净资产收益率，用于衡量公司利用股东权益创造利润的效率。ROE越高，通常说明股东回报能力越强，但也需要结合杠杆水平和利润质量判断。',
        'debt_ratio': '资产负债率用于衡量公司资产中由负债形成的比例。比例过高可能意味着偿债压力较大，但不同行业的合理区间不同。',
        'operating_cashflow': '经营现金流反映企业主营业务带来的现金流入流出净额，是观察利润含金量和主业造血能力的重要指标。',
        'gross_margin': '毛利率反映营业收入扣除营业成本后的盈利空间，常用于观察产品竞争力、成本压力和价格策略变化。',
        'net_profit': '归母净利润反映归属于上市公司股东的最终利润，是投资者判断盈利能力的重要指标。',
        'revenue': '营业收入反映企业经营规模和业务扩张情况，但收入增长还需要结合利润、现金流和费用变化判断质量。',
    }
    return explanations.get(metric_key, '该指标可作为财务分析的辅助指标，需要结合企业行业特征和其他财务数据综合判断。')


def _yoy_text(df: pd.DataFrame, key: str) -> str:
    growth = calc_growth(df, key)
    if growth is None:
        return '相邻年度数据不足或基期非正，暂不计算同比。'
    work = clean_financial_df(df)
    return f'{int(work.iloc[-1]["year"])}年同比变化 {growth:.2f}%。'


def _data_range_text(df: pd.DataFrame) -> str:
    if df.empty or 'year' not in df.columns:
        return '暂无结构化年度指标。'
    years = [int(y) for y in sorted(df['year'].dropna().unique().tolist())]
    return '、'.join(str(y) for y in years) if years else '暂无结构化年度指标。'


def generate_report(company_name: str, df: pd.DataFrame, alerts: list[dict[str, str]], compare_payload: dict[str, Any] | None, profile: str = '平衡型') -> str:
    """Generate a financial research report from the supplied analysis inputs.

    The text version is intentionally more complete than the dashboard summary:
    it includes data range, multi-year metric table, trend interpretation,
    rule-based risk explanation, score breakdown, investor-profile view and
    source traceability. The PDF renderer can then pair this text with charts.
    """
    summary = build_summary(company_name, df, alerts, profile)
    score = score_company(df)
    risk = risk_dashboard(alerts)
    source = public_message(get_source_for_metric(df), fallback=NO_SOURCE) if not df.empty else NO_SOURCE
    years_text = _data_range_text(df)
    score_text = f'{score["score"]}分' if score['available'] else '数据不足，暂不评分'
    identity = summarize_data_identity(df)
    identity_lines = identity_report_lines(identity)
    identity_lines[0] = '主企业' + identity_lines[0]

    lines = [
        PAGE_TITLE,
        REPORT_SUBTITLE,
        f'分析企业：{company_name}',
        *identity_lines,
        '',
        '一、报告摘要',
        summary,
        f'本次分析覆盖年度：{years_text}。仅在指标完整且年度可比时进行四维评分；缺失指标不推定为正常。',
        f'综合评分：{score_text}；综合风险状态：{risk.get("综合风险", "-")}。',
        '',
        '二、核心财务数据概览',
    ]

    if not df.empty:
        work = df.sort_values('year')
        for _, row in work.iterrows():
            year = int(row['year'])
            vals = []
            for key in ['revenue', 'net_profit', 'operating_cashflow', 'roe', 'debt_ratio']:
                if key in row and pd.notna(row.get(key)):
                    vals.append(f'{METRIC_LABELS[key]} {fmt_value(key, row.get(key))}')
            if vals:
                lines.append(f'- {year}年：' + '；'.join(vals))
        latest = work.iloc[-1]
        lines += [
            '',
            '三、趋势解读',
            f'- 营业收入趋势：{trend_text(company_name, work, "revenue")}',
            f'- 归母净利润趋势：{trend_text(company_name, work, "net_profit")}',
            f'- 经营现金流趋势：{trend_text(company_name, work, "operating_cashflow")}',
            f'- 股东回报观察：最新年度ROE为{fmt_value("roe", latest.get("roe"))}，用于观察权益资本回报效率。',
            f'- 财务稳健观察：最新年度资产负债率为{fmt_value("debt_ratio", latest.get("debt_ratio"))}，用于衡量财务杠杆水平。',
        ]
    else:
        lines.append(NO_METRICS)

    lines += ['', '四、风险预警与规则依据']
    for alert in alerts:
        lines.append(f'- {alert["title"]}：{alert["message"]}')
        lines.append(f'  规则依据：{alert.get("rule", "-")}')
        lines.append(f'  数据依据：{alert.get("basis", "-")}')

    lines += ['', '五、四维评分解释']
    lines.append(f'综合评分：{score_text}。' + ('评分用于辅助横向比较，不构成投资建议。' if score['available'] else score['reason']))
    for dim, val in score['detail'].items():
        lines.append(f'- {dim}：{val}分')
    for b in score.get('basis', [])[:6]:
        lines.append(f'  评分依据：{b}')

    lines += ['', '六、风险仪表盘']
    for k, v in risk.items():
        lines.append(f'- {k}：{v}')

    profile_text = PROFILE_RULES.get(profile, PROFILE_RULES['平衡型'])
    lines += [
        '',
        '七、投资者画像匹配',
        f'当前选择画像：{profile}。{profile_text}',
        '系统建议投资者重点结合利润趋势、现金流含金量、资产负债率和ROE变化进行综合判断，避免只看单一指标。',
    ]

    if compare_payload:
        lines += ['', '八、企业对比参考']
        lines.append(compare_payload['reasoning'])
        comparison_identities = compare_payload.get('data_identity_blocks') or {}
        if comparison_identities:
            for compared_name, blocks in comparison_identities.items():
                lines.append(f'对比企业：{compared_name}')
                for block in blocks:
                    compared_identity = block['summary']
                    lines.append(f'- {block["scope"]}：{compared_identity["label"]}（{identity_count_text(compared_identity)}）；'
                                 f'覆盖年度：{compared_identity["coverage_text"]}；指标缺项：{compared_identity["missing_text"]}。')
                lines.append('以上身份仅描述本次比较使用的数据来源，不代表原文或数值已核验。')
        else:
            lines.append('对比企业的逐指标来源身份未随本报告保留，仍待核验。')

    lines += [
        '',
        '九、数据来源与可追溯性',
        *identity_lines,
        f'主要结构化数据来源：{source.rstrip("。")}。来源信息按本次已接入记录列示；缺失指标来源须补充核验。',
        '',
        '十、研究结论',
        '本报告基于系统已接入的结构化财务数据、公开来源材料和规则引擎自动生成，适用于财务信息理解、企业经营分析与指标对比。报告仅供学习研究与辅助分析，不构成任何形式的投资建议或买卖指令。',
    ]
    if not df.empty:
        position = lines.index('十、研究结论') - 1
        provenance_lines = []
        for _, row in df.sort_values('year').iterrows():
            year = int(row['year'])
            for key in METRIC_LABELS:
                if safe_num(row.get(key)) is not None:
                    source_label = public_message(get_source_for_metric(df, year, key), fallback=NO_SOURCE)
                    provenance_lines.append(f'- {year}年{METRIC_LABELS[key]} {fmt_value(key, row.get(key))}：{source_label}。')
        lines[position:position] = provenance_lines
    return '\n'.join(lines)
