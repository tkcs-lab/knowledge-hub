import asyncio
import base64
import math
from datetime import date

import pytest

from knowledge_hub.embed.base import FakeEmbedder
from knowledge_hub.mcp.errors import CURSOR_FOREIGN, KhError
from knowledge_hub.search.cursor import decode_cursor, encode_cursor

KEY = b"k" * 32
AUD = ["a", "b"]
D = date(2026, 6, 1)


def tok(offset=20):
    return encode_cursor(KEY, "u1", AUD, "q", offset, D)


def test_round_trip():
    assert decode_cursor(KEY, "u1", ["b", "a"], "q", tok()) == (20, D)


def test_tamper():
    t = tok()
    bad = t[:-2] + ("AA" if not t.endswith("AA") else "BB")
    with pytest.raises(KhError) as e:
        decode_cursor(KEY, "u1", AUD, "q", bad)
    assert e.value.code == "CURSOR_FOREIGN" and e.value.text == CURSOR_FOREIGN
    with pytest.raises(KhError):
        decode_cursor(KEY, "u1", AUD, "q", "!!not base64")
    with pytest.raises(KhError):
        decode_cursor(KEY, "u1", AUD, "q", "")


def test_wrong_sub_query_audiences_key():
    with pytest.raises(KhError):
        decode_cursor(KEY, "u2", AUD, "q", tok())
    with pytest.raises(KhError):
        decode_cursor(KEY, "u1", AUD, "other", tok())
    with pytest.raises(KhError):
        decode_cursor(KEY, "u1", ["a"], "q", tok())
    with pytest.raises(KhError):
        decode_cursor(b"x" * 32, "u1", AUD, "q", tok())


def test_offset_above_maximum():
    t = tok(101)
    with pytest.raises(KhError):
        decode_cursor(KEY, "u1", AUD, "q", t)
    assert decode_cursor(KEY, "u1", AUD, "q", t, max_offset=101) == (101, D)


def test_fake_embedder():
    e = FakeEmbedder()
    assert e.model == "fake" and e.dim == 1024 and e.may_embed("x")
    a, b, c = asyncio.run(e.embed(["x", "x", "y"]))
    assert a == b and a != c and len(a) == 1024
    assert math.isclose(sum(v * v for v in a), 1.0, rel_tol=1e-9)


def _raw(t):
    return base64.urlsafe_b64decode(t + "=" * (-len(t) % 4))


def _b64(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def test_date_is_bound():
    raw = _raw(tok())
    other = (D.toordinal() + 1).to_bytes(4, "big")
    with pytest.raises(KhError) as e:
        decode_cursor(KEY, "u1", AUD, "q", _b64(raw[:-4] + other))
    assert e.value.code == "CURSOR_FOREIGN"


def test_old_40_byte_cursor_is_foreign():
    with pytest.raises(KhError) as e:
        decode_cursor(KEY, "u1", AUD, "q", _b64(b"\x00" * 32 + (20).to_bytes(8, "big")))
    assert e.value.code == "CURSOR_FOREIGN"


def test_bad_ordinal_is_foreign():
    raw = _raw(tok())
    with pytest.raises(KhError) as e:
        decode_cursor(KEY, "u1", AUD, "q", _b64(raw[:-4] + b"\xff\xff\xff\xff"))
    assert e.value.code == "CURSOR_FOREIGN"
