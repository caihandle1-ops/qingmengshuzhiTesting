"""Six-dimensional financial scope fingerprints; unknowns are never equal."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import math
import re

from core.analysis import METRIC_LABELS as ANALYSIS_LABELS
from core.db import METRIC_UNITS

METRIC_LABELS={**ANALYSIS_LABELS,'consolidated_net_profit':'合并净利润',
    'total_assets':'资产总额','total_liabilities':'负债总额',
    'total_equity':'所有者权益合计','minority_profit':'少数股东损益'}

DIMENSIONS = [('definition','指标定义'),('currency_unit','单位与币种'),('period','报告期间'),
              ('consolidation_scope','报表及合并范围'),('accounting_policy','会计准则政策'),
              ('restatement_version','披露与重述版本')]
UNKNOWN={'','unknown','未确认','未知','未记录','none','null'}


def _text(value):
    if isinstance(value,float) and not math.isfinite(value):
        return ''
    return str(value).strip() if isinstance(value,(str,int,float)) and not isinstance(value,bool) else ''


def _known(value):
    return _text(value).lower() not in UNKNOWN


def _currency(value):
    text=_text(value)
    return 'CNY' if text.upper() in {'CNY','RMB'} or text in {'人民币','人民币元'} else text


def _scope(value):
    text=_text(value)
    return 'consolidated' if text in {'合并','合并报表','合并财务报表','合并报表范围'} else text


def _definition(metric,label):
    label=re.sub(r'\s','',_text(label)).replace('（','(').replace('）',')').lower()
    if not label:
        return ''
    normalized=re.sub(r'\([^)]*\)|\[[^]]*\]','',label)
    if metric=='net_profit':
        return 'parent_net_profit' if (normalized in {'归母净利润','归属于上市公司股东的净利润','归属于母公司股东的净利润','net_profit','parent_net_profit'}) else ''
    from core.parsers import CH_ALIASES
    return metric if normalized in {re.sub(r'[\s（）()]','',name).lower() for name in CH_ALIASES.get(metric,[])} else ''


def compare_financial_scopes(data_snapshot,metric,*,expected_years=None,manual_conditions=None):
    """Inspect a frozen single-metric snapshot, optionally compare its records.

    Manual conditions fill unknown metadata only. A conflict with explicit
    source metadata remains a conflict; this never changes the business DB.
    """
    if metric not in METRIC_LABELS:
        return {'status':'unsupported','message':'该指标没有已定义的财务口径。','dimensions':[], 'fingerprints':[], 'can_compare':False}
    fingerprints=[]; errors=[]
    manual=manual_conditions if isinstance(manual_conditions,dict) else {}
    years=set(expected_years or [])
    for company,rows in (data_snapshot or {}).items():
        available_years=set()
        for row in rows:
            if not isinstance(row,dict): continue
            value=row.get(metric)
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):
                errors.append(f'{company} {row.get("year")}：缺少有限数值，不补值。');continue
            sources=row.get('metric_provenance') or {}
            source=deepcopy(sources.get(metric) or {}) if isinstance(sources,dict) else {}
            if not isinstance(source,dict): source={}
            scope_meta=source.get('scope_probe') or {}
            if not isinstance(scope_meta,dict): scope_meta={}
            fields={'definition':_definition(metric,source.get('metric_label')),
                'currency':_currency(source.get('currency') or source.get('currency_hint') or scope_meta.get('currency')),
                'unit':_text(source.get('normalized_unit')),
                'period':_text(source.get('period') or scope_meta.get('period') or 'annual'),
                'statement_scope':_scope(source.get('statement_scope') or source.get('scope_hint') or scope_meta.get('statement_scope')),
                'consolidation_scope':_text(source.get('consolidation_scope')),
                'accounting_policy':_text(source.get('accounting_policy')),
                'report_version':_text(source.get('report_version') or source.get('version_hint') or scope_meta.get('report_version'))}
            confirmed=[]; conflicts=[]
            for field in fields:
                supplied=manual.get(field)
                if supplied is None or not _known(supplied): continue
                supplied=_currency(supplied) if field=='currency' else _scope(supplied) if field=='statement_scope' else _text(supplied)
                if _known(fields[field]) and fields[field]!=supplied:
                    conflicts.append(f'{field}：人工补充与原始来源冲突，未覆盖来源')
                elif not _known(fields[field]):
                    fields[field]=supplied;confirmed.append(field)
            year=row.get('year')
            available_years.add(year)
            expected_definition='parent_net_profit' if metric=='net_profit' else metric
            if _known(fields['definition']) and fields['definition']!=expected_definition:
                conflicts.append('definition：确认的指标定义与本次指标不一致')
            if years and year not in years:
                conflicts.append('period：实际记录年度不在本次要求范围')
            unit_expected=METRIC_UNITS.get(metric)
            if _known(fields['unit']) and fields['unit']!=unit_expected:
                conflicts.append('currency_unit：指标标准单位与来源单位不一致')
            if _known(fields['currency']) and fields['currency']!='CNY' and unit_expected=='亿元':
                conflicts.append('currency_unit：非人民币，当前未配置汇率换算')
            dimensions={
                'definition':fields['definition'],
                'currency_unit':' / '.join([fields['currency'],fields['unit']]) if _known(fields['currency']) and _known(fields['unit']) else '',
                'period':fields['period'] if year is not None else '',
                'consolidation_scope':' / '.join([fields['statement_scope'],fields['consolidation_scope']]) if _known(fields['statement_scope']) and _known(fields['consolidation_scope']) else '',
                'accounting_policy':fields['accounting_policy'],
                'restatement_version':fields['report_version']}
            # A period other than annual is not silently changed into annual.
            if _known(fields['period']) and fields['period']!='annual':
                conflicts.append('period：当前结构化库仅支持完整年度')
            fingerprint={'company':company,'year':year,'metric':metric,'value':float(value),
                'fields':fields,'dimensions':dimensions,'manual_fields':confirmed,
                'conflicts':conflicts,'source':source}
            fingerprint['id']=sha256(json.dumps(fingerprint,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()
            fingerprints.append(fingerprint)
        for missing_year in sorted(years-available_years):
            errors.append(f'{company} {missing_year}：缺少要求年度的完整指标，不认定可比。')
    dimensions=[]
    for key,label in DIMENSIONS:
        values=[f['dimensions'][key] for f in fingerprints]
        missing=[f'{f["company"]} {f["year"]}' for f in fingerprints if not _known(f['dimensions'][key])]
        different=len(set(value for value in values if _known(value)))>1
        conflicts=[c for f in fingerprints for c in f['conflicts'] if c.startswith(key+':') or
                   key=='currency_unit' and c.startswith(('currency:','unit:')) or
                   key=='consolidation_scope' and c.startswith(('statement_scope:','consolidation_scope:')) or
                   key=='restatement_version' and c.startswith('report_version:')]
        status='conflict' if conflicts or different else 'pending' if missing or not values else 'passed'
        if key=='consolidation_scope' and missing:
            reason='报表范围已记录不等于合并范围可比；未记录范围变化需复核'
        else:
            reason='；'.join(conflicts) if conflicts else '来源口径不同，需统一后再比较' if different else '信息未知，需原文或人工复核' if status=='pending' else '本次记录的口径一致'
        dimensions.append({'key':key,'label':label,'status':status,'values':values,
                           'missing':missing,'reason':reason})
    conflicts=[c for f in fingerprints for c in f['conflicts']]
    status=('insufficient_data' if not fingerprints or errors else 'conflict' if conflicts or any(d['status']=='conflict' for d in dimensions)
            else 'pending' if any(d['status']=='pending' for d in dimensions) else 'passed')
    return {'schema_version':'comparability/1','status':status,'metric':metric,'label':METRIC_LABELS[metric],
        'can_compare':status=='passed','record_count':len(fingerprints),'fingerprints':fingerprints,
        'dimensions':dimensions,'errors':errors,'conflicts':conflicts,
        'manual_conditions':deepcopy(manual),'message':{'passed':'六维口径已按本次记录核对，可进行该指标比较。',
            'pending':'存在未知口径，数值可查，跨记录比较仍待复核。',
            'conflict':'发现口径冲突，请统一来源条件后再比较。',
            'insufficient_data':'缺少可核验的完整指标记录，不计算可比结论。'}[status]}


def evaluate_delivery(audit,comparability=None):
    """A conflict or unknown applicable check cannot be labelled delivered."""
    checks=[item for rows in ((audit or {}).get('paths') or {}).values() for item in rows]
    scope_items=[comparability] if isinstance(comparability,dict) else comparability or []
    if any(c.get('status') in {'conflict','failed','error'} for c in checks) or any(c.get('status')=='conflict' for c in scope_items):
        return {'status':'blocked','label':'发现冲突 · 需修正','deliverable':False,'reason':'至少一个适用核验或口径检查存在冲突。'}
    if not checks or any(c.get('status') not in {'passed','not_applicable'} for c in checks) or any(c.get('status')!='passed' for c in scope_items):
        return {'status':'needs_review','label':'待复核结果','deliverable':False,'reason':'保留数值与依据；适用项补齐并通过后才认定可交付。'}
    if not any(c.get('status')=='passed' for c in checks):
        return {'status':'needs_review','label':'待复核结果','deliverable':False,'reason':'没有实际通过的适用检查，不能仅凭不适用项认定交付。'}
    return {'status':'verified','label':'适用核验通过','deliverable':True,'reason':'本次适用项通过；不适用项不作通过认定。'}
