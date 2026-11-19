"""Signed panel tokens."""

from __future__ import annotations

import base64
import json

import pytest

from knowledge_hub.panel.session import Signer

pytestmark = pytest.mark.unit

KEY = b"k" * 32


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def test_round_trip() -> None:
    s = Signer(KEY)
    token = s.sign("session", {"email": "a@x.test"}, 60, 1000.0)
    assert token.count(".") == 1
    data = s.verify("session", token, 1010.0)
    assert data == {"email": "a@x.test", "exp": 1060}


def test_tampered_payload() -> None:
    s = Signer(KEY)
    body, sig = s.sign("session", {"email": "a@x.test"}, 60, 1000.0).split(".")
    evil = b64(json.dumps({"email": "b@x.test", "exp": 9999999999}).encode())
    assert s.verify("session", f"{evil}.{sig}", 1000.0) is None
    assert s.verify("session", f"{body}.{sig}", 1000.0) is not None


def test_tampered_signature() -> None:
    s = Signer(KEY)
    body, sig = s.sign("session", {"email": "a@x.test"}, 60, 1000.0).split(".")
    flipped = ("A" if sig[0] != "A" else "B") + sig[1:]
    assert s.verify("session", f"{body}.{flipped}", 1000.0) is None


def test_wrong_purpose_and_key() -> None:
    s = Signer(KEY)
    token = s.sign("flow", {"state": "s"}, 60, 1000.0)
    assert s.verify("session", token, 1000.0) is None
    assert Signer(b"j" * 32).verify("flow", token, 1000.0) is None


def test_expiry() -> None:
    s = Signer(KEY)
    token = s.sign("session", {"email": "a@x.test"}, 60, 1000.0)
    assert s.verify("session", token, 1059.0) is not None
    assert s.verify("session", token, 1060.0) is None


@pytest.mark.parametrize(
    "token",
    ["", ".", "a", "a.b.c", "!!!.???", "é.é", "e30.e30", b64(b"[1]") + "." + b64(b"x"), "a." + "b" * 5000],
)
def test_garbage_never_raises(token: str) -> None:
    assert Signer(KEY).verify("session", token, 1000.0) is None


def test_non_dict_and_bad_exp_payloads_rejected() -> None:
    s = Signer(KEY)
    for raw in (b"[1]", b'{"exp":"x"}', b'{"exp":true}', b"{}", b"\xff"):
        body = b64(raw)
        assert s.verify("session", f"{body}.{s._mac('session', body)}", 0.0) is None
