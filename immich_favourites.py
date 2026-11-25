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
import re
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
    ("/mnt/sea5t/JamiePixel3a2", "/external/phones/JamiePixel3a2"),
    ("/mnt/sea5t/JamiePixel7Pro", "/external/phones/JamiePixel7Pro"),
    ("/mnt/sea5t/JamieSurfaceDuo", "/external/phones/JamieSurfaceDuo"),
]


# ---------------------------------------------------------------- helpers
def log(msg, fh=None):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    if fh:
        fh.write(line + "\n")
        fh.flush()


def read_exif(path):
    """Return (wall_clock_time: datetime|None [labelled UTC], (w, h)|None)."""
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
            return dt.replace(tzinfo=timezone.utc), size  # wall-clock time, tz ignored
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
        """All assets whose stored (UTC) time is within +-secs of dt, paged."""
        body = {"takenAfter": iso(dt - timedelta(seconds=secs)),
                "takenBefore": iso(dt + timedelta(seconds=secs)),
                "size": 250, "withExif": True}
        items, page = [], 1
        while page <= 20:
            r = self._req("POST", "/search/metadata", json={**body, "page": page}).json()["assets"]
            items += r["items"]
            if not r.get("nextPage"):
                break
            page = int(r["nextPage"])
        return items

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


RAW_EXT = {".nef", ".nrw", ".cr2", ".cr3", ".crw", ".arw", ".srf", ".sr2", ".dng", ".raf",
           ".orf", ".rw2", ".pef", ".raw", ".3fr", ".erf", ".kdc", ".mrw", ".x3f"}


def pick_best(assets):
    """Choose one asset from equivalent versions of the same picture.
    Prefer jpg over other formats over raw; avoid COVER / _NNNN style names;
    then shorter path; then alphabetical, so the choice is deterministic."""
    def score(a):
        name = a.get("originalFileName") or os.path.basename(a.get("originalPath", ""))
        ext = os.path.splitext(name)[1].lower()
        fmt = 0 if ext in (".jpg", ".jpeg") else 2 if ext in RAW_EXT else 1
        stem = os.path.splitext(name)[0]
        marked = 1 if ("cover" in stem.lower() or re.search(r"_\d{4}(?!\d)", stem)) else 0
        return (fmt, marked, len(a.get("originalPath", "")), a.get("originalPath", ""))
    return min(assets, key=score)


def resolve_tie(found, args, detail, note):
    """Returns (asset_or_list, None) or (None, ambiguous_result)."""
    if args.ties == "all":
        return found, None
    if args.ties == "pick":
        best = pick_best(found)
        detail["picked_from"] = len(found)
        detail["others"] = [a["id"] for a in found if a["id"] != best["id"]]
        return best, None
    return None, ("ambiguous", {**detail, "assets": [a["id"] for a in found], "note": note})


# Flickr backup names look like DD-HH-MM-<original name>-<flickr id>.jpg and live in
# <root>/YYYY-MM/, e.g. 2019-11/05-21-03-IMG-20191105-WA0015-49021479022.jpg
FLICKR_RE = re.compile(r"^(\d\d)-(\d\d)-(\d\d)-.+-\d+\.[A-Za-z0-9]+$")
FLICKR_NAME_RE = re.compile(r"^\d\d-\d\d-\d\d-(.+)-\d+(\.[A-Za-z0-9]+)$")


