"""Behaviour of the thin-then-verify clause pass: selection, citation checks, honest failure.

A fake gateway returns canned replies. Accuracy on real contracts is measured separately by
benchmarks/cuad/verify_run.py; these tests only check the mechanics.
"""
import asyncio
import json
from types import SimpleNamespace

from packages.gateway.llm import GatewayUnavailable
from packages.review.clauses import (
    clause_findings,
    parse_hits,
    segment,
    select_candidates,
    verify_clauses,
    verify_hits,
)

CONTRACT = (
    "1. Term. This Agreement shall automatically renew for successive one-year terms.\n\n"
    "2. Payment. Fees are due within thirty days of invoice.\n\n"
    "3. Assignment. Neither party may assign this Agreement without the prior written consent of the other party.\n\n"
    "4. Notices. Notices shall be sent to the addresses set out above.\n"
)


class FakeGateway:
    def __init__(self, reply=None, fail=False):
        self.reply = reply
        self.fail = fail
        self.prompts = []

    async def complete(self, task, messages, response_schema=None, use_cache=True):
        self.prompts.append(messages[1]["content"])
        if self.fail:
            raise GatewayUnavailable("nvidia/super: no answer text (finish_reason=length)")
        content = self.reply(messages[1]["content"]) if callable(self.reply) else self.reply
        return SimpleNamespace(content=content, usage=SimpleNamespace(total_tokens=100, cost_usd=0.001))


def test_segment_keeps_offsets_that_point_at_the_source():
    for p in segment(CONTRACT):
        assert CONTRACT[p["start"]:p["end"]] == p["text"]


def test_broad_net_keeps_clause_passages_and_drops_unrelated_ones():
    texts = [c["text"] for c in select_candidates(segment(CONTRACT))]
    assert any("automatically renew" in t for t in texts)
    assert any("assign this Agreement" in t for t in texts)
    assert not any(t.startswith("4. Notices") for t in texts)


def test_a_hit_is_kept_only_when_its_quote_is_inside_the_named_passage():
    batch = [{"start": 100, "end": 160, "text": "Neither party may assign this Agreement without consent."}]
    kept, dropped = verify_hits([
        {"id": 0, "category": "anti_assignment", "quote": "Neither party may assign this Agreement"},
        {"id": 0, "category": "anti_assignment", "quote": "Either party may freely assign this Agreement"},
        {"id": 5, "category": "anti_assignment", "quote": "Neither party may assign this Agreement"},
        {"id": 0, "category": "made_up_category", "quote": "Neither party may assign this Agreement"},
    ], batch)
    assert len(kept) == 1 and dropped == 3
    assert kept[0]["start"] == 100 and kept[0]["quote"] == "Neither party may assign this Agreement"


def test_parse_hits_survives_fences_and_a_reply_cut_off_mid_list():
    full = '```json\n{"hits": [{"id": 1, "category": "renewal_term", "quote": "x y z"}]}\n```'
    assert parse_hits(full)[0]["category"] == "renewal_term"
    cut = '{"hits": [{"id": 1, "category": "renewal_term", "quote": "a b c"}, {"id": 2, "categ'
    assert [h["id"] for h in parse_hits(cut)] == [1]


def test_end_to_end_offsets_point_at_the_quoted_words():
    def reply(user):
        lines = [l for l in user.split("\n\n") if "assign this Agreement" in l]
        n = int(lines[0][1:lines[0].index("]")])
        return json.dumps({"hits": [{"id": n, "category": "anti_assignment",
                                     "quote": "Neither party may assign this Agreement without the prior written consent"}]})

    out = asyncio.run(verify_clauses(FakeGateway(reply), CONTRACT))
    hit = out["hits"][0]
    assert CONTRACT[hit["start"]:hit["end"]] == "Neither party may assign this Agreement without the prior written consent"
    assert out["failed_calls"] == 0 and out["candidates"] < out["passages"]


def test_failed_calls_are_counted_with_their_reason_and_produce_no_hits():
    out = asyncio.run(verify_clauses(FakeGateway(fail=True), CONTRACT))
    assert out["hits"] == []
    assert out["failed_calls"] == out["calls"] > 0
    assert "finish_reason=length" in out["errors"][0]


