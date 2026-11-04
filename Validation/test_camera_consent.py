"""Validate expiring consent using synthetic files, without opening a camera."""

import json

import pytest

import camera_presence


@pytest.mark.parametrize("until", [float("inf"), float("-inf"), float("nan"), "inf", "nan", 10**400])
def test_invalid_expiry_never_authorizes_camera(tmp_path, monkeypatch, until):
    consent = tmp_path / "consent.json"
    consent.write_text(json.dumps({"until": until}), encoding="utf-8")
    monkeypatch.setattr(camera_presence, "CONSENT", consent)
    assert camera_presence.allowed(now=1000) is False


@pytest.mark.parametrize("now", [float("inf"), float("-inf"), float("nan"), 10**400])
def test_invalid_current_time_never_authorizes_camera(tmp_path, monkeypatch, now):
    consent = tmp_path / "consent.json"
    consent.write_text('{"until": 2000}', encoding="utf-8")
    monkeypatch.setattr(camera_presence, "CONSENT", consent)
    assert camera_presence.allowed(now=now) is False


def test_finite_permission_expires_at_its_deadline(tmp_path, monkeypatch):
    consent = tmp_path / "consent.json"
    consent.write_text('{"until": 2000}', encoding="utf-8")
    monkeypatch.setattr(camera_presence, "CONSENT", consent)
    assert camera_presence.allowed(now=1999) is True
    assert camera_presence.allowed(now=2000) is False
    assert camera_presence.allowed(now=2001) is False
