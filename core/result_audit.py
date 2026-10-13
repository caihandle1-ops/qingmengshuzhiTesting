"""Independent, bounded result checks. Missing evidence never means passed."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from core.storage import ROOT, get_data_root

_MONEY = {'元': 1e-8, '人民币元': 1e-8, '万元': 1e-4, '人民币万元': 1e-4,
          '亿元': 1.0, '人民币亿元': 1.0}


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        n = float(str(value).replace(',', '').replace('，', '').strip().rstrip('%％'))
        return n if math.isfinite(n) else None
    except (ValueError, TypeError):
        return None


def _close(a, b, *, tolerance=1e-6):
    return a is not None and b is not None and math.isclose(a,b,rel_tol=tolerance,abs_tol=1e-7)


def _file_path(source):
    candidate = Path(str(source.get('file_path') or ''))
    if not str(source.get('file_path') or ''):
        return None
    if not candidate.is_absolute():
        candidate = get_data_root() / candidate
    resolved = candidate.resolve()
    roots = [get_data_root().resolve(), (ROOT/'data').resolve(), (ROOT/'tests'/'fixtures').resolve()]
    return resolved if any(resolved.is_relative_to(root) for root in roots) and resolved.is_file() else None


def _read_original(source):
    """Read an actual referenced cell/page, rather than matching arbitrary numbers."""
    path = _file_path(source)
    if path is None:
        return None, '未取得当前资料库中的原文件'
    if path.stat().st_size > 30*1024*1024:
        return None, '原文件超过核验体积限制'
    cell = str(source.get('cell') or '')
    if path.suffix.lower() in {'.csv','.xlsx','.xls'} and cell:
        import pandas as pd
        match = re.search(r'([A-Z]+)([1-9]\d*)$',cell)
        if not match:
            return None, '原始单元格定位未识别'
        col = 0
        for letter in match[1]:
            col = col*26 + ord(letter)-64
        row = int(match[2])-1
        if path.suffix.lower()=='.csv':
            try:
                frame=pd.read_csv(path,header=None,dtype=str,encoding='utf-8-sig')
            except UnicodeDecodeError:
                frame=pd.read_csv(path,header=None,dtype=str,encoding='gbk')
        else:
            sheet=str(source.get('sheet') or cell.rsplit('!',1)[0] if '!' in cell else source.get('sheet') or '')
            frame=pd.read_excel(path,sheet_name=sheet.strip("'") if sheet else 0,header=None,dtype=str)
        try:
            return str(frame.iat[row,col-1]), '已重新读取原始单元格'
        except IndexError:
            return None, '原始单元格超出文件范围'
    if path.suffix.lower()=='.pdf' and source.get('page'):
        import fitz
        with fitz.open(path) as doc:
            page=int(source['page'])-1
            if not 0<=page<len(doc):
                return None, '原文页码超出文件范围'
            text=doc[page].get_text()
        raw=str(source.get('raw_value') or '')
        # Presence alone cannot prove which column/metric a value belongs to.
        return None, ('原文页可读，原值出现，仍需列口径复核' if raw and raw.replace(',','') in text.replace(',','')
                      else '原文页可读，未自动定位该指标的独立单元格')
    return None, '该文件定位暂需人工原文复核'


def _accounting_basis(row,metrics):
    sources=row.get('metric_provenance') or {}
    if not isinstance(sources,dict):
        return False,'会计输入缺少逐项口径记录，数值等式仍需人工复核'
    bases=[]
    for metric in metrics:
        source=sources.get(metric) or {}
        if not isinstance(source,dict): source={}
        meta=source.get('scope_probe') or source.get('accounting_basis') or source
        fields=(meta.get('currency') or source.get('currency_hint'),
                meta.get('statement_scope') or source.get('scope_hint'),
                meta.get('report_version') or source.get('version_hint'),
                source.get('normalized_unit'))
        if any(value in {None,'','unknown','未确认','未记录'} for value in fields):
            return False,'币种、报表范围或披露版本未确认；已列出数值复算，不作会计等式通过认定'
        bases.append(fields)
    if len(set(bases))!=1:
        return False,'会计输入的币种、报表范围、版本或单位不一致，需统一口径'
    return True,'已按逐项记录的同币种、同报表范围、同披露版本与同单位复算'


def audit_answer(result: dict[str, Any]) -> dict[str, Any]:
    """Return original/formula/accounting paths, each with its own real status."""
    rows=[]
    snapshot=result.get('data_snapshot') or {}
    parsed=result.get('parsed') or {}
    requested=set(parsed.get('metrics') or [])
    for company, records in snapshot.items():
        for row in records:
            provenance=row.get('metric_provenance') or {}
            if not isinstance(provenance,dict):
                provenance={}
            for metric, source in provenance.items():
                if requested and metric not in requested:
                    continue
                source = source if isinstance(source,dict) else {}
                value=_number(row.get(metric))
                raw=_number(source.get('raw_value'))
                unit=str(source.get('raw_unit') or '')
                if value is None:
                    continue
                check={'company':company,'year':row.get('year'),'metric':metric,'value':value,
                       'path':'original','status':'pending','reason':'原值或单位缺失，需人工核验'}
                try:
                    original,reason=_read_original(source)
                    check['reason']=reason
                    if original is not None:
                        from core.import_validation import normalize_metric
                        normalized,original_unit,normalized_unit=normalize_metric(metric,original,unit)
                        check.update(status='passed' if _close(normalized,value) else 'conflict',
                                     original_value=str(original),original_unit=original_unit,
                                     recomputed_value=normalized,normalized_unit=normalized_unit,
                                     reason='原单元格按已记录单位换算并与回答数据比对')
                except Exception as exc:
                    check['reason']='原文件重读失败：'+type(exc).__name__
                rows.append(check)
    if not rows:
        rows.append({'path':'original','status':'pending','reason':'该回答未提供可独立重读的逐指标原文定位'})
    formula=[]
    attribution=result.get('attribution') or {}
    root=attribution.get('root') or {}
    if attribution.get('status')=='success' and root:
        prev,current=_number(root.get('previous_value')),_number(root.get('current_value'))
        if prev is not None and current is not None:
            delta=current-prev
            rate=delta/abs(prev)*100 if prev!=0 else None
            formula.append({'path':'formula','name':'年度利润变动','expected':delta,
                            'actual':root.get('delta'),'status':'passed' if _close(delta,_number(root.get('delta'))) else 'conflict'})
            formula.append({'path':'formula','name':'同比复算（负基期用绝对值）','expected':rate,
                            'actual':root.get('growth_rate'), 'status':('not_applicable' if prev==0 else
                            'passed' if _close(rate,_number(root.get('growth_rate')),tolerance=1e-4) else 'conflict')})
    for item in result.get('calculations') or []:
        a,b=_number(item.get('previous_value')),_number(item.get('current_value'))
        rate=(b-a)/abs(a)*100 if a not in {None,0} and b is not None else None
        formula.append({'path':'formula','name':item.get('metric','同比'),'expected':rate,
                        'actual':item.get('growth_rate'),'status':('not_applicable' if rate is None else
                        'passed' if _close(rate,_number(item.get('growth_rate')),tolerance=1e-4) else 'conflict')})
    if not formula:
        formula=[{'path':'formula','status':'not_applicable','reason':'该回答没有待核验的派生公式'}]
    accounting=[]
    for company,records in snapshot.items():
        for row in records:
            a,l,e=(_number(row.get(k)) for k in ['total_assets','total_liabilities','total_equity'])
            if all(n is not None for n in [a,l,e]):
                known,reason=_accounting_basis(row,['total_assets','total_liabilities','total_equity'])
                accounting.append({'path':'accounting','company':company,'year':row.get('year'),
                    'name':'资产=负债+权益','expected':l+e,'actual':a,
                    'numeric_match':_close(a,l+e,tolerance=1e-4), 'reason':reason,
                    'status':('passed' if _close(a,l+e,tolerance=1e-4) else 'conflict') if known else 'pending'})
            profit,parent,minority=(_number(row.get(k)) for k in ['consolidated_net_profit','net_profit','minority_profit'])
            if all(n is not None for n in [profit,parent,minority]):
                known,reason=_accounting_basis(row,['consolidated_net_profit','net_profit','minority_profit'])
                accounting.append({'path':'accounting','company':company,'year':row.get('year'),
                    'name':'合并净利润=归母净利润+少数股东损益','expected':parent+minority,'actual':profit,
                    'numeric_match':_close(profit,parent+minority,tolerance=1e-4), 'reason':reason,
                    'status':('passed' if _close(profit,parent+minority,tolerance=1e-4) else 'conflict') if known else 'pending'})
    if not accounting:
        accounting=[{'path':'accounting','status':'not_applicable','reason':'当前回答缺少同口径会计等式的全部输入，不作通过认定'}]
    paths={'original':rows,'formula':formula,'accounting':accounting}
    all_checks=rows+formula+accounting
    conflicts=sum(x['status']=='conflict' for x in all_checks)
    pending=sum(x['status']=='pending' for x in all_checks)
    return {'status':'conflict' if conflicts else 'partial' if pending else 'verified',
            'paths':paths,'conflicts':conflicts,'pending':pending,
            'message':'发现差异，请人工复核' if conflicts else '部分依据仍需人工复核' if pending else '本次适用校验完成'}
