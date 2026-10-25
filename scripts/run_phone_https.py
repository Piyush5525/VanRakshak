"""Serve VanRakshak over HTTPS on the local network so a phone can use the camera, location and compass.

Phones only allow those on HTTPS pages. This makes a self-signed certificate for this computer's network address (kept in data/certs, not in git)
and starts the app on https://<this computer>:8443. The phone shows a one-time "not private" warning because nobody official signed the
certificate: choose Advanced, then Continue. Everything stays inside your own network.

    python scripts/run_phone_https.py
"""
import datetime
import ipaddress
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
CERTS = ROOT / "data" / "certs"
PORT = 8443


def make_cert(ip: str) -> tuple[Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    CERTS.mkdir(parents=True, exist_ok=True)
    crt, key = CERTS / "lan.crt", CERTS / "lan.key"
    if crt.is_file() and key.is_file() and ip in (CERTS / "lan.ip").read_text():
        return crt, key
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "VanRakshak local")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(k.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=365))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.IPAddress(ipaddress.ip_address(ip))]), critical=False).sign(k, hashes.SHA256()))
    crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    (CERTS / "lan.ip").write_text(ip)
    return crt, key


if __name__ == "__main__":
    from app.services.mobilelink import lan_ip

    ip = lan_ip()
    if not ip:
        sys.exit("Could not find this computer's network address. Connect to Wi-Fi first.")
    crt, key = make_cert(ip)
    print(f"\nOpen on the phone:  https://{ip}:{PORT}/live\nThen choose Advanced > Continue once. QR codes made on the computer will use this address.\n")
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=PORT, ssl_certfile=str(crt), ssl_keyfile=str(key), log_level="warning")
