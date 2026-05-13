"""Shared fixtures for kalshi_bot tests."""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend


@pytest.fixture
def rsa_private_key():
    """Generate a fresh RSA-2048 key for use in tests (no file I/O needed)."""
    return rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend(),
    )


@pytest.fixture
def key_id():
    return "test-key-id-00000000"
