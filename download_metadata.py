#!/usr/bin/env python3
"""Download metadata for every GregoBase chant into a CSV file.

Walks chant IDs like download_gabc.py and scrapes each chant page (title,
version, usage, sources, tags, remarks, proofreading, history). Mode and
commentary appear only in the GABC header, so the first GABC element of each
chant is fetched as well. Writes chants.csv, one row per chant; runs over part of the ID range update
their rows in an existing chants.csv.

Uses only the Python standard library.
"""

import argparse
import csv
import html
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from download_gabc import (BASE_URL, GABC_LINK_RE, TITLE_RE, add_scan_arguments, fetch,
                           gabc_header, is_full_run, report_problems, scan)

COLUMNS = [
    "id", "url", "title", "hidden", "version", "usage", "usage_codes", "mode",
    "cantus_id", "sources", "source_ids", "tags", "gabc_elements", "warning",
    "remarks", "commentary", "original_transcriber", "proofread_by", "added",
    "last_modified", "last_modified_by", "history_entries",
]

# Multi-valued fields are joined with this inside a single CSV cell.
SEP = "; "

INFO_RE = re.compile(r'<div id="info">(.*?)<script', re.S)
H3_RE = re.compile(r"<h3>(.*?)</h3>", re.S)
WARNING_RE = re.compile(r'<div id="popup2">(.*?)</div>', re.S)
SECTION_RE = re.compile(r"<h4>([^<]*)</h4>(.*?)(?=<h4>|<hr|$)", re.S)
LI_RE = re.compile(r"<li>(.*?)</li>", re.S)
USAGE_CODE_RE = re.compile(r'class="usage (\w+)"')
SOURCE_BLOCK_RE = re.compile(r'<a name="source_(\d+)">.*?</p>', re.S)
SOURCE_IMG_RE = re.compile(r'src="sources/(\d+)/')
HISTORY_RE = re.compile(r"^(\w{3} \d{2}, \d{4}):\s*(.*)\(([^()]*)\)$", re.S)


def text(fragment):
    """Plain text of an HTML fragment, keeping <br> as line breaks."""
    fragment = re.sub(r"<br\s*/?>", "\n", fragment)
    fragment = html.unescape(re.sub(r"<[^>]+>", "", fragment))
    lines = (" ".join(line.split()) for line in fragment.splitlines())
    return "\n".join(line for line in lines if line)


def iso_date(value):
    return datetime.strptime(value, "%b %d, %Y").date().isoformat()


def chant_metadata(chant_id, delay):
    """Scrape one chant. Returns a CSV row dict, or None if the ID is unused."""
    page, page_headers = fetch(f"{BASE_URL}chant.php?id={chant_id}")
    page = page.decode("utf-8", errors="replace")
    if not TITLE_RE.search(page):
        return None
    info = INFO_RE.search(page).group(1)

    sections = {}
    for heading, body in SECTION_RE.findall(info):
        sections[heading.rstrip(":").strip()] = body

    def items(heading):
        return [text(li) for li in LI_RE.findall(sections.get(heading, ""))]

    title_html = re.sub(r'<span id="push2">.*?</span>', "", H3_RE.search(info).group(1), flags=re.S)
    warning = WARNING_RE.search(info)

    # Source images live under sources/<source id>/, which is the only place
    # the page exposes GregoBase's source IDs.
    source_ids = {}
    for block in SOURCE_BLOCK_RE.finditer(page):
        img = SOURCE_IMG_RE.search(block.group(0))
        source_ids[block.group(1)] = img.group(1) if img else ""
    sources = sections.get("Sources", "")
    source_anchors = re.findall(r'href="#source_(\d+)"', sources)

    history, original_transcriber = [], ""
    for entry in items("History"):
        if entry.startswith("Original transcriber:"):
            original_transcriber = entry.split(":", 1)[1].strip()
            continue
        match = HISTORY_RE.match(entry.removesuffix("?").strip())
        if match:
            date, message, user = match.groups()
            history.append((iso_date(date), message.strip(), user))

    added = [h for h in history if h[1] == "Added to the database"]
    remarks = re.search(r'<p class="remarks">(.*?)</p>', sections.get("Remarks", ""), re.S)

    elems = sorted(set(GABC_LINK_RE.findall(page)), key=lambda e: int(e or 0))
    # Like download_gabc.py, try the plain form when the page links no GABC.
    time.sleep(delay)
    url = f"{BASE_URL}download.php?id={chant_id}&format=gabc"
    if elems and elems[0]:
        url += f"&elem={elems[0]}"
    body, _ = fetch(url)
    header = gabc_header(body.decode("utf-8", errors="replace"))
    if not elems and body:
        elems = [""]
    time.sleep(delay)

    return {
        "id": chant_id,
        "url": f"{BASE_URL}chant.php?id={chant_id}",
        "title": text(title_html),
        # GregoBase sends visitors of hidden chants to the score list.
        "hidden": "yes" if "scores.php" in page_headers.get("Location", "") else "",
        "version": SEP.join(items("Version")),
        "usage": SEP.join(items("Usage")),
        "usage_codes": SEP.join(USAGE_CODE_RE.findall(sections.get("Usage", ""))),
        "mode": header.get("mode", ""),
        "cantus_id": SEP.join(items("Cantus ID")),
        "sources": SEP.join(items("Sources")),
        "source_ids": SEP.join(source_ids.get(a, "") for a in source_anchors),
        "tags": SEP.join(items("Tags")),
        "gabc_elements": len(elems),
        "warning": text(warning.group(1)) if warning else "",
        "remarks": text(remarks.group(1)) if remarks else "",
        "commentary": header.get("commentary", ""),
        "original_transcriber": original_transcriber,
        "proofread_by": SEP.join(items("Proofread by")),
        "added": added[0][0] if added else (history[-1][0] if history else ""),
        "last_modified": history[0][0] if history else "",
        "last_modified_by": history[0][2] if history else "",
        "history_entries": len(history),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path(__file__).parent / "chants.csv",
                        help="output CSV (default: ./chants.csv)")
    add_scan_arguments(parser)
    args = parser.parse_args()

    results, failures, stopped_at = scan(lambda i: chant_metadata(i, args.delay), args)

    # Merge into the existing CSV so a partial run (a range, --ids, or one cut
    # short by an outage) updates the chants it reached and keeps the rest.
    # Only a complete scan replaces the file outright, dropping deleted chants.
    rows = {}
    complete = is_full_run(args) and not failures and stopped_at is None
    if not complete and args.out.exists():
        with open(args.out, encoding="utf-8", newline="") as f:
            rows = {int(row["id"]): row for row in csv.DictReader(f)}
    for chant_id, row in results.items():
        if row:
            rows[chant_id] = row
        else:
            rows.pop(chant_id, None)  # ID is unused now

    with open(args.out, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows[chant_id] for chant_id in sorted(rows))
    found = sum(1 for row in results.values() if row)
    print(f"Done: {found} chants fetched; {len(rows)} chants in {args.out}")
    return report_problems(failures, stopped_at, args)


if __name__ == "__main__":
    sys.exit(main())
