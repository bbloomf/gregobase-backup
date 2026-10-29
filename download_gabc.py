#!/usr/bin/env python3
"""Download every chant's GABC from GregoBase (https://gregobase.selapa.net/).

GregoBase has no bulk export, so this walks chant IDs from 1 upward, reads each
chant page to find its GABC elements, and downloads them with download.php.
Files are written to gabc/<bucket>/<id>.gabc (or <id>.<elem>.gabc for chants
whose page links numbered elements). gabc/index.tsv lists every file with its
chant ID and name, and is rebuilt from the files on disk after each run.

Uses only the Python standard library.
"""

import argparse
import concurrent.futures
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = "https://gregobase.selapa.net/"
USER_AGENT = "gregobase-backup/1.0 (GABC archive script)"

# GregoBase IDs are sparse (deleted chants leave gaps), so keep scanning past
# the highest ID found until a whole batch comes back empty.
BATCH_SIZE = 500

# Seconds to wait before each retry of chants that failed in a batch, so a
# brief network or server outage doesn't lose chants.
RETRY_WAITS = (30, 120, 600)
# When every chant in a batch fails, GregoBase (or our connection) is down;
# keep retrying the batch for about an hour before giving up.
OUTAGE_WAITS = (60, 300) + (600,) * 6

# Chant pages link either "format=gabc" or "format=gabc&elem=N"; follow
# whichever the page uses, since the other form can return an empty file.
GABC_LINK_RE = re.compile(r'download\.php\?id=\d+&amp;format=gabc(?:&amp;elem=(\d+))?"')
TITLE_RE = re.compile(r"<title>GregoBase - (.*?)</title>", re.S)
HEADER_RE = re.compile(r"^([\w-]+):(.*?);?\s*$")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


# GregoBase redirects hidden chants to the score list (and chant 18649 to
# itself) but still sends the chant page with the redirect, so never follow
# redirects; fetch() returns the body that came with them.
OPENER = urllib.request.build_opener(NoRedirect)


def is_transient(error):
    """Whether an error is worth retrying (network or server trouble)."""
    if isinstance(error, urllib.error.HTTPError):
        return error.code >= 500
    return isinstance(error, (urllib.error.URLError, TimeoutError, ConnectionError))


