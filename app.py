"""e-Invois Accounting prototype - Flask application.

Run:  python app.py      (then open http://127.0.0.1:5000)
"""
import csv
import io
import json
import os

from flask import (Flask, Response, abort, flash, redirect, render_template, request, url_for)
from markupsafe import Markup

import accounting
import payroll
import services
from db import close_db, get_db, get_settings, init_db, save_settings
from lhdn import codes
from lhdn.client import LHDNError
from lhdn.mock import MockLHDNClient
from lhdn.ubl import build_invoice, company_as_party, compute_line
from services import ServiceError, today

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def create_app(db_path=None):
    app = Flask(__name__)
    app.config["DATABASE"] = db_path or os.environ.get("EINVOIS_DB", os.path.join(BASE_DIR, "data", "einvois.db"))
    app.config["SECRET_KEY"] = os.environ.get("EINVOIS_SECRET", "dev-only-change-me")
    app.config["UPLOAD_DIR"] = os.path.join(os.path.dirname(app.config["DATABASE"]), "certs")
    init_db(app.config["DATABASE"])
    app.teardown_appcontext(close_db)

    @app.context_processor
    def inject():
        return {"codes": codes, "settings": get_settings(get_db()), "today": today()}

    @app.template_filter("kv")
    def kv(d):
        return [(k, f"{k} - {v}") for k, v in d.items()]

    @app.template_filter("zip_self")
    def zip_self(seq):
        return [(x, x) for x in seq]

    @app.template_filter("money")
    def money(v):
        return f"{float(v or 0):,.2f}"

    @app.errorhandler(ServiceError)
    def service_error(e):
        flash(str(e), "error")
        return redirect(request.referrer or url_for("dashboard"))

    @app.errorhandler(LHDNError)
    def lhdn_error(e):
        flash(f"LHDN: {e}", "error")
        return redirect(request.referrer or url_for("dashboard"))

    register_routes(app)
    return app


def fval(name, default=""):
    return (request.form.get(name, default) or default).strip()


def fnum(name, default=0.0):
    try:
        return float(request.form.get(name) or default)
    except ValueError:
        return default


