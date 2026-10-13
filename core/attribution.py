"""Read-only financial change clues, with strictly scoped report evidence.

This module does not infer causal contributions. Every number comes through
the existing SQL validator/executor; report prose remains a cited quotation.
"""
from __future__ import annotations

from copy import deepcopy
import math
from pathlib import Path
import re
import sys
from time import perf_counter
from typing import Any

from core.retrieval import METRIC_ALIASES, _supported_causal_sentences, retrieve_documents
from core.text_to_sql import SQLPlan, run_text_to_sql


DISCLAIMER = '以下为基于财务指标变化与报告证据形成的归因线索，用于辅助分析，不代表严格因果贡献率。'
NO_DOCUMENT_EVIDENCE = '当前仅形成量化归因线索，未检索到足够的同年度报告解释证据。'
METRICS = ('net_profit', 'revenue', 'gross_margin', 'operating_cashflow')
LABELS = {'net_profit': '归母净利润变化', 'revenue': '营业收入变化',
          'gross_margin': '毛利率变化', 'operating_cashflow': '经营现金流变化'}
SEARCH_TERMS = {
    'net_profit': '净利润 利润 盈利 业绩',
    'revenue': '营业收入 营收 销售 销量 产品 市场 海外 业务增长',
    'gross_margin': '毛利率 毛利 成本 原材料 价格 产品结构 市场竞争',
    'operating_cashflow': '经营现金流 经营活动产生的现金流量净额 回款 应收 库存',
}


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _years(values: list[int] | None) -> list[int]:
    result = set()
    for value in values or []:
        if isinstance(value, bool) or not re.fullmatch(r'\d{4}', str(value)):
            raise ValueError('归因分析需要有效的四位年度。')
        result.add(int(value))
    return sorted(result)


def _query(company: str, years: list[int], metrics: list[str], trace: list[dict],
           label: str, initial_plan: SQLPlan | None = None) -> dict:
    context = {'companies': [company], 'years': list(years), 'metrics': list(metrics),
               'intent': 'finance_query', 'period': 'annual'}
    started = perf_counter()
    try:
        result = run_text_to_sql(context, initial_plan=initial_plan) if initial_plan else run_text_to_sql(context)
    except Exception as exc:
        result = {'status': 'failed', 'rows': [], 'attempts': [],
                  'reason': f'{type(exc).__name__}：{str(exc)[:180]}'}
    reason = result.get('reason') or '；'.join(
        str(item.get('error')) for item in result.get('attempts') or [] if item.get('error'))
    trace.append({'label': label, 'companies': [company], 'years': list(years), 'metrics': list(metrics),
                  'status': result.get('status', 'failed'), 'reason': reason,
                  'duration_ms': round((perf_counter() - started) * 1000, 3),
                  'attempts': deepcopy(result.get('attempts') or []),
                  'sql': result.get('sql'), 'params': deepcopy(result.get('params') or []),
                  'row_count': result.get('row_count', len(result.get('rows') or []))})
    return result


def _metric_pair(result: dict, company: str, years: list[int], metric: str) -> tuple | None:
    if result.get('status') != 'success':
        return None
    rows = result.get('rows') or []
    if len(rows) != 2:
        return None
    by_year = {}
    for row in rows:
        # The main executor validates scope too; retain this boundary check so
        # accidental alternative results cannot cross into this component.
        try:
            year = int(row.get('year'))
        except (TypeError, ValueError, OverflowError):
            return None
        if row.get('company') != company or year not in years or year in by_year:
            return None
        value = _finite_number(row.get(metric))
        if value is None:
            return None
        by_year[year] = (value, row.get('raw_source'))
    if set(by_year) != set(years):
        return None
    return by_year[years[0]], by_year[years[1]]


