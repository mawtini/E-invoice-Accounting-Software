"""Business workflows: e-Invoice lifecycle against LHDN, self-billing, consolidation."""
import json
from datetime import datetime, timedelta, timezone

from accounting import post_sales_document, reverse_entry, post_entry
from db import get_settings, connect
from lhdn.client import LHDNClient, LHDNError
from lhdn.codes import SELF_BILLED_TYPES
from lhdn.mock import MockLHDNClient
from lhdn.signer import load_p12, sign_document
from lhdn.ubl import build_invoice, company_as_party, compute_totals, submission_entry, validate_invoice

MYT = timezone(timedelta(hours=8))
_client_cache = {}


class ServiceError(Exception):
    pass


def today():
    return datetime.now(MYT).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- LHDN client
def make_logger(db_path):
    def log(mode, method, url, status, req, resp):
        conn = connect(db_path)
        conn.execute("INSERT INTO lhdn_log(mode,method,url,status,request,response) VALUES (?,?,?,?,?,?)",
                     (mode, method, url, status, json.dumps(req) if req is not None else None,
                      resp if isinstance(resp, str) else json.dumps(resp)))
        conn.commit()
        conn.close()
    return log


def get_client(db, db_path):
    s = get_settings(db)
    mode = s.get("lhdn_mode", "mock")
    logger = make_logger(db_path)
    if mode == "mock":
        return MockLHDNClient(db, logger)
    if not s.get("lhdn_client_id") or not s.get("lhdn_client_secret"):
        raise ServiceError("LHDN Client ID / Client Secret not configured (Settings)")
    key = (mode, s["lhdn_client_id"], s["lhdn_client_secret"], s.get("lhdn_onbehalfof"))
    client = _client_cache.get(key)
    if client is None:
        client = LHDNClient(mode, s["lhdn_client_id"], s["lhdn_client_secret"], s.get("lhdn_onbehalfof"))
        _client_cache[key] = client
    client.logger = logger          # token is reused; logger bound to current request
    return client


# ---------------------------------------------------------------- documents
def load_document(db, doc_id):
    doc = db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not doc:
        raise ServiceError("Document not found")
    lines = [dict(r) for r in db.execute("SELECT * FROM document_lines WHERE document_id=? ORDER BY line_no",
                                         (doc_id,))]
    party = dict(db.execute("SELECT * FROM parties WHERE id=?", (doc["party_id"],)).fetchone())
    ref = db.execute("SELECT * FROM documents WHERE id=?", (doc["ref_doc_id"],)).fetchone() if doc["ref_doc_id"] else None
    return dict(doc), lines, party, (dict(ref) if ref else None)


def save_document(db, data, lines, doc_id=None):
    totals = compute_totals(lines)
    cols = ["doc_type", "number", "party_id", "issue_date", "currency", "exchange_rate", "payment_mode",
            "payment_terms", "period_start", "period_end", "period_desc", "ref_doc_id", "notes", "expense_id",
            "is_consolidated"]
    vals = [data.get(c) for c in cols] + [totals["subtotal"], totals["tax_total"], totals["total"]]
    if doc_id:
        db.execute(f"UPDATE documents SET {', '.join(c + '=?' for c in cols)}, subtotal=?, tax_total=?, total=? "
                   f"WHERE id=?", vals + [doc_id])
        db.execute("DELETE FROM document_lines WHERE document_id=?", (doc_id,))
    else:
        cur = db.execute(f"INSERT INTO documents({', '.join(cols)}, subtotal, tax_total, total) "
                         f"VALUES ({', '.join('?' * (len(cols) + 3))})", vals)
        doc_id = cur.lastrowid
    for i, ln in enumerate(lines, 1):
        db.execute("INSERT INTO document_lines(document_id,line_no,classification,description,qty,unit_code,"
                   "unit_price,discount,tax_type,tax_rate,exemption_reason,account_code) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (doc_id, i, ln.get("classification") or "022", ln["description"], float(ln["qty"]),
                    ln.get("unit_code") or "C62", float(ln["unit_price"]), float(ln.get("discount") or 0),
                    ln.get("tax_type") or "06", float(ln.get("tax_rate") or 0), ln.get("exemption_reason"),
                    ln.get("account_code")))
    db.commit()
    return doc_id


