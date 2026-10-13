"""Executable, finite financial screening workflows using current data only."""
from __future__ import annotations

import re
from time import perf_counter

from core.analysis import safe_num
from core.retrieval import retrieve_documents


def is_research_task(question):
    return (any(w in question for w in ['筛选','找出','哪些公司','哪些企业'])
            and any(w in question for w in ['营收','营业收入'])
            and any(w in question for w in ['现金流','利润']))


def run_research_task(question, data_map, *, reports=None, index_root=None, progress=None,
                      checkpoint=None, save_checkpoint=None, check_interrupt=None):
    """Screen revenue up and cash flow/profit down; attach scoped report evidence."""
    started=perf_counter()
    checkpoint = checkpoint if checkpoint is not None else {}
    completed = checkpoint.setdefault('completed', {})
    def interrupt(stage):
        # Unlike optional progress notifications, cancellation/budget exceptions
        # must escape to the durable job runner.
        if check_interrupt is not None:
            check_interrupt(stage)
    def persist():
        if save_checkpoint is not None:
            save_checkpoint(checkpoint)
    interrupt('研究任务条件识别')
    def notify(stage, status, detail):
        if progress is not None:
            try:
                progress(stage, status, detail)
            except Exception:
                pass
    notify('understand','running','正在识别筛选指标、年度与变化条件。')
    if not is_research_task(question):
        notify('understand','blocked','请求不属于当前支持的财务筛选任务。')
        return {'status':'unsupported','message':'支持筛选营收增长、现金流或归母净利润下降的企业，并检索解释依据。'}
    if any(w in question for w in ['季度','上半年','下半年','半年度']):
        notify('understand','blocked','此筛选任务只支持全年可比数据。')
        return {'status':'unsupported','message':'此研究任务只支持全年可比数据；未用年度数据替代季度或半年。'}
    years=sorted(set(int(y) for y in re.findall(r'(?<!\d)20\d{2}(?!\d)',question)))
    if len(years)>2 or (len(years)==2 and years[1]!=years[0]+1):
        notify('understand','blocked','请确认目标年度或连续的基期与目标年度。')
        return {'status':'needs_clarification','message':'请指定一个目标年度，或连续的基期与目标年度。'}
    if not years:
        notify('understand','blocked','请指定筛选年度，后续计算尚未执行。')
        return {'status':'needs_clarification','message':'请指定筛选年度，例如“筛选2024年营收增长但现金流下降的企业”。'}
    current=years[-1]; previous=years[0] if len(years)==2 else current-1
    target='operating_cashflow' if '现金流' in question else 'net_profit'
    if not any(w in question for w in ['下降','降低','减少','下滑','增收不增利']):
        notify('understand','blocked','请确认增长与下降的筛选条件。')
        return {'status':'needs_clarification','message':'请确认筛选条件：营收增长，且现金流或归母净利润下降。'}
    notify('understand','completed','筛选期间与指标条件已识别。')
    notify('retrieve','running','正在读取企业连续年度指标并检查缺失范围。')
    metrics=['revenue',target]
    trace=[{'step':1,'name':'任务拆解','status':'completed','detail':f'{previous}至{current}年；先复核数据，再计算与筛选，最后检索解释'}]
    qualifying=[]; excluded=[]; snapshots={}; calculations=[]
    for company,frame in data_map.items():
        interrupt('企业年度筛选')
        if frame.empty or 'year' not in frame:
            excluded.append({'company':company,'reason':'没有年度指标'}); continue
        selected=frame[frame['year'].astype(int).isin([previous,current])]
        if len(selected)!=2 or set(selected['year'].astype(int))!={previous,current}:
            excluded.append({'company':company,'reason':'缺少连续两个年度'}); continue
        rows={int(row['year']):row.to_dict() for _,row in selected.iterrows()}
        vals={y:{m:safe_num(rows[y].get(m)) for m in metrics} for y in rows}
        if any(v is None for record in vals.values() for v in record.values()):
            excluded.append({'company':company,'reason':'缺少筛选指标，不补值'}); continue
        snapshots[company]=[rows[previous],rows[current]]
        if not calculations:
            notify('calculate','running','已找到完整年度指标，正在复算变化与筛选条件。')
        revenue_delta=vals[current]['revenue']-vals[previous]['revenue']
        target_delta=vals[current][target]-vals[previous][target]
        for metric in metrics:
            a,b=vals[previous][metric],vals[current][metric]
            calculations.append({'company':company,'metric':metric,'previous_value':a,'current_value':b,
                                 'growth_rate':None if a==0 else (b-a)/abs(a)*100})
        if revenue_delta>0 and target_delta<0:
            qualifying.append({'company':company,'year':current,'revenue_previous':vals[previous]['revenue'],
                'revenue_current':vals[current]['revenue'],'revenue_delta':revenue_delta,
                'target_metric':target,'target_previous':vals[previous][target],
                'target_current':vals[current][target],'target_delta':target_delta})
    trace.extend([{'step':2,'name':'年度与缺失值核验','status':'completed','detail':f'可计算{len(snapshots)}家，缺失排除{len(excluded)}家'},
                  {'step':3,'name':'变化复算与筛选','status':'completed','detail':f'符合{len(qualifying)}家，未将缺失归为不符合'}])
    notify('retrieve','completed','年度指标读取结束；缺失企业仍按缺失范围排除。')
    notify('calculate','completed' if calculations else 'skipped',
           '变化复算与条件筛选已执行。' if calculations else '没有完整可计算范围，未执行变化复算。')
    wants_evidence=any(w in question for w in ['原因','解释','年报','研报','为什么'])
    citations=[]
    checkpoint['total'] = len(qualifying) if wants_evidence else 0
    checkpoint['screened'] = len(snapshots)
    persist()
    if wants_evidence and qualifying:
        notify('retrieve','running','正在按符合条件的企业检索报告解释依据。')
    for row in qualifying:
        if not wants_evidence:
            row['explanation']='未请求报告解释'; continue
        company = row['company']
        if company in completed:
            result = completed[company]
        else:
            interrupt('企业报告检索：' + company)
            result=retrieve_documents(f'{company}{current}年'+('现金流' if target=='operating_cashflow' else '净利润')+'下降的原因是什么？',
                {'companies':[company],'years':[current],'metrics':[target],'period':'annual'},
                reports=reports,index_root=index_root)
            completed[company] = result
            # Commit the actual retrieval result before a budget/cancellation
            # check: an interrupted next step must not repeat this company.
            persist()
            interrupt('企业检索检查点已保存：' + company)
        row['explanation']=result.get('answer','未获得解释依据')
        row['evidence_status']=result.get('status','no_evidence')
        citations.extend(result.get('citations') or [])
    if wants_evidence and qualifying:
        notify('retrieve','completed','报告检索结束；筛选符合条件不等于原因获得证明。')
    trace.append({'step':4,'name':'分企业检索依据','status':'completed' if wants_evidence else 'not_applicable',
                  'detail':f'取得{len(citations)}条文档引用；财务筛选不等于原因证明'})
    names='、'.join(row['company'] for row in qualifying)
    answer=f'{current}年已接入且可计算的{len(snapshots)}家企业中，符合“营收增长、'+('现金流下降' if target=='operating_cashflow' else '归母净利润下降')+f'”的有{len(qualifying)}家。'
    if names: answer+='\n'+names
    if excluded: answer+=f'\n另有{len(excluded)}家因缺少期间或指标排除，未作不符合判断。'
    return {'status':'success','answer':answer,'message':answer,'rows':qualifying,'excluded':excluded,
        'tasks':trace,'duration_ms':round((perf_counter()-started)*1000,3),
        'parsed':{'companies':list(snapshots),'years':[previous,current],'metrics':metrics,'period':'annual','intent':'research_task'},
        'company':qualifying[0]['company'] if qualifying else next(iter(snapshots),''),
        'data_snapshot':snapshots,'calculations':calculations,'retrieval_result':{'citations':citations},
        'sql_result':{'status':'not_applicable','rows':[]},'model_used':'本地分步研究',
        'report_text':answer+'\n\n'+'\n\n'.join(r['company']+'：'+r.get('explanation','') for r in qualifying),
        'evidence':'同一指标年度差值筛选；解释仅采用已检索到的文件。'}
