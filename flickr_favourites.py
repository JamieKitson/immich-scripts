#!/usr/bin/env python3
"""
Favourite in Immich the photos you have tagged "myfavs" on Flickr.

For each Flickr photo with the tag:
  1. Get the time it was taken: from the Flickr API (date_taken extra); if Flickr doesn't know
     it, from the Flickr backup on disk (looked up by Flickr ID in flickr-index.json).
  2. Search Immich by that capture time (compared with Immich's localDateTime, see README),
     then narrow by picture shape and, where the Flickr title is still the original file name,
     by file name.
  3. Favourite the asset (only with --apply; default is a dry run).

Progress is appended to a JSONL state file, so an interrupted run resumes where it stopped.
Dry-run and --apply runs keep separate state files.

Flickr access: you need a Flickr API key (https://www.flickr.com/services/apps/create/).
Without OAuth only your public photos are visible to the API. To include private ones, also give
the OAuth key/secret and token/secret (see README).

Setup:   pip install requests pillow          (and requests-oauthlib for private photos)
Usage:   export IMMICH_API_KEY=xxxx FLICKR_API_KEY=yyyy
         ./flickr_favourites.py --flickr-user jamiekitson                  # dry run
         ./flickr_favourites.py --flickr-user jamiekitson --apply          # for real
"""
import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import requests

from immich_favourites import (FlickrIndex, Immich, close, log, pick_best,  # noqa: F401
                               ratio_close, resolve_tie)

API = "https://api.flickr.com/services/rest/"
ID_RE = re.compile(r"-(\d+)\.[A-Za-z0-9]+$")   # backup names end in -<flickr id>.<ext>


# ---------------------------------------------------------------- flickr
class Flickr:
    def __init__(self, key, secret=None, token=None, token_secret=None):
        if token and token_secret and secret:
            try:
                from requests_oauthlib import OAuth1Session
            except ImportError:
                sys.exit("pip install requests-oauthlib to use Flickr OAuth")
            self.s = OAuth1Session(key, client_secret=secret, resource_owner_key=token,
                                   resource_owner_secret=token_secret)
            self.oauth = True
        else:
            self.s = requests.Session()
            self.oauth = False
        self.key = key

    def call(self, method, **params):
        p = {"method": method, "api_key": self.key, "format": "json", "nojsoncallback": 1, **params}
        for attempt in range(4):
            try:
                r = self.s.get(API, params=p, timeout=60)
                r.raise_for_status()
                j = r.json()
                if j.get("stat") != "ok":
                    raise RuntimeError(f"flickr {method}: code {j.get('code')} {j.get('message')}")
                return j
            except (requests.ConnectionError, requests.Timeout, requests.HTTPError):
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)

    def resolve_user(self, user):
        """NSID (12345@N00), 'me' (needs OAuth), profile URL or alias -> NSID or 'me'."""
        if user == "me" or "@" in user:
            return user
        url = user if user.startswith("http") else f"https://www.flickr.com/photos/{user}/"
        return self.call("flickr.urls.lookupUser", url=url)["user"]["id"]

    def tagged(self, user_id, tag, logf):
        out, page = [], 1
        while True:
            r = self.call("flickr.photos.search", user_id=user_id, tags=tag, tag_mode="all",
                          extras="date_taken,original_format,o_dims,tags", per_page=500,
                          page=page, sort="date-posted-asc")["photos"]
            out += r["photo"]
            log(f"flickr: page {page}/{r['pages']}, {len(out)}/{r['total']} photos", logf)
            if page >= int(r["pages"]):
                return out
            page += 1


class Stream:
    """The whole photostream in a fixed order, so neighbours can be found in memory.
    (flickr.photos.getContext also gives prev/next, but only one step per API call.)"""

    KEEP = ("id", "title", "datetaken", "datetakengranularity", "datetakenunknown", "width_o", "height_o")

    def __init__(self, photos):
        self.photos = photos
        self.pos = {str(p["id"]): i for i, p in enumerate(photos)}

    @classmethod
    def load(cls, fl, uid, sort, cache, refresh, logf):
        meta = {"uid": uid, "sort": sort, "oauth": fl.oauth}
        if not refresh and os.path.exists(cache):
            try:
                with open(cache) as f:
                    c = json.load(f)
                if c.get("meta") == meta:
                    log(f"photostream loaded from {cache}: {len(c['photos'])} photos, "
                        f"{(time.time() - c['built']) / 86400:.1f} days old (--refresh-stream to rebuild)",
                        logf)
                    return cls(c["photos"])
            except Exception as e:
                log(f"stream cache unreadable ({e}), rebuilding", logf)
        out, page = [], 1
        while True:
            r = fl.call("flickr.photos.search", user_id=uid, extras="date_taken,o_dims",
                        per_page=500, page=page, sort=sort)["photos"]
            out += [{k: p.get(k) for k in cls.KEEP} for p in r["photo"]]
            log(f"photostream: page {page}/{r['pages']}, {len(out)}/{r['total']} photos", logf)
            if page >= int(r["pages"]):
                break
            page += 1
        with open(cache, "w") as f:
            json.dump({"meta": meta, "built": time.time(), "photos": out}, f)
        return cls(out)