def next_number(db, doc_type):
    s = get_settings(db)
    prefix = {"01": s["invoice_prefix"], "02": s["cn_prefix"], "03": s["dn_prefix"], "04": s["rn_prefix"]}.get(
        doc_type, s["sb_prefix"])
    n = db.execute("SELECT COUNT(*) FROM documents WHERE number LIKE ?", (prefix + "%",)).fetchone()[0] + 1
    while db.execute("SELECT 1 FROM documents WHERE number=?", (f"{prefix}{n:05d}",)).fetchone():
        n += 1
    return f"{prefix}{n:05d}"


def build_document_json(db, doc_id, issued_at=None):
    doc, lines, party, ref = load_document(db, doc_id)
    s = get_settings(db)
    version = s.get("lhdn_doc_version") or "1.0"
    j = build_invoice(doc, lines, company_as_party(s), party, version=version, ref_doc=ref, issued_at=issued_at,
                      bank_account=s.get("company_bank_account"))
    if version == "1.1":
        if not s.get("lhdn_cert_path"):
            raise ServiceError("Document version 1.1 requires a signing certificate (.p12) in Settings")
        key, cert = load_p12(s["lhdn_cert_path"], s.get("lhdn_cert_password"))
        j = sign_document(j, key, cert)
    return j


def submit_documents(db, db_path, doc_ids):
    """Build, validate locally, sign and submit a batch (max 100) to LHDN."""
    if len(doc_ids) > 100:
        raise ServiceError("LHDN accepts at most 100 documents per submission")
    client = get_client(db, db_path)
    entries, problems, by_number = [], {}, {}
    now = datetime.now(timezone.utc)
    for did in doc_ids:
        doc = db.execute("SELECT * FROM documents WHERE id=?", (did,)).fetchone()
        if doc["status"] not in ("draft", "invalid"):
            problems[doc["number"]] = [f"status is '{doc['status']}'"]
            continue
        if doc["ref_doc_id"]:
            ref = db.execute("SELECT status FROM documents WHERE id=?", (doc["ref_doc_id"],)).fetchone()
            if ref["status"] != "valid":
                problems[doc["number"]] = ["original document must be validated by LHDN first"]
                continue
        db.execute("UPDATE documents SET issue_date=? WHERE id=?", (now.astimezone(MYT).strftime("%Y-%m-%d"), did))
        j = build_document_json(db, did, issued_at=now)
        errs = validate_invoice(j)
        if errs:
            problems[doc["number"]] = errs
            db.execute("UPDATE documents SET lhdn_errors=? WHERE id=?", (json.dumps(errs), did))
            continue
        entries.append(submission_entry(j, doc["number"]))
        by_number[doc["number"]] = (did, j)
    db.commit()
    if not entries:
        return {"submitted": 0, "problems": problems}

    resp = client.submit_documents(entries)
    for acc in resp.get("acceptedDocuments", []):
        did, j = by_number[acc["invoiceCodeNumber"]]
        db.execute("UPDATE documents SET status='submitted', lhdn_uuid=?, lhdn_submission_uid=?, submitted_json=?, "
                   "lhdn_errors=NULL WHERE id=?", (acc["uuid"], resp["submissionUid"], json.dumps(j), did))
    for rej in resp.get("rejectedDocuments", []):
        did, _ = by_number[rej["invoiceCodeNumber"]]
        err = rej.get("error") or {}
        msgs = [err.get("message") or json.dumps(err)] + [d.get("message", "") for d in (err.get("details") or [])]
        problems[rej["invoiceCodeNumber"]] = msgs
        db.execute("UPDATE documents SET status='invalid', lhdn_errors=? WHERE id=?", (json.dumps(msgs), did))
    db.commit()
    return {"submitted": len(resp.get("acceptedDocuments", [])), "submission_uid": resp.get("submissionUid"),
            "problems": problems}


STATUS_MAP = {"Valid": "valid", "Invalid": "invalid", "Cancelled": "cancelled", "Rejected": "rejected",
              "Submitted": "submitted"}


