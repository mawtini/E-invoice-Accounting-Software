"""Offline simulator of the MyInvois API so the prototype works without credentials.

It mirrors the response shapes of the real API, runs the local structural
validator (and signature verification for v1.1) and stores state in SQLite.
Documents become 'Valid' after the first status poll, like the asynchronous
validation of the real platform.
"""
import base64
import hashlib
import json
import secrets
import string
from datetime import datetime, timedelta, timezone

from .client import LHDNError
from .signer import verify_document
from .ubl import TIN_RE, validate_invoice, _val

ALPHABET = string.ascii_uppercase + string.digits


def _rid(n):
    return "".join(secrets.choice(ALPHABET) for _ in range(n))


class MockLHDNClient:
    env = "mock"
    portal = "https://preprod.myinvois.hasil.gov.my"

    def __init__(self, conn, logger=None):
        self.db = conn
        self.logger = logger

    def _log(self, method, path, status, req, resp):
        if self.logger:
            self.logger("mock", method, f"mock://{path}", status, req, json.dumps(resp)[:5000])

    def validate_tin(self, tin, id_type, id_value):
        ok = bool(TIN_RE.match(tin or "")) and bool(id_value) and id_value != ""
        self._log("GET", f"/api/v1.0/taxpayer/validate/{tin}", 200 if ok else 404,
                  {"idType": id_type, "idValue": id_value}, {})
        return ok

    def search_tin(self, id_type=None, id_value=None, taxpayer_name=None):
        return {"tin": "C" + str(abs(hash((id_value, taxpayer_name))) % 10 ** 10).zfill(10)}

    def submit_documents(self, documents):
        submission_uid = _rid(26)
        accepted, rejected = [], []
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for d in documents:
            raw = base64.b64decode(d["document"])
            if hashlib.sha256(raw).hexdigest() != d["documentHash"]:
                rejected.append({"invoiceCodeNumber": d["codeNumber"], "error": {
                    "code": "BadArgument", "message": "documentHash does not match the document"}})
                continue
            doc = json.loads(raw)
            if self.db.execute("SELECT 1 FROM mock_lhdn_docs WHERE internal_id=? AND status IN ('Valid','Submitted')",
                               (d["codeNumber"],)).fetchone():
                rejected.append({"invoiceCodeNumber": d["codeNumber"], "error": {
                    "code": "DuplicateSubmission", "message": "Document with the same internal ID already exists"}})
                continue
            errors = validate_invoice(doc)
            if doc["Invoice"][0]["InvoiceTypeCode"][0].get("listVersionID") == "1.1" and not errors:
                errors += verify_document(doc)
            uuid = _rid(26)
            inv = doc["Invoice"][0]
            sup = {i["ID"][0]["schemeID"]: i["ID"][0]["_"] for i in
                   inv["AccountingSupplierParty"][0]["Party"][0]["PartyIdentification"]}
            buy = {i["ID"][0]["schemeID"]: i["ID"][0]["_"] for i in
                   inv["AccountingCustomerParty"][0]["Party"][0]["PartyIdentification"]}
            self.db.execute(
                "INSERT INTO mock_lhdn_docs(uuid,submission_uid,long_id,internal_id,type_code,status,errors,raw,"
                "issuer_tin,receiver_tin,total,received_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (uuid, submission_uid, _rid(40), d["codeNumber"], _val(inv, "InvoiceTypeCode"),
                 "Invalid" if errors else "Submitted", json.dumps(errors), raw.decode(), sup.get("TIN"),
                 buy.get("TIN"), float(_val(inv["LegalMonetaryTotal"][0], "PayableAmount")), now))
            accepted.append({"uuid": uuid, "invoiceCodeNumber": d["codeNumber"]})
        self.db.commit()
        resp = {"submissionUid": submission_uid, "acceptedDocuments": accepted, "rejectedDocuments": rejected}
        self._log("POST", "/api/v1.0/documentsubmissions", 202, {"count": len(documents)}, resp)
        return resp

    def _row_summary(self, r):
        return {"uuid": r["uuid"], "submissionUid": r["submission_uid"], "longId":
                r["long_id"] if r["status"] == "Valid" else "", "internalId": r["internal_id"],
                "typeName": r["type_code"], "status": r["status"], "issuerTin": r["issuer_tin"],
                "receiverId": r["receiver_tin"], "totalPayableAmount": r["total"],
                "dateTimeReceived": r["received_at"], "dateTimeValidated": r["received_at"]}

    def get_submission(self, submission_uid):
        # simulate asynchronous validation finishing on first poll
        self.db.execute("UPDATE mock_lhdn_docs SET status='Valid' WHERE submission_uid=? AND status='Submitted'",
                        (submission_uid,))
        self.db.commit()
        rows = self.db.execute("SELECT * FROM mock_lhdn_docs WHERE submission_uid=?", (submission_uid,)).fetchall()
        if not rows:
            raise LHDNError("Submission not found", 404)
        statuses = {r["status"] for r in rows}
        overall = "Valid" if statuses <= {"Valid", "Cancelled"} else "Invalid" if statuses == {"Invalid"} \
            else "Partially Valid" if "Valid" in statuses else "In Progress"
        resp = {"submissionUid": submission_uid, "documentCount": len(rows), "overallStatus": overall,
                "dateTimeReceived": rows[0]["received_at"], "documentSummary": [self._row_summary(r) for r in rows]}
        self._log("GET", f"/api/v1.0/documentsubmissions/{submission_uid}", 200, None, resp)
        return resp

    def _get(self, uuid):
        r = self.db.execute("SELECT * FROM mock_lhdn_docs WHERE uuid=?", (uuid,)).fetchone()
        if not r:
            raise LHDNError("Document not found", 404)
        return r

    def get_document(self, uuid):
        r = self._get(uuid)
        return {**self._row_summary(r), "document": r["raw"]}

    def get_document_details(self, uuid):
        r = self._get(uuid)
        errs = json.loads(r["errors"] or "[]")
        steps = [{"status": "Invalid" if errs else "Valid", "name": "Step03-Structure Validator",
                  "error": {"error": "; ".join(errs), "innerError": [{"error": e} for e in errs]} if errs else None}]
        resp = {**self._row_summary(r), "validationResults": {"status": r["status"], "validationSteps": steps}}
        self._log("GET", f"/api/v1.0/documents/{uuid}/details", 200, None, resp)
        return resp

    def _change_state(self, uuid, status, reason):
        r = self._get(uuid)
        if r["status"] != "Valid":
            raise LHDNError("Only valid documents can change state", 400)
        received = datetime.strptime(r["received_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - received > timedelta(hours=72):
            raise LHDNError("Document can only be cancelled / rejected within 72 hours of validation", 400)
        self.db.execute("UPDATE mock_lhdn_docs SET status=?, cancel_reason=? WHERE uuid=?",
                        ("Cancelled" if status == "cancelled" else "Rejected", reason, uuid))
        self.db.commit()
        resp = {"uuid": uuid, "status": "Cancelled" if status == "cancelled" else "Rejected"}
        self._log("PUT", f"/api/v1.0/documents/state/{uuid}/state", 200, {"status": status, "reason": reason}, resp)
        return resp

    def cancel_document(self, uuid, reason):
        return self._change_state(uuid, "cancelled", reason)

    def reject_document(self, uuid, reason):
        return self._change_state(uuid, "rejected", reason)

    def recent_documents(self, direction="Received", page=1, page_size=50, own_tin=None):
        col = "receiver_tin" if direction == "Received" else "issuer_tin"
        rows = self.db.execute(f"SELECT * FROM mock_lhdn_docs WHERE {col}=? ORDER BY received_at DESC LIMIT ?",
                               (own_tin, page_size)).fetchall() if own_tin else []
        return {"result": [self._row_summary(r) for r in rows],
                "metadata": {"totalCount": len(rows), "totalPages": 1}}

    def search_documents(self, **params):
        return self.recent_documents()

    def validation_url(self, uuid, long_id):
        return f"{self.portal}/{uuid}/share/{long_id}"

    # --- helper only the simulator has: inject an e-Invoice "issued by a supplier to us"
    def inject_received(self, raw_doc: dict, issuer_tin, receiver_tin, internal_id, total):
        uuid = _rid(26)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.db.execute(
            "INSERT INTO mock_lhdn_docs(uuid,submission_uid,long_id,internal_id,type_code,status,errors,raw,"
            "issuer_tin,receiver_tin,total,received_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (uuid, _rid(26), _rid(40), internal_id, "01", "Valid", "[]", json.dumps(raw_doc), issuer_tin,
             receiver_tin, total, now))
        self.db.commit()
        return uuid
