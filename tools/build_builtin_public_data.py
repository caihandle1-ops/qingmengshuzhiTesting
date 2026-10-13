"""Build the pinned 40-company public snapshot. Explicit maintainer command only.

No network calls are made by application startup. --snapshot revalidates and
rebuilds from the local snapshot without network access. This does not verify
every annual-report original; the provider and disclosure/version dates remain
attached to every row. Existing ten user-supplied sample files are never edited.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import csv
import json
from pathlib import Path
from urllib.parse import urlencode

import requests

ROOT = Path(__file__).resolve().parents[1]
API = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
AS_OF = '2026-10-13'
YEARS = (2023, 2024, 2025)
ADDITIONS = (
    ('600519', '食品饮料（白酒）', ['贵州茅台', '茅台']),
    ('000858', '食品饮料（白酒）', ['五粮液']),
    ('600809', '食品饮料（白酒）', ['山西汾酒', '汾酒']),
    ('600887', '食品饮料（乳制品）', ['伊利股份', '伊利']),
    ('603288', '食品饮料（调味品）', ['海天味业']),
    ('600600', '食品饮料（啤酒）', ['青岛啤酒', '青啤']),
    ('603345', '食品饮料（速冻食品）', ['安井食品']),
    ('002714', '农林牧渔（养殖）', ['牧原股份', '牧原']),
    ('000333', '家用电器（综合家电）', ['美的集团', '美的']),
    ('000651', '家用电器（空调）', ['格力电器', '格力']),
    ('600690', '家用电器（综合家电）', ['海尔智家', '海尔']),
    ('002032', '家用电器（厨房电器）', ['苏泊尔']),
    ('600276', '医药生物（创新药）', ['恒瑞医药', '恒瑞']),
    ('300760', '医药生物（医疗器械）', ['迈瑞医疗', '迈瑞']),
    ('000538', '医药生物（中药）', ['云南白药']),
    ('600436', '医药生物（中药）', ['片仔癀']),
    ('300015', '医药生物（医疗服务）', ['爱尔眼科']),
    ('603259', '医药生物（研发服务）', ['药明康德']),
    ('000063', '通信（通信设备）', ['中兴通讯', '中兴']),
    ('002230', '计算机（人工智能）', ['科大讯飞', '讯飞']),
    ('002415', '计算机（智能物联）', ['海康威视', '海康']),
    ('002475', '电子（消费电子）', ['立讯精密', '立讯']),
    ('000725', '电子（显示面板）', ['京东方A', '京东方']),
    ('601138', '电子（工业互联网）', ['工业富联']),
    ('600031', '机械设备（工程机械）', ['三一重工']),
    ('000425', '机械设备（工程机械）', ['徐工机械', '徐工']),
    ('000157', '机械设备（工程机械）', ['中联重科']),
    ('000338', '汽车（动力系统）', ['潍柴动力', '潍柴']),
    ('600660', '汽车（汽车玻璃）', ['福耀玻璃', '福耀']),
    ('601766', '轨道交通（装备制造）', ['中国中车', '中车']),
    ('601088', '能源（煤炭）', ['中国神华', '神华']),
    ('600028', '能源（石油石化）', ['中国石化', '中石化']),
    ('601857', '能源（石油石化）', ['中国石油', '中石油']),
    ('600900', '公用事业（水电）', ['长江电力']),
    ('601012', '新能源（光伏）', ['隆基绿能', '隆基股份', '隆基']),
    ('600019', '钢铁（板材）', ['宝钢股份', '宝钢']),
    ('601899', '有色金属（矿业）', ['紫金矿业', '紫金']),
    ('600309', '基础化工（化工新材料）', ['万华化学', '万华']),
    ('002352', '交通运输（快递物流）', ['顺丰控股', '顺丰']),
    ('601919', '交通运输（航运）', ['中远海控']),
)
FIELDS = {
    'revenue': 'OPERATE_INCOME_PK', 'net_profit': 'PARENTNETPROFIT',
    'operating_cashflow': 'NETCASH_OPERATE_PK', 'roe': 'ROEJQ',
    'debt_ratio': 'ZCFZL', 'gross_margin': 'XSMLL', 'eps': 'EPSJB',
}
AMOUNTS = {'revenue', 'net_profit', 'operating_cashflow'}
TABLES = {
    'profiles': ('RPT_F10_BASIC_ORGINFO', 'SECUCODE,SECURITY_CODE,SECURITY_NAME_ABBR,ORG_NAME,EM2016,CURRENCY'),
    'metrics': ('RPT_F10_FINANCE_MAINFINADATA', 'SECUCODE,SECURITY_CODE,SECURITY_NAME_ABBR,ORG_TYPE,REPORT_DATE,REPORT_TYPE,NOTICE_DATE,UPDATE_DATE,CURRENCY,' + ','.join(FIELDS.values())),
    'income': ('RPT_F10_FINANCE_GINCOME', 'SECUCODE,SECURITY_CODE,REPORT_DATE,REPORT_TYPE,NOTICE_DATE,CURRENCY,OPERATE_INCOME,OPERATE_COST,PARENT_NETPROFIT,BASIC_EPS'),
}


def params_for(table, codes):
    report, columns = TABLES[table]
    codes_sql = ','.join('"' + code + '"' for code in codes)
    query = f'(SECURITY_CODE in ({codes_sql}))'
    if table != 'profiles':
        dates_sql = ','.join("'" + str(year) + "-12-31'" for year in YEARS)
        query += f'(REPORT_DATE in ({dates_sql}))'
    return dict(reportName=report, columns=columns, filter=query, pageSize=500,
                pageNumber=1, source='WEB', client='WEB')


def download():
    snapshot = {'schema_version': 1, 'as_of': AS_OF,
                'retrieved_at': datetime.now(timezone(timedelta(hours=8))).isoformat(),
                'provider': '东方财富公开财务数据', 'endpoint': API,
                'years': list(YEARS), 'queries': {}, 'tables': {}}
    codes = [row[0] for row in ADDITIONS]
    for table in TABLES:
        params = params_for(table, codes)
        response = requests.get(API, params=params, timeout=45)
        response.raise_for_status()
        payload = response.json()
        if not payload.get('success') or not payload.get('result'):
            raise ValueError(f'{table}: provider rejected request: {payload.get("message")}')
        result = payload['result']
        if int(result.get('pages', 1)) != 1:
            raise ValueError(f'{table}: unexpected pagination; do not silently truncate')
        snapshot['queries'][table] = response.url
        snapshot['tables'][table] = result['data']
        print(f'{table}: {len(result["data"])} public records')
    return snapshot


def decimal(value):
    if value is None:
        return None
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError('Non-finite financial data')
    return result


def validate(snapshot):
    expected = {row[0] for row in ADDITIONS}
    profiles = {row['SECURITY_CODE']: row for row in snapshot['tables']['profiles']}
    if set(profiles) != expected or len(snapshot['tables']['profiles']) != 40:
        raise ValueError('Expected exactly 40 unique company profiles')
    keyed = {}
    for table in ('metrics', 'income'):
        keyed[table] = {}
        for row in snapshot['tables'][table]:
            key = (row['SECURITY_CODE'], int(row['REPORT_DATE'][:4]))
            if key in keyed[table] or key[0] not in expected or key[1] not in YEARS:
                raise ValueError(f'{table}: duplicate or unexpected company/year {key}')
            if row.get('CURRENCY') != 'CNY' or row['REPORT_DATE'][5:10] != '12-31' or row.get('REPORT_TYPE') != '年报':
                raise ValueError(f'{table}: currency or annual-period mismatch {key}')
            if not row.get('NOTICE_DATE') or row['NOTICE_DATE'][:10] > AS_OF:
                raise ValueError(f'{table}: missing/future disclosure date {key}')
            keyed[table][key] = row
        if set(keyed[table]) != {(code, year) for code in expected for year in YEARS}:
            raise ValueError(f'{table}: expected all 120 annual records')
    for key, metric in keyed['metrics'].items():
        income = keyed['income'][key]
        for field, other in [('OPERATE_INCOME_PK', 'OPERATE_INCOME'), ('PARENTNETPROFIT', 'PARENT_NETPROFIT'), ('EPSJB', 'BASIC_EPS')]:
            first, second = decimal(metric.get(field)), decimal(income.get(other))
            if first is None or second is None or abs(first-second) > max(Decimal('.000001'), abs(second)*Decimal('0.000000001')):
                raise ValueError(f'{key}: inconsistent {field}/{other}')
        revenue, cost, margin = decimal(income['OPERATE_INCOME']), decimal(income['OPERATE_COST']), decimal(metric.get('XSMLL'))
        if revenue and cost is not None and margin is not None:
            if abs((revenue-cost)/revenue*100-margin) > Decimal('.005'):
                raise ValueError(f'{key}: gross-margin definition mismatch')
        for field in FIELDS.values():
            decimal(metric.get(field))
    return profiles, keyed['metrics']


def build(snapshot):
    profiles, metrics = validate(snapshot)
    catalog_path = ROOT / 'data/builtin_companies.json'
    existing = json.loads(catalog_path.read_text(encoding='utf-8'))['companies']
    legacy = [row for row in existing if row['stock_code'] not in {item[0] for item in ADDITIONS}]
    if len(legacy) != 10:
        raise ValueError('Refusing to replace an unexpected legacy catalog')
    snapshot_name = 'builtin_public_financials_20261013.json'
    snapshot_path = ROOT / 'data/provenance' / snapshot_name
    snapshot_path.parent.mkdir(exist_ok=True)
    snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    columns = ['year', *FIELDS, 'audit_opinion', 'raw_source', 'source_url', 'source_notice_date',
               'source_update_date', 'source_currency', 'source_provider', 'source_snapshot', 'source_retrieved_at',
               *('source_'+metric+'_field' for metric in FIELDS), *('raw_'+metric for metric in FIELDS)]
    catalog = list(legacy)
    for code, industry, aliases in ADDITIONS:
        profile = profiles[code]
        name = profile['ORG_NAME']
        filename = 'sample_'+code+'.csv'
        aliases = list(dict.fromkeys([profile['SECURITY_NAME_ABBR'], *aliases]))
        catalog.append(dict(name=name, stock_code=code, industry=industry, file_name=filename, aliases=aliases))
        with (ROOT/'data'/filename).open('w', encoding='utf-8', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            for year in YEARS:
                source = metrics[(code, year)]
                row = dict(year=year, audit_opinion='', source_provider=snapshot['provider'],
                           source_url=API+'?'+urlencode(params_for('metrics', [code])),
                           source_notice_date=source['NOTICE_DATE'][:10], source_update_date=(source.get('UPDATE_DATE') or '')[:10],
                           source_currency=source['CURRENCY'], source_snapshot='data/provenance/'+snapshot_name,
                           source_retrieved_at=snapshot['retrieved_at'],
                           raw_source=f'{name} {year}年报公开结构化财务数据；东方财富F10；采集截至{AS_OF}；营业收入/归母净利润/经营现金流净额，金额由元换算为亿元；保留上游披露版本，未逐份人工核验年报原件。')
                for metric, field in FIELDS.items():
                    value = decimal(source.get(field))
                    row[metric] = '' if value is None else format(value/Decimal(100000000) if metric in AMOUNTS else value, 'f')
                    row['raw_'+metric] = '' if value is None else format(value, 'f')
                    row['source_'+metric+'_field'] = field
                writer.writerow(row)
    if len(catalog) != 50 or len({row['stock_code'] for row in catalog}) != 50:
        raise ValueError('Catalog must contain exactly 50 unique companies')
    catalog_path.write_text(json.dumps({'schema_version':1, 'companies':catalog},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('Built 50-company catalog; retained 10 legacy CSVs; added 120 annual rows for 40 companies.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, help='Rebuild from a previously downloaded snapshot without network')
    options = parser.parse_args()
    snapshot = json.loads(options.snapshot.read_text(encoding='utf-8')) if options.snapshot else download()
    build(snapshot)
