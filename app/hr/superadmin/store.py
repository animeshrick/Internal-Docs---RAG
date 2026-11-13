"""
hr/superadmin/store.py - the super admin's own audit trail (sql/13_superadmin_audit.sql).

Every leave or attendance change made for someone else through the super admin is written
here: who did it, for whom, what changed and when. Nothing here decides what the super
admin may do; that stays in service.py.
"""
from app.core.supabase import read_rows
from app.hr.attendance.store import insert_row

AUDIT_TABLE_NAME = "superadmin_audit_log"


def insert_audit_entry(fields):
    return insert_row(AUDIT_TABLE_NAME, fields, "Saving the super admin audit entry")


def list_audit_entries(limit=50):
    """Newest first."""
    return read_rows(AUDIT_TABLE_NAME, {"select": "*", "order": "id.desc", "limit": limit}, "Reading the super admin audit log")
