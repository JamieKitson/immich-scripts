#!/usr/bin/env python3
"""Copy missing Huawei P9 photos from the Flickr backup into the main library (name-based only).

Flickr backup files look like  <root>/YYYY-MM/DD-HH-MM-<title>-<flickr id>.<ext>

Date range (from the folder + file name, never the modified time): 2017-04-22 .. 2017-05-10 inclusive.
In that range:

  * IMG-YYYYMMDD-HHMMSS   (P9 camera, e.g. 14-11-09-IMG-20170514-110903-34561553701.jpg)  -> copy
  * IMG-YYYYMMDD-WAnnnn   (WhatsApp)                                                       -> skip
  * Gruffalo-...          (e.g. 23-15-20-Gruffalo-03-20-39-23-04-17-33437788844.png)       -> copy
  * VID-...               none expected                                                    -> WARNING, skipped
  * anything else                                                                          -> skipped, logged

Copies are renamed to  YYYY-MM-DD-HH-MM-<rest of name>. Existing files in the destination are never
overwritten. Dry run unless --apply is given. Keep this next to flickr_recover.py (it shares the
filename pattern).

    ./flickr_recover_p9.py --dest /path/to/library/recovered            # dry run
    ./flickr_recover_p9.py --dest /path/to/library/recovered --apply
"""
import argparse
import csv
import os
import re
import shutil
import sys
from collections import Counter
from datetime import datetime

from flickr_recover import MONTH_RE, NAME_RE

START = datetime(2017, 4, 22, 0, 0)
END = datetime(2017, 5, 10, 23, 59, 59)
MONTHS = {(2017, 4), (2017, 5)}

P9_RE = re.compile(r"^IMG[-_]\d{8}[-_]\d{6}$", re.I)
WA_RE = re.compile(r"^IMG[-_]\d{8}[-_]WA\d+$", re.I)
GRUFFALO_RE = re.compile(r"^Gruffalo\b", re.I)
VID_RE = re.compile(r"^VID\b", re.I)


def decide(path):
    """Return (status, reason, date|None, new_name|None); status is copy/skip/warn."""
    folder = os.path.basename(os.path.dirname(path))
    m, fm = NAME_RE.match(os.path.basename(path)), MONTH_RE.match(folder)
    if not (m and fm):
        return "skip", "not in flickr layout", None, None
    day, hh, mm, title, fid, ext = m.groups()
    try:
        when = datetime(int(fm[1]), int(fm[2]), int(day), int(hh), int(mm))
    except ValueError:
        return "skip", "invalid date in path", None, None
    if not START <= when <= END:
        return "skip", "outside date range", when, None
    if VID_RE.match(title):
        return "warn", "VID file in range (unexpected)", when, None
    if WA_RE.match(title):
        return "skip", "WhatsApp image", when, None
    if P9_RE.match(title):
        reason = "P9 IMG name"
    elif GRUFFALO_RE.match(title):
        reason = "Gruffalo image"
    else:
        return "skip", "other name", when, None
    return "copy", reason, when, f"{when:%Y-%m-%d-%H-%M}-{title}-{fid}.{ext}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--flickr-root", default="/mnt/disk/flickrBackup/jamie/", help="Flickr backup directory")
    ap.add_argument("--dest", required=True, help="Directory to copy the recovered files into")
    ap.add_argument("--apply", action="store_true", help="Actually copy (default is a dry run)")
    ap.add_argument("--report", default="flickr-recover-p9.csv", help="CSV log of every decision")
    args = ap.parse_args()

    if not os.path.isdir(args.flickr_root):
        sys.exit(f"Flickr root not found: {args.flickr_root}")

    files = []
    for entry in sorted(os.listdir(args.flickr_root)):
        fm = MONTH_RE.match(entry)
        d = os.path.join(args.flickr_root, entry)
        if fm and (int(fm[1]), int(fm[2])) in MONTHS and os.path.isdir(d):
            files += [os.path.join(d, f) for f in sorted(os.listdir(d)) if os.path.isfile(os.path.join(d, f))]
    print(f"{len(files)} files in candidate month folders")

    if args.apply:
        os.makedirs(args.dest, exist_ok=True)

    stats = Counter()
    with open(args.report, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["status", "reason", "date", "source", "destination"])
        for path in files:
            decision, reason, when, new_name = decide(path)
            dest = os.path.join(args.dest, new_name) if decision == "copy" else ""
            if decision == "copy" and os.path.exists(dest):
                status, reason = "exists", "destination already exists"
            elif decision == "copy":
                status = "copied" if args.apply else "would_copy"
                if args.apply:
                    try:
                        shutil.copy2(path, dest)
                    except OSError as e:
                        status, reason = "error", str(e)
            elif decision == "warn":
                status = "warning"
                print(f"WARNING: {reason}: {path}")
            else:
                status = "skipped"
            stats[status if status != "skipped" else f"skipped: {reason}"] += 1
            w.writerow([status, reason, when.isoformat(" ") if when else "", path, dest])
            if status in ("copied", "would_copy", "error"):
                print(f"{status}: {path} -> {dest}")

    print()
    for k, v in sorted(stats.items()):
        print(f"{v:6d}  {k}")
    print(f"\nFull log: {args.report}" + ("" if args.apply else "   (dry run; use --apply to copy)"))


if __name__ == "__main__":
    main()
