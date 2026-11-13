"""End-to-end flows through the Flask app against the offline MyInvois simulator."""
import json

import pytest

from app import create_app
from db import connect


@pytest.fixture
def client(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    app.config["TESTING"] = True
    c = app.test_client()
    c.post("/demo-data")
    c.db_path = str(tmp_path / "t.db")
    return c


def q(c, sql, *a):
    conn = connect(c.db_path)
    r = conn.execute(sql, a).fetchall()
    conn.close()
    return r


def tb_balanced(c):
    r = q(c, "SELECT ROUND(SUM(debit),2) d, ROUND(SUM(credit),2) c FROM journal_lines")[0]
    return r["d"] == r["c"]


def bal(c, acct):
    return round(q(c, "SELECT COALESCE(SUM(debit-credit),0) b FROM journal_lines WHERE account_code=?", acct)[0]["b"], 2)


def new_invoice(c, doc_type="01", ref=None, price="1000", tax_type="02", rate="8"):
    pid = q(c, "SELECT id FROM parties WHERE tin='C20880050010'")[0]["id"]
    number = f"T-{doc_type}-{len(q(c, 'SELECT id FROM documents'))}"
    data = {"doc_type": doc_type, "number": number, "issue_date": "2026-11-13", "party_id": pid,
            "currency": "MYR", "payment_mode": "03", "line_description": ["Services"], "line_qty": ["1"],
            "line_unit_price": [price], "line_tax_type": [tax_type], "line_tax_rate": [rate],
            "line_classification": ["022"], "line_unit_code": ["C62"], "line_discount": ["0"],
            "line_account_code": ["4100"], "line_exemption_reason": [""]}
    if ref:
        data["ref_doc_id"] = ref
    r = c.post("/documents/new", data=data)
    assert r.status_code == 302, r.data
    return q(c, "SELECT id FROM documents WHERE number=?", number)[0]["id"]


def test_pages_render(client):
    for url in ["/", "/parties", "/documents", "/documents?kind=self", "/documents/new", "/receipts", "/expenses",
                "/expenses/new", "/employees", "/employees/new", "/payroll", "/accounting/journal",
                "/accounting/reports", "/lhdn", "/lhdn?received=1", "/settings", "/parties/new"]:
        assert client.get(url).status_code == 200, url


def test_invoice_lifecycle(client):
    did = new_invoice(client)
    client.post(f"/documents/{did}/submit")
    d = q(client, "SELECT * FROM documents WHERE id=?", did)[0]
    assert d["status"] == "submitted" and d["lhdn_uuid"]
    client.post(f"/documents/{did}/refresh")
    d = q(client, "SELECT * FROM documents WHERE id=?", did)[0]
    assert d["status"] == "valid" and d["lhdn_long_id"] and d["posted_entry_id"]
    assert bal(client, "1100") == 1080 and bal(client, "4100") == -1000 and bal(client, "2100") == -80
    assert b"<svg" in client.get(f"/documents/{did}/print").data       # QR code with validation link
    # credit note against it
    cn = new_invoice(client, "02", ref=did, price="200")
    client.post(f"/documents/{cn}/submit")
    client.post(f"/documents/{cn}/refresh")
    assert q(client, "SELECT status FROM documents WHERE id=?", cn)[0]["status"] == "valid"
    assert bal(client, "1100") == 864
    sub = json.loads(q(client, "SELECT submitted_json FROM documents WHERE id=?", cn)[0]["submitted_json"])
    assert sub["Invoice"][0]["BillingReference"][0]["InvoiceDocumentReference"][0]["UUID"][0]["_"] == d["lhdn_uuid"]
    # payment and cancellation
    client.post(f"/documents/{did}/paid")
    assert bal(client, "1000") == 1080
    client.post(f"/documents/{cn}/cancel", data={"reason": "Wrong amount"})
    assert q(client, "SELECT status FROM documents WHERE id=?", cn)[0]["status"] == "cancelled"
    assert bal(client, "1100") == 0          # 1080 invoiced - 216 CN - 1080 paid + 216 CN reversed
    assert tb_balanced(client)


def test_invalid_document_reports_errors(client):
    pid = client.post("/parties/new", data={"kind": "customer", "name": "Bad TIN Co", "tin": "XYZ", "id_type": "BRN",
                                            "id_value": "123", "state_code": "10", "country_code": "MYS"})
    bad = q(client, "SELECT id FROM parties WHERE name='Bad TIN Co'")[0]["id"]
    did = new_invoice(client)
    client.post("/documents/{}/edit".format(did), data={
        "doc_type": "01", "number": "BAD-1", "issue_date": "2026-11-13", "party_id": bad, "currency": "MYR",
        "line_description": ["x"], "line_qty": ["1"], "line_unit_price": ["10"], "line_tax_type": ["06"],
        "line_tax_rate": ["0"]})
    client.post(f"/documents/{did}/submit")
    d = q(client, "SELECT * FROM documents WHERE id=?", did)[0]
    assert d["status"] == "draft" and "TIN" in d["lhdn_errors"]


def test_signed_v11_submission(client):
    client.post("/settings/demo-cert")
    client.post("/settings", data={"lhdn_doc_version": "1.1"})
    did = new_invoice(client)
    client.post(f"/documents/{did}/submit")
    client.post(f"/documents/{did}/refresh")
    d = q(client, "SELECT * FROM documents WHERE id=?", did)[0]
    assert d["status"] == "valid", d["lhdn_errors"]
    assert "UBLExtensions" in json.loads(d["submitted_json"])["Invoice"][0]


def test_expense_self_billed(client):
    sup = q(client, "SELECT id FROM parties WHERE tin='EI00000000030'")[0]["id"]
    r = client.post("/expenses/new", data={"date": "2026-11-10", "party_id": sup, "account_code": "6060",
                                           "description": "Cloud hosting Nov", "amount": "500", "tax_type": "06",
                                           "paid": "1", "einvoice_source": "self_billed", "classification": "035"})
    did = int(r.headers["Location"].rsplit("/", 1)[1])
    client.post(f"/documents/{did}/submit")
    client.post(f"/documents/{did}/refresh")
    d = q(client, "SELECT * FROM documents WHERE id=?", did)[0]
    assert d["doc_type"] == "11" and d["status"] == "valid" and d["posted_entry_id"] is None   # no double posting
    assert bal(client, "6060") == 500 and bal(client, "1000") == -500


def test_received_einvoice_import(client):
    client.post("/lhdn/simulate-received")
    uuid = q(client, "SELECT uuid FROM mock_lhdn_docs")[0]["uuid"]
    r = client.get(f"/lhdn/import/{uuid}")
    assert r.status_code == 302 and "supplier_einvoice_uuid=" + uuid in r.headers["Location"]


def test_consolidated(client):
    for i in range(3):
        client.post("/receipts", data={"number": f"R{i}", "date": "2026-10-0%d" % (i + 1), "description": "Walk-in",
                                       "amount": "100", "tax_type": "01", "tax_rate": "10", "payment_mode": "01",
                                       "account_code": "4000"})
    r = client.post("/receipts/consolidate", data={"month": "2026-10"})
    did = int(r.headers["Location"].rsplit("/", 1)[1])
    client.post(f"/documents/{did}/submit")
    client.post(f"/documents/{did}/refresh")
    d = q(client, "SELECT * FROM documents WHERE id=?", did)[0]
    assert d["status"] == "valid" and d["total"] == 330 and d["posted_entry_id"] is None
    assert bal(client, "4000") == -300                 # posted once, via receipts


def test_payroll_flow(client):
    client.post("/payroll", data={"period": "2026-11", "pay_date": "2026-11-28"})
    rid = q(client, "SELECT id FROM payroll_runs")[0]["id"]
    assert client.get(f"/payroll/{rid}").status_code == 200
    client.post(f"/payroll/{rid}/post")
    for k in ("SAL", "EPF", "SOCSO", "EIS", "PCB"):
        client.post(f"/payroll/{rid}/pay/{k}")
    for acct in ("2200", "2210", "2220", "2230", "2240"):
        assert bal(client, acct) == 0, acct
    assert tb_balanced(client)
    foreign = q(client, "SELECT p.* FROM payslips p JOIN employees e ON e.id=p.employee_id WHERE e.residency='foreign'")[0]
    assert foreign["epf_ee"] == 48 and foreign["eis_ee"] == 0 and foreign["socso_ee"] == 0
    for kind in ("epf", "socso", "pcb", "bank"):
        assert client.get(f"/payroll/{rid}/export/{kind}.csv").status_code == 200