def test_clause_findings_use_the_deep_read_shape_with_document_index():
    reply = json.dumps({"hits": [{"id": 0, "category": "renewal_term", "quote": "This Agreement shall automatically renew"}]})
    docs = [{"name": "a", "text": CONTRACT}, {"name": "b", "text": CONTRACT}]
    out = asyncio.run(clause_findings(FakeGateway(reply), docs))
    assert {f["spans"][0]["doc"] for f in out["findings"]} == {0, 1}
    f = out["findings"][0]
    assert f["lens"] == "legal" and f["title"] == "Renewal term" and f["source"] == "ai"
    assert out["usage"]["calls"] >= 2


def test_confirmation_drops_rejected_tags_and_keeps_tags_without_a_verdict():
    from packages.review.clauses import confirm_hits

    hits = [
        {"category": "anti_assignment", "start": 0, "end": 10, "quote": "a"},
        {"category": "renewal_term", "start": 20, "end": 30, "quote": "b"},
        {"category": "exclusivity", "start": 40, "end": 50, "quote": "c"},
    ]
    reply = '{"verdicts": [{"id": 0, "keep": true}, {"id": 1, "keep": false}]}'
    out = asyncio.run(confirm_hits(FakeGateway(reply), CONTRACT, hits))
    assert [h["category"] for h in out["hits"]] == ["anti_assignment", "exclusivity"]
    assert out["rejected"] == 1 and out["unconfirmed"] == 1


class RecordingGateway(FakeGateway):
    """Also records the system prompt and task of every call."""

    def __init__(self, reply=None, fail=False):
        super().__init__(reply, fail)
        self.systems = []
        self.tasks = []

    async def complete(self, task, messages, response_schema=None, use_cache=True):
        self.systems.append(messages[0]["content"])
        self.tasks.append(task)
        return await super().complete(task, messages, response_schema, use_cache)


def test_prompt_options_are_off_by_default_and_switch_on_when_asked():
    from packages.review.clauses import NOT_THIS, build_system_prompt

    plain = build_system_prompt()
    assert not plain.startswith("/no_think") and "Not this:" not in plain
    assert build_system_prompt(no_think=True).startswith("/no_think\n")
    tuned = build_system_prompt(near_miss=True)
    assert tuned.count("Not this:") == len(NOT_THIS) > 0
    first = next(iter(NOT_THIS))
    assert NOT_THIS[first] in tuned


def test_verify_clauses_passes_the_prompt_options_to_the_model():
    gw = RecordingGateway('{"hits": []}')
    asyncio.run(verify_clauses(gw, CONTRACT, no_think=True, near_miss=True))
    assert gw.systems and all(s.startswith("/no_think") and "Not this:" in s for s in gw.systems)
    gw = RecordingGateway('{"hits": []}')
    asyncio.run(verify_clauses(gw, CONTRACT, no_think=False, near_miss=False))
    assert gw.systems and all(not s.startswith("/no_think") and "Not this:" not in s for s in gw.systems)


def test_defaults_are_the_measured_plain_prompt_because_the_dev_gain_did_not_hold_out():
    import packages.review.clauses as clauses

    assert not clauses.USE_NO_THINK and not clauses.USE_NEAR_MISS and not clauses.USE_GATE
    gw = RecordingGateway('{"hits": []}')
    asyncio.run(verify_clauses(gw, CONTRACT))
    assert gw.systems and all(not s.startswith("/no_think") and "Not this:" not in s for s in gw.systems)


def test_benchmark_prompt_options_follow_the_shipped_defaults_unless_plain():
    import argparse

    from benchmarks.cuad.verify_run import resolve_prompt_options

    ns = lambda **kw: argparse.Namespace(**{"plain_prompt": False, "no_think": False, "near_miss": False, **kw})
    assert resolve_prompt_options(ns()) == (False, False)  # the shipped defaults (both off)
    assert resolve_prompt_options(ns(no_think=True, near_miss=True)) == (True, True)
    assert resolve_prompt_options(ns(plain_prompt=True)) == (False, False)
    assert resolve_prompt_options(ns(plain_prompt=True, near_miss=True)) == (False, True)


