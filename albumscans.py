
#!/usr/bin/env python3

import os
import sys
import json
import urllib.request
import urllib.parse
import urllib.error

BASE_URL = "http://192.168.1.4:2283/api"

HOST_ROOT = "/mnt/sea5t/backup/Scans/Abba"
IMMICH_ROOT = "/external/newer/Scans/Abba"

ALBUM_NAME = "Film Scans"
BATCH_SIZE = 500

API_KEY = os.environ.get("IMMICH_API_KEY")

if not API_KEY:
    sys.exit("ERROR: IMMICH_API_KEY is not set")


def api(method, endpoint, data=None):
    url = BASE_URL + endpoint

    body = json.dumps(data).encode() if data is not None else None

    req = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "x-api-key": API_KEY,
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
    )

    try:
        with urllib.request.urlopen(req) as response:
            raw = response.read()
            return json.loads(raw) if raw else None

    except urllib.error.HTTPError as e:
        print(f"\nAPI ERROR {e.code}: {e.read().decode()}")
        raise


def get_assets(folder):
    path = urllib.parse.urlencode({"path": folder})
    result = api("GET", "/view/folder?" + path)

    if isinstance(result, list):
        return result

    if isinstance(result, dict):
        assets = result.get("assets", [])

        if isinstance(assets, dict):
            return assets.get("items", [])

        if isinstance(assets, list):
            return assets

        if isinstance(result.get("items"), list):
            return result["items"]

    raise ValueError(
        f"Unexpected folder response for {folder}: "
        f"{str(result)[:300]}"
    )


def find_images():
    asset_ids = set()
    sample_paths = []
    directories = 0

    print("Scanning directories and querying Immich...\n")

    for root, dirs, files in os.walk(HOST_ROOT):
        relative = os.path.relpath(root, HOST_ROOT)

        immich_folder = IMMICH_ROOT

        if relative != ".":
            immich_folder += "/" + relative.replace(os.sep, "/")

        directories += 1

        assets = get_assets(immich_folder)

        for asset in assets:
            original_path = asset.get("originalPath", "")

            if not original_path.startswith(IMMICH_ROOT + "/"):
                continue

            if asset.get("type") != "IMAGE":
                continue

            asset_id = asset.get("id")

            if asset_id:
                asset_ids.add(asset_id)

                if len(sample_paths) < 10:
                    sample_paths.append(original_path)

    return asset_ids, directories, sample_paths


def find_album():
    albums = api("GET", "/albums")

    if isinstance(albums, dict):
        albums = albums.get("items", [])

    for album in albums:
        if album.get("albumName") == ALBUM_NAME:
            return album

    return None


def main():
    if not os.path.isdir(HOST_ROOT):
        sys.exit(f"Directory not found: {HOST_ROOT}")

    apply = "--apply" in sys.argv

    asset_ids, directories, sample_paths = find_images()

    print(f"Directories checked: {directories}")
    print(f"Images found:        {len(asset_ids)}")
    print("\nSample files:")

    for path in sample_paths:
        print(" ", path)

    print()

    if not asset_ids:
        print("No images found. Nothing to do.")
        return

    album = find_album()

    if album:
        album_id = album["id"]
        print(f"Existing album found: {ALBUM_NAME}")
    else:
        album_id = None
        print(f"Album does not exist yet: {ALBUM_NAME}")

    if not apply:
        print("\nDRY RUN: No changes made.")

        if not album:
            print("The album would be created.")

        print(f"{len(asset_ids)} images would be added.")
        print("\nTo apply, run:")
        print("python3 albumscans.py --apply")
        return

    if not album:
        print("\nCreating album...")

        album = api(
            "POST",
            "/albums",
            {"albumName": ALBUM_NAME}
        )

        album_id = album["id"]

    print(f"\nAdding images to album: {ALBUM_NAME}")

    ids = list(asset_ids)

    successful = 0
    failed = 0
    skipped = 0

    for start in range(0, len(ids), BATCH_SIZE):
        batch = ids[start:start + BATCH_SIZE]

        results = api(
            "PUT",
            f"/albums/{album_id}/assets",
            {"ids": batch}
        )

        if not isinstance(results, list):
            raise ValueError(
                f"Unexpected response when adding batch: {results}"
            )

        for result in results:
            if result.get("success"):
                successful += 1
            else:
                error = str(result.get("error", ""))

                if "duplicate" in error.lower():
                    skipped += 1
                else:
                    failed += 1
                    print(
                        f"Failed asset {result.get('id')}: {error}"
                    )

        print(
            f"Processed {min(start + len(batch), len(ids))}"
            f"/{len(ids)}"
        )

    print("\nFinished.")
    print(f"Successfully added: {successful}")
    print(f"Already present:    {skipped}")
    print(f"Failed:             {failed}")


if __name__ == "__main__":
    main()

