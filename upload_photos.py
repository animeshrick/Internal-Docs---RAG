"""
upload_photo_one.py - upload one employee photograph to Supabase and link it.

For a single new joiner, instead of re-running upload_photos.py over everybody.

    python upload_photo_one.py EMP-0001 testuser-0001.png --dry-run
    python upload_photo_one.py EMP-0001 testuser-0001.png

Reads SUPABASE_URL and SUPABASE_SECRET_KEY from .env in the current folder.
The photo is looked for in photos/ unless you give a full path.
Uploads to employee-photos/<EMP-ID>/photo.<ext> and sets employee.image.
Running it again for the same employee replaces their photo.
"""
import argparse
import os
import sys
from pathlib import Path

import requests

BUCKET = "employee-photos"
PHOTOS_DIR = Path("photos")
DEFAULT_URL = "https://qeayqzfuowdvhwntlyhh.supabase.co"
TIMEOUT = 60


def load_env(path=Path(".env")):
    """Read KEY=VALUE lines from .env, without overriding anything already in the environment."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def get_credentials():
    load_env()
    url = os.environ.get("SUPABASE_URL", DEFAULT_URL).rstrip("/")
    for name in ("SUPABASE_SECRET_KEY", "SUPABASE_SERVICE_KEY", "SUPABASE_SERVICE_ROLE_KEY"):
        if os.environ.get(name):
            return url, os.environ[name], name
    sys.exit("No service key found. Put SUPABASE_SECRET_KEY in .env.")


def stored_extension(file_name):
    """What the object is saved as. .jpeg is normalised to .jpg to match the existing photos."""
    ext = Path(file_name).suffix.lower()
    if ext in (".jpg", ".jpeg"):
        return ".jpg", "image/jpeg"
    if ext == ".png":
        return ".png", "image/png"
    if ext == ".webp":
        return ".webp", "image/webp"
    sys.exit(f"Unsupported image type: {ext or '(none)'}")


def find_employee(url, key, emp_id):
    response = requests.get(
        f"{url}/rest/v1/employee",
        params={"select": "id,emp_id,full_name,image", "emp_id": f"eq.{emp_id}"},
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    rows = response.json()
    if not rows:
        sys.exit(f"No employee with emp_id {emp_id}.")
    return rows[0]


def main():
    parser = argparse.ArgumentParser(description="Upload one employee photograph.")
    parser.add_argument("emp_id", help="for example EMP-0001")
    parser.add_argument("photo", help="file name inside photos/, or a full path")
    parser.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    args = parser.parse_args()

    url, key, key_name = get_credentials()
    print(f"Reading credentials from .env ({key_name})")
    print(f"Project: {url}")

    source = Path(args.photo)
    if not source.exists():
        source = PHOTOS_DIR / args.photo
    if not source.exists():
        sys.exit(f"No such file: {args.photo} (looked in . and {PHOTOS_DIR}/)")

    employee = find_employee(url, key, args.emp_id)
    ext, content_type = stored_extension(source.name)
    object_path = f"{employee['emp_id']}/photo{ext}"
    public_url = f"{url}/storage/v1/object/public/{BUCKET}/{object_path}"

    print()
    print(f"  employee : {employee['emp_id']}  {employee['full_name']}")
    print(f"  file     : {source}  ({source.stat().st_size:,} bytes)")
    print(f"  upload to: {BUCKET}/{object_path}")
    print(f"  image URL: {public_url}")
    if employee.get("image"):
        print(f"  replacing: {employee['image']}")

    if args.dry_run:
        print("\nDry run, nothing uploaded.")
        return

    upload = requests.post(
        f"{url}/storage/v1/object/{BUCKET}/{object_path}",
        data=source.read_bytes(),
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": content_type,
            "x-upsert": "true",
        },
        timeout=TIMEOUT,
    )
    if not upload.ok:
        sys.exit(f"Upload failed ({upload.status_code}): {upload.text}")

    patch = requests.patch(
        f"{url}/rest/v1/employee",
        params={"id": f"eq.{employee['id']}"},
        json={"image": public_url},
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
        timeout=TIMEOUT,
    )
    if not patch.ok:
        sys.exit(f"Uploaded, but linking failed ({patch.status_code}): {patch.text}")

    print("\nDone. Photo uploaded and employee.image updated.")


if __name__ == "__main__":
    main()