def numeric_title(p):
    """int if the Flickr title is just digits (an untouched scan name like '0045'), else None."""
    m = re.fullmatch(r"\s*(\d+)\s*", re.sub(r"\.[A-Za-z0-9]{2,4}$", "", p.get("title") or ""))
    return int(m.group(1)) if m else None


class BackupById:
    """Date lookup in the flickr backup by Flickr photo ID, using FlickrIndex (cached crawl)."""

    def __init__(self, fi):
        self.fi, self.ids = fi, None

    def date_for_id(self, pid):
        if self.fi.files is None:
            self.fi._load()
        if self.ids is None:
            self.ids = {}
            for name in self.fi.files:
                m = ID_RE.search(name)
                if m:
                    self.ids.setdefault(m.group(1), name)
        name = self.ids.get(str(pid))
        if not name:
            return None, None, "not in flickr backup", None
        return self.fi.date_for(name)


# ---------------------------------------------------------------- core
def norm(name):
    return re.sub(r"[^a-z0-9]", "", re.sub(r"\.[A-Za-z0-9]{2,4}$", "", name or "").lower())


def time_assets(im, t, w, max_tz_offset):
    """Assets whose wall-clock time (Immich localDateTime) is within w seconds of t."""
    out = []
    for a in im.by_time(t, max_tz_offset * 3600 + w):
        try:
            lt = datetime.fromisoformat(a["localDateTime"].replace("Z", "+00:00"))
        except Exception:
            continue
        if close(lt, t, w):
            out.append(a)
    return out


