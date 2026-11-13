"""Build LHDN MyInvois UBL 2.1 documents in JSON notation and validate them locally.

The JSON notation follows the SDK: every element is an array of objects, the
element value lives under "_" and attributes sit beside it.
"""
import base64
import hashlib
import json
import re
from datetime import datetime, timezone

from .codes import ADJUSTMENT_TYPES, SELF_BILLED_TYPES, GENERIC_TIN

NS = {
    "_D": "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2",
    "_A": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "_B": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
}
TAX_SCHEME = [{"ID": [{"_": "OTH", "schemeID": "UN/ECE 5153", "schemeAgencyID": "6"}]}]


def r2(x):
    return round(float(x) + 1e-9, 2)


def v(value, **attrs):
    """One UBL JSON element: [{"_": value, attr: ...}]"""
    d = {"_": value}
    d.update(attrs)
    return [d]


def amt(value, cur):
    return v(r2(value), currencyID=cur)


# ---------------------------------------------------------------- line maths
def compute_line(line):
    qty = float(line["qty"])
    price = float(line["unit_price"])
    discount = float(line.get("discount") or 0)
    subtotal = r2(qty * price)
    net = r2(subtotal - discount)
    rate = float(line.get("tax_rate") or 0)
    tax = r2(net * rate / 100) if line.get("tax_type") not in ("06", "E") else 0.0
    return {"subtotal": subtotal, "discount": r2(discount), "net": net, "tax": tax, "total": r2(net + tax)}


def compute_totals(lines):
    sub = tax = 0.0
    for ln in lines:
        c = compute_line(ln)
        sub += c["net"]
        tax += c["tax"]
    return {"subtotal": r2(sub), "tax_total": r2(tax), "total": r2(sub + tax)}


# ---------------------------------------------------------------- parties
def _party_ids(p):
    ids = [{"ID": v(p["tin"], schemeID="TIN")},
           {"ID": v(p.get("id_value") or "NA", schemeID=p.get("id_type") or "BRN")},
           {"ID": v(p.get("sst_no") or "NA", schemeID="SST")},
           {"ID": v(p.get("ttx_no") or "NA", schemeID="TTX")}]
    return ids


def _address(p):
    lines = [x for x in (p.get("addr1"), p.get("addr2"), p.get("addr3")) if x] or ["NA"]
    addr = {
        "CityName": v(p.get("city") or "NA"),
        "PostalZone": v(p.get("postcode") or ""),
        "CountrySubentityCode": v(p.get("state_code") or "17"),
        "AddressLine": [{"Line": v(x)} for x in lines],
        "Country": [{"IdentificationCode": v(p.get("country_code") or "MYS", listID="ISO3166-1", listAgencyID="6")}],
    }
    if not addr["PostalZone"][0]["_"]:
        del addr["PostalZone"]
    return [addr]


def _party(p, supplier: bool):
    party = {}
    if supplier:
        party["IndustryClassificationCode"] = v(p.get("msic") or "00000", name=p.get("activity") or "NOT APPLICABLE")
    party["PartyIdentification"] = _party_ids(p)
    party["PostalAddress"] = _address(p)
    party["PartyLegalEntity"] = [{"RegistrationName": v(p["name"])}]
    contact = {"Telephone": v(p.get("phone") or "NA")}
    if p.get("email"):
        contact["ElectronicMail"] = v(p["email"])
    party["Contact"] = [contact]
    return [{"Party": [party]}]


def company_as_party(s):
    return {
        "name": s["company_name"], "tin": s["company_tin"], "id_type": s.get("company_id_type", "BRN"),
        "id_value": s["company_brn"], "sst_no": s.get("company_sst"), "ttx_no": s.get("company_ttx"),
        "msic": s.get("company_msic"), "activity": s.get("company_activity"), "email": s.get("company_email"),
        "phone": s.get("company_phone"), "addr1": s.get("company_addr1"), "addr2": s.get("company_addr2"),
        "addr3": s.get("company_addr3"), "city": s.get("company_city"), "postcode": s.get("company_postcode"),
        "state_code": s.get("company_state"), "country_code": s.get("company_country") or "MYS",
    }


