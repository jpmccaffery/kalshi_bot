"""
Kalshi API request signing.

Kalshi uses RSA-PSS (PSS padding + SHA-256 digest + MGF1-SHA256).
The message signed is:  timestamp + METHOD + path
where timestamp is milliseconds since epoch as a string.

Headers added to every authenticated request:
    Kalshi-Access-Key           API key ID
    Kalshi-Access-Signature     base64-encoded RSA signature
    Kalshi-Access-Timestamp     millisecond timestamp used in signature
    Content-Type                application/json
"""

from __future__ import annotations

import base64
import time
from pathlib import Path


def load_private_key(path: str | Path):
    """Load an RSA private key from a PEM file."""
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    with open(path, "rb") as f:
        return load_pem_private_key(f.read(), password=None)


def sign_request(private_key, timestamp: str, method: str, path: str) -> str:
    """
    Return a base64-encoded RSA-SHA256 signature for a Kalshi request.

    Parameters
    ----------
    private_key:
        A loaded cryptography RSAPrivateKey object.
    timestamp:
        Millisecond epoch timestamp as a string (e.g. "1700000000000").
    method:
        HTTP method, upper-cased (e.g. "GET", "POST").
    path:
        API path only, no base URL (e.g. "/trade-api/v2/markets/KXINX-24").
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    message = (timestamp + method.upper() + path).encode()
    sig = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.MAX_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode()


def auth_headers(private_key, key_id: str, method: str, path: str) -> dict[str, str]:
    """
    Build the full set of Kalshi authentication headers for a request.

    Parameters
    ----------
    private_key:
        Loaded RSA private key.
    key_id:
        The API key ID from the Kalshi dashboard.
    method:
        HTTP method (e.g. "GET").
    path:
        API path (e.g. "/trade-api/v2/portfolio/balance").
    """
    timestamp = str(int(time.time() * 1000))
    signature = sign_request(private_key, timestamp, method, path)
    return {
        "Kalshi-Access-Key":       key_id,
        "Kalshi-Access-Signature": signature,
        "Kalshi-Access-Timestamp": timestamp,
        "Content-Type":            "application/json",
    }
