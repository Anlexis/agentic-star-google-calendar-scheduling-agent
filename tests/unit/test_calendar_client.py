# CMN-C2-235 - Unit tests: GoogleCalendarClient service (Google Calendar API v3 shape)
# Pure service layer (stdlib-only, no framework imports) - plain function tests.

import pytest

from src.services.calendar_client import CalendarApiError, GoogleCalendarClient


def test_create_event_success_with_injected_post():
    captured = {}

    def post(url, headers, body):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = body
        return 200, {"kind": "calendar#event", "id": "evt123", "summary": "Standup"}

    client = GoogleCalendarClient("https://calendar.example.test/v3/", post=post)
    payload = {"summary": "Standup", "start": {"dateTime": "2026-08-01T14:00:00"}}
    resp = client.create_event(payload, "tok123")
    assert resp["id"] == "evt123"
    assert captured["url"] == "https://calendar.example.test/v3/calendars/primary/events"
    # Google Calendar API v3 auth: the per-call OAuth token travels as a Bearer header.
    assert captured["headers"]["Authorization"] == "Bearer tok123"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["body"] == payload


def test_update_event_success_with_injected_patch():
    captured = {}

    def patch(url, headers, body):
        captured["url"] = url
        captured["body"] = body
        return 200, {"kind": "calendar#event", "id": "evt123", "summary": "Standup"}

    client = GoogleCalendarClient("https://calendar.example.test/v3", patch=patch)
    resp = client.update_event("evt123", {"summary": "Standup"}, "tok")
    assert resp["id"] == "evt123"
    assert captured["url"] == "https://calendar.example.test/v3/calendars/primary/events/evt123"
    assert captured["body"] == {"summary": "Standup"}


def test_cancel_event_with_injected_delete_empty_body_falls_back_to_receipt():
    # A live DELETE returns 204 with an empty body - the client synthesizes the
    # cancel receipt so the caller can still reference the affected event.
    captured = {}

    def delete(url, headers, body):
        captured["url"] = url
        captured["body"] = body
        return 204, {}

    client = GoogleCalendarClient("https://calendar.example.test/v3", delete=delete)
    resp = client.cancel_event("evt999", "tok")
    assert resp == {"id": "evt999", "status": "cancelled"}
    assert captured["url"] == "https://calendar.example.test/v3/calendars/primary/events/evt999"
    # The _gcal_op body key is a stub-routing sentinel only.
    assert captured["body"] == {"_gcal_op": "delete"}


def test_custom_calendar_id_in_url():
    captured = {}

    def post(url, headers, body):
        captured["url"] = url
        return 200, {"id": "evt1"}

    client = GoogleCalendarClient("https://calendar.example.test/v3", "team", post=post)
    client.create_event({"summary": "x"}, "tok")
    assert captured["url"] == "https://calendar.example.test/v3/calendars/team/events"


def test_non_2xx_raises_calendar_api_error():
    def post(url, headers, body):
        return 409, {"error": {"code": 409, "message": "duplicate event"}}

    client = GoogleCalendarClient("https://calendar.example.test/v3", post=post)
    with pytest.raises(CalendarApiError) as exc:
        client.create_event({"summary": "x"}, "tok")
    assert exc.value.status_code == 409
    assert "duplicate event" in str(exc.value)


def test_default_stub_transport_create_shape():
    # No transport injected -> deterministic, network-free v1 stub.
    client = GoogleCalendarClient()
    assert client.uses_stub_transport is True
    resp = client.create_event({"summary": "Standup"}, "tok")
    assert resp.get("_stub") is True
    assert resp["kind"] == "calendar#event"
    assert resp["id"].startswith("evt")
    assert resp["status"] == "confirmed"
    assert resp["summary"] == "Standup"
    assert resp["id"] in resp["htmlLink"]


def test_default_stub_transport_create_echoes_supplied_id():
    client = GoogleCalendarClient()
    resp = client.create_event({"id": "customid1", "summary": "x"}, "tok")
    assert resp["id"] == "customid1"


def test_default_stub_transport_update_echoes_target_id():
    client = GoogleCalendarClient()
    resp = client.update_event("evt42", {"summary": "renamed"}, "tok")
    assert resp.get("_stub") is True
    assert resp["id"] == "evt42"
    assert resp["summary"] == "renamed"


def test_default_stub_transport_cancel_echoes_target_id():
    client = GoogleCalendarClient()
    resp = client.cancel_event("evt999", "tok")
    assert resp.get("_stub") is True
    assert resp["id"] == "evt999"
    assert resp["status"] == "cancelled"


def test_injected_transport_disables_stub_flag():
    client = GoogleCalendarClient(post=lambda url, headers, body: (200, {"id": "evt1"}))
    assert client.uses_stub_transport is False