GATE_HITS = [
    {"category": "anti_assignment", "start": 0, "end": 10, "quote": "a"},
    {"category": "renewal_term", "start": 20, "end": 30, "quote": "b"},
    {"category": "exclusivity", "start": 40, "end": 50, "quote": "c"},
]


def test_gate_drops_a_no_keeps_a_yes_and_keeps_an_unanswered_item_as_unchecked():
    from packages.review.clauses import GATE_TASK, gate_hits

    gw = RecordingGateway('{"verdicts": [{"id": 0, "keep": true}, {"id": 1, "keep": false}]}')
    out = asyncio.run(gate_hits(gw, CONTRACT, GATE_HITS))
    assert [(h["category"], h["gate"]) for h in out["hits"]] == [("anti_assignment", "yes"), ("exclusivity", "unchecked")]
    assert out["rejected"] == 1 and out["unchecked"] == 1 and out["failed_calls"] == 0
    assert gw.tasks == [GATE_TASK]


def test_gate_failure_keeps_every_tag_as_unchecked_and_counts_the_call():
    from packages.review.clauses import gate_hits

    out = asyncio.run(gate_hits(FakeGateway(fail=True), CONTRACT, GATE_HITS))
    assert [h["category"] for h in out["hits"]] == [h["category"] for h in GATE_HITS]
    assert all(h["gate"] == "unchecked" for h in out["hits"])
    assert out["failed_calls"] == 1 and out["unchecked"] == 3 and out["rejected"] == 0


def test_gate_with_no_hits_makes_no_call():
    from packages.review.clauses import gate_hits

    gw = RecordingGateway('{"verdicts": []}')
    out = asyncio.run(gate_hits(gw, CONTRACT, []))
    assert out["hits"] == [] and gw.tasks == []


def test_clause_findings_apply_the_gate_only_when_it_is_switched_on(monkeypatch):
    import packages.review.clauses as clauses

    def reply(user):
        if user.startswith("[0] Clause type"):
            return '{"verdicts": [{"id": 0, "keep": false}]}'
        return json.dumps({"hits": [{"id": 0, "category": "renewal_term", "quote": "This Agreement shall automatically renew"}]})

    docs = [{"name": "a", "text": CONTRACT}]
    off = asyncio.run(clauses.clause_findings(RecordingGateway(reply), docs))
    assert len(off["findings"]) == 1 and "gate_rejected" not in off["usage"]

    monkeypatch.setattr(clauses, "USE_GATE", True)
    gw = RecordingGateway(reply)
    on = asyncio.run(clauses.clause_findings(gw, docs))
    assert on["findings"] == [] and on["usage"]["gate_rejected"] == 1
    assert clauses.GATE_TASK in gw.tasks


def test_benchmark_output_never_overwrites_an_existing_history_file(tmp_path, monkeypatch):
    import argparse

    import pytest

    from benchmarks.cuad import verify_run

    monkeypatch.setattr(verify_run, "HISTORY", tmp_path)
    (tmp_path / "taken.json").write_text("{}", encoding="utf-8")
    args = argparse.Namespace(out="taken.json", split="dev", confirm=False)
    with pytest.raises(SystemExit):
        verify_run.output_path(args)
    assert verify_run.output_path(argparse.Namespace(out="new.json", split="dev", confirm=False)) == tmp_path / "new.json"


def test_benchmark_sample_draw_is_repeatable_and_exclusion_removes_the_old_sample():
    from benchmarks.cuad.verify_run import draw_sample

    pool = list(range(100))
    first = draw_sample(pool, 11, 30)
    assert first == draw_sample(pool, 11, 30) and len(first) == 30
    rest = [i for i in pool if i not in set(first)]
    assert not set(draw_sample(rest, 12, 30)) & set(first)


