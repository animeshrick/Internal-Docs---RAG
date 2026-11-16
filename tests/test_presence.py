"""Tests for app/core/presence.py - the best-effort "online" signal behind the green dot in
1-to-1 messaging."""
import time

from app.core import presence


def setup_function():
    presence._reset_for_tests()


def test_nobody_is_online_until_marked_active():
    assert presence.is_online(1) is False
    presence.mark_active(1)
    assert presence.is_online(1) is True


def test_only_the_marked_employee_is_online():
    presence.mark_active(1)
    assert presence.is_online(1) is True
    assert presence.is_online(2) is False


def test_falls_offline_once_outside_the_online_window(monkeypatch):
    presence.mark_active(1)
    # Simulate time passing by moving the window itself down to 0, rather than sleeping the tests.
    monkeypatch.setattr(presence, "ONLINE_WINDOW_SECONDS", 0)
    time.sleep(0.01)
    assert presence.is_online(1) is False
