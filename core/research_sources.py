"""Bounded public research-source queries. Provider failures remain failures."""
from __future__ import annotations

from datetime import date
import json
import re
from urllib.parse import urljoin, urlsplit

import requests

from core.online_disclosure import resolve_stock_code

SOURCES = {
    '北交所公告': 'https://www.bse.cn/disclosure/announcement.html',
    '东方财富个股研报': 'https://data.eastmoney.com/report/',
    '东方财富行业研报': 'https://data.eastmoney.com/report/',
    '国家统计局年度数据': 'https://data.stats.gov.cn/easyquery.htm?cn=C01',
}
MAX_RESPONSE = 3 * 1024 * 1024


def _request(url, *, params=None, data=None, referer=''):
    # URLs are internal constants, never user supplied. No challenge bypass or
    # arbitrary redirects; this adapter reads public listings only.
    method = 'POST' if data is not None else 'GET'
    with requests.request(method, url, params=params, data=data,
            headers={'User-Agent': 'Mozilla/5.0 FinancialResearchReader/3.0',
                     'Referer': referer, 'Accept': 'application/json,text/plain,*/*'},
            timeout=(5, 12), stream=True, allow_redirects=False) as response:
        response.raise_for_status()
        if 300 <= response.status_code < 400:
            raise ValueError('来源要求跳转，请打开原站查阅。')
        parts=[]; size=0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > MAX_RESPONSE:
                raise ValueError('来源返回数据超过本次读取上限。')
            parts.append(chunk)
        text=b''.join(parts).decode('utf-8-sig')
    wrapper=re.fullmatch(r'[\w$]+\((.*)\)\s*;?\s*',text.strip(),flags=re.S)
    return json.loads(wrapper.group(1) if wrapper else text)


def _public_url(value, base, hosts):
    candidate=urljoin(base,str(value or '').strip())
    parsed=urlsplit(candidate)
    if parsed.scheme not in {'https','http'} or parsed.hostname not in hosts or parsed.username or parsed.password:
        return ''
    return candidate.replace('http://','https://',1)


def _bse(query,year,limit):
    code=resolve_stock_code(query)
    keyword=(f'{year}年' if year else '')+'年度报告'
    if query and not code:
        keyword=query+' '+keyword
    data=_request('https://www.bse.cn/disclosureInfoController/companyAnnouncement.do',
        data={'page':0,'companyCd':code or '', 'isNewThree':1,
              'keyword':keyword,'xxfcbj[]':'2',
              'startTime':f'{year}-01-01' if year else '', 'endTime':date.today().isoformat(),
              'needFields[]':['companyCd','companyName','disclosureTitle','disclosurePostTitle','destFilePath','publishDate','fileExt']},
        referer=SOURCES['北交所公告'])
    if not isinstance(data,list) or not data or not isinstance(data[0],dict):
        raise ValueError('北交所未返回可识别的公告列表。')
    info=data[0].get('listInfo')
    if not isinstance(info,dict) or not isinstance(info.get('content'),list):
        raise ValueError('北交所公告列表结构发生变化，请在原站查阅。')
    rows=[]
    for row in info['content']:
        title=str(row.get('disclosureTitle') or '')+str(row.get('disclosurePostTitle') or '')
        if year and not re.search(rf'{year}\s*年',title):
            continue
        rows.append({'title':title,'company':row.get('companyName',''),'code':row.get('companyCd',''),
            'date':str(row.get('publishDate',''))[:10],'source':'北交所公告',
            'url':_public_url(row.get('destFilePath'), 'https://www.bse.cn',
                             {'www.bse.cn','bse.cn','www.neeq.com.cn','static.neeq.com.cn'})})
    return rows[:limit], info.get('totalElements')