# ---------------------------------------------------------------- document
def build_invoice(doc, lines, company, counterparty, version="1.0", ref_doc=None, issued_at=None,
                  bank_account=None):
    """Return the UBL JSON (dict) for a document.

    doc / lines / company / counterparty / ref_doc are plain dicts.
    For self-billed types the counterparty is the supplier and the company the buyer.
    """
    cur = doc.get("currency") or "MYR"
    issued_at = issued_at or datetime.now(timezone.utc)
    self_billed = doc["doc_type"] in SELF_BILLED_TYPES
    supplier, buyer = (counterparty, company) if self_billed else (company, counterparty)

    inv = {
        "ID": v(doc["number"]),
        "IssueDate": v(issued_at.strftime("%Y-%m-%d")),
        "IssueTime": v(issued_at.strftime("%H:%M:%SZ")),
        "InvoiceTypeCode": v(doc["doc_type"], listVersionID=version),
        "DocumentCurrencyCode": v(cur),
        "TaxCurrencyCode": v("MYR"),
    }
    if doc.get("period_start") and doc.get("period_end"):
        inv["InvoicePeriod"] = [{"StartDate": v(doc["period_start"]), "EndDate": v(doc["period_end"]),
                                 "Description": v(doc.get("period_desc") or "Monthly")}]
    if doc["doc_type"] in ADJUSTMENT_TYPES:
        if not ref_doc:
            raise ValueError("Credit/debit/refund notes must reference the original e-Invoice")
        inv["BillingReference"] = [{"InvoiceDocumentReference": [{
            "ID": v(ref_doc["number"]), "UUID": v(ref_doc.get("lhdn_uuid") or "NA")}]}]
    inv["AccountingSupplierParty"] = _party(supplier, supplier=True)
    inv["AccountingCustomerParty"] = _party(buyer, supplier=False)

    if doc.get("payment_mode"):
        pm = {"PaymentMeansCode": v(doc["payment_mode"])}
        if bank_account and not self_billed:
            pm["PayeeFinancialAccount"] = [{"ID": v(bank_account)}]
        inv["PaymentMeans"] = [pm]
    if doc.get("payment_terms"):
        inv["PaymentTerms"] = [{"Note": v(doc["payment_terms"])}]
    if cur != "MYR":
        inv["TaxExchangeRate"] = [{"SourceCurrencyCode": v(cur), "TargetCurrencyCode": v("MYR"),
                                   "CalculationRate": v(float(doc.get("exchange_rate") or 1))}]

    # lines + tax subtotals by tax type
    inv_lines, by_tax = [], {}
    tot_net = tot_tax = tot_disc = 0.0
    for i, ln in enumerate(lines, 1):
        c = compute_line(ln)
        tot_net += c["net"]
        tot_tax += c["tax"]
        tot_disc += c["discount"]
        tax_type = ln.get("tax_type") or "06"
        cat = {"ID": v(tax_type)}
        if tax_type == "E":
            cat["TaxExemptionReason"] = v(ln.get("exemption_reason") or "Exempted")
        cat["TaxScheme"] = TAX_SCHEME
        sub = {"TaxableAmount": amt(c["net"], cur), "TaxAmount": amt(c["tax"], cur),
               "Percent": v(r2(ln.get("tax_rate") or 0)), "TaxCategory": [cat]}
        line = {
            "ID": v(str(i)),
            "InvoicedQuantity": v(float(ln["qty"]), unitCode=ln.get("unit_code") or "C62"),
            "LineExtensionAmount": amt(c["net"], cur),
        }
        if c["discount"]:
            line["AllowanceCharge"] = [{"ChargeIndicator": v(False), "AllowanceChargeReason": v("Discount"),
                                        "Amount": amt(c["discount"], cur)}]
        line["TaxTotal"] = [{"TaxAmount": amt(c["tax"], cur), "TaxSubtotal": [sub]}]
        line["Item"] = [{
            "CommodityClassification": [{"ItemClassificationCode": v(ln.get("classification") or "022", listID="CLASS")}],
            "Description": v(ln["description"]),
        }]
        line["Price"] = [{"PriceAmount": amt(ln["unit_price"], cur)}]
        line["ItemPriceExtension"] = [{"Amount": amt(c["subtotal"], cur)}]
        inv_lines.append(line)

        b = by_tax.setdefault(tax_type, {"taxable": 0.0, "tax": 0.0, "reason": ln.get("exemption_reason")})
        b["taxable"] += c["net"]
        b["tax"] += c["tax"]

    subtotals = []
    for tax_type, b in by_tax.items():
        cat = {"ID": v(tax_type)}
        if tax_type == "E":
            cat["TaxExemptionReason"] = v(b["reason"] or "Exempted")
        cat["TaxScheme"] = TAX_SCHEME
        subtotals.append({"TaxableAmount": amt(b["taxable"], cur), "TaxAmount": amt(b["tax"], cur),
                          "TaxCategory": [cat]})
    inv["TaxTotal"] = [{"TaxAmount": amt(tot_tax, cur), "TaxSubtotal": subtotals}]
    inv["LegalMonetaryTotal"] = [{
        "LineExtensionAmount": amt(tot_net, cur),
        "TaxExclusiveAmount": amt(tot_net, cur),
        "TaxInclusiveAmount": amt(tot_net + tot_tax, cur),
        "AllowanceTotalAmount": amt(0, cur),
        "PayableAmount": amt(tot_net + tot_tax, cur),
    }]
    inv["InvoiceLine"] = inv_lines
    return {**NS, "Invoice": [inv]}


