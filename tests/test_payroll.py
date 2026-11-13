import pytest
from payroll import epf, socso, eis, compute_payslip, age_on
from datetime import date


@pytest.mark.parametrize("wage,ee,er", [
    (3000, 330, 390),        # Third Schedule: 2,980.01-3,000.00
    (5000, 550, 650),        # 13% employer up to RM5,000
    (5001, 561, 612),        # band 5,000.01-5,100 -> 12% employer
    (2510, 278, 328),        # band 2,500.01-2,520 -> 277.2 / 327.6 rounded up
])
def test_epf_below_60(wage, ee, er):
    assert epf(wage) == (ee, er)


def test_epf_60_and_foreign():
    assert epf(3000, age=62) == (0, 120)
    assert epf(3000, residency="foreign") == (60, 60)


def test_socso_and_eis_table_values():
    assert socso(3000) == (14.75, 51.65, 1)        # PERKESO table 2,900.01-3,000.00
    assert socso(8000) == (29.75, 104.15, 1)       # capped at RM6,000 ceiling
    assert socso(3000, age=61) == (0, 36.90, 2)    # Category 2 - employment injury only
    assert eis(3000) == (5.90, 5.90)
    assert eis(9000) == (11.90, 11.90)
    assert eis(3000, residency="foreign") == (0, 0)


def test_age_from_nric():
    assert age_on(None, "620815-07-3311", date(2026, 11, 1)) == 64
    assert age_on(None, "050101-14-1111", date(2026, 11, 1)) == 21


def test_payslip_net():
    emp = {"id": 1, "dob": None, "nric": "880512-14-5566", "residency": "citizen", "basic_salary": 4500,
           "fixed_allowance": 300, "pcb_monthly": 105.5}
    s = compute_payslip(emp, "2026-11", overtime=200)
    assert s["gross"] == 5000
    assert s["epf_ee"] == 528          # EPF on 4,800 (overtime excluded)
    assert s["net"] == round(5000 - s["epf_ee"] - s["socso_ee"] - s["eis_ee"] - 105.5, 2)
