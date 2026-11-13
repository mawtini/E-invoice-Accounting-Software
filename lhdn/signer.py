"""Digital signature for MyInvois document version 1.1 (JSON notation).

Implements the 8-step procedure in the SDK guide "Signature Creation (JSON)":
 1. Take the document without UBLExtensions and Signature
 2. Minify and hash it (SHA-256)            -> DocDigest
 3. Sign the minified document (RSA-SHA256)  -> SignatureValue
 4. Hash the signing certificate (SHA-256)  -> CertDigest
 5. Populate SignedProperties (signing time, cert digest, issuer, serial)
 6. Hash the SignedProperties                -> PropsDigest
 7. Populate UBLExtensions + Signature
 8. Submit (hash of the final document is the submission documentHash)

The certificate must be the PKCS#12 (.p12/.pfx) issued to the taxpayer by an
LHDN-recognised CA (e.g. MSC Trustgate, Pos Digicert). For sandbox testing a
self-signed certificate is accepted by the local verifier only.
"""
import base64
import copy
import hashlib
from datetime import datetime, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import pkcs12

from .ubl import minify

SHA256_URI = "http://www.w3.org/2001/04/xmlenc#sha256"
RSA_SHA256_URI = "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"
XADES_URI = "urn:oasis:names:specification:ubl:dsig:enveloped:xades"
SIGNED_PROPS_TYPE = "http://uri.etsi.org/01903/v1.3.2#SignedProperties"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def sha256_b64(data: bytes) -> str:
    return b64(hashlib.sha256(data).digest())


def name_string(name: x509.Name) -> str:
    """Distinguished name as 'CN=..., O=..., C=MY' (most specific first)."""
    return ", ".join(attr.rfc4514_string() for rdn in reversed(name.rdns) for attr in rdn)


def load_p12(path: str, password: str):
    with open(path, "rb") as fh:
        key, cert, _extra = pkcs12.load_key_and_certificates(fh.read(), (password or "").encode() or None)
    if key is None or cert is None:
        raise ValueError("PKCS#12 file must contain both the private key and certificate")
    return key, cert


def strip_signature(doc: dict) -> dict:
    d = copy.deepcopy(doc)
    inv = d["Invoice"][0]
    inv.pop("UBLExtensions", None)
    inv.pop("Signature", None)
    return d


def _signed_properties(cert_digest, signing_time, issuer, serial):
    return {
        "Target": "signature",
        "SignedProperties": [{
            "Id": "id-xades-signed-props",
            "SignedSignatureProperties": [{
                "SigningTime": [{"_": signing_time}],
                "SigningCertificate": [{"Cert": [{
                    "CertDigest": [{"DigestMethod": [{"_": "", "Algorithm": SHA256_URI}],
                                    "DigestValue": [{"_": cert_digest}]}],
                    "IssuerSerial": [{"X509IssuerName": [{"_": issuer}],
                                      "X509SerialNumber": [{"_": serial}]}],
                }]}],
            }],
        }],
    }


def sign_document(doc: dict, private_key, cert: x509.Certificate, signing_time: datetime = None) -> dict:
    """Return a copy of `doc` carrying the v1.1 enveloped signature."""
    unsigned = strip_signature(doc)
    payload = minify(unsigned)                                        # step 1-2
    doc_digest = sha256_b64(payload)
    signature_value = b64(private_key.sign(payload, padding.PKCS1v15(), hashes.SHA256()))  # step 3

    cert_der = cert.public_bytes(serialization.Encoding.DER)
    cert_digest = sha256_b64(cert_der)                                # step 4
    issuer, subject, serial = name_string(cert.issuer), name_string(cert.subject), str(cert.serial_number)
    signing_time = (signing_time or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")

    qualifying = _signed_properties(cert_digest, signing_time, issuer, serial)   # step 5
    props_digest = sha256_b64(minify(qualifying))                                # step 6

    ext = [{"UBLExtension": [{
        "ExtensionURI": [{"_": XADES_URI}],
        "ExtensionContent": [{"UBLDocumentSignatures": [{"SignatureInformation": [{
            "ID": [{"_": "urn:oasis:names:specification:ubl:signature:1"}],
            "ReferencedSignatureID": [{"_": "urn:oasis:names:specification:ubl:signature:Invoice"}],
            "Signature": [{
                "Id": "signature",
                "Object": [{"QualifyingProperties": [qualifying]}],
                "KeyInfo": [{"X509Data": [{
                    "X509Certificate": [{"_": b64(cert_der)}],
                    "X509SubjectName": [{"_": subject}],
                    "X509IssuerSerial": [{"X509IssuerName": [{"_": issuer}],
                                          "X509SerialNumber": [{"_": serial}]}],
                }]}],
                "SignatureValue": [{"_": signature_value}],
                "SignedInfo": [{
                    "SignatureMethod": [{"_": "", "Algorithm": RSA_SHA256_URI}],
                    "Reference": [
                        {"Type": SIGNED_PROPS_TYPE, "URI": "#id-xades-signed-props",
                         "DigestMethod": [{"_": "", "Algorithm": SHA256_URI}],
                         "DigestValue": [{"_": props_digest}]},
                        {"Type": "", "URI": "",
                         "DigestMethod": [{"_": "", "Algorithm": SHA256_URI}],
                         "DigestValue": [{"_": doc_digest}]},
                    ],
                }],
            }],
        }]}]}],
    }]}]

    signed = copy.deepcopy(unsigned)
    inv = signed["Invoice"][0]
    inv["UBLExtensions"] = ext                                         # step 7
    inv["Signature"] = [{"ID": [{"_": "urn:oasis:names:specification:ubl:signature:Invoice"}],
                         "SignatureMethod": [{"_": XADES_URI}]}]
    return signed


def verify_document(doc: dict) -> list:
    """Verify a signed document; returns a list of problems (empty == valid)."""
    problems = []
    try:
        sig = (doc["Invoice"][0]["UBLExtensions"][0]["UBLExtension"][0]["ExtensionContent"][0]
               ["UBLDocumentSignatures"][0]["SignatureInformation"][0]["Signature"][0])
    except (KeyError, IndexError, TypeError):
        return ["Signature block not found"]
    payload = minify(strip_signature(doc))
    refs = sig["SignedInfo"][0]["Reference"]
    doc_ref = next(r for r in refs if r["URI"] == "")
    props_ref = next(r for r in refs if r["URI"] == "#id-xades-signed-props")
    if doc_ref["DigestValue"][0]["_"] != sha256_b64(payload):
        problems.append("Document digest mismatch (document altered after signing)")

    cert_der = base64.b64decode(sig["KeyInfo"][0]["X509Data"][0]["X509Certificate"][0]["_"])
    cert = x509.load_der_x509_certificate(cert_der)
    try:
        cert.public_key().verify(base64.b64decode(sig["SignatureValue"][0]["_"]), payload,
                                 padding.PKCS1v15(), hashes.SHA256())
    except Exception:
        problems.append("SignatureValue does not verify against the embedded certificate")

    qualifying = sig["Object"][0]["QualifyingProperties"][0]
    if props_ref["DigestValue"][0]["_"] != sha256_b64(minify(qualifying)):
        problems.append("SignedProperties digest mismatch")
    cert_digest = (qualifying["SignedProperties"][0]["SignedSignatureProperties"][0]["SigningCertificate"][0]
                   ["Cert"][0]["CertDigest"][0]["DigestValue"][0]["_"])
    if cert_digest != sha256_b64(cert_der):
        problems.append("Certificate digest mismatch")
    return problems