def refresh_status(db, db_path, doc_id):
    doc, lines, party, ref = load_document(db, doc_id)
    if not doc["lhdn_submission_uid"]:
        raise ServiceError("Document has not been submitted")
    client = get_client(db, db_path)
    sub = client.get_submission(doc["lhdn_submission_uid"])
    summary = next((d for d in sub.get("documentSummary", []) if d["uuid"] == doc["lhdn_uuid"]), None)
    if not summary:
        return doc["status"]
    new_status = STATUS_MAP.get(summary["status"], doc["status"])
    errors = None
    if new_status == "invalid":
        details = client.get_document_details(doc["lhdn_uuid"])
        errors = []
        for step in (details.get("validationResults") or {}).get("validationSteps", []):
            if step.get("status") == "Invalid" and step.get("error"):
                inner = step["error"].get("innerError") or []
                errors += [f"{step['name']}: {e.get('error')}" for e in inner] or [f"{step['name']}: {step['error'].get('error')}"]
    db.execute("UPDATE documents SET status=?, lhdn_long_id=COALESCE(NULLIF(?,''),lhdn_long_id), "
               "lhdn_validated_at=COALESCE(lhdn_validated_at, ?), lhdn_errors=COALESCE(?, lhdn_errors) WHERE id=?",
               (new_status, summary.get("longId"), summary.get("dateTimeValidated") if new_status == "valid" else None,
                json.dumps(errors) if errors else None, doc_id))
    # ledger: post once validated; reverse when cancelled/rejected
    if new_status == "valid" and not doc["posted_entry_id"] and not doc["is_consolidated"]:
        eid = post_sales_document(db, doc, lines)
        db.execute("UPDATE documents SET posted_entry_id=? WHERE id=?", (eid, doc_id))
    if new_status in ("cancelled", "rejected") and doc["posted_entry_id"]:
        reverse_entry(db, doc["posted_entry_id"], today(), f"{doc['number']}-X", f"Reversal: {doc['number']} {new_status}")
        db.execute("UPDATE documents SET posted_entry_id=NULL WHERE id=?", (doc_id,))
    db.commit()
    return new_status


def cancel_document(db, db_path, doc_id, reason):
    doc = db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if doc["status"] != "valid":
        raise ServiceError("Only LHDN-validated documents can be cancelled")
    if not reason.strip():
        raise ServiceError("A cancellation reason is required")
    client = get_client(db, db_path)
    client.cancel_document(doc["lhdn_uuid"], reason)
    db.execute("UPDATE documents SET status='cancelled' WHERE id=?", (doc_id,))
    if doc["posted_entry_id"]:
        reverse_entry(db, doc["posted_entry_id"], today(), f"{doc['number']}-X", f"Cancelled: {reason}")
        db.execute("UPDATE documents SET posted_entry_id=NULL WHERE id=?", (doc_id,))
    if doc["is_consolidated"]:
        db.execute("UPDATE receipts SET consolidated_doc_id=NULL WHERE consolidated_doc_id=?", (doc_id,))
    db.commit()


def validation_url(db, db_path, doc):
    if doc["lhdn_uuid"] and doc["lhdn_long_id"]:
        return get_client(db, db_path).validation_url(doc["lhdn_uuid"], doc["lhdn_long_id"])
    return None


def mark_document_paid(db, doc_id):
    doc = db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if doc["status"] != "valid" or doc["paid"] or doc["doc_type"] != "01":
        raise ServiceError("Only unpaid, validated invoices can be marked paid")
    amount = round(doc["total"] * (doc["exchange_rate"] or 1), 2)
    post_entry(db, today(), f"RCPT-{doc['number']}", f"Payment received {doc['number']}",
               [("1000", amount, 0), ("1100", 0, amount)], "payment", doc_id)
    db.execute("UPDATE documents SET paid=1 WHERE id=?", (doc_id,))
    db.commit()


