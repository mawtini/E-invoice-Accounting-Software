"""Double-entry general ledger: posting rules for sales e-Invoices, expenses and payroll."""
from lhdn.codes import REDUCING_TYPES, SELF_BILLED_TYPES


class UnbalancedEntry(ValueError):
    pass


def post_entry(db, date, ref, description, lines, source_type=None, source_id=None):
    """lines: list of (account_code, debit, credit). Returns entry id."""
    lines = [(a, round(d or 0, 2), round(c or 0, 2)) for a, d, c in lines if round(d or 0, 2) or round(c or 0, 2)]
    dr = round(sum(l[1] for l in lines), 2)
    cr = round(sum(l[2] for l in lines), 2)
    if dr != cr:
        raise UnbalancedEntry(f"Entry {ref} unbalanced: Dr {dr} != Cr {cr}")
    cur = db.execute("INSERT INTO journal_entries(date,ref,description,source_type,source_id) VALUES (?,?,?,?,?)",
                     (date, ref, description, source_type, source_id))
    eid = cur.lastrowid
    db.executemany("INSERT INTO journal_lines(entry_id,account_code,debit,credit) VALUES (?,?,?,?)",
                   [(eid, a, d, c) for a, d, c in lines])
    return eid


def reverse_entry(db, entry_id, date, ref, description):
    lines = db.execute("SELECT account_code, debit, credit FROM journal_lines WHERE entry_id=?", (entry_id,)).fetchall()
    return post_entry(db, date, ref, description, [(l["account_code"], l["credit"], l["debit"]) for l in lines],
                      "reversal", entry_id)


def post_sales_document(db, doc, lines):
    """Invoice/DN: Dr AR, Cr revenue (per line account), Cr SST payable. CN/RN reverse it.
    Amounts are converted to MYR at the document exchange rate."""
    if doc["doc_type"] in SELF_BILLED_TYPES:
        return None   # self-billed documents are recorded through the expense
    from lhdn.ubl import compute_line
    fx = float(doc["exchange_rate"] or 1)
    revenue, tax = {}, 0.0
    for ln in lines:
        c = compute_line(ln)
        acct = ln["account_code"] or "4000"
        revenue[acct] = revenue.get(acct, 0) + c["net"] * fx
        tax += c["tax"] * fx
    revenue = {a: round(v, 2) for a, v in revenue.items()}
    tax = round(tax, 2)
    total = round(sum(revenue.values()) + tax, 2)
    entry = [("1100", total, 0)] + [(a, 0, v) for a, v in revenue.items()] + [("2100", 0, tax)]
    if doc["doc_type"] in REDUCING_TYPES:
        entry = [(a, c, d) for a, d, c in entry]
    return post_entry(db, doc["issue_date"], doc["number"], f"Sales document {doc['number']}", entry,
                      "document", doc["id"])


def post_expense(db, exp):
    credit_acct = "1000" if exp["paid"] else "2000"
    return post_entry(db, exp["date"], f"EXP-{exp['id']}", exp["description"],
                      [(exp["account_code"], exp["total"], 0), (credit_acct, 0, exp["total"])], "expense", exp["id"])


def post_payroll(db, run, slips):
    t = {k: round(sum(s[k] for s in slips), 2) for k in
         ("gross", "epf_ee", "epf_er", "socso_ee", "socso_er", "eis_ee", "eis_er", "pcb", "net", "other_deduction")}
    lines = [
        ("6100", t["gross"], 0), ("6110", t["epf_er"], 0), ("6120", t["socso_er"], 0), ("6130", t["eis_er"], 0),
        ("2200", 0, t["net"] + t["other_deduction"]),
        ("2210", 0, t["epf_ee"] + t["epf_er"]), ("2220", 0, t["socso_ee"] + t["socso_er"]),
        ("2230", 0, t["eis_ee"] + t["eis_er"]), ("2240", 0, t["pcb"]),
    ]
    return post_entry(db, run["pay_date"], f"PAY-{run['period']}", f"Payroll {run['period']}", lines,
                      "payroll", run["id"])


def account_balances(db, date_from=None, date_to=None):
    q = """SELECT a.code, a.name, a.type, COALESCE(SUM(x.debit),0) dr, COALESCE(SUM(x.credit),0) cr
           FROM accounts a LEFT JOIN (
               SELECT l.account_code, l.debit, l.credit FROM journal_lines l
               JOIN journal_entries e ON e.id=l.entry_id WHERE e.date >= ? AND e.date <= ?) x
             ON x.account_code=a.code
           GROUP BY a.code ORDER BY a.code"""
    rows = db.execute(q, (date_from or "0000-00-00", date_to or "9999-12-31")).fetchall()
    out = []
    for r in rows:
        bal = r["dr"] - r["cr"]
        out.append({"code": r["code"], "name": r["name"], "type": r["type"], "debit": round(r["dr"], 2),
                    "credit": round(r["cr"], 2), "balance": round(bal, 2)})
    return out