def _eastmoney(source,query,year,limit):
    stock=source=='东方财富个股研报'
    code=resolve_stock_code(query) if stock else None
    if stock and query and not code:
        raise ValueError('请填写6位股票代码，或系统已认识的公司名称。')
    params={'qType':0 if stock else 1,'pageNo':1,'pageSize':100 if query and not stock else limit,
            'beginTime':f'{year}-01-01' if year else f'{date.today().year}-01-01',
            'endTime':min(f'{year}-12-31',date.today().isoformat()) if year else date.today().isoformat()}
    if code: params['code']=code
    data=_request('https://reportapi.eastmoney.com/report/list',params=params,referer=SOURCES[source])
    if not isinstance(data,dict) or not isinstance(data.get('data'),list):
        raise ValueError('研报来源未返回可识别列表。')
    rows=[]
    for row in data['data']:
        title=str(row.get('title') or '')
        if query and not stock and query not in title+' '+str(row.get('industryName','')):
            continue
        info=str(row.get('infoCode') or '')
        url=('https://data.eastmoney.com/report/zw_stock.jshtml?infocode='+info) if re.fullmatch(r'AP\d+',info) else ''
        rows.append({'title':title,'company':row.get('stockName',''),'code':row.get('stockCode',''),
            'industry':row.get('industryName',''),'institution':row.get('orgSName',''),
            'date':str(row.get('publishDate',''))[:10],'source':source,'url':url})
    return rows[:limit], data.get('hits')


def _nbs(query,year,limit):
    indicator=query.strip() or 'A0201'
    if not re.fullmatch(r'[A-Z]\d{2,12}',indicator):
        raise ValueError('请填写统计指标代码，例如A0201（国内生产总值）；详细代码在原站查询。')
    data=_request('https://data.stats.gov.cn/easyquery.htm',params={
        'm':'querydata','dbcode':'hgnd','rowcode':'zb','colcode':'sj','wds':'[]',
        'dfwds':json.dumps([{'wdcode':'zb','valuecode':indicator},{'wdcode':'sj','valuecode':str(year) if year else 'LAST5'}])},
        referer=SOURCES['国家统计局年度数据'])
    if not isinstance(data,dict) or data.get('returncode') not in {0,'0',None}:
        raise ValueError('国家统计局未完成数据查询。')
    body=data.get('returndata')
    if not isinstance(body,dict) or not isinstance(body.get('datanodes'),list):
        raise ValueError('国家统计局数据结构未识别，请在原站查阅。')
    labels={}
    for dimension in body.get('wdnodes',[]):
        for node in dimension.get('nodes',[]):
            labels[(dimension.get('wdcode'),node.get('code'))]=node
    rows=[]
    for row in body['datanodes']:
        dims={x.get('wdcode'):x.get('valuecode') for x in row.get('wds',[])}
        metric=labels.get(('zb',dims.get('zb')), {})
        value=row.get('data') or {}
        # Missing observations are not zero.
        rows.append({'indicator':metric.get('cname') or metric.get('name') or dims.get('zb'),
            'code':dims.get('zb'),'year':dims.get('sj'),'value':value.get('data') if value.get('hasdata') else None,
            'unit':metric.get('unit',''),'source':'国家统计局年度数据','url':SOURCES['国家统计局年度数据']})
    return rows[:limit],len(body['datanodes'])


def search_research_sources(source,query='',*,year=None,limit=20):
    if source not in SOURCES:
        return {'status':'invalid','rows':[],'message':'未识别的数据来源。'}
    try:
        if year is not None and not 2000<=int(year)<=date.today().year:
            raise ValueError('查询年份超出支持范围。')
        year=int(year) if year is not None else None
        limit=max(1,min(int(limit),100))
        if source=='北交所公告': rows,total=_bse(query,year,limit)
        elif source.startswith('东方财富'): rows,total=_eastmoney(source,query,year,limit)
        else: rows,total=_nbs(query,year,limit)
        return {'status':'success','rows':rows,'provider_total':total,'source':source,'query':query,'year':year,
            'retrieved_at':date.today().isoformat(),'message':f'本次取得{len(rows)}条；研报预测和宏观数据不会自动写入公司财报指标。',
            'portal':SOURCES[source]}
    except (requests.RequestException, ValueError, KeyError, TypeError, UnicodeError) as exc:
        detail=f'HTTP {exc.response.status_code}' if isinstance(exc,requests.HTTPError) and exc.response is not None else type(exc).__name__
        message=str(exc) if isinstance(exc,ValueError) else f'来源访问失败（{detail}），可打开原站下载后导入。'
        return {'status':'unavailable','rows':[],'message':message,'source':source,'portal':SOURCES[source]}
