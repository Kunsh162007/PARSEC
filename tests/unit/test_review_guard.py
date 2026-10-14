from packages.review.guard import ReviewGuard


class Clock:
    def __init__(self, now=1_700_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def test_open_when_no_token_is_configured():
    guard = ReviewGuard(token="")
    assert guard.auth_required is False
    assert guard.token_ok(None) is True
    assert guard.token_ok("anything") is True


def test_token_must_match_when_configured():
    guard = ReviewGuard(token="s3cret-code")
    assert guard.auth_required is True
    assert guard.token_ok("s3cret-code") is True
    assert guard.token_ok("wrong") is False
    assert guard.token_ok(None) is False
    assert guard.token_ok("") is False


def test_rate_limit_allows_n_per_minute_then_blocks_with_retry_after():
    clock = Clock()
    guard = ReviewGuard(per_minute=3, clock=clock)
    assert [guard.rate_ok("1.2.3.4")[0] for _ in range(3)] == [True, True, True]
    ok, retry = guard.rate_ok("1.2.3.4")
    assert ok is False
    assert 1 <= retry <= 60
    clock.now += 61
    assert guard.rate_ok("1.2.3.4")[0] is True


def test_rate_limit_is_per_client():
    guard = ReviewGuard(per_minute=1, clock=Clock())
    assert guard.rate_ok("a")[0] is True
    assert guard.rate_ok("b")[0] is True
    assert guard.rate_ok("a")[0] is False


def test_daily_budget_blocks_until_the_next_day():
    clock = Clock()
    guard = ReviewGuard(daily_budget_usd=0.01, clock=clock)
    assert guard.budget_ok() is True
    guard.record_cost(0.006)
    assert guard.budget_ok() is True
    guard.record_cost(0.006)
    assert guard.budget_ok() is False
    clock.now += 86_400
    assert guard.budget_ok() is True


def test_status_reports_remaining_budget_without_leaking_the_token():
    guard = ReviewGuard(token="abc", daily_budget_usd=1.0, per_minute=5, clock=Clock())
    guard.record_cost(0.25)
    status = guard.status()
    assert status["auth_required"] is True
    assert status["budget_remaining_usd"] == 0.75
    assert status["per_minute"] == 5
    assert "abc" not in str(status)


def test_from_env_reads_configuration(monkeypatch):
    monkeypatch.setenv("REVIEW_API_TOKEN", "t")
    monkeypatch.setenv("REVIEW_RATE_PER_MINUTE", "2")
    monkeypatch.setenv("REVIEW_DAILY_BUDGET_USD", "0.5")
    guard = ReviewGuard.from_env()
    assert guard.auth_required is True
    assert guard.per_minute == 2
    assert guard.daily_budget_usd == 0.5


def test_from_env_ignores_garbage_values(monkeypatch):
    monkeypatch.setenv("REVIEW_RATE_PER_MINUTE", "lots")
    assert ReviewGuard.from_env().per_minute == 6


def test_concurrent_reservations_cannot_overshoot_the_budget():
    guard = ReviewGuard(daily_budget_usd=0.10, clock=Clock())
    first = guard.reserve(0.05)
    second = guard.reserve(0.05)
    assert first is not None and second is not None
    # Both requests are still running and nothing is recorded yet; a third must wait for them.
    assert guard.reserve(0.05) is None
    assert guard.budget_ok() is False


def test_settling_charges_the_actual_cost_and_frees_the_estimate():
    guard = ReviewGuard(daily_budget_usd=0.10, clock=Clock())
    with guard.reserve(0.08) as spend:
        spend.record(0.01)
    assert guard.status()["budget_remaining_usd"] == 0.09
    assert guard.reserve(0.05) is not None


def test_a_failed_call_releases_its_reservation():
    guard = ReviewGuard(daily_budget_usd=0.10, clock=Clock())
    try:
        with guard.reserve(0.10):
            raise RuntimeError("model down")
    except RuntimeError:
        pass
    assert guard.budget_ok() is True
    assert guard.status()["budget_remaining_usd"] == 0.10


def test_reservations_are_safe_across_threads():
    import threading

    guard = ReviewGuard(daily_budget_usd=1.0, clock=Clock())
    granted = []
    barrier = threading.Barrier(50)

    def worker():
        barrier.wait()
        granted.append(guard.reserve(0.1))

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for g in granted if g is not None) == 10


def test_idle_clients_are_pruned_from_the_rate_table():
    clock = Clock()
    guard = ReviewGuard(per_minute=3, clock=clock)
    for n in range(100):
        guard.rate_ok(f"10.0.0.{n}")
    clock.now += 61
    guard.rate_ok("1.1.1.1")
    assert set(guard._hits) == {"1.1.1.1"}