def test_benchmark_hit_details_mark_label_overlap_and_gate_verdicts():
    from benchmarks.cuad.verify_run import hit_details

    contracts = [{"title": "t", "labels": {"Renewal Term": [(0, 50)], "Anti-Assignment": [(100, 150)]}}]
    yes = {"category": "renewal_term", "start": 10, "end": 20, "quote": "q1"}
    other = {"category": "renewal_term", "start": 110, "end": 120, "quote": "q2"}
    model = {0: {"hits": [yes, other], "gate": {"hits": [{**yes, "gate": "yes"}]}}}
    rows = hit_details(contracts, [0], model, {0: 42})
    assert [(r["on_own_category"], r["on_any_mapped_label"], r["gate"]) for r in rows] == [
        (True, True, "yes"), (False, True, "no")]
    assert rows[0]["contract"] == 42


def test_official_definitions_cover_every_category_and_switch_the_prompt():
    from packages.review.clauses import CATEGORIES, OFFICIAL_DEFINITIONS, build_system_prompt, definition_of

    assert set(OFFICIAL_DEFINITIONS) == set(CATEGORIES)
    official = build_system_prompt(official=True)
    plain = build_system_prompt()
    for cid, (label, mine, _) in CATEGORIES.items():
        assert f"- {cid}: {label}. {OFFICIAL_DEFINITIONS[cid]}" in official
        assert f"- {cid}: {label}. {mine}" in plain
        assert definition_of(cid, official=True) == OFFICIAL_DEFINITIONS[cid]
        assert definition_of(cid, official=False) == mine


def test_official_definitions_match_the_cuad_question_text_when_the_dataset_is_present():
    import re
    import zipfile

    import pytest

    from benchmarks.cuad.dataset import DEFAULT_ZIP
    from benchmarks.cuad.verify_run import CATEGORY_TO_CUAD
    from packages.review.clauses import OFFICIAL_DEFINITIONS

    if not DEFAULT_ZIP.exists():
        pytest.skip("CUAD zip not downloaded")
    with zipfile.ZipFile(DEFAULT_ZIP) as z:
        name = next(n for n in z.namelist() if n.endswith("CUAD_v1.json"))
        qas = json.loads(z.read(name))["data"][0]["paragraphs"][0]["qas"]
    details = {re.search(r'related to "([^"]+)"', q["question"]).group(1): q["question"].split("Details:", 1)[1]
               for q in qas}
    # Spaces and curly versus straight quotes are the only differences allowed.
    norm = lambda s: re.sub(r"\s+", " ", re.sub("[‘’“”'\"]", "'", s.replace("\xa0", " "))).strip()
    for cid, cuad in CATEGORY_TO_CUAD.items():
        assert norm(OFFICIAL_DEFINITIONS[cid]) == norm(details[cuad]), cid


def test_verify_clauses_sends_official_definitions_only_when_asked():
    from packages.review.clauses import OFFICIAL_DEFINITIONS

    marker = OFFICIAL_DEFINITIONS["anti_assignment"]
    gw = RecordingGateway('{"hits": []}')
    asyncio.run(verify_clauses(gw, CONTRACT, official=True))
    assert gw.systems and all(marker in s for s in gw.systems)
    gw = RecordingGateway('{"hits": []}')
    asyncio.run(verify_clauses(gw, CONTRACT))
    assert gw.systems and all(marker not in s for s in gw.systems)


def _hit(cat, start, end, quote="q"):
    return {"category": cat, "start": start, "end": end, "quote": quote}


def test_voting_keeps_hits_that_two_of_three_passes_agree_on_with_the_first_pass_span():
    from packages.review.clauses import vote_hits

    passes = [
        [_hit("anti_assignment", 10, 40, "first"), _hit("exclusivity", 100, 130)],
        [_hit("anti_assignment", 20, 60, "second"), _hit("renewal_term", 200, 230)],
        [_hit("exclusivity", 300, 330), _hit("renewal_term", 210, 220, "third")],
    ]
    kept = vote_hits(passes, 2)
    assert [(h["category"], h["start"], h["quote"]) for h in kept] == [
        ("anti_assignment", 10, "first"), ("renewal_term", 200, "q")]


def test_voting_needs_the_same_category_not_just_an_overlapping_span():
    from packages.review.clauses import vote_hits

    passes = [[_hit("anti_assignment", 0, 50)], [_hit("change_of_control", 0, 50)], []]
    assert vote_hits(passes, 2) == []
    assert len(vote_hits([[_hit("anti_assignment", 0, 50)]] * 3, 3)) == 1


