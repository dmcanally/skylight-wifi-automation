from __future__ import annotations

import ssl

_PEM_BEGIN = "-----BEGIN CERTIFICATE-----"
_PEM_END = "-----END CERTIFICATE-----"


def build_ssl_context(ca_pem: str) -> ssl.SSLContext:
    """Trust only the pinned console PEM and keep hostname checks on.

    The UCG certificate is self-signed and has no basicConstraints extension.
    Python 3.13 turns on VERIFY_X509_STRICT, which rejects that certificate.
    This clears only that flag. Verification and the hostname check stay on.
    """
    if _PEM_BEGIN not in ca_pem or _PEM_END not in ca_pem:
        raise ValueError("unifi_tls_ca must contain a PEM certificate")
    context = ssl.create_default_context()
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    strict = getattr(ssl, "VERIFY_X509_STRICT", 0)
    if strict:
        context.verify_flags &= ~strict
    context.load_verify_locations(cadata=ca_pem)
    return context
