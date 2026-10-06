"""AgentCore Platform v1.0 - Google Calendar API v3 client.

Service layer: a thin wrapper around the Google Calendar API v3 event
endpoints. Contains NO business logic, NO routing, and NO credentials - the
integration token (an OAuth 2.0 access token) is passed in per call by the
node (which reads it via ctx.secrets). This module imports no framework/SDK
internals - pure stdlib (import isolation, verified by the boundary tests).

v1 LIMITATION (deliberate, documented):
    The DEFAULT transport is a deterministic, NETWORK-FREE stub. It returns the
    documented Google Calendar API v3 Events resource shapes (an event resource
    with an ``id`` for insert/patch, derived from the request; a cancel receipt
    echoing the event id for delete) so the pipeline is runnable and testable
    without a live Google Workspace tenant or the ``requests`` package - it
    does NOT perform a live Google API call. The design rule: never fake a
    live call; document the limitation.

    To perform real Google Calendar calls, inject live transports
    (requests-based ``post`` / ``patch`` / ``delete``) at construction time;
    the method contracts and payload shapes are already Google Calendar API v3
    exact, so no business-logic change is needed to go live. A live transport
    also requires a real integration token (see CallCalendarApiNode - the stub
    runs without one because no request ever leaves the process).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

# A transport callable: (url, headers, json_body) -> (status_code, response_dict)
Transport = Callable[[str, "dict[str, Any]", "dict[str, Any]"], "tuple[int, dict[str, Any]]"]

_BASE_URL = "https://www.googleapis.com/calendar/v3"
_CALENDAR_ID = "primary"


class CalendarApiError(Exception):
    """Raised when the Google Calendar API returns a non-2xx status."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"Google Calendar API error {status_code}: {message}")


class GoogleCalendarClient:
    """Google Calendar API v3 event client.

    Args:
        base_url: API base URL (default https://www.googleapis.com/calendar/v3).
        calendar_id: target calendar (default ``primary``).
        post/patch/delete: optional injected transports (tests or a live client).
            When none is injected, a deterministic NETWORK-FREE v1 stub is used
            (see the module docstring - it returns the documented shape without
            a live Google API call).
    """

    def __init__(
        self,
        base_url: str = _BASE_URL,
        calendar_id: str = _CALENDAR_ID,
        *,
        post: Transport | None = None,
        patch: Transport | None = None,
        delete: Transport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._calendar_id = calendar_id
        self._post = post
        self._patch = patch
        self._delete = delete

    # -- transport mode --------------------------------------------------------

    @property
    def uses_stub_transport(self) -> bool:
        """True when NO live transport is injected (the network-free v1 default)."""
        return self._post is None and self._patch is None and self._delete is None

    @property
    def calendar_id(self) -> str:
        return self._calendar_id

    # -- auth ----------------------------------------------------------------

    def _headers(self, api_token: str) -> "dict[str, str]":
        """Build the Google Calendar API v3 auth headers.

        api_token is supplied per-call by the node (from ctx.secrets); it is
        never persisted on the instance or logged.
        """
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_token}",
        }

    # -- v1 deterministic stub transport (default; NO network) ----------------

    def _stub_transport(
        self, url: str, headers: "dict[str, Any]", json_body: "dict[str, Any]"
    ) -> "tuple[int, dict[str, Any]]":
        """Deterministic, network-free v1 stub - returns the documented shape.

        NOT a live call. Synthetic ids are derived from the request so the
        response is stable and inspectable. The target event id (patch/delete)
        is taken from the URL path, exactly where the live API carries it. See
        the module docstring for the v1 limitation and how to inject live
        transports.
        """
        seed = url + "|" + json.dumps(json_body, sort_keys=True, ensure_ascii=False, default=str)
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        url_tail = url.rsplit("/", 1)[-1]
        if json_body.get("_gcal_op") == "delete":
            # Live DELETE returns 204 with an empty body; the stub echoes the
            # cancelled event id so the caller can reference the affected record.
            return 200, {"id": url_tail, "status": "cancelled", "_stub": True}
        if url_tail != "events":
            # PATCH /events/<id> - documented Events resource echo.
            return 200, {
                "kind": "calendar#event",
                "id": url_tail,
                "status": "confirmed",
                "summary": str(json_body.get("summary", "")),
                "_stub": True,  # marks the network-free v1 stub response
            }
        # POST /events (insert) - documented Events resource with a synthetic id
        # (echoed from the request when the caller supplied one).
        event_id = str(json_body.get("id", "")) or f"evt{digest[:10]}"
        return 200, {
            "kind": "calendar#event",
            "id": event_id,
            "status": "confirmed",
            "summary": str(json_body.get("summary", "")),
            "htmlLink": f"https://www.google.com/calendar/event?eid={event_id}",
            "_stub": True,  # marks the network-free v1 stub response
        }

    def _resolve(self, injected: Transport | None) -> Transport:
        return injected or self._stub_transport

    # -- public API ---------------------------------------------------------

    def create_event(self, payload: "dict[str, Any]", api_token: str) -> "dict[str, Any]":
        """POST /calendars/{calendarId}/events - insert a new event.

        ``payload`` is the documented Events resource request body (summary,
        start, end, location, description, ...). Returns the parsed Events
        resource (containing ``id``). Raises CalendarApiError on non-2xx.
        """
        url = f"{self._base_url}/calendars/{self._calendar_id}/events"
        transport = self._resolve(self._post)
        status, body = transport(url, self._headers(api_token), payload)
        if not (200 <= status < 300):
            raise CalendarApiError(status, _err_message(body))
        return body

    def update_event(self, event_id: str, payload: "dict[str, Any]", api_token: str) -> "dict[str, Any]":
        """PATCH /calendars/{calendarId}/events/{eventId} - partial update.

        ``payload`` carries only the Events resource fields being changed.
        Returns the parsed Events resource echo. Raises CalendarApiError on a
        non-2xx status.
        """
        url = f"{self._base_url}/calendars/{self._calendar_id}/events/{event_id}"
        transport = self._resolve(self._patch)
        status, body = transport(url, self._headers(api_token), payload)
        if not (200 <= status < 300):
            raise CalendarApiError(status, _err_message(body))
        return body

    def cancel_event(self, event_id: str, api_token: str) -> "dict[str, Any]":
        """DELETE /calendars/{calendarId}/events/{eventId} - cancel an event.

        The live endpoint returns 204 with an empty body; a live ``delete``
        transport adapter may return an empty dict (the stub echoes the
        cancelled id). The ``_gcal_op`` body key is a stub-routing sentinel
        only - a live DELETE adapter sends no body and ignores it. Raises
        CalendarApiError on a non-2xx status.
        """
        url = f"{self._base_url}/calendars/{self._calendar_id}/events/{event_id}"
        transport = self._resolve(self._delete)
        status, body = transport(url, self._headers(api_token), {"_gcal_op": "delete"})
        if not (200 <= status < 300):
            raise CalendarApiError(status, _err_message(body))
        return body or {"id": event_id, "status": "cancelled"}


def _err_message(body: Any) -> str:
    """Extract a human-readable error message from a Google API error body."""
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            msg = error.get("message")
            if msg:
                return str(msg)
        msg = body.get("message")
        if msg:
            return str(msg)
    return str(body)