def _node(metric: str, company: str, years: list[int], pair: tuple | None) -> dict:
    node = {'id': metric, 'metric': metric, 'label': LABELS[metric],
            'company': company, 'previous_year': years[0], 'current_year': years[1],
            'role': ('target' if metric == 'net_profit' else
                     'supporting_signal' if metric == 'operating_cashflow' else 'quantitative_driver'),
            'unit': '%' if metric == 'gross_margin' else '亿元',
            'delta_unit': '个百分点' if metric == 'gross_margin' else '亿元',
            'previous_value': None, 'current_value': None, 'delta': None,
            'growth_rate': None, 'growth_label': '', 'direction': None,
            'evidence_status': 'insufficient', 'evidence': [], 'interpretation': '',
            'quantitative_sources': [], 'reason': ''}
    if pair is None:
        node['reason'] = f'缺少{years[0]}或{years[1]}年可校验的{LABELS[metric].removesuffix("变化")}数据。'
        return node
    (previous, previous_source), (current, current_source) = pair
    delta = current - previous
    if not math.isfinite(delta):
        node['reason'] = '指标变化超出可计算范围，未形成量化线索。'
        return node
    growth = delta / abs(previous) * 100 if previous else None
    if growth is not None and not math.isfinite(growth):
        growth = None
    node.update(previous_value=previous, current_value=current, delta=delta,
                growth_rate=None if metric == 'gross_margin' else growth,
                growth_label=('较上年变化' if metric == 'gross_margin' else
                              '上年为零，变动比例不适用' if previous == 0 else
                              '较上年变动（以上年绝对值为基数）' if previous < 0 else '年度同比'),
                direction='up' if delta > 0 else 'down' if delta < 0 else 'flat',
                evidence_status='quantitative_only', reason=NO_DOCUMENT_EVIDENCE,
                quantitative_sources=[{'year': years[0], 'raw_source': previous_source},
                                      {'year': years[1], 'raw_source': current_source}])
    return node


def _interpretation(node: dict, root: dict) -> str:
    if node['evidence_status'] == 'insufficient':
        return '两年数据不足，暂不形成线索。'
    direction = node['direction']
    if node['metric'] == 'revenue':
        return {'up': '收入端正向线索', 'down': '收入端压力线索', 'flat': '收入端未见同比变动'}[direction]
    if node['metric'] == 'gross_margin':
        return {'up': '盈利能力正向线索', 'down': '盈利能力压力线索', 'flat': '毛利率较上年持平'}[direction]
    if node['metric'] == 'operating_cashflow':
        if direction != root['direction']:
            return '经营质量辅助线索：现金流与利润变化不同步，需结合回款等原文进一步分析。'
        if direction == 'flat':
            return '经营质量辅助线索：现金流与利润均较上年持平。'
        return '经营质量辅助线索：现金流与利润同向变化；不作为利润变化的直接原因。'
    return '结构化数据揭示净利润发生了什么变化。'


