# CMN-C2-235 - Unit tests: declared runtime config ARRIVES where it is consumed.
#
# A green suite proves nothing about configuration unless a declared value is
# traced to its consumer: a config loader that silently returns {} degrades
# every declared setting to its default without failing a single test. These
# tests read the REAL config/config.yaml and prove each declared value reaches
# the component that consumes it - the graph constructor path for the runtime
# scalars, and the inner workflow (through _parent_config() and the state
# injection) for the google_calendar integration section.

import pathlib

import yaml

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.context_bridge import set_caller_event_context
from src.graph.domain_workflow_graph import CalendarWorkflowGraph
from src.graph.graph import CalendarWorkflowGraphNode
from src.schemas.state import from_json
from src.services.calendar_client import GoogleCalendarClient

_RUNTIME_PATH = pathlib.Path(__file__).parents[2] / "config" / "config.yaml"


def _declared():
    return yaml.safe_load(_RUNTIME_PATH.read_text(encoding="utf-8"))


def test_parent_config_reads_the_live_runtime_file():
    """_parent_config() must forward the CURRENT config/config.yaml
    google_calendar section - not a stale location (the legacy nested
    manifest) that would make it silently return {}."""
    declared = _declared()
    forwarded = CalendarWorkflowGraphNode()._parent_config()
    assert forwarded["configurable"]["google_calendar"] == declared["google_calendar"]
    assert forwarded["configurable"]["google_calendar"]["base_url"]  # non-empty, from the file


def test_inner_graph_state_carries_the_declared_section():
    """File -> _parent_config() -> _extra_initial_state() -> State JSON."""
    set_caller_event_context({})  # deterministic: no caller data in this probe
    declared = _declared()
    graph = CalendarWorkflowGraph(config=CalendarWorkflowGraphNode()._parent_config())
    extra = graph._extra_initial_state()
    assert from_json(extra["calendar_config"], {}) == declared["google_calendar"]


def test_declared_settings_reach_the_calendar_client_end_to_end(monkeypatch):
    """Full outer invoke: the client the API node constructs must be built
    with the base_url AND calendar_id DECLARED in config/config.yaml -
    proving the values survive every hop (file -> parent config -> inner
    state -> node)."""
    import src.nodes.call_calendar_api_node as node_mod
    from src.graph.graph import GoogleWorkspaceCalendarAgent

    captured: list = []

    class RecordingClient(GoogleCalendarClient):
        def __init__(self, *args, **kwargs):
            captured.append((kwargs.get("base_url"), kwargs.get("calendar_id")))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(node_mod, "GoogleCalendarClient", RecordingClient)

    agent = GoogleWorkspaceCalendarAgent()
    agent.compile()
    result = agent.invoke(
        user_input='Schedule a meeting titled "config arrival probe"\nStart: 2026-09-01 14:00',
        ctx=InvocationContext(caller_id="cfg-probe", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
    )
    assert result["status"] == "success", f"result={result!r}"
    declared = _declared()["google_calendar"]
    assert captured == [(declared["base_url"], declared["calendar_id"])]


def test_max_retry_reaches_the_graph_when_config_is_passed():
    """The framework run loop reads max_retry from the config handed to the
    graph constructor - the standalone server loads config/config.yaml and
    passes it (asserted against the server module in the endpoint tests)."""
    from src.api.server import _load_runtime_config
    from src.graph.graph import GoogleWorkspaceCalendarAgent

    declared = _declared()
    agent = GoogleWorkspaceCalendarAgent(config=_load_runtime_config())
    assert agent.config.get("max_retry") == declared["max_retry"]
    assert agent.config.get("timeout_s") == declared["timeout_s"]


def test_missing_runtime_file_degrades_to_safe_defaults(monkeypatch, tmp_path):
    """A missing config file must not crash graph construction - the inner
    nodes fall back to the documented stub-client defaults."""
    import src.graph.graph as graph_mod

    monkeypatch.setattr(graph_mod, "_RUNTIME_CONFIG_PATH", tmp_path / "absent.yaml")
    forwarded = CalendarWorkflowGraphNode()._parent_config()
    assert forwarded == {"configurable": {}}
