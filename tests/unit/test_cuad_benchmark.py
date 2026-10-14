import io
import json
import zipfile

import pytest

from benchmarks.cuad.dataset import load_contracts, verify_offsets
from benchmarks.cuad.mapping import MAPPED_CATEGORIES, RULE_TO_CATEGORIES
from benchmarks.cuad.score import aggregate, score_contract

TEXT = "The Supplier has exclusive rights. Either party may terminate for convenience on notice."
EXCL = (TEXT.index("exclusive rights"), TEXT.index("exclusive rights") + len("exclusive rights"))
TFC = (TEXT.index("terminate for convenience"), TEXT.index("terminate for convenience") + len("terminate for convenience"))


def finding(rule, start, end):
    return {"id": rule, "spans": [{"doc": 0, "start": start, "end": end}]}


def make_zip(contexts):
    data = {"data": [{"title": f"C{i}", "paragraphs": [{"context": ctx, "qas": [
        {"id": f"C{i}__Exclusivity", "question": 'Highlight the parts (if any) of this contract related to "Exclusivity" that should be reviewed by a lawyer. Details: x',
         "answers": [{"text": "exclusive rights", "answer_start": ctx.find("exclusive rights")}] if "exclusive rights" in ctx else [],
         "is_impossible": "exclusive rights" not in ctx},
    ]}]} for i, ctx in enumerate(contexts)]}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("CUAD_v1/CUAD_v1.json", json.dumps(data))
    buf.seek(0)
    return buf


def test_mapping_points_only_at_categories_that_exist_in_the_dataset():
    assert "Change Of Control" in MAPPED_CATEGORIES and "Uncapped Liability" in MAPPED_CATEGORIES
    assert all(rule.startswith("leg-") for rule in RULE_TO_CATEGORIES)
    assert all(cats for cats in RULE_TO_CATEGORIES.values())


def test_loader_reads_contracts_and_their_labeled_spans():
    contracts = load_contracts(make_zip(["Supplier has exclusive rights here.", "Nothing relevant."]))
    assert [c["title"] for c in contracts] == ["C0", "C1"]
    assert contracts[0]["labels"]["Exclusivity"] == [(13, 29)]
    assert contracts[1]["labels"]["Exclusivity"] == []


def test_offsets_that_do_not_match_the_text_are_reported():
    contracts = load_contracts(make_zip(["Supplier has exclusive rights here."]))
    assert verify_offsets(contracts) == 0
    contracts[0]["labels"]["Exclusivity"] = [(0, 5)]
    assert verify_offsets(contracts) == 1


def test_a_finding_that_overlaps_the_labeled_clause_counts_as_detection():
    labels = {"Exclusivity": [EXCL], "Termination For Convenience": [TFC], "Change Of Control": []}
    found = [finding("leg-restrictive", EXCL[0], EXCL[1])]
    s = score_contract(labels, found)
    assert s["categories"]["Exclusivity"] == {"labeled": True, "detected": True}
    assert s["categories"]["Termination For Convenience"] == {"labeled": True, "detected": False}
    assert s["categories"]["Change Of Control"]["labeled"] is False


def test_a_finding_from_the_wrong_rule_does_not_detect_a_category():
    labels = {"Exclusivity": [EXCL]}
    s = score_contract(labels, [finding("leg-tfc", EXCL[0], EXCL[1])])
    assert s["categories"]["Exclusivity"]["detected"] is False


def test_rule_hits_are_split_into_on_label_and_off_label():
    labels = {"Exclusivity": [EXCL]}
    found = [finding("leg-restrictive", EXCL[0], EXCL[1]), finding("leg-restrictive", 60, 70)]
    s = score_contract(labels, found)
    assert s["rules"]["leg-restrictive"] == {"hits": 2, "on_label": 1, "on_any_label": 1}


def test_aggregate_reports_recall_and_precision_with_counts_not_just_rates():
    rows = [
        score_contract({"Exclusivity": [EXCL]}, [finding("leg-restrictive", EXCL[0], EXCL[1])]),
        score_contract({"Exclusivity": [EXCL]}, []),
        score_contract({"Exclusivity": []}, [finding("leg-restrictive", 0, 3)]),
    ]
    agg = aggregate(rows)
    assert agg["categories"]["Exclusivity"] == {"contracts_with_clause": 2, "detected": 1, "recall": 0.5}
    assert agg["rules"]["leg-restrictive"]["hits"] == 2
    assert agg["rules"]["leg-restrictive"]["precision"] == 0.5


def test_an_unobserved_rate_is_none_not_zero():
    agg = aggregate([score_contract({"Exclusivity": []}, [])])
    assert agg["categories"]["Exclusivity"]["recall"] is None
    assert agg["rules"] == {}