# ---------------------------------------------------------------- self-billing (expenses)
def create_self_billed(db, expense_id):
    exp = db.execute("SELECT * FROM expenses WHERE id=?", (expense_id,)).fetchone()
    if exp["self_billed_doc_id"]:
        raise ServiceError("Self-billed e-Invoice already created for this expense")
    if not exp["party_id"]:
        raise ServiceError("Expense needs a supplier to self-bill")
    doc_id = save_document(db, {
        "doc_type": "11", "number": next_number(db, "11"), "party_id": exp["party_id"], "issue_date": exp["date"],
        "currency": "MYR", "exchange_rate": 1, "payment_mode": "03" if exp["paid"] else None,
        "expense_id": expense_id, "notes": f"Self-billed for expense #{expense_id}", "is_consolidated": 0,
    }, [{"classification": exp["classification"] or "036", "description": exp["description"], "qty": 1,
         "unit_code": "C62", "unit_price": exp["amount"], "tax_type": exp["tax_type"] or "06",
         "tax_rate": exp["tax_rate"] or 0, "account_code": exp["account_code"]}])
    db.execute("UPDATE expenses SET self_billed_doc_id=?, einvoice_source='self_billed' WHERE id=?",
               (doc_id, expense_id))
    db.commit()
    return doc_id


def verify_supplier_einvoice(db, db_path, expense_id):
    exp = db.execute("SELECT * FROM expenses WHERE id=?", (expense_id,)).fetchone()
    if not exp["supplier_einvoice_uuid"]:
        raise ServiceError("Enter the supplier's e-Invoice UUID first")
    client = get_client(db, db_path)
    try:
        det = client.get_document_details(exp["supplier_einvoice_uuid"].strip())
        status = det.get("status") or (det.get("validationResults") or {}).get("status")
    except LHDNError as e:
        status = "Not found" if e.status == 404 else f"Error {e.status}"
    db.execute("UPDATE expenses SET supplier_einvoice_status=? WHERE id=?", (status, expense_id))
    db.commit()
    return status


def parse_received_document(raw):
    """Extract supplier + totals from a received UBL JSON document (for importing as an expense)."""
    doc = json.loads(raw) if isinstance(raw, str) else raw
    inv = doc["Invoice"][0]
    party = inv["AccountingSupplierParty"][0]["Party"][0]
    ids = {i["ID"][0].get("schemeID"): i["ID"][0]["_"] for i in party["PartyIdentification"]}
    lmt = inv["LegalMonetaryTotal"][0]
    return {
        "supplier_name": party["PartyLegalEntity"][0]["RegistrationName"][0]["_"], "tin": ids.get("TIN"),
        "id_type": next((k for k in ("BRN", "NRIC", "PASSPORT", "ARMY") if k in ids), "BRN"),
        "id_value": next((ids[k] for k in ("BRN", "NRIC", "PASSPORT", "ARMY") if k in ids), "NA"),
        "number": inv["ID"][0]["_"], "date": inv["IssueDate"][0]["_"],
        "amount": float(lmt["TaxExclusiveAmount"][0]["_"]), "tax": float(inv["TaxTotal"][0]["TaxAmount"][0]["_"]),
        "description": inv["InvoiceLine"][0]["Item"][0]["Description"][0]["_"],
    }


# ---------------------------------------------------------------- consolidated B2C e-Invoice
def create_consolidated(db, month):
    """Aggregate a month's unconsolidated receipts into one e-Invoice (must be issued within 7 days after month end)."""
    rows = db.execute("SELECT * FROM receipts WHERE substr(date,1,7)=? AND consolidated_doc_id IS NULL ORDER BY date",
                      (month,)).fetchall()
    if not rows:
        raise ServiceError(f"No unconsolidated receipts for {month}")
    gp = db.execute("SELECT id FROM parties WHERE tin='EI00000000010'").fetchone()
    y, m = map(int, month.split("-"))
    last = (datetime(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)).strftime("%Y-%m-%d")
    lines = [{"classification": "004", "description": f"Receipt {r['number']}", "qty": 1, "unit_code": "C62",
              "unit_price": r["amount"], "tax_type": r["tax_type"], "tax_rate": r["tax_rate"],
              "account_code": r["account_code"]} for r in rows]
    doc_id = save_document(db, {
        "doc_type": "01", "number": next_number(db, "01"), "party_id": gp["id"], "issue_date": today(),
        "currency": "MYR", "exchange_rate": 1, "payment_mode": "01", "period_start": f"{month}-01",
        "period_end": last, "period_desc": "Monthly", "is_consolidated": 1,
        "notes": f"Consolidated e-Invoice for {month} ({len(rows)} receipts)"}, lines)
    db.executemany("UPDATE receipts SET consolidated_doc_id=? WHERE id=?", [(doc_id, r["id"]) for r in rows])
    db.commit()
    return doc_id
