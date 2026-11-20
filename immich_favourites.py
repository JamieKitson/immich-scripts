#!/usr/bin/env python3
"""
Mark photos as favourites in Immich based on a directory of favourite copies.

For each file in --source:
  1. Look for files with the same name under the library roots (host paths).
       1 hit   -> use that path to find the asset in Immich
       N hits  -> compare EXIF capture time of the source with each hit, use the match
       0 hits  -> search Immich by capture time (+ aspect-ratio tie-break)
  2. Favourite the asset (only with --apply; default is a dry run).

Progress is appended to a JSONL state file so an interrupted run resumes where
it stopped. Dry-run and --apply runs keep separate state files.

Setup:   pip install requests pillow
Usage:   export IMMICH_API_KEY=xxxx
         ./immich_favourites.py --url http://localhost:2283/api            # dry run
         ./immich_favourites.py --url http://localhost:2283/api --apply    # for real
"""
import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import requests
from PIL import Image

EXIF_IFD = 0x8769
T_DATETIME_ORIGINAL = 36867   # 0x9003
T_DATETIME_DIGITIZED = 36868
T_OFFSET_ORIGINAL = 0x9011
T_SUBSEC_ORIGINAL = 0x9291
T_DATETIME = 306

DEFAULT_MAPS = [
    ("/btsync", "/external/older"),
    ("/mnt/sea5t/backup", "/external/newer"),
]


# ---------------------------------------------------------------- helpers
def log(msg, fh=None):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    if fh:
        fh.write(line + "\n")
        fh.flush()


def read_exif(path):
    """Return (taken_utc: datetime|None, (w, h)|None). Naive times are treated as UTC,
    which is what Immich does when a file has no offset tag."""
    try:
        with Image.open(path) as im:
            size = im.size
            ex = im.getexif()
            sub = ex.get_ifd(EXIF_IFD) if hasattr(ex, "get_ifd") else {}
            raw = (sub.get(T_DATETIME_ORIGINAL) or sub.get(T_DATETIME_DIGITIZED)
                   or ex.get(T_DATETIME))
            if not raw:
                return None, size
            dt = datetime.strptime(str(raw).strip()[:19], "%Y:%m:%d %H:%M:%S")
            frac = str(sub.get(T_SUBSEC_ORIGINAL) or "").strip()
            if frac.isdigit():
                dt = dt.replace(microsecond=int((frac + "000000")[:6]))
            off = str(sub.get(T_OFFSET_ORIGINAL) or "").strip()
            tz = timezone.utc
            if len(off) == 6 and off[0] in "+-" and off[3] == ":":
                sign = 1 if off[0] == "+" else -1
                tz = timezone(sign * timedelta(hours=int(off[1:3]), minutes=int(off[4:6])))
            return dt.replace(tzinfo=tz).astimezone(timezone.utc), size
    except Exception:
        return None, None


def close(a, b, secs):
    return abs((a - b).total_seconds()) <= secs


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def ratio_close(s1, s2, tol=0.02):
    if not s1 or not s2:
        return True  # can't tell, don't exclude
    r1, r2 = s1[0] / s1[1], s2[0] / s2[1]
    return abs(r1 - r2) / r1 < tol or abs(r1 - 1 / r2) / r1 < tol  # allow rotation


# ---------------------------------------------------------------- immich
class Immich:
    def __init__(self, url, key):
        self.url = url.rstrip("/")
        self.s = requests.Session()
        self.s.headers.update({"x-api-key": key, "Accept": "application/json"})

    def _req(self, method, path, **kw):
        for attempt in range(4):
            try:
                r = self.s.request(method, self.url + path, timeout=60, **kw)
                if r.status_code >= 500:
                    raise requests.HTTPError(f"{r.status_code} {r.text[:200]}")
                r.raise_for_status()
                return r
            except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)

    def search(self, body):
        body = {"size": 100, "withExif": True, **body}
        r = self._req("POST", "/search/metadata", json=body)
        return r.json()["assets"]["items"]

    def by_path(self, container_path):
        return [a for a in self.search({"originalPath": container_path})
                if a.get("originalPath") == container_path]

    def by_time(self, dt, secs):
        return self.search({"takenAfter": iso(dt - timedelta(seconds=secs)),
                            "takenBefore": iso(dt + timedelta(seconds=secs))})

    def favourite(self, asset_id):
        self._req("PUT", "/assets", json={"ids": [asset_id], "isFavorite": True})