def fetch(url, retries=4):
    """GET a URL, retrying transient failures. Returns (body, headers)."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(retries + 1):
        try:
            with OPENER.open(request, timeout=60) as response:
                return response.read(), response.headers
        except urllib.error.HTTPError as error:
            if 300 <= error.code < 400:
                return error.read(), error.headers
            if not is_transient(error) or attempt == retries:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == retries:
                raise
        time.sleep(2 ** attempt)


def gabc_header(gabc):
    """Header fields (before the %% line) of a GABC file."""
    fields = {}
    for line in gabc.splitlines():
        if line.strip() == "%%":
            break
        match = HEADER_RE.match(line)
        if match:
            fields[match.group(1)] = match.group(2).strip()
    return fields


def run_batch(task, ids, workers):
    """Run task(id) concurrently. Returns ({id: result}, {id: exception})."""
    results, failures = {}, {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(task, i): i for i in ids}
        for future in concurrent.futures.as_completed(futures):
            chant_id = futures[future]
            try:
                results[chant_id] = future.result()
            except Exception as error:  # keep going; scan() retries or reports it
                failures[chant_id] = error
                print(f"  failed id={chant_id}: {error!r}", file=sys.stderr)
    return results, failures


def parse_ids(spec):
    """Parse an ID list like "7737-7760,9001" into a sorted list of ints."""
    ids = set()
    for part in spec.split(","):
        first, _, last = part.strip().partition("-")
        ids.update(range(int(first), int(last or first) + 1))
    return sorted(ids)


def format_ids(ids):
    """Inverse of parse_ids: compress IDs into ranges like "7737-7760,9001"."""
    ranges = []
    for chant_id in sorted(ids):
        if ranges and chant_id == ranges[-1][1] + 1:
            ranges[-1][1] = chant_id
        else:
            ranges.append([chant_id, chant_id])
    return ",".join(f"{a}-{b}" if a != b else str(a) for a, b in ranges)


def add_scan_arguments(parser):
    """Options shared by the scripts that walk chant IDs."""
    parser.add_argument("--start", type=int, default=1, help="first chant ID")
    parser.add_argument("--end", type=int,
                        help="last chant ID (default: scan until a batch of "
                             f"{BATCH_SIZE} IDs comes back empty)")
    parser.add_argument("--ids", type=parse_ids,
                        help='only these chant IDs, e.g. "7737-7760,9001" '
                             "(overrides --start/--end)")
    parser.add_argument("--workers", type=int, default=4,
                        help="concurrent requests (default: 4)")
    parser.add_argument("--delay", type=float, default=0.1,
                        help="seconds each worker waits between requests")


def is_full_run(args):
    return args.ids is None and args.start == 1 and args.end is None


def scan(task, args):
    """Run task(id) over the chant IDs selected by args, retrying failures.

    task returns something truthy for an existing chant and something falsy
    for an unused ID. Without --end or --ids, scanning stops after a batch with
    no chants. Returns ({id: result}, {id: error}, stopped_at), where
    stopped_at is the first ID never tried because GregoBase stayed
    unreachable, or None if the scan finished. Only network and server errors
    are retried; any other error (e.g. a page the parser can't handle) is
    reported straight away.
    """
    if args.ids is not None:
        batches = [args.ids[i:i + BATCH_SIZE] for i in range(0, len(args.ids), BATCH_SIZE)]
    else:
        def ranges():
            batch_start = args.start
            while args.end is None or batch_start <= args.end:
                stop = batch_start + BATCH_SIZE - 1
                if args.end is not None:
                    stop = min(stop, args.end)
                yield range(batch_start, stop + 1)
                batch_start = stop + 1
        batches = ranges()

    results, failures = {}, {}
    for batch in batches:
        print(f"Chants {format_ids(batch)}...", flush=True)
        found, failed = run_batch(task, batch, args.workers)
        permanent = {}

        def set_aside_permanent():
            for chant_id in [i for i, error in failed.items() if not is_transient(error)]:
                permanent[chant_id] = failed.pop(chant_id)

        set_aside_permanent()
        waits = OUTAGE_WAITS if failed and not found and not permanent else RETRY_WAITS
        for attempt, wait in enumerate(waits, 1):
            if not failed:
                break
            print(f"  retrying {len(failed)} failed chants in {wait}s "
                  f"(attempt {attempt} of {len(waits)})", flush=True)
            time.sleep(wait)
            retried, failed = run_batch(task, sorted(failed), args.workers)
            found.update(retried)
            set_aside_permanent()
        failed.update(permanent)
        results.update(found)
        chants = sum(1 for result in found.values() if result)
        print(f"  {chants} chants" + (f", {len(failed)} failed" if failed else ""), flush=True)

        if not found and not permanent:
            # Nothing in the batch succeeded even after an hour of retries:
            # stop rather than burn through the remaining IDs the same way.
            return results, failures, batch[0]
        failures.update(failed)
        if args.ids is None and args.end is None and not chants:
            break  # an empty batch means we're past the last chant
    return results, failures, None


def report_problems(failures, stopped_at, args):
    """Print how to re-fetch what this run missed. Returns the exit status."""
    if not failures and stopped_at is None:
        return 0
    script = Path(sys.argv[0]).name
    if failures:
        print(f"{len(failures)} chants failed even after retries:", file=sys.stderr)
        for chant_id, error in sorted(failures.items()):
            print(f"  {chant_id}: {error!r}", file=sys.stderr)
        print(f"Fetch them with: python3 {script} --ids {format_ids(failures)}",
              file=sys.stderr)
    if stopped_at is not None:
        print(f"Stopped at chant {stopped_at}: GregoBase was unreachable for over an hour.",
              file=sys.stderr)
        if args.ids is not None:
            rest = [i for i in args.ids if i >= stopped_at]
            print(f"Continue with: python3 {script} --ids {format_ids(rest)}", file=sys.stderr)
        else:
            end = f" --end {args.end}" if args.end is not None else ""
            print(f"Continue with: python3 {script} --start {stopped_at}{end}", file=sys.stderr)
    return 1


def chant_path(out_dir, chant_id, elem):
    bucket = f"{chant_id // 1000 * 1000:05d}"
    suffix = f".{elem}" if elem else ""
    return out_dir / bucket / f"{chant_id:05d}{suffix}.gabc"


def download_chant(chant_id, out_dir, delay):
    """Download all GABC elements of one chant. Returns the paths written."""
    page, _ = fetch(f"{BASE_URL}chant.php?id={chant_id}")
    page = page.decode("utf-8", errors="replace")
    if not TITLE_RE.search(page):
        return []  # "Wrong id": deleted or never-used ID

    paths = []
    # If the page has no GABC link at all, still try the plain form.
    elems = set(GABC_LINK_RE.findall(page)) or {""}
    for elem in sorted(elems, key=lambda e: int(e or 0)):
        time.sleep(delay)
        url = f"{BASE_URL}download.php?id={chant_id}&format=gabc"
        if elem:
            url += f"&elem={elem}"
        body, _ = fetch(url)
        if not body:
            continue
        path = chant_path(out_dir, chant_id, elem)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        paths.append(path)
    time.sleep(delay)
    return paths


def write_index(out_dir):
    """Rebuild index.tsv from the .gabc files on disk."""
    rows = []
    for path in out_dir.rglob("*.gabc"):
        chant_id, _, elem = path.stem.partition(".")
        header = gabc_header(path.read_bytes().decode("utf-8", errors="replace"))
        name = " ".join(header.get("name", "").split())
        rows.append((int(chant_id), int(elem or 0), elem, path.relative_to(out_dir).as_posix(), name))

    lines = ["id\telem\tpath\tname"]
    for chant_id, _, elem, rel_path, name in sorted(rows):
        lines.append(f"{chant_id}\t{elem}\t{rel_path}\t{name}")
    (out_dir / "index.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(rows)


def remove_stale(out_dir, results, everything):
    """Delete .gabc files GregoBase no longer has.

    Always removes files of fetched IDs that weren't written this run (deleted
    chants and dropped elements). With everything=True, also removes files of
    any chant not fetched, which is only safe after a complete scan.
    """
    keep = {path for paths in results.values() for path in paths}
    removed = 0
    for path in out_dir.rglob("*.gabc"):
        chant_id = int(path.stem.partition(".")[0])
        if path not in keep and (everything or chant_id in results):
            path.unlink()
            removed += 1
    for bucket in out_dir.iterdir():
        if bucket.is_dir() and not any(bucket.iterdir()):
            bucket.rmdir()
    return removed


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path(__file__).parent / "gabc",
                        help="output directory (default: ./gabc)")
    add_scan_arguments(parser)
    args = parser.parse_args()

    out_dir = args.out.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    results, failures, stopped_at = scan(lambda i: download_chant(i, out_dir, args.delay), args)
    paths = [path for chant_paths in results.values() for path in chant_paths]

    # Only a complete, error-free scan knows the full set of chants.
    complete = is_full_run(args) and not failures and stopped_at is None
    removed = remove_stale(out_dir, results, everything=complete)
    if removed:
        print(f"Removed {removed} files for chants no longer on GregoBase")
    indexed = write_index(out_dir)

    chants = sum(1 for chant_paths in results.values() if chant_paths)
    print(f"Done: {chants} chants, {len(paths)} GABC files downloaded; "
          f"{indexed} files in index.tsv")
    return report_problems(failures, stopped_at, args)


if __name__ == "__main__":
    sys.exit(main())
