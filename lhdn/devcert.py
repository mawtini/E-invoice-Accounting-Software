"""Generate a self-signed PKCS#12 certificate for local testing of v1.1 signing.
LHDN only accepts certificates from recognised CAs - use this for the simulator and tests only."""
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID


def make_p12(password: str, org: str = "Demo Sdn Bhd", tin: str = "C0000000000") -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "MY"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, org),
        x509.NameAttribute(NameOID.ORGANIZATION_IDENTIFIER, tin),
        x509.NameAttribute(NameOID.COMMON_NAME, org),
    ])
    issuer = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "MY"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Self-signed Test CA"),
        x509.NameAttribute(NameOID.COMMON_NAME, "Self-signed Test CA"),
    ])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=365)).sign(key, hashes.SHA256()))
    return pkcs12.serialize_key_and_certificates(
        b"signing", key, cert, None, serialization.BestAvailableEncryption(password.encode()))