class FlickrIndex:
    """name -> [relative dirs] for the flickr backup. Crawled lazily (only when a file
    without EXIF needs it) and cached on disk because the drive is slow (USB 2)."""

    def __init__(self, root, cache_path, reindex, logf):
        self.root, self.cache_path, self.reindex, self.logf = root, cache_path, reindex, logf
        self.files = None

    def _load(self):
        if not self.reindex and os.path.exists(self.cache_path):
            try:
                with open(self.cache_path) as f:
                    c = json.load(f)
                if c.get("root") == self.root:
                    age = (time.time() - c["built"]) / 86400
                    self.files = c["files"]
                    log(f"flickr index loaded from {self.cache_path}: {len(self.files)} names, "
                        f"{age:.1f} days old (--reindex-flickr to rebuild)", self.logf)
                    return
            except Exception as e:
                log(f"flickr cache unreadable ({e}), rebuilding", self.logf)
        log(f"crawling {self.root} (slow on USB 2, only done once)...", self.logf)
        t0, n, files = time.time(), 0, defaultdict(list)
        for dp, _dn, fns in os.walk(self.root, onerror=lambda e: log(f"walk error: {e}", self.logf)):
            rel = os.path.relpath(dp, self.root)
            for fn in fns:
                files[fn].append(rel)
                n += 1
                if n % 20000 == 0:
                    log(f"  ...{n} files, {time.time() - t0:.0f}s", self.logf)
        self.files = dict(files)
        tmp = self.cache_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"root": self.root, "built": time.time(), "files": self.files}, f)
        os.replace(tmp, self.cache_path)
        log(f"flickr index built: {n} files in {time.time() - t0:.0f}s, cached to {self.cache_path}",
            self.logf)

    def date_for(self, name):
        """Return (datetime, win, note, path). Prefers the EXIF date of the backup file itself
        (exact, window=None); else the date from its dir + name at HH:MM:30 (win=31s)."""
        m = FLICKR_RE.match(name)
        if not m:
            return None, None, "name is not in flickr backup form", None
        if self.files is None:
            self._load()
        dirs = self.files.get(name, [])
        if not dirs:
            return None, None, "not found in flickr backup", None
        if len(dirs) > 1:
            return None, None, f"found in {len(dirs)} flickr month dirs", None
        path = os.path.join(self.root, dirs[0], name)
        t, _ = read_exif(path)
        if t:
            return t, None, None, path
        ym = re.search(r"(\d{4})-(\d\d)", dirs[0])
        if not ym:
            return None, None, f"flickr dir {dirs[0]!r} is not YYYY-MM", path
        try:
            dt = datetime(int(ym[1]), int(ym[2]), int(m[1]), int(m[2]), int(m[3]), 30,
                          tzinfo=timezone.utc)
        except ValueError:
            return None, None, "invalid date from flickr dir/name", path
        return dt, 31.0, None, path