def test_verify_clauses_with_votes_runs_each_pass_on_the_vote_route_and_keeps_majority_hits():
    from packages.review.clauses import TASK, VOTE_TASK

    calls = {"n": 0}

    def reply(user):
        calls["n"] += 1
        lines = [l for l in user.split("\n\n") if "assign this Agreement" in l]
        if not lines or calls["n"] == 3:  # one of the three passes misses it
            return '{"hits": []}'
        n = int(lines[0][1:lines[0].index("]")])
        return json.dumps({"hits": [{"id": n, "category": "anti_assignment",
                                     "quote": "Neither party may assign this Agreement"}]})

    gw = RecordingGateway(reply)
    single = asyncio.run(verify_clauses(RecordingGateway('{"hits": []}'), CONTRACT))
    out = asyncio.run(verify_clauses(gw, CONTRACT, votes=3))
    assert set(gw.tasks) == {VOTE_TASK} and len(gw.tasks) == 3 * single["calls"]
    assert out["calls"] == 3 * single["calls"] and out["votes"] == 3 and len(out["pass_hits"]) == 3
    assert [h["category"] for h in out["hits"]] == ["anti_assignment"]
    gw1 = RecordingGateway('{"hits": []}')
    asyncio.run(verify_clauses(gw1, CONTRACT, votes=1))
    assert set(gw1.tasks) == {TASK} and "votes" not in single


def test_voting_drops_a_hit_only_one_pass_found():
    calls = {"n": 0}

    def reply(user):
        calls["n"] += 1
        if calls["n"] != 1:
            return '{"hits": []}'
        return json.dumps({"hits": [{"id": 0, "category": "renewal_term",
                                     "quote": "This Agreement shall automatically renew"}]})

    out = asyncio.run(verify_clauses(FakeGateway(reply), CONTRACT, votes=3))
    assert out["hits"] == [] and sum(len(p) for p in out["pass_hits"]) == 1


def test_new_options_are_off_by_default():
    import packages.review.clauses as clauses

    assert not clauses.USE_OFFICIAL_DEFINITIONS and not clauses.USE_VOTING
    gw = RecordingGateway('{"hits": []}')
    asyncio.run(verify_clauses(gw, CONTRACT))
    assert set(gw.tasks) == {clauses.TASK}


def test_vote_route_is_configured_with_a_sampling_temperature():
    from packages.gateway.llm import LLMGateway
    from packages.review.clauses import TASK, VOTE_TASK

    gw = LLMGateway()
    assert gw.route_task(VOTE_TASK)["temperature"] > 0
    assert gw.route_task(VOTE_TASK)["model"] == gw.route_task(TASK)["model"]


def test_benchmark_round7_options_follow_defaults_unless_plain():
    import argparse

    from benchmarks.cuad.verify_run import resolve_round7_options

    ns = lambda **kw: argparse.Namespace(**{"plain_prompt": False, "official_definitions": False, "votes": None, **kw})
    assert resolve_round7_options(ns()) == (False, 1)
    assert resolve_round7_options(ns(official_definitions=True, votes=3)) == (True, 3)
    assert resolve_round7_options(ns(plain_prompt=True)) == (False, 1)


def test_benchmark_chained_exclusion_reproduces_each_earlier_held_out_sample():
    from benchmarks.cuad.verify_run import draw_sample, exclude_samples

    pool = list(range(200))
    first = draw_sample(pool, 11, 30)
    second = draw_sample([i for i in pool if i not in set(first)], 23, 30)
    rest, excluded = exclude_samples(pool, [11, 23], 30)
    assert excluded == sorted(first + second) and len(excluded) == 60
    assert not set(rest) & set(excluded) and len(rest) == 140
    assert exclude_samples(pool, [], 30) == (pool, [])


def test_confirmation_failure_keeps_every_tag_and_says_so():
    from packages.review.clauses import confirm_hits

    hits = [{"category": "anti_assignment", "start": 0, "end": 10, "quote": "a"}]
    out = asyncio.run(confirm_hits(FakeGateway(fail=True), CONTRACT, hits))
    assert out["hits"] == hits
    assert out["failed_calls"] == 1 and out["unconfirmed"] == 1
