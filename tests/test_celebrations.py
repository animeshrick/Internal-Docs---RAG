"""Tests for the birthday and work anniversary list of the home page (people_service.get_celebrations)."""
from datetime import date

from app.hr.people import service as people_service
from app.hr.people import store as people_store


def person(emp_id, name, dob, doj, role="employee", confidential=False):
    return {"id": 1, "emp_id": emp_id, "full_name": name, "dept": "IT", "dob": dob, "doj": doj, "role": role, "is_confidential": confidential}


def test_birthdays_and_anniversaries_of_today(monkeypatch):
    monkeypatch.setattr(people_store, "list_dates_of_active_employees", lambda: [
        person("EMP-1", "Ann", "1995-11-12", "2024-01-05"),
        person("EMP-2", "Raj", "1990-03-03", "2021-11-12"),
        person("EMP-3", "Sue", "1992-11-13", "2022-11-13"),
        person("EMP-4", "Joiner", "1992-01-01", "2026-11-12"),       # joined today: not an anniversary
    ])
    result = people_service.get_celebrations(date(2026, 11, 12))
    assert [(c["full_name"], c["kind"], c["years"]) for c in result] == [("Ann", "birthday", None), ("Raj", "anniversary", 5)]


def test_the_page_shows_no_age_or_date(monkeypatch):
    monkeypatch.setattr(people_store, "list_dates_of_active_employees", lambda: [person("EMP-1", "Ann", "1995-11-12", None)])
    entry = people_service.get_celebrations(date(2026, 11, 12))[0]
    assert set(entry) == {"emp_id", "full_name", "dept", "kind", "years"}


def test_the_ceo_the_cto_and_the_super_admin_are_left_out(monkeypatch):
    monkeypatch.setattr(people_store, "list_dates_of_active_employees", lambda: [
        person("EMP-1", "Ceo", "1980-11-12", None, role="ceo"),
        person("EMP-2", "Cto", "1985-11-12", None, confidential=True),
        person("EMP-3", "Admin", "1990-11-12", None, role="superadmin"),
    ])
    assert people_service.get_celebrations(date(2026, 11, 12)) == []


def test_missing_or_broken_dates_are_ignored_and_29_february_moves_to_28(monkeypatch):
    monkeypatch.setattr(people_store, "list_dates_of_active_employees", lambda: [
        person("EMP-1", "No dates", None, None), person("EMP-2", "Bad", "not a date", "also bad"), person("EMP-3", "Leap", "2000-02-29", None)])
    assert people_service.get_celebrations(date(2026, 11, 12)) == []
    assert [c["full_name"] for c in people_service.get_celebrations(date(2027, 2, 28))] == ["Leap"]
    assert [c["full_name"] for c in people_service.get_celebrations(date(2028, 2, 29))] == ["Leap"]
    assert people_service.get_celebrations(date(2028, 2, 28)) == []
