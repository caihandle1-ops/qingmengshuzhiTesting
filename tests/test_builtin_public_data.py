"""Financial definition/unit checks against independently published annual reports."""
from copy import deepcopy
from decimal import Decimal
import csv
import json
from pathlib import Path

import pytest

from tools.build_builtin_public_data import validate

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / 'data/provenance/builtin_public_financials_20261013.json'


@pytest.fixture(scope='module')
def snapshot():
    return json.loads(SNAPSHOT.read_text(encoding='utf-8'))


def test_all_public_rows_have_matching_income_definitions_and_complete_periods(snapshot):
    profiles, metrics = validate(snapshot)
    assert len(profiles) == 40 and len(metrics) == 120
    for code in profiles:
        with (ROOT / f'data/sample_{code}.csv').open(encoding='utf-8', newline='') as stream:
            rows = list(csv.DictReader(stream))
        assert {int(row['year']) for row in rows} == {2023, 2024, 2025}
        for row in rows:
            assert row['source_currency'] == 'CNY'
            assert row['source_snapshot'] == SNAPSHOT.relative_to(ROOT).as_posix()
            assert row['audit_opinion'] == ''
            for field in ('revenue', 'net_profit', 'operating_cashflow'):
                assert Decimal(row[field]) * Decimal('100000000') == Decimal(row['raw_' + field])


@pytest.mark.parametrize('code,year,expected', [
    # CNINFO 1222993920.PDF, Moutai 2024 annual report, financial highlights (yuan).
    ('600519', 2024, ('170899152276.34', '86228146421.62', '92463692168.43', '36.02', '68.64')),
    # CNINFO 1225058109.PDF, Midea 2025 annual report summary, pp.4-5 (thousand yuan).
    ('000333', 2024, ('407149600000', '38537237000', '60511572000', '21.29', '5.44')),
    ('000333', 2025, ('456451731000', '43945411000', '53345930000', '19.70', '5.80')),
])
def test_official_report_spot_checks(code, year, expected):
    with (ROOT / f'data/sample_{code}.csv').open(encoding='utf-8', newline='') as stream:
        row = next(row for row in csv.DictReader(stream) if int(row['year']) == year)
    fields = ('raw_revenue', 'raw_net_profit', 'raw_operating_cashflow', 'roe', 'eps')
    assert [Decimal(row[field]) for field in fields] == [Decimal(value) for value in expected]


@pytest.mark.parametrize('damage', ['currency', 'period', 'future', 'duplicate', 'revenue', 'margin'])
def test_rejects_invalid_or_incompatible_public_data(snapshot, damage):
    broken = deepcopy(snapshot)
    row = broken['tables']['metrics'][0]
    if damage == 'currency':
        row['CURRENCY'] = 'USD'
    elif damage == 'period':
        row['REPORT_DATE'] = row['REPORT_DATE'][:4] + '-09-30 00:00:00'
    elif damage == 'future':
        row['NOTICE_DATE'] = '2099-01-01 00:00:00'
    elif damage == 'duplicate':
        broken['tables']['metrics'].append(deepcopy(row))
    elif damage == 'revenue':
        row['OPERATE_INCOME_PK'] = float(row['OPERATE_INCOME_PK']) * 1000
    else:
        row['XSMLL'] = 12345
    with pytest.raises(ValueError):
        validate(broken)
