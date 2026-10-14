import asyncio
import json

import httpx
import pytest

from packages.gateway.llm import GatewayUnavailable, LLMGateway


def run(coro):
    return asyncio.run(coro)


def make_gateway(handler, api_key="test-key"):
    gateway = LLMGateway(api_key=api_key, transport=httpx.MockTransport(handler), retry_delay=0)
    return gateway


def ok(content, prompt=11, completion=7):
    return httpx.Response(200, json={
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
    })


def test_without_a_key_it_refuses_instead_of_inventing_an_answer():
    gateway = LLMGateway(api_key="")
    assert gateway.is_configured is False
    with pytest.raises(GatewayUnavailable, match="NEBIUS_API_KEY"):
        run(gateway.complete(task="classification", messages=[{"role": "user", "content": "hi"}]))


def test_a_successful_call_reports_real_usage_and_the_model_that_answered():
    gateway = make_gateway(lambda request: ok('{"flag": true}'))
    res = run(gateway.complete(task="document_triage_json", messages=[{"role": "user", "content": "x"}]))
    assert res.content == '{"flag": true}'
    assert res.usage.prompt_tokens == 11 and res.usage.completion_tokens == 7
    assert res.tier == "nano"
    assert "nano" in res.model_used.lower()


def test_missing_usage_counts_as_zero_not_an_invented_number():
    gateway = make_gateway(lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "hello"}}]}))
    res = run(gateway.complete(task="classification", messages=[{"role": "user", "content": "x"}]))
    assert res.usage.total_tokens == 0
    assert res.usage.cost_usd == 0.0


def test_an_unpriced_model_has_unknown_cost_rather_than_an_assumed_rate():
    gateway = make_gateway(lambda request: ok("x"))
    assert gateway.calculate_cost("vendor/model-nobody-priced", 1000, 1000) == 0.0


def test_the_configured_fallback_model_is_tried_when_the_primary_fails():
    seen = []

    def handler(request):
        model = json.loads(request.content)["model"]
        seen.append(model)
        if "Ultra" in model:
            return httpx.Response(503, json={"error": "overloaded"})
        return ok('{"verdict": "confirm"}')

    gateway = make_gateway(handler)
    res = run(gateway.complete(task="document_adjudication_json", messages=[{"role": "user", "content": "x"}], use_cache=False))
    assert "Ultra" in seen[0] and "Ultra" not in seen[-1]   # primary is retried, then the fallback answers
    assert res.model_used == seen[-1]
    assert res.tier == "super"


def test_when_every_model_fails_the_error_says_so():
    gateway = make_gateway(lambda request: httpx.Response(401, json={"error": "bad key"}))
    with pytest.raises(GatewayUnavailable) as err:
        run(gateway.complete(task="document_review_json", messages=[{"role": "user", "content": "x"}], use_cache=False))
    assert "401" in str(err.value)


def test_a_json_error_body_is_not_mistaken_for_an_answer():
    gateway = make_gateway(lambda request: httpx.Response(200, json={"error": {"message": "rate limited"}}))
    with pytest.raises(GatewayUnavailable):
        run(gateway.complete(task="classification", messages=[{"role": "user", "content": "x"}], use_cache=False))


def test_tokens_and_cost_are_metered_per_tier():
    gateway = make_gateway(lambda request: ok("x", prompt=1_000_000, completion=0))
    run(gateway.complete(task="document_review_json", messages=[{"role": "user", "content": "x"}], use_cache=False))
    assert gateway.total_tokens_super == 1_000_000
    assert gateway.total_cost_usd == pytest.approx(0.40)


def test_a_throttled_request_is_retried_before_giving_up_on_the_model():
    calls = []

    def handler(request):
        calls.append(json.loads(request.content)["model"])
        return httpx.Response(429, json={"error": "slow down"}) if len(calls) < 3 else ok("fine")

    gateway = make_gateway(handler)
    res = run(gateway.complete(task="document_review_json", messages=[{"role": "user", "content": "x"}], use_cache=False))
    assert res.content == "fine"
    assert len(calls) == 3 and len(set(calls)) == 1     # the same (primary) model, three attempts


def test_a_bad_key_is_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(401, json={"error": "bad key"})

    gateway = make_gateway(handler)
    with pytest.raises(GatewayUnavailable):
        run(gateway.complete(task="document_review_json", messages=[{"role": "user", "content": "x"}], use_cache=False))
    assert len(calls) == 2    # once per configured model (primary and fallback), never repeated


def test_retries_are_exhausted_then_the_fallback_model_is_tried():
    seen = []

    def handler(request):
        model = json.loads(request.content)["model"]
        seen.append(model)
        return httpx.Response(503) if "Ultra" in model else ok("answered by fallback")

    gateway = make_gateway(handler)
    res = run(gateway.complete(task="document_adjudication_json", messages=[{"role": "user", "content": "x"}], use_cache=False))
    assert res.content == "answered by fallback"
    assert sum("Ultra" in m for m in seen) == 3         # first try plus two retries


def test_an_empty_answer_is_a_named_failure_and_the_fallback_is_tried():
    import asyncio
    import httpx
    from packages.gateway.llm import LLMGateway

    seen = []

    def handler(request):
        body = request.read().decode()
        seen.append(body)
        if "nemotron-3-super" in body:
            return httpx.Response(200, json={"choices": [{"message": {"content": None}, "finish_reason": "length"}], "usage": {}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "{\"hits\": []}"}, "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 5, "completion_tokens": 3}})

    gw = LLMGateway(api_key="k", transport=httpx.MockTransport(handler), retries=0, retry_delay=0)
    resp = asyncio.run(gw.complete("clause_verification_json", [{"role": "user", "content": "x"}], use_cache=False))
    assert resp.content == '{"hits": []}'
    assert "nano" in resp.model_used.lower()
    assert len(seen) == 2


def test_an_empty_answer_with_no_fallback_left_reports_the_reason():
    import asyncio
    import httpx
    import pytest
    from packages.gateway.llm import GatewayUnavailable, LLMGateway

    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}, "finish_reason": "length"}], "usage": {}})

    gw = LLMGateway(api_key="k", transport=httpx.MockTransport(handler), retries=0, retry_delay=0)
    with pytest.raises(GatewayUnavailable, match="finish_reason=length"):
        asyncio.run(gw.complete("clause_verification_json", [{"role": "user", "content": "x"}], use_cache=False))


def test_a_json_array_reply_keeps_its_content_but_is_not_parsed_as_an_object():
    gateway = make_gateway(lambda request: ok('[{"quote": "x"}]'))
    res = run(gateway.complete(task="document_review_json", messages=[{"role": "user", "content": "x"}], use_cache=False))
    assert res.content == '[{"quote": "x"}]'
    assert res.parsed_json is None


def test_a_non_json_success_body_is_reported_as_unavailable():
    gateway = make_gateway(lambda request: httpx.Response(200, text="<html>gateway error</html>"))
    with pytest.raises(GatewayUnavailable, match="not JSON"):
        run(gateway.complete(task="document_review_json", messages=[{"role": "user", "content": "x"}], use_cache=False))
