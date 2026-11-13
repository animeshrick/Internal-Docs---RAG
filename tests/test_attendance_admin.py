"""Tests for the office-network admin settings (app/hr/attendance/service.py: get_admin_settings,
add_office_network, set_office_network_active, set_network_check)."""
import pytest

from app.hr.attendance import service
from app.hr.attendance.service import AttendanceError
from fakes_attendance import FakeAttendanceDatabase, login_of

HR, PM, EMPLOYEE, CEO, ADMIN = (login_of(person_id) for person_id in (1, 2, 3, 5, 8))


@pytest.fixture
def database(monkeypatch):
    return FakeAttendanceDatabase().install(monkeypatch)


def test_only_hr_or_the_super_admin_may_manage_the_office_network(database):
    for person in (PM, EMPLOYEE, CEO):
        with pytest.raises(AttendanceError) as error:
            service.get_admin_settings(person)
        assert error.value.status_code == 403
    service.get_admin_settings(HR)       # does not raise
    service.get_admin_settings(ADMIN)    # neither does the super admin


def test_a_fresh_install_has_the_check_off_and_no_networks(database):
    settings = service.get_admin_settings(HR)
    assert settings == {"network_check_enabled": False, "office_networks": []}


def test_turning_the_check_on_with_no_active_network_is_refused(database):
    with pytest.raises(AttendanceError, match="lock everyone out"):
        service.set_network_check(HR, True)
    assert database.get_settings()["network_check_enabled"] is False


def test_adding_a_network_then_turning_the_check_on_works(database):
    network = service.add_office_network(HR, "Kolkata office", "203.0.113.0/24")
    assert network == {"id": network["id"], "label": "Kolkata office", "cidr": "203.0.113.0/24", "is_active": True}
    result = service.set_network_check(HR, True)
    assert result == {"network_check_enabled": True}
    assert database.get_settings()["network_check_enabled"] is True


def test_an_invalid_network_address_is_refused(database):
    with pytest.raises(AttendanceError, match="not a valid network address"):
        service.add_office_network(HR, "Typo", "not-an-ip")
    assert database.list_all_office_networks() == []


def test_a_network_without_a_label_is_refused(database):
    with pytest.raises(AttendanceError, match="label"):
        service.add_office_network(HR, "  ", "203.0.113.0/24")


def test_turning_a_network_off_takes_it_out_of_the_active_list(database):
    network = service.add_office_network(HR, "Kolkata office", "203.0.113.0/24")
    service.set_network_check(HR, True)
    updated = service.set_office_network_active(HR, network["id"], False)
    assert updated["is_active"] is False
    assert database.list_active_office_networks() == []
    # Turning the network off does not retroactively disable the switch - that stays an explicit HR action.
    assert database.get_settings()["network_check_enabled"] is True


def test_an_unknown_network_id_gives_a_404(database):
    with pytest.raises(AttendanceError) as error:
        service.set_office_network_active(HR, 999, False)
    assert error.value.status_code == 404


def test_admin_settings_lists_every_network_active_or_not(database):
    first = service.add_office_network(HR, "Kolkata office", "203.0.113.0/24")
    service.add_office_network(HR, "VPN range", "198.51.100.5/32")
    service.set_office_network_active(HR, first["id"], False)
    settings = service.get_admin_settings(HR)
    assert {network["label"]: network["is_active"] for network in settings["office_networks"]} == {
        "Kolkata office": False, "VPN range": True}