def _citation_rejection(citation: dict, company: str, year: int, node: dict) -> str:
    metric = node['metric']
    if citation.get('company') != company or str(citation.get('report_year')) != str(year):
        return '引用的企业或报告年度与归因条件不一致。'
    page = citation.get('page')
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        return '引用缺少有效PDF页码。'
    path_value = citation.get('file_path')
    if not citation.get('file_name') or not path_value:
        return '引用缺少文件名或原文路径。'
    if re.search(r'半年|季度|(?<![A-Za-z])[Qq][1-4](?!\d)', str(citation['file_name'])):
        return '引用文件标识为半年或季度报告，不能作为年度归因依据。'
    path = Path(str(path_value))
    path = path if path.is_absolute() else Path(__file__).resolve().parents[1] / path
    if path.suffix.lower() != '.pdf' or not path.is_file():
        return '引用的PDF原文当前不可用。'
    snippet = str(citation.get('snippet') or '').strip()
    supported = _supported_causal_sentences(snippet, [x.lower() for x in METRIC_ALIASES[metric]])
    if not snippet or not supported:
        return '引用未在同一句中直接解释该指标变化。'
    for sentence in supported:
        mentioned_years = set(re.findall(r'20\d{2}', sentence))
        if mentioned_years and str(year) not in mentioned_years:
            return '引用段落明确描述其他年度，不能解释目标年度。'
        if re.search(r'上半年|下半年|半年度|[一二三四1234]季度', sentence):
            return '引用段落描述半年或季度变化，未作为年度变化原因。'
        # Locate the requested metric's own clause, whether the cause comes
        # before or after it. A cost decrease is not a profit decrease.
        aliases = '|'.join(re.escape(alias) for alias in sorted(METRIC_ALIASES[metric], key=len, reverse=True))
        for match in re.finditer(aliases, sentence, re.I):
            prefix_years = re.findall(r'20\d{2}', sentence[:match.start()])
            if prefix_years and prefix_years[-1] != str(year):
                return '引用中该指标明确对应其他年度，不能解释目标年度。'
            tail = sentence[match.end():match.end() + 60]
            tail = re.split(r'主要系|主要由于|由于|原因(?:是|为)|主要受到|导致|[，,；;。]', tail, maxsplit=1)[0]
            other_positions = [found.start() for key, names in METRIC_ALIASES.items() if key != metric
                               for name in names if (found := re.search(re.escape(name), tail, re.I))]
            if other_positions:
                tail = tail[:min(other_positions)]
            if re.search(r'(?:未|没有|并无).{0,10}(?:变化|变动)', tail):
                return '引用否定了明确变化，未将其作为已验证方向依据。'
            stated = re.search(r'增长|增加|上升|提升|下降|减少|下滑|降低|持平|保持稳定', tail)
            if stated:
                if re.search(r'未|没有|并无|不再|不曾|并不', tail[:stated.start()]):
                    return '引用包含否定的指标变化表述，未将其作为已验证方向依据。'
                direction = ('up' if stated.group() in {'增长', '增加', '上升', '提升'} else
                             'flat' if stated.group() in {'持平', '保持稳定'} else 'down')
                if direction != node['direction']:
                    return '引用所述指标变化方向与本次结构化数据不一致。'
    return ''


def _attach_evidence(node: dict, company: str, year: int, checks: list[dict]) -> None:
    metric = node['metric']
    if node['evidence_status'] == 'insufficient':
        return
    # Reuse one-metric causal filtering in the existing retriever. The user's
    # question is retained in the result, but its other metrics cannot broaden
    # this node's evidence matching.
    query = f'{company}{year}年{LABELS[metric]}的原因是什么？{SEARCH_TERMS[metric]}'
    context = {'companies': [company], 'years': [year], 'metrics': [metric], 'period': 'annual'}
    started = perf_counter()
    try:
        retrieval = retrieve_documents(query, context, top_k=3)
    except Exception as exc:
        checks.append({'metric': metric, 'status': 'failed', 'companies': [company], 'years': [year],
                       'query': query, 'reason': f'{type(exc).__name__}：{str(exc)[:180]}',
                       'duration_ms': round((perf_counter() - started) * 1000, 3),
                       'accepted_citations': 0, 'rejected_citations': [], 'checks': []})
        return
    accepted, rejected, seen = [], [], set()
    for citation in (retrieval.get('citations') or []) if retrieval.get('status') == 'success' else []:
        rejection = _citation_rejection(citation, company, year, node)
        if rejection:
            rejected.append({'file_name': citation.get('file_name'), 'page': citation.get('page'), 'reason': rejection})
            continue
        key = (citation.get('file_path'), citation.get('page'), citation.get('snippet'))
        if key in seen:
            continue
        seen.add(key)
        accepted.append(deepcopy(citation))
        if len(accepted) == 2:
            break
    node['evidence'] = accepted
    if accepted:
        node.update(evidence_status='quantitative_and_document', reason='同企业、同年度报告提供该指标变化的原文说明。')
    checks.append({'metric': metric, 'status': 'success' if accepted else 'no_evidence',
                   'companies': [company], 'years': [year], 'query': query,
                   'reason': retrieval.get('reason', '') if accepted else NO_DOCUMENT_EVIDENCE,
                   'retrieval_reason': retrieval.get('reason', ''),
                   'duration_ms': round((perf_counter() - started) * 1000, 3),
                   'accepted_citations': len(accepted), 'rejected_citations': rejected,
                   'checks': deepcopy(retrieval.get('document_checks') or [])})


