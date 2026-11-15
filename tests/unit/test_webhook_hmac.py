import hashlib
import hmac

from knowledge_hub.auth.webhook_hmac import verify_forgejo_signature

SECRET = b"test-only-secret"
BODY = b'{"ref": "refs/heads/main"}'


def sign(body: bytes, secret: bytes = SECRET) -> str:
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


def test_valid_signature():
    assert verify_forgejo_signature(BODY, sign(BODY), SECRET)
    assert verify_forgejo_signature(BODY, sign(BODY), SECRET.decode())


def test_wrong_secret():
    assert not verify_forgejo_signature(BODY, sign(BODY, b"other"), SECRET)


def test_missing_or_malformed_header():
    assert not verify_forgejo_signature(BODY, None, SECRET)
    assert not verify_forgejo_signature(BODY, "", SECRET)
    assert not verify_forgejo_signature(BODY, "sha256=" + sign(BODY), SECRET)
    assert not verify_forgejo_signature(BODY, "zz" * 32, SECRET)


def test_tampered_body():
    assert not verify_forgejo_signature(BODY + b" ", sign(BODY), SECRET)


def test_empty_secret_never_verifies():
    assert not verify_forgejo_signature(BODY, sign(BODY, b""), b"")
