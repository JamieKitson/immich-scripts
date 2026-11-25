#!/usr/bin/env python3
"""
Work out which timezone/offset Immich applies to photos, by comparing what Immich stored with
what is in the file's own EXIF. Read-only: nothing is written to Immich.

For a random sample of image assets whose files we can read, it records:
  file:   naive DateTimeOriginal, OffsetTimeOriginal tag (if any), whether it has GPS
  immich: dateTimeOriginal (UTC), timeZone, localDateTime
and derives  immich_offset = localDateTime - dateTimeOriginal.

Then it groups by "what the file offered" (offset tag / GPS only / neither) and shows which
immich_offset values occur in each group, and whether the offset tag always equals immich_offset.

Usage:  export IMMICH_API_KEY=xxxx
        ./tz_probe.py --url http://localhost:2283/api --sample 300
"""
import argparse
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime

import requests
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from immich_favourites import DEFAULT_MAPS  # noqa: E402  (reuse the host/container mounts)

EXIF_IFD, GPS_IFD = 0x8769, 0x8825
T_DTO, T_OFFSET = 36867, 0x9011


def to_host(container_path, maps):
    for host, cont in maps:
        c = cont.rstrip("/")
        if container_path == c or container_path.startswith(c + "/"):
            return host.rstrip("/") + container_path[len(c):]
    return None


def file_info(path):
    """(naive datetime|None, offset_seconds|None, has_gps) from the file's EXIF."""
    try:
        with Image.open(path) as im:
            ex = im.getexif()
            sub = ex.get_ifd(EXIF_IFD)
            raw = sub.get(T_DTO)
            if not raw:
                return None, None, False
            naive = datetime.strptime(str(raw).strip()[:19], "%Y:%m:%d %H:%M:%S")
            off = str(sub.get(T_OFFSET) or "").strip()
            off_s = None
            if len(off) == 6 and off[0] in "+-" and off[3] == ":":
                off_s = (1 if off[0] == "+" else -1) * (int(off[1:3]) * 3600 + int(off[4:6]) * 60)
            gps = ex.get_ifd(GPS_IFD)
            return naive, off_s, bool(gps and gps.get(2))
    except Exception:
        return None, None, False


def parse(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)


def hours(sec):
    return f"{sec / 3600:+.2f}h"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("IMMICH_URL", "http://localhost:2283/api"))
    ap.add_argument("--api-key", default=os.environ.get("IMMICH_API_KEY"))
    ap.add_argument("--sample", type=int, default=300, help="assets to examine")
    ap.add_argument("--show", type=int, default=15, help="example rows to print per group")
    ap.add_argument("--search-root", action="append", metavar="HOST=CONTAINER")
    args = ap.parse_args()
    if not args.api_key:
        sys.exit("Set IMMICH_API_KEY or pass --api-key")
    maps = [tuple(x.split("=", 1)) for x in args.search_root] if args.search_root else DEFAULT_MAPS

    s = requests.Session()
    s.headers.update({"x-api-key": args.api_key})
    base = args.url.rstrip("/")

    rows, seen, tries = [], set(), 0
    while len(rows) < args.sample and tries < 50:
        tries += 1
        r = s.post(base + "/search/random", json={"size": 250, "withExif": True, "type": "IMAGE"})
        r.raise_for_status()
        for a in r.json():
            if a["id"] in seen:
                continue
            seen.add(a["id"])
            e = a.get("exifInfo") or {}
            if not (e.get("dateTimeOriginal") and a.get("localDateTime")):
                continue
            host = to_host(a["originalPath"], maps)
            if not host or not os.path.exists(host):
                continue
            naive, off_s, gps = file_info(host)
            if naive is None:
                continue
            utc, local = parse(e["dateTimeOriginal"]), parse(a["localDateTime"])
            rows.append({
                "name": a["originalFileName"], "naive": naive, "tag": off_s, "gps": gps,
                "utc": utc, "local": local, "tz": e.get("timeZone"),
                "imm_off": int((local - utc).total_seconds()),
            })
            if len(rows) >= args.sample:
                break
    print(f"examined {len(rows)} assets ({tries} random batches)\n")
    if not rows:
        sys.exit("no usable assets: check --search-root and that you run this where the files are mounted")

    bad = [r for r in rows if r["local"] != r["naive"]]
    print(f"localDateTime == file's naive EXIF time: {len(rows) - len(bad)}/{len(rows)}")
    for r in bad[:5]:
        print(f"   differs: {r['name']} file={r['naive']} localDateTime={r['local']}")
    print()

    groups = defaultdict(list)
    for r in rows:
        groups["offset tag" if r["tag"] is not None else "GPS, no tag" if r["gps"] else "neither"].append(r)

    for g, rs in groups.items():
        print(f"=== file has: {g}  ({len(rs)} assets)")
        print("  immich offset (local - stored UTC):",
              ", ".join(f"{hours(k)} x{v}" for k, v in Counter(r['imm_off'] for r in rs).most_common()))
        print("  immich timeZone field:",
              ", ".join(f"{k} x{v}" for k, v in Counter(r['tz'] for r in rs).most_common(8)))
        if g == "offset tag":
            same = sum(1 for r in rs if r["tag"] == r["imm_off"])
            print(f"  offset tag == immich offset: {same}/{len(rs)}")
        print(f"  {'file':28} {'naive EXIF':19} {'tag':>7} {'immich UTC':19} {'tz field':>14} {'offset':>7}")
        for r in rs[:args.show]:
            print(f"  {r['name'][:28]:28} {r['naive']!s:19} "
                  f"{(hours(r['tag']) if r['tag'] is not None else '-'):>7} {r['utc']!s:19} "
                  f"{str(r['tz'])[:14]:>14} {hours(r['imm_off']):>7}")
        print()


if __name__ == "__main__":
    main()
