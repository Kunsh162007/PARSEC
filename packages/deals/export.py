"""Marked copies of a deal's documents: each problem highlighted in the text, numbered, and explained underneath.

Each copy is one standalone HTML file (no scripts, styles inline) written into <deal>/_marked/, so it opens in any
browser, prints to PDF, and can be sent on. A problem that spans two documents links to its mark in the other copy.
Every piece of text from the documents and the findings is HTML-escaped.
"""
import re
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any, Dict, List

from packages.deals.workspace import MARKED_DIR, deal_dir, write_inside

SEVERITIES = ("critical", "high", "medium", "low")
LABEL = {"critical": "Critical", "high": "High", "medium": "Medium", "low": "Low"}
LEGEND = ('<ul class="legend" aria-label="What the colours mean">'
          '<li><i style="background:rgba(220,38,38,.17);border-color:#dc2626"></i>Critical</li>'
          '<li><i style="background:rgba(234,88,12,.18);border-color:#ea580c"></i>High</li>'
          '<li><i style="background:rgba(234,179,8,.30);border-color:#ca8a04"></i>Medium</li>'
          '<li><i style="background:rgba(37,99,235,.14);border-color:#2563eb"></i>Low</li>'
          '<li><i style="border-bottom-style:dashed;border-color:#5f6672"></i>Also in another document</li></ul>')

STYLE = """
:root { --ink:#14161b; --ink-3:#5f6672; --rule:#d8dbe2; --paper:#fff;
  --critical:#dc2626; --high:#ea580c; --medium:#ca8a04; --low:#2563eb; }
body { margin:0; background:#f1f2f5; color:var(--ink); font:16px/1.6 Georgia, 'Times New Roman', serif; }
header, main, footer { max-width:860px; margin:0 auto; padding:0 20px; }
header { padding-top:28px; font-family:'Segoe UI', system-ui, sans-serif; }
h1 { font-size:1.5rem; margin:0 0 4px; overflow-wrap:anywhere; } .meta { color:var(--ink-3); margin:0 0 18px; }
.sheet { background:var(--paper); border:1px solid var(--rule); padding:28px 32px; white-space:pre-wrap; overflow-wrap:anywhere; }
mark { background:rgba(234,179,8,.30); border-bottom:3px solid var(--medium); color:inherit; padding:0 1px; }
mark.critical { background:rgba(220,38,38,.17); border-color:var(--critical); }
mark.high { background:rgba(234,88,12,.18); border-color:var(--high); }
mark.low { background:rgba(37,99,235,.14); border-color:var(--low); }
.legend { display:flex; flex-wrap:wrap; gap:6px 16px; margin:0 0 14px; font:13px 'Segoe UI', system-ui, sans-serif; list-style:none; padding:0; }
.legend li { display:inline-flex; align-items:center; gap:6px; }
.legend i { display:inline-block; width:22px; height:11px; border-bottom:3px solid; }
mark.cross { border-bottom-style:dashed; }
sup a { font:700 .7em 'Segoe UI', system-ui, sans-serif; color:var(--ink); text-decoration:none; }
.notes { font-family:'Segoe UI', system-ui, sans-serif; padding-left:0; list-style:none; }
.notes li { border-left:5px solid var(--medium); background:var(--paper); padding:12px 16px; margin:12px 0; }
.notes li.critical { border-color:var(--critical); } .notes li.high { border-color:var(--high); }
.notes li.low { border-color:var(--low); }
.sev { font-weight:700; } .notes p { margin:4px 0; } .where { color:var(--ink-3); }
table { border-collapse:collapse; width:100%; background:var(--paper); font-family:'Segoe UI', system-ui, sans-serif; }
th, td { border:1px solid var(--rule); padding:8px 10px; text-align:left; vertical-align:top; }
footer { color:var(--ink-3); font:13px 'Segoe UI', system-ui, sans-serif; padding:24px 20px 40px; }
@media print { body { background:#fff; } .sheet { border:0; padding:0; } }
"""


def copy_name(document_name: str) -> str:
    """The marked copy's file name: folders flattened with "__", characters Windows refuses replaced."""
    flat = re.sub(r"[\\/]+", "__", document_name.strip())
    flat = re.sub(r'[<>:"|?*\x00-\x1f]', "_", flat).lstrip(".") or "document"
    return f"{flat[:180]}.marked.html"


def _sev(f: Dict[str, Any]) -> str:
    s = f.get("severity")
    return s if s in SEVERITIES else "medium"