# ---------------------------------------------------------------- core
def to_container(path, maps):
    p = os.path.normpath(path)
    for host, cont in maps:
        h = os.path.normpath(host)
        if p == h or p.startswith(h + os.sep):
            return cont.rstrip("/") + p[len(h):].replace(os.sep, "/")
    raise ValueError(f"no --map covers {path}")


def process(src, index, im, maps, args):
    """Return (status, detail dict). Statuses: favourited/would_favourite/already_favourite/
    not_in_immich/not_found/ambiguous/no_exif/error."""
    key = os.path.basename(src)
    hits = index.get(key.lower() if args.ignore_case else key, [])
    taken, size = read_exif(src)
    detail = {"candidates": len(hits)}
    asset = None
    how = None

    if len(hits) == 1:
        how = "unique-name"
        found = im.by_path(to_container(hits[0], maps))
        if found:
            asset = found[0]
        else:
            detail["note"] = "path not in immich, trying time search"
            hits = []  # fall through to time search

    elif len(hits) > 1:
        if taken is None:
            return "no_exif", {**detail, "note": "multiple name matches but source has no EXIF date"}
        good = []
        for h in hits:
            t, _ = read_exif(h)
            if t and close(t, taken, args.date_window):
                good.append(h)
        if len(good) == 0:
            detail["note"] = "name matches but none have matching EXIF date, trying time search"
            hits = []
        else:
            how = "name+exif"
            found = []
            for h in good:
                found += im.by_path(to_container(h, maps))
            if len(found) == 1:
                asset = found[0]
            elif len(found) > 1:
                if args.ties == "all":
                    asset = found
                else:
                    return "ambiguous", {**detail, "assets": [a["id"] for a in found],
                                         "note": f"{len(found)} files share name+date (use --ties all)"}

    if asset is None and not hits:
        how = "time-search"
        if taken is None:
            return "no_exif", {**detail, "note": "no name match and source has no EXIF date"}
        items = im.by_time(taken, args.date_window)
        def dims(a):
            e = a.get("exifInfo") or {}
            w, h = e.get("exifImageWidth"), e.get("exifImageHeight")
            return (w, h) if w and h else None
        items = [a for a in items if ratio_close(size, dims(a))]
        if not items:
            return "not_found", detail
        if len(items) > 1:
            same_name = [a for a in items if a.get("originalFileName", "").lower() == key.lower()]
            items = same_name or items
        if len(items) > 1:
            if args.ties == "all":
                asset = items
            else:
                return "ambiguous", {**detail, "assets": [a["id"] for a in items],
                                     "note": f"{len(items)} immich assets at that time"}
        else:
            asset = items[0]

    if asset is None:
        return "not_in_immich", detail

    assets = asset if isinstance(asset, list) else [asset]
    detail.update(how=how, assets=[a["id"] for a in assets],
                  paths=[a.get("originalPath") for a in assets])
    todo = [a for a in assets if not a.get("isFavorite")]
    if not todo:
        return "already_favourite", detail
    if args.apply:
        for a in todo:
            im.favourite(a["id"])
        return "favourited", detail
    return "would_favourite", detail


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("IMMICH_URL", "http://localhost:2283/api"),
                    help="Immich API base URL (ends in /api)")
    ap.add_argument("--api-key", default=os.environ.get("IMMICH_API_KEY"))
    ap.add_argument("--source", default="/srv/http/192.168.1.4/resized/")
    ap.add_argument("--search-root", action="append", metavar="HOST=CONTAINER",
                    help="host dir to search and its path inside the immich container; repeatable. "
                         "Default: /btsync=/external/older, /mnt/sea5t/backup=/external/newer")
    ap.add_argument("--apply", action="store_true", help="actually write to Immich (default: dry run)")
    ap.add_argument("--date-window", type=float, default=1.0, help="EXIF time tolerance in seconds")
    ap.add_argument("--ties", choices=["skip", "all"], default="skip",
                    help="when several assets match equally: skip (log) or favourite all")
    ap.add_argument("--ignore-case", action="store_true", help="case-insensitive filename matching")
    ap.add_argument("--state", help="state file (default: favourites-<dryrun|apply>.jsonl)")
    ap.add_argument("--restart", action="store_true", help="ignore existing state and start over")
    ap.add_argument("--retry-errors", action="store_true", help="re-process entries that errored")
    ap.add_argument("--stats-every", type=int, default=25, help="print stats every N files")
    ap.add_argument("--limit", type=int, help="only process the first N files (for testing)")
    args = ap.parse_args()

    if not args.api_key:
        sys.exit("Set IMMICH_API_KEY or pass --api-key")
    maps = DEFAULT_MAPS
    if args.search_root:
        maps = [tuple(x.split("=", 1)) for x in args.search_root]

    mode = "apply" if args.apply else "dryrun"
    state_path = args.state or f"favourites-{mode}.jsonl"
    logf = open(f"favourites-{mode}.log", "a")
    log(f"mode={mode.upper()} source={args.source} state={state_path}", logf)

    done = {}
    if os.path.exists(state_path) and not args.restart:
        with open(state_path) as f:
            for line in f:
                try:
                    r = json.loads(line)
                    done[r["file"]] = r["status"]
                except Exception:
                    pass
        if args.retry_errors:
            done = {k: v for k, v in done.items() if v != "error"}
        log(f"resuming: {len(done)} files already processed", logf)
    state = open(state_path, "a" if not args.restart else "w")

    log("indexing library roots (one-off walk)...", logf)
    t0 = time.time()
    index = defaultdict(list)
    for host, _ in maps:
        n = 0
        for dp, _dn, fns in os.walk(host, onerror=lambda e: log(f"walk error: {e}", logf)):
            for fn in fns:
                index[fn.lower() if args.ignore_case else fn].append(os.path.join(dp, fn))
                n += 1
        log(f"  {host}: {n} files", logf)
    log(f"index built in {time.time() - t0:.0f}s ({len(index)} distinct names)", logf)

    files = sorted(f for f in os.listdir(args.source)
                   if os.path.isfile(os.path.join(args.source, f)))
    if args.limit:
        files = files[:args.limit]
    total = len(files)
    log(f"{total} source files, {len(done)} to skip", logf)

    im = Immich(args.url, args.api_key)
    stats = Counter()
    start, n_done = time.time(), 0
    try:
        for i, fn in enumerate(files, 1):
            if fn in done:
                continue
            src = os.path.join(args.source, fn)
            try:
                status, detail = process(src, index, im, maps, args)
            except Exception as e:
                status, detail = "error", {"error": repr(e)}
            stats[status] += 1
            n_done += 1
            state.write(json.dumps({"file": fn, "status": status, **detail}) + "\n")
            state.flush()
            if status in ("ambiguous", "not_found", "not_in_immich", "no_exif", "error"):
                log(f"[{i}/{total}] {status.upper()} {fn} {detail.get('note', detail.get('error', ''))}", logf)
            else:
                log(f"[{i}/{total}] {status} {fn} via {detail.get('how')}", logf)
            if n_done % args.stats_every == 0:
                rate = n_done / (time.time() - start)
                remaining = total - len(done) - n_done
                eta = timedelta(seconds=int(remaining / rate)) if rate else "?"
                log(f"STATS {dict(stats)} | {rate:.1f}/s | ETA {eta}", logf)
    except KeyboardInterrupt:
        log("interrupted - re-run the same command to resume", logf)

    log(f"FINISHED this run: {dict(stats)}", logf)
    if not args.apply:
        log("dry run: nothing was written to Immich. Re-run with --apply.", logf)


if __name__ == "__main__":
    main()

