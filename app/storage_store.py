"""
storage_store.py - private files in Supabase Storage (leave documents now, payslips later).

The bucket "leave-documents" is private. Nobody can open a file by guessing its address.
To let someone read a file we make a signed link that stops working after a few minutes.
"""
from urllib.parse import quote

import requests

from config import get_required_setting
from supabase_store import REQUEST_TIMEOUT_SECONDS, build_headers, check_response

LEAVE_DOCUMENTS_BUCKET = "leave-documents"
SIGNED_LINK_SECONDS = 300


def get_storage_url():
    return get_required_setting("SUPABASE_URL").rstrip("/") + "/storage/v1"


def upload_file(bucket_name, file_path, file_bytes, content_type):
    """Store a file. The path must not exist yet (we never overwrite)."""
    response = requests.post(
        f"{get_storage_url()}/object/{bucket_name}/{quote(file_path)}",
        headers=build_headers({"Content-Type": content_type, "x-upsert": "false"}),
        data=file_bytes,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    check_response(response, "Uploading the file")


def create_signed_link(bucket_name, file_path):
    """A temporary link to read one file."""
    response = requests.post(
        f"{get_storage_url()}/object/sign/{bucket_name}/{quote(file_path)}",
        headers=build_headers(),
        json={"expiresIn": SIGNED_LINK_SECONDS},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    check_response(response, "Creating the document link")
    return get_storage_url() + response.json()["signedURL"]