def _clean(findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Ids and titles as text, whatever the page sent."""
    return [{**f, "id": str(f.get("id")), "title": str(f.get("title") or "")} for f in findings]


def _anchor(finding_id: str) -> str:
    return "f-" + re.sub(r"[^A-Za-z0-9_-]", "-", str(finding_id))


def _docs_of(finding: Dict[str, Any]) -> List[int]:
    return list(dict.fromkeys(s.get("doc") for s in finding.get("spans", [])))


def _page(title: str, body: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")
    return (f'<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1"><title>{escape(title)}</title>'
            f"<style>{STYLE}</style></head><body>{body}"
            f"<footer>Marked by Fine Print on {stamp}. These marks are points worth asking about, not conclusions; "
            "problems the reviewer removed are not shown.</footer></body></html>")


def _numbered(findings: List[Dict[str, Any]], index: int) -> List[Dict[str, Any]]:
    """The findings with a span in this document, in reading order."""
    here = [f for f in findings if any(s.get("doc") == index for s in f.get("spans", []))]
    return sorted(here, key=lambda f: min(s["start"] for s in f["spans"] if s.get("doc") == index))


def _marked_text(text: str, here: List[Dict[str, Any]], index: int) -> str:
    number = {f["id"]: n for n, f in enumerate(here, start=1)}
    spans = [(max(0, s["start"]), min(len(text), s["end"]), f) for f in here for s in f["spans"]
             if s.get("doc") == index and s["end"] > s["start"]]
    cuts = sorted({0, len(text), *[a for a, _, _ in spans], *[b for _, b, _ in spans]})
    out, anchored = [], set()
    for a, b in zip(cuts, cuts[1:]):
        piece = escape(text[a:b])
        cover = [f for s, e, f in spans if s <= a and e >= b]
        if not cover:
            out.append(piece)
            continue
        top = min(cover, key=lambda f: SEVERITIES.index(_sev(f)))
        cross = any(len(_docs_of(f)) > 1 for f in cover)
        fresh = [f for f in cover if f["id"] not in anchored]
        anchored.update(f["id"] for f in fresh)
        ids = "".join(f' id="{_anchor(f["id"])}"' for f in fresh[:1])
        refs = "".join(f'<sup><a href="#note-{number[f["id"]]}">{number[f["id"]]}</a></sup>' for f in fresh)
        out.append(f'<mark class="{_sev(top)}{" cross" if cross else ""}"{ids}>{piece}</mark>{refs}')
    return "".join(out)


def _note(n: int, f: Dict[str, Any], documents: List[Dict[str, str]], index: int) -> str:
    severity = _sev(f)
    others = [i for i in _docs_of(f) if i != index and 0 <= i < len(documents)]
    where = ""
    if others:
        links = ", ".join(f'<a href="{escape(copy_name(documents[i]["name"]))}#{_anchor(f["id"])}">'
                          f'{escape(documents[i]["name"])}</a>' for i in others)
        where = f'<p class="where">Also marked in {links}: read the passages together.</p>'
    detail = f'<p>{escape(str(f["detail"]))}</p>' if f.get("detail") else ""
    return (f'<li id="note-{n}" class="{severity}"><p><span class="sev">{n}. {LABEL[severity]}:</span> '
            f'{escape(str(f.get("title", "")))}</p>{detail}{where}'
            f'<p><b>Why it matters.</b> {escape(str(f.get("why", "")))}</p>'
            f'<p><b>Ask.</b> {escape(str(f.get("ask", "")))}</p></li>')


def marked_html(documents: List[Dict[str, str]], index: int, findings: List[Dict[str, Any]]) -> str:
    """The marked copy of documents[index]."""
    doc = documents[index]
    findings = _clean(findings)
    here = _numbered(findings, index)
    notes = "".join(_note(n, f, documents, index) for n, f in enumerate(here, start=1))
    body = (f'<header><h1>{escape(doc["name"])}</h1><p class="meta">{len(here)} problem'
            f'{"" if len(here) == 1 else "s"} marked. <a href="summary.html">All documents</a></p>{LEGEND}</header>'
            f'<main><div class="sheet">{_marked_text(doc["text"], here, index)}</div>'
            f'<h2>Problems in this document</h2><ol class="notes">{notes or "<li>None marked.</li>"}</ol></main>')
    return _page(f"{doc['name']} (marked)", body)


def summary_html(deal: str, documents: List[Dict[str, str]], findings: List[Dict[str, Any]]) -> str:
    findings = _clean(findings)
    counts = {s: sum(1 for f in findings if _sev(f) == s) for s in SEVERITIES}
    rows = "".join(
        f'<tr><td><a href="{escape(copy_name(d["name"]))}">{escape(d["name"])}</a></td>'
        f"<td>{len(_numbered(findings, i))}</td></tr>" for i, d in enumerate(documents))
    ranked = sorted(findings, key=lambda f: SEVERITIES.index(_sev(f)))
    items = "".join(
        f'<li class="{_sev(f)}"><p><span class="sev">{LABEL[_sev(f)]}:</span> '
        f'{escape(str(f.get("title", "")))}</p><p class="where">In '
        + ", ".join(f'<a href="{escape(copy_name(documents[i]["name"]))}#{_anchor(f["id"])}">{escape(documents[i]["name"])}</a>'
                    for i in _docs_of(f) if isinstance(i, int) and 0 <= i < len(documents))
        + f'</p><p><b>Ask.</b> {escape(str(f.get("ask", "")))}</p></li>' for f in ranked)
    tally = ", ".join(f"{counts[s]} {s}" for s in SEVERITIES)
    body = (f'<header><h1>{escape(deal)}: marked documents</h1><p class="meta">{len(findings)} problems ({tally}) '
            f"across {len(documents)} documents.</p></header><main><table><tr><th>Document</th><th>Problems</th></tr>"
            f'{rows}</table><h2>Every problem, most serious first</h2><ol class="notes">{items or "<li>None.</li>"}</ol></main>')
    return _page(f"{deal}: marked documents", body)


def write_export(root: Path, deal: str, documents: List[Dict[str, str]], findings: List[Dict[str, Any]]) -> List[str]:
    """Write a marked copy of every document and a summary into <deal>/_marked/. Returns the paths written."""
    folder = deal_dir(root, deal)
    written = []
    for index, doc in enumerate(documents):
        rel = f"{MARKED_DIR}/{copy_name(doc['name'])}"
        write_inside(folder, rel, marked_html(documents, index, findings))
        written.append(rel)
    write_inside(folder, f"{MARKED_DIR}/summary.html", summary_html(deal, documents, findings))
    return [*written, f"{MARKED_DIR}/summary.html"]