def register_routes(app):
    db_path = lambda: app.config["DATABASE"]  # noqa: E731

    # ================================================================ dashboard
    @app.route("/")
    def dashboard():
        db = get_db()
        month = today()[:7]
        stats = {
            "sales_month": db.execute("SELECT COALESCE(SUM(total*exchange_rate),0) FROM documents WHERE doc_type='01' "
                                      "AND status='valid' AND substr(issue_date,1,7)=?", (month,)).fetchone()[0],
            "expenses_month": db.execute("SELECT COALESCE(SUM(total),0) FROM expenses WHERE substr(date,1,7)=?",
                                         (month,)).fetchone()[0],
            "receivable": db.execute("SELECT COALESCE(SUM(debit-credit),0) FROM journal_lines WHERE account_code='1100'").fetchone()[0],
            "payroll_month": db.execute("SELECT COALESCE(SUM(gross),0) FROM payslips p JOIN payroll_runs r ON r.id=p.run_id "
                                        "WHERE r.period=?", (month,)).fetchone()[0],
        }
        by_status = db.execute("SELECT status, COUNT(*) n FROM documents GROUP BY status").fetchall()
        recent = db.execute("SELECT d.*, p.name party FROM documents d JOIN parties p ON p.id=d.party_id "
                            "ORDER BY d.id DESC LIMIT 8").fetchall()
        return render_template("dashboard.html", stats=stats, by_status=by_status, recent=recent, month=month)

    # ================================================================ parties
    @app.route("/parties")
    def parties():
        rows = get_db().execute("SELECT * FROM parties ORDER BY name").fetchall()
        return render_template("parties.html", rows=rows)

    PARTY_FIELDS = ["kind", "name", "tin", "id_type", "id_value", "sst_no", "ttx_no", "msic", "activity", "email",
                    "phone", "addr1", "addr2", "addr3", "city", "postcode", "state_code", "country_code"]

    @app.route("/parties/new", methods=["GET", "POST"])
    @app.route("/parties/<int:pid>/edit", methods=["GET", "POST"])
    def party_form(pid=None):
        db = get_db()
        row = db.execute("SELECT * FROM parties WHERE id=?", (pid,)).fetchone() if pid else None
        if request.method == "POST":
            vals = {k: fval(k) for k in PARTY_FIELDS}
            vals["tin"] = vals["tin"].upper()
            for k in ("id_value", "sst_no", "ttx_no", "phone", "addr1", "city"):
                vals[k] = vals[k] or "NA"
            if not vals["name"] or not vals["tin"]:
                flash("Name and TIN are required", "error")
                return render_template("party_form.html", row=vals)
            if pid:
                db.execute(f"UPDATE parties SET {', '.join(k + '=?' for k in PARTY_FIELDS)}, tin_verified=0 WHERE id=?",
                           list(vals.values()) + [pid])
            else:
                pid = db.execute(f"INSERT INTO parties({', '.join(PARTY_FIELDS)}) VALUES ({', '.join('?' * len(PARTY_FIELDS))})",
                                 list(vals.values())).lastrowid
            db.commit()
            flash("Saved", "ok")
            return redirect(request.args.get("next") or url_for("parties"))
        return render_template("party_form.html", row=row)

    @app.post("/parties/<int:pid>/validate")
    def party_validate(pid):
        db = get_db()
        p = db.execute("SELECT * FROM parties WHERE id=?", (pid,)).fetchone()
        if p["tin"] in codes.GENERIC_TIN:
            flash(f"{p['tin']} is an LHDN generic TIN ({codes.GENERIC_TIN[p['tin']]}) - no validation needed", "ok")
            return redirect(url_for("parties"))
        ok = services.get_client(db, db_path()).validate_tin(p["tin"], p["id_type"], p["id_value"])
        db.execute("UPDATE parties SET tin_verified=? WHERE id=?", (1 if ok else -1, pid))
        db.commit()
        flash(f"TIN {p['tin']} {'is VALID' if ok else 'could NOT be validated'} with LHDN", "ok" if ok else "error")
        return redirect(url_for("parties"))

    # ================================================================ e-invoice documents
    @app.route("/documents")
    def documents():
        db = get_db()
        kind = request.args.get("kind", "sales")
        types = tuple(codes.SELF_BILLED_TYPES) if kind == "self" else ("01", "02", "03", "04")
        rows = db.execute(f"SELECT d.*, p.name party FROM documents d JOIN parties p ON p.id=d.party_id "
                          f"WHERE d.doc_type IN ({','.join('?' * len(types))}) ORDER BY d.id DESC", types).fetchall()
        return render_template("documents.html", rows=rows, kind=kind)

    def _doc_form_lines():
        f = request.form
        lines = []
        for i, desc in enumerate(f.getlist("line_description")):
            if not desc.strip():
                continue
            g = lambda k, d="": (f.getlist(k)[i] if i < len(f.getlist(k)) else d) or d  # noqa: E731
            lines.append({"description": desc.strip(), "classification": g("line_classification", "022"),
                          "qty": float(g("line_qty", "1")), "unit_code": g("line_unit_code", "C62"),
                          "unit_price": float(g("line_unit_price", "0")), "discount": float(g("line_discount", "0")),
                          "tax_type": g("line_tax_type", "06"), "tax_rate": float(g("line_tax_rate", "0")),
                          "exemption_reason": g("line_exemption_reason") or None,
                          "account_code": g("line_account_code") or None})
        return lines

    @app.route("/documents/new", methods=["GET", "POST"])
    @app.route("/documents/<int:did>/edit", methods=["GET", "POST"])
    def document_form(did=None):
        db = get_db()
        doc = lines = None
        if did:
            doc, lines, _p, _r = services.load_document(db, did)
            if doc["status"] not in ("draft", "invalid"):
                flash("Only draft or invalid documents can be edited", "error")
                return redirect(url_for("document_view", did=did))
        doc_type = (doc or {}).get("doc_type") or request.args.get("type", "01")
        ref_id = (doc or {}).get("ref_doc_id") or request.args.get("ref", type=int)
        ref = db.execute("SELECT * FROM documents WHERE id=?", (ref_id,)).fetchone() if ref_id else None

        if request.method == "POST":
            data = {k: fval(k) or None for k in ("doc_type", "number", "issue_date", "currency", "payment_mode",
                                                 "payment_terms", "period_start", "period_end", "period_desc", "notes")}
            data["party_id"] = int(fval("party_id"))
            data["exchange_rate"] = fnum("exchange_rate", 1) if data["currency"] != "MYR" else 1
            data["ref_doc_id"] = int(fval("ref_doc_id")) if fval("ref_doc_id") else None
            data["expense_id"] = (doc or {}).get("expense_id")
            data["is_consolidated"] = (doc or {}).get("is_consolidated", 0)
            new_lines = _doc_form_lines()
            if not new_lines:
                flash("At least one line item is required", "error")
            elif data["doc_type"] in codes.ADJUSTMENT_TYPES and not data["ref_doc_id"]:
                flash("Credit / debit / refund notes must reference an original document", "error")
            else:
                try:
                    did = services.save_document(db, data, new_lines, did)
                except Exception as e:  # e.g. duplicate number
                    flash(f"Could not save: {e}", "error")
                else:
                    if did and doc and doc["status"] == "invalid":
                        db.execute("UPDATE documents SET status='draft' WHERE id=?", (did,))
                        db.commit()
                    flash("Document saved", "ok")
                    return redirect(url_for("document_view", did=did))
            doc = {**(doc or {}), **data}
            lines = new_lines

        if not doc:
            doc = {"doc_type": doc_type, "number": services.next_number(db, doc_type), "issue_date": today(),
                   "currency": "MYR", "exchange_rate": 1, "payment_mode": "03", "ref_doc_id": ref_id,
                   "party_id": ref["party_id"] if ref else None}
            if ref:   # prefill CN/DN lines from the original
                lines = [dict(r) for r in db.execute("SELECT * FROM document_lines WHERE document_id=?", (ref_id,))]
        self_billed = doc_type in codes.SELF_BILLED_TYPES
        party_kinds = ("supplier", "both") if self_billed else ("customer", "both")
        parties_ = db.execute("SELECT * FROM parties WHERE kind IN (?,?) ORDER BY name", party_kinds).fetchall()
        refs = db.execute("SELECT id, number, doc_type FROM documents WHERE status='valid' AND doc_type IN (?, ?) "
                          "ORDER BY id DESC", ("11", "11") if self_billed else ("01", "03")).fetchall()
        accounts = db.execute("SELECT * FROM accounts WHERE type IN ('income','expense') ORDER BY code").fetchall()
        return render_template("document_form.html", doc=doc, lines=lines or [{}], parties=parties_, refs=refs,
                               accounts=accounts, self_billed=self_billed)

    @app.route("/documents/<int:did>")
    def document_view(did):
        db = get_db()
        doc, lines, party, ref = services.load_document(db, did)
        for ln in lines:
            ln.update(compute_line(ln))
        children = db.execute("SELECT * FROM documents WHERE ref_doc_id=?", (did,)).fetchall()
        receipts = db.execute("SELECT * FROM receipts WHERE consolidated_doc_id=?", (did,)).fetchall()
        return render_template("document_view.html", doc=doc, lines=lines, party=party, ref=ref, children=children,
                               receipts=receipts, errors=json.loads(doc["lhdn_errors"] or "[]"),
                               vurl=services.validation_url(db, db_path(), doc))

    @app.post("/documents/<int:did>/submit")
    def document_submit(did):
        res = services.submit_documents(get_db(), db_path(), [did])
        _flash_submission(res)
        return redirect(url_for("document_view", did=did))

    @app.post("/documents/submit-batch")
    def document_submit_batch():
        ids = [int(i) for i in request.form.getlist("ids")]
        if not ids:
            flash("Select at least one draft document", "error")
        else:
            _flash_submission(services.submit_documents(get_db(), db_path(), ids))
        return redirect(request.referrer or url_for("documents"))

    def _flash_submission(res):
        if res.get("submitted"):
            flash(f"{res['submitted']} document(s) accepted by LHDN (submission {res['submission_uid']}). "
                  f"Refresh status to see validation result.", "ok")
        for num, errs in res.get("problems", {}).items():
            flash(Markup(f"<b>{num}</b> not submitted:<br>") + Markup("<br>").join(errs), "error")

    @app.post("/documents/<int:did>/refresh")
    def document_refresh(did):
        st = services.refresh_status(get_db(), db_path(), did)
        flash(f"LHDN status: {st}", "ok" if st == "valid" else "error" if st == "invalid" else "info")
        return redirect(url_for("document_view", did=did))

    @app.post("/documents/<int:did>/cancel")
    def document_cancel(did):
        services.cancel_document(get_db(), db_path(), did, fval("reason"))
        flash("Document cancelled on MyInvois and reversed in the ledger", "ok")
        return redirect(url_for("document_view", did=did))

    @app.post("/documents/<int:did>/paid")
    def document_paid(did):
        services.mark_document_paid(get_db(), did)
        flash("Payment recorded", "ok")
        return redirect(url_for("document_view", did=did))

    @app.post("/documents/<int:did>/delete")
    def document_delete(did):
        db = get_db()
        doc = db.execute("SELECT * FROM documents WHERE id=?", (did,)).fetchone()
        if doc["status"] not in ("draft", "invalid"):
            raise ServiceError("Only draft/invalid documents can be deleted")
        db.execute("UPDATE expenses SET self_billed_doc_id=NULL WHERE self_billed_doc_id=?", (did,))
        db.execute("UPDATE receipts SET consolidated_doc_id=NULL WHERE consolidated_doc_id=?", (did,))
        db.execute("DELETE FROM documents WHERE id=?", (did,))
        db.commit()
        flash("Deleted", "ok")
        return redirect(url_for("documents"))

    @app.route("/documents/<int:did>/json")
    def document_json(did):
        db = get_db()
        doc = db.execute("SELECT * FROM documents WHERE id=?", (did,)).fetchone()
        if doc["submitted_json"]:
            j = json.loads(doc["submitted_json"])
        else:
            j = services.build_document_json(db, did)
        return Response(json.dumps(j, indent=2, ensure_ascii=False), mimetype="application/json")

    @app.route("/documents/<int:did>/print")
    def document_print(did):
        db = get_db()
        doc, lines, party, ref = services.load_document(db, did)
        for ln in lines:
            ln.update(compute_line(ln))
        vurl = services.validation_url(db, db_path(), doc)
        qr_svg = None
        if vurl:
            import qrcode
            import qrcode.image.svg
            qr_svg = Markup(qrcode.make(vurl, image_factory=qrcode.image.svg.SvgPathImage, box_size=8)
                            .to_string(encoding="unicode"))
        s = get_settings(db)
        company = company_as_party(s)
        supplier, buyer = (party, company) if doc["doc_type"] in codes.SELF_BILLED_TYPES else (company, party)
        return render_template("document_print.html", doc=doc, lines=lines, supplier=supplier, buyer=buyer,
                               ref=ref, vurl=vurl, qr_svg=qr_svg)

    # ================================================================ B2C receipts / consolidation
    @app.route("/receipts", methods=["GET", "POST"])
    def receipts():
        db = get_db()
        if request.method == "POST":
            amount, rate = fnum("amount"), fnum("tax_rate")
            tax = round(amount * rate / 100, 2) if fval("tax_type") not in ("06", "E") else 0
            cur = db.execute("INSERT INTO receipts(date,number,description,amount,tax_type,tax_rate,payment_mode,account_code) "
                             "VALUES (?,?,?,?,?,?,?,?)", (fval("date"), fval("number"), fval("description"), amount,
                                                          fval("tax_type", "06"), rate, fval("payment_mode", "01"),
                                                          fval("account_code", "4000")))
            eid = accounting.post_entry(db, fval("date"), fval("number"), f"Cash sale {fval('number')}",
                                        [("1000", amount + tax, 0), (fval("account_code", "4000"), 0, amount),
                                         ("2100", 0, tax)], "receipt", cur.lastrowid)
            db.execute("UPDATE receipts SET posted_entry_id=? WHERE id=?", (eid, cur.lastrowid))
            db.commit()
            flash("Receipt recorded", "ok")
            return redirect(url_for("receipts"))
        rows = db.execute("SELECT r.*, d.number cons_number, d.status cons_status FROM receipts r "
                          "LEFT JOIN documents d ON d.id=r.consolidated_doc_id ORDER BY r.date DESC").fetchall()
        n = db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] + 1
        accounts = db.execute("SELECT * FROM accounts WHERE type='income'").fetchall()
        return render_template("receipts.html", rows=rows, next_no=f"RCT-{n:05d}", accounts=accounts)

    @app.post("/receipts/consolidate")
    def receipts_consolidate():
        did = services.create_consolidated(get_db(), fval("month"))
        flash("Consolidated e-Invoice drafted. Review and submit within 7 days after month end.", "ok")
        return redirect(url_for("document_view", did=did))

    # ================================================================ expenses
    @app.route("/expenses")
    def expenses():
        rows = get_db().execute("SELECT e.*, p.name party, d.number sb_number, d.status sb_status FROM expenses e "
                                "LEFT JOIN parties p ON p.id=e.party_id LEFT JOIN documents d ON d.id=e.self_billed_doc_id "
                                "ORDER BY e.date DESC, e.id DESC").fetchall()
        return render_template("expenses.html", rows=rows)

    @app.route("/expenses/new", methods=["GET", "POST"])
    @app.route("/expenses/<int:eid>/edit", methods=["GET", "POST"])
    def expense_form(eid=None):
        db = get_db()
        row = dict(db.execute("SELECT * FROM expenses WHERE id=?", (eid,)).fetchone()) if eid else None
        if request.method == "POST":
            amount, rate, tax_type = fnum("amount"), fnum("tax_rate"), fval("tax_type", "06")
            tax = fnum("tax") if fval("tax") else (round(amount * rate / 100, 2) if tax_type not in ("06", "E") else 0)
            vals = {"date": fval("date"), "party_id": int(fval("party_id")) if fval("party_id") else None,
                    "account_code": fval("account_code"), "description": fval("description"), "amount": amount,
                    "tax": tax, "total": round(amount + tax, 2), "tax_type": tax_type, "tax_rate": rate,
                    "classification": fval("classification", "022"), "paid": 1 if fval("paid") else 0,
                    "einvoice_source": fval("einvoice_source", "supplier"),
                    "supplier_einvoice_uuid": fval("supplier_einvoice_uuid") or None}
            if row and row["posted_entry_id"]:
                accounting.reverse_entry(db, row["posted_entry_id"], today(), f"EXP-{eid}-X", "Expense amended")
            if eid:
                db.execute(f"UPDATE expenses SET {', '.join(k + '=?' for k in vals)} WHERE id=?", list(vals.values()) + [eid])
            else:
                eid = db.execute(f"INSERT INTO expenses({', '.join(vals)}) VALUES ({', '.join('?' * len(vals))})",
                                 list(vals.values())).lastrowid
            exp = db.execute("SELECT * FROM expenses WHERE id=?", (eid,)).fetchone()
            db.execute("UPDATE expenses SET posted_entry_id=? WHERE id=?", (accounting.post_expense(db, exp), eid))
            db.commit()
            flash("Expense saved and posted", "ok")
            if vals["einvoice_source"] == "self_billed" and not exp["self_billed_doc_id"]:
                did = services.create_self_billed(db, eid)
                flash("Self-billed e-Invoice drafted - review and submit it to LHDN", "ok")
                return redirect(url_for("document_view", did=did))
            return redirect(url_for("expenses"))
        if not row:
            row = {"date": today(), "account_code": "6000", "tax_type": "06", "paid": 1, "einvoice_source": "supplier",
                   "classification": "022"}
            for k in ("description", "amount", "tax", "supplier_einvoice_uuid", "party_id", "date"):  # import prefill
                if request.args.get(k):
                    row[k] = request.args[k]
            if row.get("party_id"):
                row["party_id"] = int(row["party_id"])
        parties_ = db.execute("SELECT * FROM parties WHERE kind IN ('supplier','both') ORDER BY name").fetchall()
        accounts = db.execute("SELECT * FROM accounts WHERE type='expense' AND code NOT IN ('6100','6110','6120','6130') "
                              "ORDER BY code").fetchall()
        return render_template("expense_form.html", row=row, parties=parties_, accounts=accounts)

    @app.post("/expenses/<int:eid>/self-bill")
    def expense_self_bill(eid):
        did = services.create_self_billed(get_db(), eid)
        flash("Self-billed e-Invoice drafted", "ok")
        return redirect(url_for("document_view", did=did))

    @app.post("/expenses/<int:eid>/verify")
    def expense_verify(eid):
        st = services.verify_supplier_einvoice(get_db(), db_path(), eid)
        flash(f"Supplier e-Invoice status on MyInvois: {st}", "ok" if st == "Valid" else "error")
        return redirect(url_for("expenses"))

    @app.post("/expenses/<int:eid>/paid")
    def expense_paid(eid):
        db = get_db()
        exp = db.execute("SELECT * FROM expenses WHERE id=?", (eid,)).fetchone()
        if not exp["paid"]:
            accounting.post_entry(db, today(), f"PMT-EXP-{eid}", f"Payment: {exp['description']}",
                                  [("2000", exp["total"], 0), ("1000", 0, exp["total"])], "payment", eid)
            db.execute("UPDATE expenses SET paid=1 WHERE id=?", (eid,))
            db.commit()
        return redirect(url_for("expenses"))

    # ================================================================ employees
    EMP_FIELDS = ["emp_no", "name", "nric", "dob", "residency", "epf_no", "socso_no", "tax_no", "basic_salary",
                  "fixed_allowance", "pcb_monthly", "bank_name", "bank_account"]

    @app.route("/employees")
    def employees():
        rows = get_db().execute("SELECT * FROM employees ORDER BY emp_no").fetchall()
        return render_template("employees.html", rows=rows)

    @app.route("/employees/new", methods=["GET", "POST"])
    @app.route("/employees/<int:eid>/edit", methods=["GET", "POST"])
    def employee_form(eid=None):
        db = get_db()
        row = db.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone() if eid else None
        if request.method == "POST":
            vals = [fnum(k) if k in ("basic_salary", "fixed_allowance", "pcb_monthly") else fval(k) or None
                    for k in EMP_FIELDS]
            active = 1 if fval("active") else 0
            try:
                if eid:
                    db.execute(f"UPDATE employees SET {', '.join(k + '=?' for k in EMP_FIELDS)}, active=? WHERE id=?",
                               vals + [active, eid])
                else:
                    db.execute(f"INSERT INTO employees({', '.join(EMP_FIELDS)}, active) VALUES "
                               f"({', '.join('?' * len(EMP_FIELDS))}, ?)", vals + [active])
                db.commit()
            except Exception as e:
                flash(f"Could not save: {e}", "error")
                return render_template("employee_form.html", row=dict(zip(EMP_FIELDS, vals)))
            flash("Employee saved", "ok")
            return redirect(url_for("employees"))
        return render_template("employee_form.html", row=row or {"residency": "citizen", "active": 1})

    # ================================================================ payroll
    @app.route("/payroll", methods=["GET", "POST"])
    def payroll_runs():
        db = get_db()
        if request.method == "POST":
            period, pay_date = fval("period"), fval("pay_date")
            if db.execute("SELECT 1 FROM payroll_runs WHERE period=?", (period,)).fetchone():
                raise ServiceError(f"Payroll for {period} already exists")
            emps = db.execute("SELECT * FROM employees WHERE active=1").fetchall()
            if not emps:
                raise ServiceError("No active employees")
            rid = db.execute("INSERT INTO payroll_runs(period,pay_date) VALUES (?,?)", (period, pay_date)).lastrowid
            for e in emps:
                _save_slip(db, rid, payroll.compute_payslip(e, period))
            db.commit()
            return redirect(url_for("payroll_run", rid=rid))
        runs = db.execute("SELECT r.*, COUNT(p.id) n, SUM(p.gross) gross, SUM(p.net) net FROM payroll_runs r "
                          "LEFT JOIN payslips p ON p.run_id=r.id GROUP BY r.id ORDER BY r.period DESC").fetchall()
        return render_template("payroll.html", runs=runs, default_period=today()[:7])

    SLIP_COLS = ["employee_id", "basic", "allowance", "overtime", "bonus", "other_deduction", "gross", "epf_ee",
                 "epf_er", "socso_ee", "socso_er", "eis_ee", "eis_er", "pcb", "net"]

    def _save_slip(db, rid, s, slip_id=None):
        if slip_id:
            db.execute(f"UPDATE payslips SET {', '.join(c + '=?' for c in SLIP_COLS)} WHERE id=?",
                       [s[c] for c in SLIP_COLS] + [slip_id])
        else:
            db.execute(f"INSERT INTO payslips(run_id, {', '.join(SLIP_COLS)}) VALUES (?, {', '.join('?' * len(SLIP_COLS))})",
                       [rid] + [s[c] for c in SLIP_COLS])

    def _run_slips(db, rid):
        return db.execute("SELECT p.*, e.emp_no, e.name, e.nric, e.epf_no, e.socso_no, e.tax_no, e.residency "
                          "FROM payslips p JOIN employees e ON e.id=p.employee_id WHERE run_id=? ORDER BY e.emp_no",
                          (rid,)).fetchall()

    @app.route("/payroll/<int:rid>", methods=["GET", "POST"])
    def payroll_run(rid):
        db = get_db()
        run = db.execute("SELECT * FROM payroll_runs WHERE id=?", (rid,)).fetchone()
        if request.method == "POST":   # recalc with variable items
            if run["status"] != "draft":
                raise ServiceError("Posted payroll cannot be changed")
            for slip in _run_slips(db, rid):
                emp = db.execute("SELECT * FROM employees WHERE id=?", (slip["employee_id"],)).fetchone()
                s = payroll.compute_payslip(emp, run["period"], overtime=fnum(f"ot_{slip['id']}"),
                                            bonus=fnum(f"bonus_{slip['id']}"), other_deduction=fnum(f"ded_{slip['id']}"))
                _save_slip(db, rid, s, slip["id"])
            db.commit()
            flash("Recalculated", "ok")
            return redirect(url_for("payroll_run", rid=rid))
        slips = _run_slips(db, rid)
        totals = {k: sum(s[k] or 0 for s in slips) for k in SLIP_COLS[1:]}
        stat_paid = {r["ref"].split("-")[0] for r in db.execute(
            "SELECT ref FROM journal_entries WHERE source_type='statutory' AND source_id=?", (rid,))}
        return render_template("payroll_run.html", run=run, slips=slips, totals=totals, stat_paid=stat_paid)

    @app.post("/payroll/<int:rid>/post")
    def payroll_post(rid):
        db = get_db()
        run = db.execute("SELECT * FROM payroll_runs WHERE id=?", (rid,)).fetchone()
        if run["status"] != "draft":
            raise ServiceError("Already posted")
        eid = accounting.post_payroll(db, run, [dict(s) for s in _run_slips(db, rid)])
        db.execute("UPDATE payroll_runs SET status='posted', posted_entry_id=? WHERE id=?", (eid, rid))
        db.commit()
        flash("Payroll posted to the general ledger", "ok")
        return redirect(url_for("payroll_run", rid=rid))

    @app.post("/payroll/<int:rid>/delete")
    def payroll_delete(rid):
        db = get_db()
        run = db.execute("SELECT * FROM payroll_runs WHERE id=?", (rid,)).fetchone()
        if run["status"] != "draft":
            raise ServiceError("Posted payroll cannot be deleted")
        db.execute("DELETE FROM payroll_runs WHERE id=?", (rid,))
        db.commit()
        return redirect(url_for("payroll_runs"))

    STAT = {"SAL": ("2200", "Net salaries"), "EPF": ("2210", "EPF (KWSP)"), "SOCSO": ("2220", "SOCSO (PERKESO)"),
            "EIS": ("2230", "EIS (SIP)"), "PCB": ("2240", "PCB / MTD (LHDN)")}

    @app.post("/payroll/<int:rid>/pay/<kind>")
    def payroll_pay(rid, kind):
        db = get_db()
        run = db.execute("SELECT * FROM payroll_runs WHERE id=?", (rid,)).fetchone()
        if run["status"] == "draft":
            raise ServiceError("Post the payroll first")
        if kind not in STAT:
            abort(404)
        if db.execute("SELECT 1 FROM journal_entries WHERE source_type='statutory' AND source_id=? AND ref LIKE ?",
                      (rid, f"{kind}-%")).fetchone():
            raise ServiceError(f"{STAT[kind][1]} for {run['period']} already paid")
        slips = _run_slips(db, rid)
        amount = {"SAL": sum(s["net"] + s["other_deduction"] for s in slips),
                  "EPF": sum(s["epf_ee"] + s["epf_er"] for s in slips),
                  "SOCSO": sum(s["socso_ee"] + s["socso_er"] for s in slips),
                  "EIS": sum(s["eis_ee"] + s["eis_er"] for s in slips), "PCB": sum(s["pcb"] for s in slips)}[kind]
        accounting.post_entry(db, today(), f"{kind}-{run['period']}", f"Payment of {STAT[kind][1]} {run['period']}",
                              [(STAT[kind][0], amount, 0), ("1000", 0, amount)], "statutory", rid)
        db.commit()
        flash(f"{STAT[kind][1]} payment of RM {amount:,.2f} recorded", "ok")
        return redirect(url_for("payroll_run", rid=rid))

    @app.route("/payroll/<int:rid>/export/<kind>.csv")
    def payroll_export(rid, kind):
        db = get_db()
        run = db.execute("SELECT * FROM payroll_runs WHERE id=?", (rid,)).fetchone()
        slips = _run_slips(db, rid)
        s = get_settings(db)
        buf = io.StringIO()
        w = csv.writer(buf)
        if kind == "epf":      # KWSP Borang A style
            w.writerow(["Employer", s["company_name"], "Contribution month", run["period"]])
            w.writerow(["Member No", "IC No", "Name", "Wages", "Employer Share", "Employee Share", "Total"])
            for r in slips:
                w.writerow([r["epf_no"], r["nric"], r["name"], f"{r['gross'] - r['overtime']:.2f}", f"{r['epf_er']:.2f}",
                            f"{r['epf_ee']:.2f}", f"{r['epf_er'] + r['epf_ee']:.2f}"])
        elif kind == "socso":  # PERKESO Borang 8A style, SOCSO + EIS
            w.writerow(["Employer", s["company_name"], "Contribution month", run["period"]])
            w.writerow(["IC No", "SOCSO No", "Name", "Wages", "SOCSO Employer", "SOCSO Employee", "EIS Employer",
                        "EIS Employee", "Total"])
            for r in slips:
                w.writerow([r["nric"], r["socso_no"], r["name"], f"{r['basic'] + r['allowance'] + r['overtime']:.2f}",
                            f"{r['socso_er']:.2f}", f"{r['socso_ee']:.2f}", f"{r['eis_er']:.2f}", f"{r['eis_ee']:.2f}",
                            f"{r['socso_er'] + r['socso_ee'] + r['eis_er'] + r['eis_ee']:.2f}"])
        elif kind == "pcb":    # CP39 style
            w.writerow(["Employer TIN", s["company_tin"], "Month", run["period"]])
            w.writerow(["Tax No", "IC No", "Name", "Gross", "PCB"])
            for r in slips:
                w.writerow([r["tax_no"], r["nric"], r["name"], f"{r['gross']:.2f}", f"{r['pcb']:.2f}"])
        elif kind == "bank":
            w.writerow(["Name", "Bank", "Account", "Amount", "Reference"])
            for r in slips:
                e = db.execute("SELECT bank_name, bank_account FROM employees WHERE id=?", (r["employee_id"],)).fetchone()
                w.writerow([r["name"], e["bank_name"], e["bank_account"], f"{r['net']:.2f}", f"SALARY {run['period']}"])
        else:
            abort(404)
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename={kind}-{run['period']}.csv"})

    @app.route("/payslip/<int:sid>")
    def payslip(sid):
        db = get_db()
        slip = db.execute("SELECT p.*, e.*, p.id slip_id FROM payslips p JOIN employees e ON e.id=p.employee_id "
                          "WHERE p.id=?", (sid,)).fetchone()
        run = db.execute("SELECT * FROM payroll_runs WHERE id=?", (slip["run_id"],)).fetchone()
        return render_template("payslip.html", s=slip, run=run)

    # ================================================================ accounting
    @app.route("/accounting/journal")
    def journal():
        db = get_db()
        entries = db.execute("SELECT * FROM journal_entries ORDER BY date DESC, id DESC LIMIT 300").fetchall()
        lines = {}
        for l in db.execute("SELECT l.*, a.name FROM journal_lines l JOIN accounts a ON a.code=l.account_code "
                            "WHERE entry_id IN (SELECT id FROM journal_entries ORDER BY date DESC, id DESC LIMIT 300)"):
            lines.setdefault(l["entry_id"], []).append(l)
        return render_template("journal.html", entries=entries, lines=lines)

    @app.route("/accounting/reports")
    def reports():
        db = get_db()
        d_from = request.args.get("from") or today()[:4] + "-01-01"
        d_to = request.args.get("to") or today()
        tb = accounting.account_balances(db, None, d_to)
        pl = accounting.account_balances(db, d_from, d_to)
        income = [a for a in pl if a["type"] == "income" and a["balance"]]
        expense = [a for a in pl if a["type"] == "expense" and a["balance"]]
        ti, te = -sum(a["balance"] for a in income), sum(a["balance"] for a in expense)
        sst = db.execute("SELECT COALESCE(SUM(l.credit-l.debit),0) FROM journal_lines l JOIN journal_entries e "
                         "ON e.id=l.entry_id WHERE l.account_code='2100' AND e.date BETWEEN ? AND ?",
                         (d_from, d_to)).fetchone()[0]
        return render_template("reports.html", tb=tb, income=income, expense=expense, ti=ti, te=te, d_from=d_from,
                               d_to=d_to, sst=sst)

    # ================================================================ LHDN tools
    @app.route("/lhdn")
    def lhdn_tools():
        db = get_db()
        s = get_settings(db)
        log = db.execute("SELECT * FROM lhdn_log ORDER BY id DESC LIMIT 50").fetchall()
        received, err = [], None
        if request.args.get("received"):
            try:
                c = services.get_client(db, db_path())
                res = c.recent_documents("Received", own_tin=s["company_tin"]) if isinstance(c, MockLHDNClient) \
                    else c.recent_documents("Received")
                received = res.get("result", [])
            except (LHDNError, ServiceError) as e:
                err = str(e)
        return render_template("lhdn.html", log=log, received=received, err=err)

    @app.post("/lhdn/validate-tin")
    def lhdn_validate_tin():
        ok = services.get_client(get_db(), db_path()).validate_tin(fval("tin").upper(), fval("id_type"), fval("id_value"))
        flash(f"TIN {fval('tin').upper()} / {fval('id_type')} {fval('id_value')}: {'VALID' if ok else 'NOT valid'}",
              "ok" if ok else "error")
        return redirect(url_for("lhdn_tools"))

    @app.post("/lhdn/test-login")
    def lhdn_test_login():
        c = services.get_client(get_db(), db_path())
        if isinstance(c, MockLHDNClient):
            flash("Mock mode - no login needed", "info")
        else:
            c._token = None
            c.token()
            flash(f"Logged in to MyInvois {c.env} successfully", "ok")
        return redirect(url_for("lhdn_tools"))

    @app.route("/lhdn/import/<uuid>")
    def lhdn_import(uuid):
        """Turn a received supplier e-Invoice into an expense (prefilled form)."""
        db = get_db()
        raw = services.get_client(db, db_path()).get_document(uuid)
        info = services.parse_received_document(raw["document"])
        p = db.execute("SELECT id FROM parties WHERE tin=?", (info["tin"],)).fetchone()
        if not p:
            pid = db.execute("INSERT INTO parties(kind,name,tin,id_type,id_value) VALUES ('supplier',?,?,?,?)",
                             (info["supplier_name"], info["tin"], info["id_type"], info["id_value"])).lastrowid
            db.commit()
        else:
            pid = p["id"]
        return redirect(url_for("expense_form", party_id=pid, description=f"{info['number']}: {info['description']}",
                                amount=info["amount"], tax=info["tax"], supplier_einvoice_uuid=uuid,
                                date=info["date"]))

    @app.post("/lhdn/simulate-received")
    def lhdn_simulate_received():
        db = get_db()
        c = services.get_client(db, db_path())
        if not isinstance(c, MockLHDNClient):
            raise ServiceError("Only available in mock mode")
        s = get_settings(db)
        supplier = {"name": "Kedai Alat Tulis Maju Sdn Bhd", "tin": "C10987654321", "id_type": "BRN",
                    "id_value": "199901012345", "msic": "47612", "activity": "Retail sale of stationery",
                    "phone": "+60377778888", "addr1": "12 Jalan SS2/24", "city": "Petaling Jaya", "postcode": "47300",
                    "state_code": "10"}
        n = db.execute("SELECT COUNT(*) FROM mock_lhdn_docs WHERE issuer_tin='C10987654321'").fetchone()[0] + 1
        number = f"KATM-{n:04d}"
        j = build_invoice({"doc_type": "01", "number": number, "currency": "MYR", "payment_mode": "03"},
                          [{"description": "Office stationery", "qty": 10, "unit_price": 25.5, "tax_type": "01",
                            "tax_rate": 10, "classification": "022"}], supplier, company_as_party(s))
        c.inject_received(j, supplier["tin"], s["company_tin"], number, 280.5)
        flash("Simulated supplier e-Invoice issued to your TIN", "ok")
        return redirect(url_for("lhdn_tools", received=1))

    # ================================================================ settings
    @app.route("/settings", methods=["GET", "POST"])
    def settings_page():
        db = get_db()
        if request.method == "POST":
            vals = {k: fval(k) for k in request.form if k != "lhdn_client_secret" or fval(k)}
            cert = request.files.get("cert_file")
            if cert and cert.filename:
                os.makedirs(app.config["UPLOAD_DIR"], exist_ok=True)
                path = os.path.join(app.config["UPLOAD_DIR"], "signing.p12")
                cert.save(path)
                vals["lhdn_cert_path"] = path
            if vals.get("lhdn_doc_version") == "1.1":
                path = vals.get("lhdn_cert_path") or get_settings(db).get("lhdn_cert_path")
                try:
                    from lhdn.signer import load_p12
                    _k, crt = load_p12(path, vals.get("lhdn_cert_password", ""))
                    flash(f"Signing certificate loaded: {crt.subject.rfc4514_string()}", "ok")
                except Exception as e:
                    flash(f"Certificate problem: {e}", "error")
            save_settings(db, vals)
            flash("Settings saved", "ok")
            return redirect(url_for("settings_page"))
        return render_template("settings.html")

    @app.post("/settings/demo-cert")
    def settings_demo_cert():
        """Generate a self-signed test certificate so v1.1 signing can be exercised in mock mode."""
        from lhdn.devcert import make_p12
        os.makedirs(app.config["UPLOAD_DIR"], exist_ok=True)
        path = os.path.join(app.config["UPLOAD_DIR"], "demo-selfsigned.p12")
        s = get_settings(get_db())
        with open(path, "wb") as fh:
            fh.write(make_p12("demo", s["company_name"], s["company_tin"]))
        save_settings(get_db(), {"lhdn_cert_path": path, "lhdn_cert_password": "demo"})
        flash("Self-signed demo certificate created (password 'demo'). Not accepted by real LHDN.", "ok")
        return redirect(url_for("settings_page"))

    @app.post("/demo-data")
    def demo_data():
        from seed import seed_demo
        seed_demo(get_db())
        flash("Demo customers, suppliers and employees added", "ok")
        return redirect(url_for("dashboard"))


app = create_app() if __name__ == "__main__" or os.environ.get("EINVOIS_AUTOCREATE") else None

if __name__ == "__main__":
    app.run(debug=True, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 5000)))
