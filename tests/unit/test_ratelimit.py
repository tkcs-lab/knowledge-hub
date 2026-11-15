from knowledge_hub.ratelimit import RateLimiter


def test_burst_up_to_capacity_then_refused():
    rl = RateLimiter(60)
    assert all(rl.allow("a", 0.0)[0] for _ in range(60))
    ok, retry = rl.allow("a", 0.0)
    assert not ok and retry == 1


def test_refill_over_time():
    rl = RateLimiter(60)  # one token per second
    for _ in range(60):
        rl.allow("a", 0.0)
    assert not rl.allow("a", 0.5)[0]
    assert rl.allow("a", 1.0)[0]
    assert not rl.allow("a", 1.0)[0]


def test_retry_seconds_reflect_the_wait():
    rl = RateLimiter(6)  # one token per ten seconds
    for _ in range(6):
        rl.allow("a", 0.0)
    ok, retry = rl.allow("a", 0.0)
    assert not ok and retry == 10
    assert rl.allow("a", 10.0)[0]


def test_refill_is_capped_at_capacity():
    rl = RateLimiter(3)
    assert rl.allow("a", 0.0)[0]
    results = [rl.allow("a", 10_000.0)[0] for _ in range(4)]
    assert results == [True, True, True, False]


def test_subs_are_isolated():
    rl = RateLimiter(2)
    assert rl.allow("a", 0.0)[0] and rl.allow("a", 0.0)[0] and not rl.allow("a", 0.0)[0]
    assert rl.allow("b", 0.0)[0] and rl.allow("b", 0.0)[0]