def process(src, index, im, maps, args):
    """Return (status, detail dict). Statuses: favourited/would_favourite/already_favourite/
    not_in_immich/not_found/ambiguous/no_exif/error."""
    key = os.path.basename(src)
    taken, size = read_exif(src)
    win = args.date_window
    detail = {}

    def lookup(name):
        return index.get(name.lower() if args.ignore_case else name, [])

    # Flickr backup names are DD-HH-MM-<original name>-<flickr id>.<ext>. Look for the
    # original name on disk first; if found, carry on as for any other file.
    hits = []
    m = FLICKR_NAME_RE.match(key)
    if m:
        text, ext = m.group(1), m.group(2)
        variants = []
        # as-is, then Flickr often turns the last "_" into "-" (DSC_3521 -> DSC-3521), then all
        for t in (text, re.sub(r"-(?=[^-]*$)", "_", text), text.replace("-", "_")):
            for e in (ext, ext.lower(), ext.upper()):
                if t + e not in variants:
                    variants.append(t + e)
        for v in variants:
            hits = lookup(v)
            if hits:
                detail["flickr_original_name"] = v
                break
    if not hits:
        hits = lookup(key)
    detail["candidates"] = len(hits)

    def need_date():
        """Capture time of the source. If it has no EXIF and is a flickr backup name, take it
        from the backup (crawled lazily, only when actually needed)."""
        nonlocal taken, win
        if taken is None and args.flickr is not None and FLICKR_RE.match(key):
            t, fwin, fnote, _fpath = args.flickr.date_for(key)
            if t:
                taken = t
                if fwin:  # backup file has no EXIF either: name only gives the minute
                    win = fwin
                    detail["flickr_time"] = t.strftime("%Y-%m-%d %H:%M") + " (from path)"
                else:
                    detail["flickr_time"] = t.strftime("%Y-%m-%d %H:%M:%S") + " (from backup file EXIF)"
            else:
                detail["flickr"] = fnote
        return taken

    def time_assets(t, w):
        """Assets whose wall-clock time (Immich localDateTime) is within w seconds of t.
        Immich stores UTC and we don't know the camera's offset, so fetch a wide window
        (+-max_tz_offset hours) and filter locally on localDateTime."""
        out = []
        for a in im.by_time(t, args.max_tz_offset * 3600 + w):
            s_ = a.get("localDateTime")
            try:
                lt = datetime.fromisoformat(s_.replace("Z", "+00:00"))
            except Exception:
                continue
            if close(lt, t, w):
                out.append(a)
        return out

    def find_by_paths(host_paths):
        """Immich assets for these library files. The per-path lookup is slow (~0.5 s), so
        first try one fast time search using the library file's own EXIF time and pick
        out assets by exact originalPath; anything not found that way uses the path lookup."""
        wanted = list(dict.fromkeys(to_container(h, maps) for h in host_paths))
        found = {}
        if args.time_first:
            t = next((x for x in (read_exif(h)[0] for h in host_paths) if x), None) or taken
            if t is not None:
                for a in time_assets(t, args.date_window):
                    if a.get("originalPath") in wanted:
                        found[a["originalPath"]] = a
        detail["lookup"] = "time" if found and len(found) == len(wanted) else (
            "mixed" if found else "path")
        for p in wanted:
            if p not in found:
                found.update({a["originalPath"]: a for a in im.by_path(p)})
        return [found[p] for p in wanted if p in found]

    asset = None
    how = None

    if len(hits) == 1:
        how = "unique-name"
        found = find_by_paths([hits[0]])
        if found:
            asset = found[0]
        else:
            detail["note"] = f"found on disk ({hits[0]}) but no immich asset at {to_container(hits[0], maps)}"
            hits = []  # fall through to time search

    elif len(hits) > 1:
        if need_date() is None:
            return "no_exif", {**detail, "note": "multiple name matches but source has no EXIF date"}
        found = []
        if args.time_first:
            # No disk reads: one time search, keep assets whose path is one of the candidates.
            cand = {to_container(h, maps) for h in hits}
            found = [a for a in time_assets(taken, win) if a.get("originalPath") in cand]
            detail["lookup"] = "time"
            if not found:
                detail["note"] = "name matches but none in immich at that time, trying time search"
                hits = []
        else:
            good = []
            for h in hits:
                t, _ = read_exif(h)  # slow: opens every candidate file
                if t and close(t, taken, win):
                    good.append(h)
            if not good:
                detail["note"] = "name matches but none have matching EXIF date, trying time search"
                hits = []
            else:
                found = find_by_paths(good)
        if found:
            how = "name+exif"
            if len(found) == 1:
                asset = found[0]
            else:
                asset, res = resolve_tie(found, args, detail,
                                         f"{len(found)} files share name+date")
                if res:
                    return res

    if asset is None and not hits:
        how = "time-search"
        if need_date() is None:
            return "no_exif", {**detail, "note": ((detail["note"] + "; ") if detail.get("note") else "no name match; ") + "no usable date for time search (flickr: " + str(detail.get("flickr")) + ")"}
        items = time_assets(taken, win)
        detail["time_candidates"] = len(items)
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
            asset, res = resolve_tie(items, args, detail,
                                     f"{len(items)} immich assets at that time")
            if res:
                return res
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
    ap.add_argument("--web-url", help="Immich web UI base URL for links in the log "
                    "(default: --url without the trailing /api)")
    ap.add_argument("--source", default="/srv/http/192.168.1.4/resized/")
    ap.add_argument("--search-root", action="append", metavar="HOST=CONTAINER",
                    help="host dir to search and its path inside the immich container; repeatable. "
                         "Default: the five mounts in DEFAULT_MAPS (older, newer, 3 phone dirs)")
    ap.add_argument("--apply", action="store_true", help="actually write to Immich (default: dry run)")
    ap.add_argument("--date-window", type=float, default=1.0, help="EXIF time tolerance in seconds")
    ap.add_argument("--no-time-first", dest="time_first", action="store_false",
                    help="look files up by path only (slow, ~0.5 s each); default is to try a fast "
                         "time search first and fall back to the path lookup")
    ap.add_argument("--max-tz-offset", type=float, default=14.0,
                    help="max camera UTC offset (hours) considered when time-searching")
    ap.add_argument("--flickr-root", default="/mnt/disk/flickrBackup/jamie/",
                    help="flickr backup dir, used to date EXIF-less files named DD-HH-MM-name-id.jpg "
                         "(pass '' to disable)")
    ap.add_argument("--flickr-cache", default="flickr-index.json", help="cache for the flickr crawl")
    ap.add_argument("--reindex-flickr", action="store_true", help="re-crawl the flickr backup")
    ap.add_argument("--ties", choices=["pick", "skip", "all"], default="pick",
                    help="when several assets match equally: pick one (jpg over raw, avoiding "
                         "COVER/_NNNN names; default), skip (log as ambiguous), or favourite all")
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

    web_url = (args.web_url or args.url.rstrip("/").removesuffix("/api")).rstrip("/")
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

    args.flickr = (FlickrIndex(args.flickr_root.rstrip("/") or "/", args.flickr_cache,
                               args.reindex_flickr, logf) if args.flickr_root else None)
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
                if status == "ambiguous":
                    for aid in detail.get("assets", []):
                        log(f"    {web_url}/photos/{aid}", logf)
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