def photo_time(p, backup, detail, args):
    """(datetime labelled UTC, window) for a Flickr photo, or (None, None)."""
    if str(p.get("datetakenunknown", "0")) != "1" and str(p.get("datetakengranularity", "0")) == "0":
        try:
            t = datetime.strptime(p["datetaken"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            detail["time_from"] = "flickr api"
            return t, args.date_window
        except (KeyError, ValueError):
            pass
    detail["flickr_date"] = (f"unknown={p.get('datetakenunknown')} "
                             f"granularity={p.get('datetakengranularity')} value={p.get('datetaken')}")
    if backup is not None:
        t, win, note, _path = backup.date_for_id(p["id"])
        if t:
            detail["time_from"] = "backup path (minute)" if win else "backup file EXIF"
            return t, win or args.date_window
        detail["backup"] = note
    return None, None


def flickr_size(p):
    try:
        return int(p["width_o"]), int(p["height_o"])
    except (KeyError, ValueError, TypeError):
        return None


def asset_dims(a):
    e = a.get("exifInfo") or {}
    w, h = e.get("exifImageWidth"), e.get("exifImageHeight")
    return (w, h) if w and h else None


def candidates_by_time(p, im, backup, args, detail):
    """Immich assets at the photo's capture time with a similar shape. (None, note) if no date."""
    t, win = photo_time(p, backup, detail, args)
    if t is None:
        return None
    detail["time"] = t.strftime("%Y-%m-%d %H:%M:%S")
    items = time_assets(im, t, win, args.max_tz_offset)
    detail["time_candidates"] = len(items)
    if not args.no_ratio:
        size = flickr_size(p)
        items = [a for a in items if ratio_close(size, asset_dims(a))]
    return items


def finish(assets, detail, im, args):
    detail.update(assets=[a["id"] for a in assets], paths=[a.get("originalPath") for a in assets])
    todo = [a for a in assets if not a.get("isFavorite")]
    if not todo:
        return "already_favourite", detail
    if args.apply:
        for a in todo:
            im.favourite(a["id"])
        return "favourited", detail
    return "would_favourite", detail


def via_neighbours(p, stream, im, backup, args, detail):
    """Film scans whose Flickr title was changed: walk the photostream to the nearest photos whose
    title is still just a number, find those in Immich, and work out this photo's original file
    name from the distance. Returns an asset or None (reason in detail['neighbours'])."""
    i = stream.pos.get(str(p["id"]))
    if i is None:
        detail["neighbours"] = "photo not in photostream"
        return None

    def nearest(step):
        for d in range(1, args.neighbour_steps + 1):
            j = i + step * d
            if not 0 <= j < len(stream.photos):
                return None
            n = numeric_title(stream.photos[j])
            if n is not None:
                return stream.photos[j], d, n
        return None

    sign = 1 if args.scan_order == "asc" else -1
    before, after = nearest(-1), nearest(+1)
    guesses = []   # (anchor flickr photo, wanted number)
    if before:
        guesses.append((before[0], before[2] + sign * before[1]))
    if after:
        guesses.append((after[0], after[2] - sign * after[1]))
    if not guesses:
        detail["neighbours"] = f"no numeric title within {args.neighbour_steps} photos either way"
        return None
    if len(guesses) == 2 and guesses[0][1] != guesses[1][1]:
        detail["neighbours"] = (f"neighbours disagree: {before[0]['title']} (-{before[1]}) says "
                                f"{guesses[0][1]}, {after[0]['title']} (+{after[1]}) says {guesses[1][1]}")
        return None
    if len(guesses) == 1 and not args.one_sided:
        detail["neighbours"] = "only one numeric neighbour (use --one-sided to accept)"
        return None
    wanted = guesses[0][1]
    detail["wanted_number"] = wanted
    detail["anchors"] = [q["id"] for q, _ in guesses]

    for q, _n in guesses:
        qn = numeric_title(q)
        cands = candidates_by_time(q, im, backup, args, {}) or []
        anchors = [a for a in cands
                   if re.fullmatch(r"\d+", os.path.splitext(a.get("originalFileName") or "")[0])
                   and int(os.path.splitext(a["originalFileName"])[0]) == qn]
        if len(anchors) != 1:
            detail["neighbours"] = f"anchor {q['title']} ({q['id']}) matched {len(anchors)} immich assets"
            continue
        anchor = anchors[0]
        stem, ext = os.path.splitext(anchor["originalFileName"])
        path = os.path.dirname(anchor["originalPath"]) + "/" + str(wanted).zfill(len(stem)) + ext
        found = im.by_path(path)
        if not found:
            detail["neighbours"] = f"worked out {path} but it is not in immich"
            continue
        if not args.no_ratio and not ratio_close(flickr_size(p), asset_dims(found[0])):
            detail["neighbours"] = f"{path} found but its shape differs from the flickr photo"
            continue
        detail["neighbours"] = f"from {q['title']} ({q['id']}): {path}"
        return found[0]
    return None


def process(p, im, backup, stream, args):
    """Return (status, detail). Statuses: favourited/would_favourite/already_favourite/
    not_found/ambiguous/no_date/error."""
    title = p.get("title", "")
    detail = {"title": title}
    items = candidates_by_time(p, im, backup, args, detail)
    if items is None:
        detail["note"] = "no usable date from flickr or the backup"
    elif items:
        if len(items) > 1 and norm(title):
            named = [a for a in items if norm(a.get("originalFileName")) == norm(title)]
            if named:
                items = named
                detail["name_match"] = True
        if len(items) == 1:
            return finish(items, detail, im, args)
        asset, res = resolve_tie(items, args, detail, f"{len(items)} immich assets at that time")
        return res if res else finish(asset if isinstance(asset, list) else [asset], detail, im, args)

    if stream is not None:
        asset = via_neighbours(p, stream, im, backup, args, detail)
        if asset:
            detail["time_from"] = "photostream neighbours"
            return finish([asset], detail, im, args)
    return ("no_date" if items is None else "not_found"), detail


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("IMMICH_URL", "http://localhost:2283/api"),
                    help="Immich API base URL (ends in /api)")
    ap.add_argument("--api-key", default=os.environ.get("IMMICH_API_KEY"), help="Immich API key")
    ap.add_argument("--web-url", help="Immich web UI base URL for links (default: --url without /api)")
    ap.add_argument("--flickr-key", default=os.environ.get("FLICKR_API_KEY"), help="Flickr API key")
    ap.add_argument("--flickr-secret", default=os.environ.get("FLICKR_API_SECRET"),
                    help="Flickr API secret (OAuth only)")
    ap.add_argument("--flickr-token", default=os.environ.get("FLICKR_OAUTH_TOKEN"),
                    help="Flickr OAuth token (OAuth only)")
    ap.add_argument("--flickr-token-secret", default=os.environ.get("FLICKR_OAUTH_SECRET"),
                    help="Flickr OAuth token secret (OAuth only)")
    ap.add_argument("--flickr-user", required=True,
                    help="your Flickr NSID, alias or profile URL ('me' with OAuth)")
    ap.add_argument("--tag", default="myfavs", help="Flickr tag to favourite (default: myfavs)")
    ap.add_argument("--apply", action="store_true", help="actually write to Immich (default: dry run)")
    ap.add_argument("--date-window", type=float, default=1.0, help="time tolerance in seconds")
    ap.add_argument("--max-tz-offset", type=float, default=14.0,
                    help="max camera UTC offset (hours) considered when time-searching")
    ap.add_argument("--no-ratio", action="store_true", help="don't filter on picture shape")
    ap.add_argument("--no-neighbours", dest="neighbours", action="store_false",
                    help="don't try to work out renamed film scans from neighbouring photos")
    ap.add_argument("--stream-sort", default="date-posted-asc",
                    choices=["date-posted-asc", "date-taken-asc", "date-posted-desc", "date-taken-desc"],
                    help="photostream order used to find neighbours (should match the order the scans "
                         "were uploaded in)")
    ap.add_argument("--scan-order", choices=["asc", "desc"], default="asc",
                    help="do scan numbers increase (asc) or decrease along the stream order?")
    ap.add_argument("--neighbour-steps", type=int, default=50,
                    help="how far to walk to find a photo whose title is just a number")
    ap.add_argument("--one-sided", action="store_true",
                    help="accept a guess from a numeric neighbour on one side only (default needs both "
                         "sides to agree)")
    ap.add_argument("--stream-cache", default="flickr-stream.json", help="cache for the photostream list")
    ap.add_argument("--refresh-stream", action="store_true", help="re-download the photostream list")
    ap.add_argument("--ties", choices=["pick", "skip", "all"], default="pick",
                    help="when several assets match equally: pick one (default), skip, or favourite all")
    ap.add_argument("--flickr-root", default="/mnt/disk/flickrBackup/jamie/",
                    help="flickr backup dir, used when the API has no capture time ('' to disable)")
    ap.add_argument("--flickr-cache", default="flickr-index.json", help="cache for the backup crawl")
    ap.add_argument("--reindex-flickr", action="store_true", help="re-crawl the flickr backup")
    ap.add_argument("--state", help="state file (default: flickr-favourites-<dryrun|apply>.jsonl)")
    ap.add_argument("--restart", action="store_true", help="ignore existing state and start over")
    ap.add_argument("--retry-errors", action="store_true", help="re-process entries that errored")
    ap.add_argument("--stats-every", type=int, default=25, help="print stats every N photos")
    ap.add_argument("--limit", type=int, help="only process the first N photos (for testing)")
    args = ap.parse_args()

    if not args.api_key:
        sys.exit("Set IMMICH_API_KEY or pass --api-key")
    if not args.flickr_key:
        sys.exit("Set FLICKR_API_KEY or pass --flickr-key")

    web_url = (args.web_url or args.url.rstrip("/").removesuffix("/api")).rstrip("/")
    mode = "apply" if args.apply else "dryrun"
    state_path = args.state or f"flickr-favourites-{mode}.jsonl"
    logf = open(f"flickr-favourites-{mode}.log", "a")
    log(f"mode={mode.upper()} tag={args.tag} state={state_path}", logf)

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
        log(f"resuming: {len(done)} photos already processed", logf)
    state = open(state_path, "a" if not args.restart else "w")

    fl = Flickr(args.flickr_key, args.flickr_secret, args.flickr_token, args.flickr_token_secret)
    if not fl.oauth:
        log("no Flickr OAuth: only public photos are visible to the API", logf)
    uid = fl.resolve_user(args.flickr_user)
    photos = fl.tagged(uid, args.tag, logf)
    if args.limit:
        photos = photos[:args.limit]
    total = len(photos)
    log(f"{total} photos tagged {args.tag!r}, {len(done)} already done", logf)

    backup = (BackupById(FlickrIndex(args.flickr_root.rstrip("/") or "/", args.flickr_cache,
                                     args.reindex_flickr, logf)) if args.flickr_root else None)
    stream = None
    if args.neighbours:
        stream = Stream.load(fl, uid, args.stream_sort, args.stream_cache, args.refresh_stream, logf)
    im = Immich(args.url, args.api_key)
    stats = Counter()
    start, n_done = time.time(), 0
    try:
        for i, p in enumerate(photos, 1):
            pid = str(p["id"])
            if pid in done:
                continue
            try:
                status, detail = process(p, im, backup, stream, args)
            except Exception as e:
                status, detail = "error", {"title": p.get("title"), "error": repr(e)}
            stats[status] += 1
            n_done += 1
            state.write(json.dumps({"file": pid, "status": status, **detail}) + "\n")
            state.flush()
            label = f"{pid} {detail.get('title')!r}"
            if status in ("ambiguous", "not_found", "no_date", "error"):
                log(f"[{i}/{total}] {status.upper()} {label} "
                    f"{detail.get('neighbours') or detail.get('note', detail.get('error', detail.get('time', '')))} "
                    f"https://www.flickr.com/photo.gne?id={pid}", logf)
                if status == "ambiguous":
                    for aid in detail.get("assets", []):
                        log(f"    {web_url}/photos/{aid}", logf)
            else:
                log(f"[{i}/{total}] {status} {label} via {detail.get('time_from')}"
                    f"{' +name' if detail.get('name_match') else ''}", logf)
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