def build_net_profit_attribution(company: str, years: list[int], question: str,
                                 available_years: list[int] | None = None) -> dict[str, Any]:
    """Build an annual single-company comparison without relaxing user scope.

    available_years is an optional list of row years, never a source of values.
    All financial values are fetched independently through run_text_to_sql.
    """
    started = perf_counter()
    result = {'status': 'insufficient', 'company': company, 'target_metric': 'net_profit',
              'previous_year': None, 'current_year': None, 'root': None, 'drivers': [],
              'question': question, 'disclaimer': DISCLAIMER, 'scope_note': '', 'reason': '',
              'sql_trace': [], 'document_checks': [], 'duration_ms': 0.0}

    def finish(reason: str = '') -> dict:
        result['reason'] = reason
        result['duration_ms'] = round((perf_counter() - started) * 1000, 3)
        return result

    if not isinstance(company, str) or not company.strip():
        return finish('缺少明确企业，未生成归因树。')
    try:
        requested = _years(years)
    except ValueError as exc:
        return finish(str(exc))
    if len(requested) > 2 or (len(requested) == 2 and requested[1] != requested[0] + 1):
        return finish('当前归因树支持一个目标年度，或连续两个年度；请明确要解释的目标年度。')
    if requested:
        pair_years = [requested[-1] - 1, requested[-1]]
        result['scope_note'] = f'归因比较范围：{pair_years[0]} → {pair_years[1]}年，年度口径。'
    else:
        # Discover years with usable root data without requiring optional
        # metrics. All values still come from the dedicated comparison query.
        # A missing latest profit may select an earlier valid pair only when
        # the user has not requested a year, with the chosen scope disclosed.
        discovery = _query(company, [], [], result['sql_trace'], '查找可用年度', SQLPlan(
            'SELECT c.name AS company, fm.year FROM financial_metrics fm '
            'JOIN companies c ON c.id = fm.company_id WHERE c.name = ? '
            'AND fm.net_profit IS NOT NULL AND ABS(fm.net_profit) <= ? ORDER BY fm.year;',
            [company, sys.float_info.max], 'attribution_year_discovery'))
        if discovery.get('status') != 'success' or any(
            row.get('company') != company for row in discovery.get('rows') or []
        ):
            return finish('未能通过只读查询确认该企业的可用年度。')
        try:
            available = _years([row.get('year') for row in discovery.get('rows') or []])
            if available_years is not None:
                hint_years = set(_years(available_years))
                available = [year for year in available if year in hint_years]
        except ValueError as exc:
            return finish(str(exc))
        available_set = set(available)
        current = next((year for year in reversed(available) if year - 1 in available_set), None)
        if current is None:
            return finish('缺少同一企业连续两年的年度数据，暂不能形成净利润变化归因。')
        pair_years = [current - 1, current]
        result['scope_note'] = f'未指定年度，采用该企业净利润数据可用的最新连续两个年度：{current - 1} → {current}年。'
    result.update(previous_year=pair_years[0], current_year=pair_years[1])

    combined = _query(company, pair_years, list(METRICS), result['sql_trace'], '查询两年归因指标')
    nodes = []
    for metric in METRICS:
        pair = _metric_pair(combined, company, pair_years, metric)
        if pair is None:
            single = _query(company, pair_years, [metric], result['sql_trace'], f'独立校验{LABELS[metric].removesuffix("变化")}')
            pair = _metric_pair(single, company, pair_years, metric)
        node = _node(metric, company, pair_years, pair)
        nodes.append(node)
        # An unavailable target makes driver calculations irrelevant; do not
        # turn optional data into a substitute answer or retrieve fake causes.
        if metric == 'net_profit' and node['evidence_status'] == 'insufficient':
            result['root'] = node
            return finish(node['reason'])
    result['root'], result['drivers'] = nodes[0], nodes[1:]
    for node in nodes:
        node['interpretation'] = _interpretation(node, nodes[0])
        _attach_evidence(node, company, pair_years[1], result['document_checks'])
    result['status'] = 'success'
    return finish()
