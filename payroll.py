"""Malaysian statutory payroll contributions: EPF (KWSP), SOCSO (PERKESO) and EIS (SIP).

Rates applied (as at 2026):
  EPF  - citizen/PR below 60 : employee 11%, employer 13% (wages <= RM5,000) / 12% (> RM5,000)
         citizen aged 60+     : employee 0%,  employer 4%
         PR aged 60+          : employee 5.5%, employer 6.5%
         foreign worker       : employee 2%,  employer 2%  (mandatory since Oct 2025)
         Amounts follow the Third Schedule banding: RM20 wage bands up to RM5,000,
         RM100 bands up to RM20,000, exact % above; contributions rounded up to the next ringgit.
  SOCSO - wage ceiling RM6,000, bracket mid-point basis, rounded up to 5 sen.
         Category 1 (below 60): Employment Injury 1.25% + Invalidity 0.5% employer, 0.5% employee
         Category 2 (60+) and foreign workers: Employment Injury only, 1.25% employer
  EIS  - 0.2% employee + 0.2% employer, ceiling RM6,000, citizens/PR aged 18-59 only.
  PCB/MTD - taken from the employee record (compute with LHDN's e-PCB calculator); this
         prototype does not compute the MTD formula.

Note: Employment income is exempt from e-Invoicing (LHDN e-Invoice Guideline), so payroll is
posted to the ledger but no e-Invoice is issued for salaries, EPF or SOCSO.
"""
import math
from datetime import date


def _ceil_ringgit(x):
    return float(math.ceil(round(x, 6)))


def _ceil_5sen(x):
    return math.ceil(round(x * 20, 6)) / 20.0


def age_on(dob: str, nric: str, on: date) -> int:
    d = None
    if dob:
        try:
            d = date.fromisoformat(dob)
        except ValueError:
            d = None
    if d is None and nric:
        digits = "".join(c for c in nric if c.isdigit())
        if len(digits) >= 6:
            yy, mm, dd = int(digits[:2]), int(digits[2:4]), int(digits[4:6])
            century = 2000 if yy <= on.year % 100 else 1900
            try:
                d = date(century + yy, mm, dd)
            except ValueError:
                d = None
    if d is None:
        return 30
    return on.year - d.year - ((on.month, on.day) < (d.month, d.day))


def epf_rates(wage, age, residency):
    if residency == "foreign":
        return 0.02, 0.02
    if age >= 60:
        return (0.055, 0.065) if residency == "pr" else (0.0, 0.04)
    return 0.11, (0.13 if wage <= 5000 else 0.12)


def epf(wage, age=30, residency="citizen"):
    """Returns (employee, employer)."""
    if wage <= 10:
        return 0.0, 0.0
    ee_rate, er_rate = epf_rates(wage, age, residency)
    if wage <= 5000:
        base = math.ceil(wage / 20 - 1e-9) * 20
    elif wage <= 20000:
        base = math.ceil(wage / 100 - 1e-9) * 100
    else:
        base = wage
    return _ceil_ringgit(base * ee_rate), _ceil_ringgit(base * er_rate)


SOCSO_CEILING = 6000.0
_LOW_BRACKETS = [(0, 30), (30, 50), (50, 70), (70, 100), (100, 140), (140, 200), (200, 300)]


def _bracket_mid(wage):
    w = min(wage, SOCSO_CEILING + 0.01)
    if w > SOCSO_CEILING:
        return SOCSO_CEILING - 50
    for lo, hi in _LOW_BRACKETS:
        if lo < w <= hi:
            return (lo + hi) / 2
    hi = math.ceil(w / 100 - 1e-9) * 100
    return hi - 50


def socso(wage, age=30, residency="citizen"):
    """Returns (employee, employer, category)."""
    if wage <= 0:
        return 0.0, 0.0, None
    mid = _bracket_mid(wage)
    if age >= 60 or residency == "foreign":
        return 0.0, _ceil_5sen(mid * 0.0125), 2
    return _ceil_5sen(mid * 0.005), _ceil_5sen(mid * 0.0175), 1


def eis(wage, age=30, residency="citizen"):
    """Returns (employee, employer)."""
    if wage <= 0 or residency == "foreign" or age >= 60 or age < 18:
        return 0.0, 0.0
    c = _ceil_5sen(_bracket_mid(wage) * 0.002)
    return c, c


def compute_payslip(emp, period: str, overtime=0.0, bonus=0.0, other_deduction=0.0):
    """emp: employee row (dict-like). period: 'YYYY-MM'."""
    y, m = map(int, period.split("-"))
    age = age_on(emp["dob"], emp["nric"], date(y, m, 1))
    basic = float(emp["basic_salary"])
    allowance = float(emp["fixed_allowance"])
    gross = round(basic + allowance + overtime + bonus, 2)
    epf_wage = gross - overtime                 # EPF: overtime is not "wages" under the EPF Act; bonus is
    socso_wage = basic + allowance + overtime   # SOCSO/EIS: overtime is wages; bonus is not
    epf_ee, epf_er = epf(epf_wage, age, emp["residency"])
    socso_ee, socso_er, cat = socso(socso_wage, age, emp["residency"])
    eis_ee, eis_er = eis(socso_wage, age, emp["residency"])
    pcb = float(emp["pcb_monthly"] or 0)
    net = round(gross - epf_ee - socso_ee - eis_ee - pcb - other_deduction, 2)
    return {"employee_id": emp["id"], "basic": basic, "allowance": allowance, "overtime": overtime,
            "bonus": bonus, "other_deduction": other_deduction, "gross": gross,
            "epf_ee": epf_ee, "epf_er": epf_er, "socso_ee": socso_ee, "socso_er": socso_er,
            "eis_ee": eis_ee, "eis_er": eis_er, "pcb": pcb, "net": net, "age": age, "socso_category": cat}