# ---------------------------------------------------------------- serialisation
def minify(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def submission_entry(doc_json: dict, code_number: str) -> dict:
    """Entry for POST /api/v1.0/documentsubmissions."""
    raw = minify(doc_json)
    return {"format": "JSON", "document": base64.b64encode(raw).decode(),
            "documentHash": hashlib.sha256(raw).hexdigest(), "codeNumber": code_number}


# ---------------------------------------------------------------- validation
TIN_RE = re.compile(r"^(IG\d{9,12}|[A-Z]{1,2}\d{8,12}|EI\d{11})$")


def _val(el, *path):
    cur = el
    for p in path:
        if isinstance(cur, list):
            if not cur:
                return None
            cur = cur[0]
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    if isinstance(cur, list) and cur and isinstance(cur[0], dict) and "_" in cur[0]:
        return cur[0]["_"]
    return cur


def validate_invoice(doc_json: dict) -> list:
    """Local pre-submission checks mirroring the mandatory fields in the LHDN SDK.
    Returns a list of error strings (empty when the structure looks valid)."""
    errors = []
    try:
        inv = doc_json["Invoice"][0]
    except (KeyError, IndexError, TypeError):
        return ["Root element 'Invoice' missing"]

    for f in ("ID", "IssueDate", "IssueTime", "InvoiceTypeCode", "DocumentCurrencyCode",
              "AccountingSupplierParty", "AccountingCustomerParty", "TaxTotal", "LegalMonetaryTotal", "InvoiceLine"):
        if f not in inv:
            errors.append(f"Mandatory field '{f}' missing")
    if errors:
        return errors

    version = inv["InvoiceTypeCode"][0].get("listVersionID")
    if version not in ("1.0", "1.1"):
        errors.append("InvoiceTypeCode.listVersionID must be 1.0 or 1.1")
    if version == "1.1" and ("UBLExtensions" not in inv or "Signature" not in inv):
        errors.append("Version 1.1 documents must carry a digital signature (UBLExtensions + Signature)")

    try:
        issued = datetime.strptime(f"{_val(inv, 'IssueDate')}T{_val(inv, 'IssueTime')}", "%Y-%m-%dT%H:%M:%SZ")
        age = (datetime.now(timezone.utc).replace(tzinfo=None) - issued).total_seconds()
        if age > 72 * 3600:
            errors.append("IssueDate/IssueTime is older than 72 hours")
        if age < -300:
            errors.append("IssueDate/IssueTime is in the future")
    except ValueError:
        errors.append("IssueDate must be YYYY-MM-DD and IssueTime HH:MM:SSZ (UTC)")

    if _val(inv, "InvoiceTypeCode") in ADJUSTMENT_TYPES and "BillingReference" not in inv:
        errors.append("Credit/Debit/Refund note must include BillingReference to the original document")

    for role in ("AccountingSupplierParty", "AccountingCustomerParty"):
        party = inv[role][0]["Party"][0]
        label = "Supplier" if role == "AccountingSupplierParty" else "Buyer"
        ids = {i["ID"][0].get("schemeID"): i["ID"][0]["_"] for i in party.get("PartyIdentification", [])}
        tin = ids.get("TIN")
        if not tin:
            errors.append(f"{label} TIN missing")
        elif not TIN_RE.match(tin):
            errors.append(f"{label} TIN '{tin}' has an invalid format")
        if not any(k in ids for k in ("BRN", "NRIC", "PASSPORT", "ARMY")):
            errors.append(f"{label} registration / identification number missing")
        if not _val(party, "PartyLegalEntity", "RegistrationName"):
            errors.append(f"{label} name missing")
        if not party.get("PostalAddress"):
            errors.append(f"{label} address missing")
        if not _val(party, "Contact", "Telephone"):
            errors.append(f"{label} contact number missing")
        if role == "AccountingSupplierParty" and not party.get("IndustryClassificationCode"):
            errors.append("Supplier MSIC code missing")
        if tin in GENERIC_TIN and role == "AccountingSupplierParty" and tin != "EI00000000030" and \
                _val(inv, "InvoiceTypeCode") in SELF_BILLED_TYPES:
            errors.append("Self-billed supplier may only use the foreign supplier generic TIN EI00000000030")

    # arithmetic consistency
    sum_lines = sum_tax = 0.0
    for ln in inv["InvoiceLine"]:
        sum_lines += float(_val(ln, "LineExtensionAmount"))
        sum_tax += float(_val(ln, "TaxTotal", "TaxAmount"))
        if not _val(ln, "Item", "CommodityClassification", "ItemClassificationCode"):
            errors.append(f"Line {_val(ln, 'ID')}: classification code missing")
        if not _val(ln, "Item", "Description"):
            errors.append(f"Line {_val(ln, 'ID')}: description missing")
    lmt = inv["LegalMonetaryTotal"][0]
    if abs(sum_lines - float(_val(lmt, "TaxExclusiveAmount"))) > 0.011:
        errors.append("Sum of line amounts does not equal TaxExclusiveAmount")
    if abs(sum_tax - float(_val(inv, "TaxTotal", "TaxAmount"))) > 0.011:
        errors.append("Sum of line tax does not equal TaxTotal.TaxAmount")
    if abs(float(_val(lmt, "TaxExclusiveAmount")) + float(_val(inv, "TaxTotal", "TaxAmount"))
           - float(_val(lmt, "TaxInclusiveAmount"))) > 0.011:
        errors.append("TaxInclusiveAmount must equal TaxExclusiveAmount + TaxAmount")
    if _val(inv, "DocumentCurrencyCode") != "MYR" and "TaxExchangeRate" not in inv:
        errors.append("Foreign currency documents require TaxExchangeRate")
    return errors
