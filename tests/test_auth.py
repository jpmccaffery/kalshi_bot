"""Tests for kalshi_bot.auth."""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from kalshi_bot.auth import auth_headers, sign_request


class TestSignRequest:
    def test_returns_base64_string(self, rsa_private_key):
        sig = sign_request(rsa_private_key, "1700000000000", "GET", "/trade-api/v2/markets")
        decoded = base64.b64decode(sig)
        assert len(decoded) == 256   # RSA-2048 signature is 256 bytes

    def test_signature_verifies(self, rsa_private_key):
        from cryptography.hazmat.primitives.asymmetric import padding as _padding

        timestamp = "1700000000000"
        method    = "POST"
        path      = "/trade-api/v2/portfolio/orders"
        sig_b64   = sign_request(rsa_private_key, timestamp, method, path)

        message   = (timestamp + method + path).encode()
        sig_bytes = base64.b64decode(sig_b64)

        # Should not raise
        rsa_private_key.public_key().verify(
            sig_bytes,
            message,
            _padding.PSS(
                mgf=_padding.MGF1(hashes.SHA256()),
                salt_length=_padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )

    def test_different_inputs_produce_different_signatures(self, rsa_private_key):
        sig1 = sign_request(rsa_private_key, "1000", "GET", "/path/a")
        sig2 = sign_request(rsa_private_key, "1000", "GET", "/path/b")
        assert sig1 != sig2

    def test_method_uppercased(self, rsa_private_key):
        """Lower and upper case method should both produce valid signatures over the same message."""
        from cryptography.hazmat.primitives.asymmetric import padding as _padding

        message = ("1000" + "GET" + "/path").encode()
        for method in ("get", "GET"):
            sig_bytes = base64.b64decode(sign_request(rsa_private_key, "1000", method, "/path"))
            # Should not raise
            rsa_private_key.public_key().verify(
                sig_bytes,
                message,
                _padding.PSS(
                    mgf=_padding.MGF1(hashes.SHA256()),
                    salt_length=_padding.PSS.MAX_LENGTH,
                ),
                hashes.SHA256(),
            )


class TestAuthHeaders:
    def test_returns_required_keys(self, rsa_private_key, key_id):
        headers = auth_headers(rsa_private_key, key_id, "GET", "/trade-api/v2/markets")
        assert set(headers.keys()) == {
            "Kalshi-Access-Key",
            "Kalshi-Access-Signature",
            "Kalshi-Access-Timestamp",
            "Content-Type",
        }

    def test_key_id_in_header(self, rsa_private_key, key_id):
        headers = auth_headers(rsa_private_key, key_id, "GET", "/path")
        assert headers["Kalshi-Access-Key"] == key_id

    def test_timestamp_is_numeric_string(self, rsa_private_key, key_id):
        headers = auth_headers(rsa_private_key, key_id, "GET", "/path")
        assert headers["Kalshi-Access-Timestamp"].isdigit()

    def test_content_type(self, rsa_private_key, key_id):
        headers = auth_headers(rsa_private_key, key_id, "GET", "/path")
        assert headers["Content-Type"] == "application/json"
