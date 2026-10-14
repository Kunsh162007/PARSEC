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
