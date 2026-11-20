import socket
import ssl
import subprocess
import threading
from pathlib import Path

import pytest

from skylight_wifi_automation.tls import build_ssl_context


def _cert(tmp_path: Path, hostname: str) -> tuple[str, Path, Path]:
    config = tmp_path / f"{hostname}.cnf"
    config.write_text(
        "\n".join(
            [
                "[req]",
                "distinguished_name=req_dn",
                "x509_extensions=v3",
                "prompt=no",
                "[req_dn]",
                f"CN={hostname}",
                "[v3]",
                f"subjectAltName=DNS:{hostname}",
                "keyUsage=digitalSignature,keyEncipherment,keyCertSign",
                "extendedKeyUsage=serverAuth",
            ]
        ),
        encoding="utf-8",
    )
    key = tmp_path / f"{hostname}.key"
    cert = tmp_path / f"{hostname}.pem"
    subprocess.check_call(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "2",
            "-nodes",
            "-config",
            str(config),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return cert.read_text(encoding="utf-8"), cert, key


def _serve(cert: Path, key: Path, ports: list[int]) -> None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    sock.settimeout(5)
    ports.append(sock.getsockname()[1])
    try:
        conn, _addr = sock.accept()
    except TimeoutError:
        sock.close()
        return
    try:
        tls = context.wrap_socket(conn, server_side=True)
        tls.settimeout(5)
        tls.recv(8)
        tls.close()
    except (OSError, ssl.SSLError):
        conn.close()
    finally:
        sock.close()


def _connect(port: int, pem: str, server_hostname: str) -> None:
    context = build_ssl_context(pem)
    raw = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        tls = context.wrap_socket(raw, server_hostname=server_hostname)
    except Exception:
        raw.close()
        raise
    tls.close()


def test_trust_store_loads_only_the_pinned_cert(tmp_path):
    pem, cert, _key = _cert(tmp_path, "unifi.local")
    if not pem.endswith("\n"):
        pem += "\n"
    der = subprocess.check_output(["openssl", "x509", "-in", str(cert), "-outform", "DER"])
    context = build_ssl_context(pem)
    stats = context.cert_store_stats()
    assert stats["x509"] == 1
    assert context.get_ca_certs(binary_form=True) == [der]
    source = Path("src/skylight_wifi_automation/tls.py").read_text(encoding="utf-8")
    assert "create_default_context" not in source
    assert "load_default_certs" not in source


def test_a_certificate_from_another_ca_is_rejected(tmp_path):
    pinned_dir = tmp_path / "pinned"
    other_dir = tmp_path / "other"
    pinned_dir.mkdir()
    other_dir.mkdir()
    pinned, _pinned_cert, _pinned_key = _cert(pinned_dir, "unifi.local")
    _other_pem, other_cert, other_key = _cert(other_dir, "unifi.local")
    ports: list[int] = []
    thread = threading.Thread(target=_serve, args=(other_cert, other_key, ports), daemon=True)
    thread.start()
    while not ports:
        thread.join(0.05)
    context = build_ssl_context(pinned)
    raw = socket.create_connection(("127.0.0.1", ports[0]), timeout=5)
    with pytest.raises(ssl.SSLCertVerificationError):
        try:
            context.wrap_socket(raw, server_hostname="unifi.local")
        finally:
            raw.close()
    thread.join(5)


def test_ssl_context_keeps_verification_and_clears_only_the_strict_flag(tmp_path):
    pem, _cert_path, _key = _cert(tmp_path, "unifi.local")
    context = build_ssl_context(pem)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    strict = getattr(ssl, "VERIFY_X509_STRICT", 0)
    assert context.verify_flags & strict == 0
    source = Path("src/skylight_wifi_automation/tls.py").read_text(encoding="utf-8")
    client = Path("src/skylight_wifi_automation/unifi.py").read_text(encoding="utf-8")
    assert "CERT_NONE" not in source and "CERT_NONE" not in client
    assert "verify=False" not in source and "verify=False" not in client
    assert "VERIFY_X509_STRICT" in source


def test_hostname_check_accepts_unifi_local_and_rejects_a_different_name(tmp_path):
    pem, cert, key = _cert(tmp_path, "unifi.local")
    text = subprocess.check_output(
        ["openssl", "x509", "-in", str(cert), "-noout", "-text"], text=True
    )
    assert "CA:TRUE" not in text
    assert "Basic Constraints" not in text

    matched: list[int] = []
    thread = threading.Thread(target=_serve, args=(cert, key, matched), daemon=True)
    thread.start()
    while not matched:
        thread.join(0.05)
    _connect(matched[0], pem, "unifi.local")
    thread.join(5)

    rejected: list[int] = []
    thread = threading.Thread(target=_serve, args=(cert, key, rejected), daemon=True)
    thread.start()
    while not rejected:
        thread.join(0.05)
    with pytest.raises(ssl.SSLCertVerificationError):
        _connect(rejected[0], pem, "evil.example")
    thread.join(5)
