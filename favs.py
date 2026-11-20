
#!/usr/bin/env python3

import os
import re
import csv
import json
import sys
import time
import urllib.request
from pathlib import Path

IMMICH_URL = "http://192.168.1.4:2283"
SOURCE_DIR = Path("/srv/http/192.168.1.4/resized/")
REPORT = Path("favourite_matches.csv")

API_KEY = os.environ.get("IMMICH_API_KEY")

APPLY = "--apply" in sys.argv


def api_request(endpoint, data):
    url = IMMICH_URL + "/api/" + endpoint

    request = urllib.request.Request(
        url,
        data=json.dumps(data).encode(),
        headers={
            "x-api-key": API_KEY,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def search_immich(filename):
    """Search Immich for a filename and return all results."""

    results = []
    page = 1

    while True:
        response = api_request(
            "search/metadata",
            {
                "originalFileName": filename,
                "type": "IMAGE",
                "page": page,
                "size": 1000,
            }
        )

        assets = response["assets"]
        results.extend(assets["items"])

        if not assets.get("nextPage"):
            break

        page += 1

    return results


def normalise(filename):
    stem = Path(filename).stem.lower()

    # Remove duplicate suffixes such as (1), _2, -3
    stem = re.sub(r"\s*\(\d+\)$", "", stem)
    stem = re.sub(r"[_-]\d+$", "", stem)

    return stem


def favourite(asset_id):
    request = urllib.request.Request(
        IMMICH_URL + "/api/assets",
        data=json.dumps({
            "ids": [asset_id],
            "isFavorite": True,
        }).encode(),
        headers={
            "x-api-key": API_KEY,
            "Content-Type": "application/json",
        },
        method="PUT",
    )

    with urllib.request.urlopen(request, timeout=60):
        pass


def main():
    if not API_KEY:
        sys.exit("Set IMMICH_API_KEY first.")

    files = sorted(
        p for p in SOURCE_DIR.iterdir() if p.is_file()
    )

    print(f"Source files: {len(files)}")

    unique = []
    multiple = []
    missing = []

    rows = []

    for number, source in enumerate(files, 1):

        filename = source.name

        # First try the complete filename.
        results = search_immich(filename)

        # Keep only genuine filename matches.
        matches = [
            a for a in results
            if a["originalFileName"].lower() == filename.lower()
        ]

        match_type = "exact"

        # If no exact match, try normalised filename.
        if not matches:
            key = normalise(filename)

            if key:
                results = search_immich(key)

                matches = [
                    a for a in results
                    if normalise(a["originalFileName"]) == key
                ]

                match_type = "normalised"

        # Remove duplicate asset IDs.
        matches = list({
            a["id"]: a for a in matches
        }.values())

        if len(matches) == 1:
            status = "UNIQUE"
            unique.append((source, matches[0], match_type))

        elif len(matches) > 1:
            status = "MULTIPLE"
            multiple.append((source, matches))

        else:
            status = "NOT FOUND"
            missing.append(source)

        rows.append({
            "source_filename": filename,
            "status": status,
            "match_type": match_type if matches else "",
            "match_count": len(matches),
            "immich_filenames": "; ".join(
                a["originalFileName"] for a in matches
            ),
            "immich_ids": "; ".join(
                a["id"] for a in matches
            ),
        })

        if number % 100 == 0:
            print(f"Processed {number}/{len(files)}")

    # Write CSV report.
    with REPORT.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    print("\n========== RESULTS ==========")
    print(f"Unique matches:  {len(unique)}")
    print(f"Multiple matches: {len(multiple)}")
    print(f"No matches:      {len(missing)}")

    print(f"\nReport saved to: {REPORT.resolve()}")

    if not APPLY:
        print("\nDRY RUN: Nothing changed.")
        return

    print("\nFavouriting unique matches...")

    done = 0
    already = 0

    for source, asset, match_type in unique:

        if asset.get("isFavorite"):
            already += 1
            continue

        favourite(asset["id"])
        done += 1

        if done % 100 == 0:
            print(f"Favourited {done}")

    print("\n========== COMPLETE ==========")
    print(f"New favourites: {done}")
    print(f"Already favourites: {already}")


if __name__ == "__main__":
    main()

