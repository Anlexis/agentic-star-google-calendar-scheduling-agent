# CMN-C2-235 - Unit tests: manifest + runtime config sanity.
#
# config/agent.yaml is the FLAT registry manifest (every key at root level);
# config/config.yaml carries the runtime parameters (max_retry, timeout_s)
# and the google_calendar integration section forwarded to the inner graph.

import pathlib

import pytest

try:
    import yaml  # pyyaml (transitive dep of the framework wheel)

    _YAML_ERROR = None
except Exception as exc:  # pragma: no cover
    yaml = None
    _YAML_ERROR = exc

_MANIFEST_PATH = pathlib.Path(__file__).parents[2] / "config" / "agent.yaml"
_RUNTIME_PATH = pathlib.Path(__file__).parents[2] / "config" / "config.yaml"

pytestmark = pytest.mark.skipif(_YAML_ERROR is not None, reason=f"pyyaml unavailable: {_YAML_ERROR}")


def _manifest():
    return yaml.safe_load(_MANIFEST_PATH.read_text())


def _runtime():
    return yaml.safe_load(_RUNTIME_PATH.read_text())


def test_manifest_identity():
    data = _manifest()
    assert data["id"] == "CMN-C2-235"
    assert data["name"] == "GoogleWorkspaceCalendarAgent"
    assert data["namespace"] == "cmn"
    assert data["category"] == "Cat 2"
    assert data["industry"] == "CMN"
    assert data["base_type"] == "ToolCallingAgent"
    assert data["enabled"] is True


def test_manifest_entry_point():
    data = _manifest()
    # Single dotted path (module.Class) at root level - no agent: nesting.
    assert data["class"] == "src.graph.graph.GoogleWorkspaceCalendarAgent"
    assert "agent" not in data


def test_manifest_generation_mode_deterministic():
    # v1 constructs no LLM client anywhere (docs/02 "v1 Implementation Note").
    assert _manifest()["generation_mode"] == "deterministic"


def test_manifest_security():
    data = _manifest()
    # Agent-level entry trust, enforced by the outer backbone pre_process gate
    # (VERIFIED_EXTERNAL); inner domain nodes stay ANONYMOUS.
    assert data["required_trust_level"] == "VERIFIED_EXTERNAL"


def test_manifest_requires_no_compile_time_secret():
    """The Calendar token is read via the OPTIONAL ctx.secrets.get() - the
    shipped network-free stub transport runs without it, so the manifest must
    not declare it as a required (compile-time-provisioned) secret. A live
    deployment provisions GOOGLE_CALENDAR_TOKEN through the secrets provider
    without a manifest change."""
    data = _manifest()
    assert data["requires"]["secrets"] == []
    assert data["requires"]["extras"] == []


def test_runtime_google_calendar_integration_section():
    data = _runtime()
    # Forwarded to the inner graph by CalendarWorkflowGraphNode._parent_config().
    assert data["google_calendar"]["base_url"] == "https://www.googleapis.com/calendar/v3"
    assert data["google_calendar"]["calendar_id"] == "primary"


def test_runtime_scalars():
    data = _runtime()
    assert isinstance(data["max_retry"], int)
    assert isinstance(data["timeout_s"], int)
