import base64
import copy
import hashlib
import json

import pytest

from lhdn.devcert import make_p12
from lhdn.signer import sign_document, verify_document
from lhdn.ubl import build_invoice, submission_entry, validate_invoice, minify
from cryptography.hazmat.primitives.serialization import pkcs12

SUPPLIER = {"name": "Demo Sdn Bhd", "tin": "C25845632020", "id_type": "BRN", "id_value": "202001234567",
            "msic": "62010", "activity": "Programming", "phone": "+60312345678", "addr1": "Lot 1",
            "city": "Kuala Lumpur", "postcode": "50480", "state_code": "14"}
BUYER = {"name": "Buyer Bhd", "tin": "C20880050010", "id_type": "BRN", "id_value": "201901012345",
         "phone": "+60388889999", "addr1": "No. 8", "city": "PJ", "state_code": "10"}
LINES = [{"description": "Consulting", "qty": 10, "unit_price": 150, "tax_type": "02", "tax_rate": 8},
         {"description": "Licence", "qty": 1, "unit_price": 999.99, "discount": 100, "tax_type": "E",
          "exemption_reason": "Exempt under Schedule A"}]


def doc(t="01", **kw):
    return {"doc_type": t, "number": "INV-00001", "currency": "MYR", "payment_mode": "03", **kw}


def test_build_and_validate():
    j = build_invoice(doc(), LINES, SUPPLIER, BUYER)
    assert validate_invoice(j) == []
    inv = j["Invoice"][0]
    assert inv["LegalMonetaryTotal"][0]["TaxExclusiveAmount"][0]["_"] == 2399.99
    assert inv["TaxTotal"][0]["TaxAmount"][0]["_"] == 120.0
    assert inv["LegalMonetaryTotal"][0]["PayableAmount"][0]["_"] == 2519.99
    assert {s["TaxCategory"][0]["ID"][0]["_"] for s in inv["TaxTotal"][0]["TaxSubtotal"]} == {"02", "E"}
    assert inv["AccountingSupplierParty"][0]["Party"][0]["IndustryClassificationCode"][0]["_"] == "62010"


def test_credit_note_requires_reference():
    with pytest.raises(ValueError):
        build_invoice(doc("02"), LINES, SUPPLIER, BUYER)
    j = build_invoice(doc("02"), LINES, SUPPLIER, BUYER, ref_doc={"number": "INV-00001", "lhdn_uuid": "ABC"})
    assert j["Invoice"][0]["BillingReference"][0]["InvoiceDocumentReference"][0]["UUID"][0]["_"] == "ABC"


def test_self_billed_swaps_parties():
    j = build_invoice(doc("11"), LINES, company=BUYER, counterparty=SUPPLIER)
    sup = j["Invoice"][0]["AccountingSupplierParty"][0]["Party"][0]
    assert sup["PartyLegalEntity"][0]["RegistrationName"][0]["_"] == "Demo Sdn Bhd"


def test_validator_catches_errors():
    j = build_invoice(doc(), LINES, SUPPLIER, {**BUYER, "tin": "bad"})
    j["Invoice"][0]["LegalMonetaryTotal"][0]["TaxInclusiveAmount"][0]["_"] = 1
    errs = validate_invoice(j)
    assert any("Buyer TIN" in e for e in errs)
    assert any("TaxInclusiveAmount" in e for e in errs)


def test_submission_entry_hash():
    j = build_invoice(doc(), LINES, SUPPLIER, BUYER)
    e = submission_entry(j, "INV-00001")
    raw = base64.b64decode(e["document"])
    assert hashlib.sha256(raw).hexdigest() == e["documentHash"]
    assert json.loads(raw) == j


def _keys():
    key, cert, _ = pkcs12.load_key_and_certificates(make_p12("pw"), b"pw")
    return key, cert


def test_sign_and_verify_roundtrip():
    key, cert = _keys()
    j = build_invoice(doc(), LINES, SUPPLIER, BUYER, version="1.1")
    signed = sign_document(j, key, cert)
    assert validate_invoice(signed) == []
    assert verify_document(signed) == []
    # signature section is excluded from the signed payload
    assert minify({k: v for k, v in signed.items()}) != minify(j)


def test_tampering_detected():
    key, cert = _keys()
    signed = sign_document(build_invoice(doc(), LINES, SUPPLIER, BUYER, version="1.1"), key, cert)
    bad = copy.deepcopy(signed)
    bad["Invoice"][0]["LegalMonetaryTotal"][0]["PayableAmount"][0]["_"] = 1.0
    problems = verify_document(bad)
    assert any("digest" in p.lower() for p in problems)
    assert any("SignatureValue" in p for p in problems)
