#!/usr/bin/env python3
"""
Load government IDs into Semicolon-HRMS. All six who submitted one.

    cd "D:\\AI-LLM-Projects\\Internal Docs - RAG"
    python .\\upload_govt_id.py --dry-run
    python .\\upload_govt_id.py

Aadhaar rows go in with the front only. back_image_path stays null, which
marks the back as outstanding; those four get prompted at login.

Delete this file after running: it holds six ID numbers in plain text.
"""

import os
import sys
import pathlib
import uuid
import requests

from app.core.encryption import encrypt_text as encrypt

BUCKET = "government-ids"
DEFAULT_URL = "https://qeayqzfuowdvhwntlyhh.supabase.co"
KEY_NAMES = ("SUPABASE_SECRET_KEY", "SUPABASE_SERVICE_KEY",
             "SUPABASE_SERVICE_ROLE_KEY")

# emp_id, name, id_type, number, filename fragment, submitted_at
RECORDS = [
    ("EMP-0003", "Sayan Mukherjee",   "aadhaar", "503252521551",
     "1e727066",            "2026-11-24T20:23:00+05:30"),
    ("EMP-0004", "Sneha Panda",       "pan",     "GBOPP0223B",
     "11.24.06 am - sneha", "2026-11-25T11:33:00+05:30"),
    ("EMP-0009", "Sukanta Dolai",     "pan",     "CREPD0308P",
     "sukanta dolai",       "2026-11-24T19:48:00+05:30"),
    ("EMP-0011", "Shyamal Roy",       "aadhaar", "821360194311",
     "17955866024885",      "2026-11-25T11:35:00+05:30"),
    ("EMP-0012", "Subhajit Karmakar", "aadhaar", "695347937747",
     "17955860346968",      "2026-11-25T11:25:00+05:30"),
    ("EMP-0013", "Komal Rajak",       "aadhaar", "308775714280",
     "7.13.28 pm",          "2026-11-24T19:16:00+05:30"),
]


def load_env():
    for candidate in (".env", "/opt/hrms/app/.env"):
        path = pathlib.Path(candidate)
        if path.is_file():
            for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.strip()
                if line and not line.startswith("#") and "=" in line:
                    line = line[7:] if line.startswith("export ") else line
                    name, _, value = line.partition("=")
                    os.environ.setdefault(name.strip(),
                                          value.strip().strip('"').strip("'"))
            print(f"Credentials from {path}")
            return


def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    args = [a for a in args if not a.startswith("--")]

    load_env()
    base = (os.environ.get("SUPABASE_URL") or DEFAULT_URL).rstrip("/")
    key = next((os.environ[n] for n in KEY_NAMES if os.environ.get(n)), None)
    if not key and not dry_run:
        sys.exit("No Supabase secret key found.")

    if args:
        src = pathlib.Path(args[0])
    elif pathlib.Path("govtids").is_dir():
        src = pathlib.Path("govtids")
    else:
        src = pathlib.Path(".")
    if not src.is_dir():
        sys.exit(f"No such folder: {src}")

    print(f"Project: {base}")
    print(f"Folder:  {src.resolve()}\n")

    auth = {"Authorization": f"Bearer {key}", "apikey": key} if key else {}
    loaded = 0

    for emp_id, name, id_type, number, fragment, submitted in RECORDS:
        hits = [p for p in src.iterdir()
                if p.is_file() and fragment in p.name.lower()]
        if not hits:
            print(f"MISS  {emp_id}  {name:<18} {id_type.upper():<8} "
                  f"no file matching '{fragment}'")
            continue

        local = hits[0]
        pending = "  (back image outstanding)" if id_type == "aadhaar" else ""
        print(f"READY {emp_id}  {name:<18} {id_type.upper():<8} "
              f"<- {local.name}{pending}")
        if dry_run:
            continue

        r = requests.get(f"{base}/rest/v1/employee", headers=auth,
                         params={"emp_id": f"eq.{emp_id}", "select": "id"},
                         timeout=30)
        rows = r.json()
        if not rows:
            print(f"FAIL  {emp_id}: not in employee table")
            continue
        emp_pk = rows[0]["id"]

        ext = ".jpg" if local.suffix.lower() in (".jpg", ".jpeg") else ".png"
        mime = "image/jpeg" if ext == ".jpg" else "image/png"
        dest = f"{emp_pk}/{uuid.uuid4().hex}-front{ext}"

        up = requests.post(f"{base}/storage/v1/object/{BUCKET}/{dest}",
                           headers={**auth, "Content-Type": mime, "x-upsert": "true"},
                           data=local.read_bytes(), timeout=120)
        if up.status_code not in (200, 201):
            print(f"FAIL  {emp_id} upload: {up.status_code} {up.text[:200]}")
            continue

        ins = requests.post(f"{base}/rest/v1/employee_government_id",
                            headers={**auth, "Content-Type": "application/json"},
                            json={
                                "employee_id": emp_pk,
                                "id_type": id_type,
                                "id_number_encrypted": encrypt(number),
                                "id_number_last4": number[-4:],
                                "front_image_path": dest,
                                "back_image_path": None,
                                "front_content_type": mime,
                                "back_content_type": None,
                                "submitted_at": submitted,
                            }, timeout=30)
        if ins.status_code not in (200, 201, 204):
            print(f"FAIL  {emp_id} insert: {ins.status_code} {ins.text[:300]}")
            continue

        print(f"OK    {emp_id}  {name:<18} -> {dest}")
        loaded += 1

    print("\nDry run, nothing uploaded." if dry_run
          else f"\n{loaded} loaded. Ankur Dutta submitted no ID at all.")


if __name__ == "__main__":
    main()