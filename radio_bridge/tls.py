"""Самоподписанный сертификат: создаётся в data_dir при первом старте и переиспользуется."""
import datetime
import ipaddress
import logging
import socket
import ssl
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

log = logging.getLogger(__name__)


def _san_entries(names: list[str]) -> list[x509.GeneralName]:
    out: list[x509.GeneralName] = []
    for n in names:
        try:
            out.append(x509.IPAddress(ipaddress.ip_address(n)))
        except ValueError:
            out.append(x509.DNSName(n))
    return out


def ensure_cert(data_dir: Path, san: list[str]) -> tuple[Path, Path]:
    cert_p, key_p = data_dir / "cert.pem", data_dir / "key.pem"
    if cert_p.exists() and key_p.exists():
        return cert_p, key_p
    data_dir.mkdir(parents=True, exist_ok=True)
    names = san or [socket.gethostname(), "localhost", "127.0.0.1"]
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject).issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(_san_entries(names)), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    key_p.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    key_p.chmod(0o600)
    cert_p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    log.info("TLS: создан самоподписанный сертификат для %s", ", ".join(names))
    return cert_p, key_p


def ssl_context(data_dir: Path, san: list[str]) -> ssl.SSLContext:
    cert_p, key_p = ensure_cert(data_dir, san)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cert_p, key_p)
    return ctx
