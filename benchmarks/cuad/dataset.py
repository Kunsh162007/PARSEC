"""CUAD loader and downloader.

CUAD (Contract Understanding Atticus Dataset) v1: 510 real commercial contracts from SEC EDGAR filings, with
13,823 clause spans labeled by lawyers across 41 categories. License CC BY 4.0, The Atticus Project.
Hendrycks, Burns, Chen, Ball. "CUAD: An Expert-Annotated NLP Dataset for Legal Contract Review", NeurIPS 2021.
DOI 10.5281/zenodo.4595826.

    python -m benchmarks.cuad.dataset --fetch      # download into data/cuad/ and verify the checksum
"""
import argparse
import hashlib
import json
import re
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, BinaryIO, Dict, List, Tuple, Union

ZENODO_RECORD = "https://zenodo.org/api/records/4595826"
DEFAULT_ZIP = Path(__file__).resolve().parents[2] / "data" / "cuad" / "CUAD_v1.zip"
USER_AGENT = "FinePrintBenchmark/1.0"
_CATEGORY = re.compile(r'related to "([^"]+)"')

Contract = Dict[str, Any]


def load_contracts(source: Union[str, Path, BinaryIO]) -> List[Contract]:
    """Contracts as {title, text, labels: {category: [(start, end)]}, label_text: {category: [text]}}."""
    with zipfile.ZipFile(source) as z:
        name = next(n for n in z.namelist() if n.endswith("CUAD_v1.json"))
        data = json.loads(z.read(name))["data"]
    contracts: List[Contract] = []
    for entry in data:
        paragraph = entry["paragraphs"][0]
        labels: Dict[str, List[Tuple[int, int]]] = {}
        label_text: Dict[str, List[str]] = {}
        for qa in paragraph["qas"]:
            match = _CATEGORY.search(qa["question"])
            if not match:
                continue
            category = match.group(1)
            labels.setdefault(category, [])
            label_text.setdefault(category, [])
            for answer in qa["answers"]:
                start = answer["answer_start"]
                labels[category].append((start, start + len(answer["text"])))
                label_text[category].append(answer["text"])
        contracts.append({"title": entry["title"], "text": paragraph["context"], "labels": labels, "label_text": label_text})
    return contracts


def verify_offsets(contracts: List[Contract]) -> int:
    """How many labeled spans do not match the text at their offsets. Should be zero."""
    bad = 0
    for contract in contracts:
        for category, spans in contract["labels"].items():
            for (start, end), expected in zip(spans, contract["label_text"].get(category, [])):
                if contract["text"][start:end] != expected:
                    bad += 1
    return bad


def fetch(dest: Path = DEFAULT_ZIP) -> Path:
    """Download CUAD v1 from Zenodo and verify its MD5 against the value Zenodo publishes."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(ZENODO_RECORD, headers={"User-Agent": USER_AGENT})
    record = json.load(urllib.request.urlopen(request, timeout=60))
    entry = next(f for f in record["files"] if f["key"] == "CUAD_v1.zip")
    expected = entry["checksum"].split(":", 1)[1]

    if dest.exists() and hashlib.md5(dest.read_bytes()).hexdigest() == expected:
        return dest
    download = urllib.request.Request(entry["links"]["self"], headers={"User-Agent": USER_AGENT})
    digest = hashlib.md5()
    with urllib.request.urlopen(download, timeout=600) as response, open(dest, "wb") as out:
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            digest.update(chunk)
            out.write(chunk)
    if digest.hexdigest() != expected:
        dest.unlink(missing_ok=True)
        raise RuntimeError("Downloaded file failed its checksum and was removed. Try again.")
    return dest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch", action="store_true", help="download and verify the dataset")
    args = parser.parse_args()
    if not args.fetch:
        parser.print_help()
        sys.exit(0)
    path = fetch()
    contracts = load_contracts(path)
    print(f"{path}: {len(contracts)} contracts, {verify_offsets(contracts)} label offsets that do not match the text")
