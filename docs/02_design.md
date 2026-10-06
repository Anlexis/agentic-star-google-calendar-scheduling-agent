# Template Design Specification — CMN-C2-235 Google Workspace Calendar Agent

## Position in AgentCore Architecture

- **Agent Class**: `GoogleWorkspaceCalendarAgent` (`src/graph/graph.py`)
| L1 Base (framework base class) | `AgentBaseGraph` — direct framework inheritance |
- **Category**: Cat 2 (multi-step domain workflow, ToolCallingAgent). Outer
  `AgentBaseGraph` 5-node backbone; the domain pipeline is encapsulated in a
  `GraphNode` (`main` slot) wrapping an inner `BaseGraph`
  (`src/graph/domain_workflow_graph.py`).
- **Agent Type**: ToolCallingAgent — classify intent -> infer calendar event
  fields -> build a Google Calendar API v3 request -> call the tool -> format
  the confirmation. No RAG retrieval, no autonomous ReAct loop.
- **Three-Layer Separation**:
  - State: flat TypedDict `State(AgentState)` (no Pydantic — msgpack incompatible)
  - Node: framework base-class inheritance (Template Method: `execute(self, state) -> dict` override only)
  - Graph: composition (`register_nodes()` + `super().register_nodes()`; `add_edges()`
    not overridden on the outer graph)

## Architecture Overview

### Outer graph — node configuration (`src/graph/graph.py`)

| Node | Responsibility | Input State | Output State | Trust | Inherits/Overrides |
|------|---------------|-------------|--------------|-------------|-------------------|
| initialize | framework setup (schema, session, trust) | user_input | session/trust fields | framework default | InitializeNode (default) |
| pre_process | own the caller-data contract: template injection screen (request text RAW + `input_context` post-parse, keys included, any depth); every event-id hint checked against the bounded event-id shape; structured `event` fields bounded + contact-identifier screened; HTML/length sanitize; serialize NL request + hint into `validated_input` (JSON) | user_input, input_context | validated_input, event_hint, caller_event | **VERIFIED_EXTERNAL** (the single external gate) | PreProcessNode (FunctionNode) |
| main | run the inner calendar workflow subgraph (validated caller event data crosses on the context bridge) | validated_input, event_hint, caller_event | result, intent, event_id, event_ref, event_summary, confirmation, calendar_payload | GraphNode (caller ctx forwarded unchanged) | CalendarWorkflowGraphNode (GraphNode) |
| post_process | shape caller-facing `formatted_output`; module-level `_security_gate_output()` scan (walks the NESTED output, keys as well as values); every non-success return contained by `_contain()` into a closed-set reason code | inner-result fields | formatted_output | ANONYMOUS | PostProcessNode (FunctionNode) |
| finalize | framework finalize (metadata, timing) | — | response_metadata | framework default | FinalizeNode (default) |

### Inner workflow — node configuration (`src/graph/domain_workflow_graph.py`)

Inner graph inherits `BaseGraph` (fully custom linear topology). The 5 pipeline
steps map 1:1 to inner nodes. **Every inner domain node declares
`required_trust_level = TrustLevel.ANONYMOUS`** — the caller's
`InvocationContext` is forwarded into the subgraph unchanged, so the single
external trust gate stays on the backbone `pre_process`.

| Inner node | Step | Responsibility | Output | Trust |
|------|------|---------------|--------|-------------|
| validate_input | 1 ValidateInput | empty/non-request guard; deterministic (regex) flag-and-redact of email/token-like strings before logging | validated_input, event_hint, redaction_flags | ANONYMOUS |
| classify_intent | 2 ClassifyIntent | deterministic keyword classification -> create_event / update_event / cancel_event; every intent is a write, so an unrecognized request is a hard status=error (never guess a write) | intent | ANONYMOUS |
| infer_calendar_fields | 3 InferCalendarFields | merge the validated caller event fields (authoritative) with text extraction (fills the gaps): event id / title / start / end / location / description; assemble the Google Calendar API v3 event body per intent; an unresolved event id is left empty (never invented); missing datetimes are never invented | event_summary, event_id, calendar_payload | ANONYMOUS |
| call_calendar_api | 4 CallCalendarApi | POST /events (create) / PATCH /events/{id} (update) / DELETE /events/{id} (cancel) request shapes via `GoogleCalendarClient` (v1: served by the deterministic network-free stub transport — see "v1 Limitation — Google Calendar client"); token via ctx.secrets; 4xx/5xx -> status=error | event_id, event_ref, event_summary | ANONYMOUS |
| confirm | 5 Confirm | format intent + event id + reference into a human-readable confirmation | confirmation, result | ANONYMOUS |

### Data Flow

```
Outer:  START -> initialize -> pre_process -> main -> {route} -> post_process -> finalize -> END
                                              | (RETRY, max 3) ^
Inner (inside main / CalendarWorkflowGraphNode):
        START -> validate_input -> classify_intent -> infer_calendar_fields
              -> call_calendar_api -> confirm -> END
```

Structured parameters travel on two channels:

- **Serialized request text**: `pre_process` serializes `{"text", "event_hint"}`
  into `validated_input`, `CalendarWorkflowGraphNode.extract_input()` hands that
  JSON to the subgraph, and the first inner node (`validate_input`) parses it
  back. This channel is rewritten by the framework's PII-masking heuristics at
  every node boundary.
- **Context bridge** (`src/graph/context_bridge.py`): the VALIDATED caller
  event data (`event_hint` + the structured `event` fields) crosses the graph
  boundary on a ContextVar — stashed by `extract_input()`, seeded by the inner
  graph's `_extra_initial_state()`. This channel is NOT masked, which is the
  point: a Title Case event title ("Quarterly Business Review") or a
  hyphenated numeric event id embedded in the serialized request arrives at
  the calendar write as "[MASKED]" — corrupted caller data. The bridge carries
  only data that already passed the pre_process contract, never the raw
  request body. The state-field copy of the hint takes precedence over the
  (maskable) embedded copy inside `validated_input`; the embedded copy is a
  fallback for direct runs.

### State Definition (`src/schemas/state.py`)

All domain fields are declared `NotRequired[...]` (state contract — fields are
absent until their producer node writes them). Dict/list payloads are stored
as JSON strings (`Optional[str]`) via the module helpers `to_json` /
`from_json`, used by every producer and consumer.

| Field | Type | Purpose | Producer |
|-------|------|---------|----------|
| event_hint | NotRequired[str] | caller-supplied event-id hint, validated against the bounded event-id shape; never inferred | pre_process / validate_input |
| event_id | NotRequired[str] | resolved Google Calendar event id (v1: pass-through when the hint/text already carries an id; create receives it from the API response) | infer_calendar_fields / call_calendar_api |
| caller_event | NotRequired[Optional[str]] | JSON — validated structured event fields from `input_context.event`, carried over the context bridge (unmasked channel); authoritative over text inference | pre_process / inner graph |
| redaction_flags | NotRequired[Optional[str]] | JSON list of patterns redacted before logging | validate_input |
| event_summary | NotRequired[str] | event title (Google Calendar `summary`) | infer_calendar_fields / call_calendar_api |
| calendar_payload | NotRequired[Optional[str]] | JSON — assembled Google Calendar API v3 event body (msgpack-safe: stored as a JSON string via `to_json`/`from_json`) | infer_calendar_fields |
| calendar_config | NotRequired[Optional[str]] | JSON — `config/config.yaml` `google_calendar:` section forwarded by `_parent_config()` and injected via the inner graph's `_extra_initial_state()` | inner graph |
| event_ref | NotRequired[str] | human-readable event reference (`gcal://calendars/<calendar_id>/events/<id>`) | call_calendar_api |
| confirmation | NotRequired[str] | human-readable confirmation | confirm |

`intent`, `result`, `validated_input`, `formatted_output` are inherited from
`AgentState` and are **not** re-declared.

**State Constraints (mandatory, satisfied):**
- Flat TypedDict only (primitives + JSON-serializable) — no Pydantic/dataclass.
- No JWT / API keys / credentials in State — the Google Calendar token is accessed via `ctx.secrets`.
- InvocationContext read via `InvocationContext.from_state(state)`, never stored in State.

## Configuration (flat manifest + runtime file)

`config/agent.yaml` is the FLAT registry manifest — identity, entry point
(`class: "src.graph.graph.GoogleWorkspaceCalendarAgent"`), trust requirement
and the compile-time `requires:` gates, every key at root level. Runtime
parameters live in `config/config.yaml`:

- `max_retry` / `timeout_s` — consumed by the framework through the graph
  constructor; the standalone server (`src/api/server.py`) loads the file and
  passes it as `GoogleWorkspaceCalendarAgent(config=...)`.
- `google_calendar:` (`base_url`, `calendar_id`) — the Google Calendar API v3
  integration section.

Nodes take **no constructor arguments** (SDK v1 nodes are no-arg; configuration
never rides on node instances). `CalendarWorkflowGraphNode._parent_config()`
loads `config/config.yaml` and forwards the `google_calendar:` section (and
`llm:` if ever declared) to the inner graph under `config["configurable"]` —
never `{}` (an empty parent config would silently turn every declared setting
into its default). The inner graph's `_extra_initial_state()` then injects the
`google_calendar` section into State as a JSON string (`calendar_config`),
where `CallCalendarApiNode.execute(state, config=None)` reads it (an explicit
`config["configurable"]["google_calendar"]` override is also honoured for
direct/unit invocation). The runtime scalars are deliberately not re-forwarded
to the inner graph — their consumer is the outer framework run loop.
Declared-value arrival is pinned by tests end-to-end (file -> parent config ->
inner state -> the constructed calendar client).

## Structured Product (`get_output()` override)

`GoogleWorkspaceCalendarAgent.get_output()` **extends** `super().get_output()`
(never replaces the base envelope — `output`/`status`/`trace_id`/
`correlation_id`/`node_history` are preserved) and surfaces the structured
product keys (`event_id`, `event_ref`, `event_summary`, `intent`,
`confirmation`) **only when the run ended SUCCESS**, re-applying the
module-level `_security_gate_output()` scan before surfacing — on error, a
non-dict output, or any gate violation the base envelope is returned unchanged
(fail-closed).

### Refusal is not containment

The base envelope's `output` is projected as `formatted_output or result`,
with **no status check**. The inner workflow answer has already been written
to `result` by the time `post_process` runs, so an error return that leaves
state untouched still delivers that un-gated answer to the caller under
`status: error` — the refusal is visible, the containment is not there.

Every non-success return in `PostProcessNode` therefore goes through the one
module-level `_contain()` helper, which:

1. **Clears the output-bearing state fields** — `result`, `event_summary`,
   `confirmation`, `calendar_payload`, `redaction_flags`, `event_id`,
   `event_ref` — so nothing un-gated survives in state for the projection, a
   checkpoint, or a downstream reader.
2. **Installs a TRUTHY envelope** into `formatted_output`. `""`, `{}` and `[]`
   are falsy and would make the `or result` fallback fire again; the constant
   `reason` key guarantees the projection stops at the envelope.
3. **Writes the violation entries to `error_log` only** — never to the caller.

### The error envelope is a closed set

Clearing the answer and bounding the error channel are different properties,
and the second is what the caller-visible error contract is about. The
envelope carries **only labels this module chose from a declared set**:

```json
{"status": "error", "output": {"reason": "output_withheld_by_gate"}}
```

`reason` is one of `ERROR_REASONS` — `calendar_workflow_failed` (the inner
workflow reported an error) or `output_withheld_by_gate` (the output gate
refused the response). Nothing else rides it: not `error_log`, not the gate's
violation entries, not the event, not an exception string. Those lines can
embed a Google Calendar API error body, an event title, an organizer's name or
a caller-derived fragment, and truncating or credential-redacting them is not a
closed set. `error_log` stays the **internal** channel — the state reducer
appends to it and the audit trail needs it — and the inner entries are never
re-emitted by `post_process` (the reducer already holds them; re-emitting would
duplicate every line). The audit event carries a reason code and a count, never
message content.

The same rule runs upstream, at the node that owns the external call:
`CallCalendarApiNode` logs a Google Calendar failure as its **HTTP status**
(`Google Calendar API error 403`) and a transport failure as its **exception
class name** — never `CalendarApiError`'s own message, which embeds the API's
response body.

### Violations name a location, never a value

`error_log` is itself scanned by the framework credential gate on the way out,
so quoting the matched value makes that gate raise on this very result — the
node then returns a traceback instead, and the clearing above it is discarded.
Field paths are normalised to an inert alphabet before interpolation, and a
mapping key that is itself credential-shaped is withheld from the label
(`__withheld__`) rather than quoted — the walk scans keys as well as values,
so the key that triggers the violation must not be repeated by it.

Pinned end-to-end: an output-gate violation driven through the real `/invoke`
yields an ERROR envelope whose only value is a declared reason code, with a
sentinel seeded into `error_log` asserted absent from every key and value of
the response at any depth, no event reference and no traceback — checked
against a clean-path control on the same request.

## Security Design

- **Trust gate** — the single external trust gate is on the outer backbone
  `PreProcessNode.required_trust_level = TrustLevel.VERIFIED_EXTERNAL`; every inner
  domain node — **including the write-capable `CallCalendarApiNode`** — declares
  `TrustLevel.ANONYMOUS`. `GraphNode.execute()` forwards the caller's
  `InvocationContext` into the subgraph **unchanged** (no elevation), and
  `VERIFIED_EXTERNAL (1) < INTERNAL (2)`, so declaring an inner node `INTERNAL`
  would deny a legitimate external caller before the call runs — the boundary
  is therefore enforced exactly once, at `pre_process`. Agent-level default trust
  `VERIFIED_EXTERNAL` is declared in `config/agent.yaml`. `src/api/server.py`
  enforces the standalone entry-point Bearer-token auth boundary
  (`INVOKE_AUTH_TOKEN` -> VERIFIED_EXTERNAL elevation) and caps the
  `input_context` envelope at 256 KB (HTTP 413).
- **Caller-data contract** — `pre_process` owns it: `input_context` must be an
  object. An explicit target event id (`event_id` / `event_hint` /
  `calendar_event_id`) must be a string matching the bounded event-id shape
  (`[A-Za-z0-9][A-Za-z0-9_-]{3,63}`, single definition in
  `src/services/security.py`); a present-but-malformed value is a hard
  `status=error` that names the FIELD and never echoes the value (fail closed —
  silently dropping a hint could update or cancel a different event than the
  caller intended). Structured event fields ride in `input_context.event`
  (`summary` <= 200 chars, `location` <= 500, `description` <= 2000, `start` /
  `end` as explicit ISO-like datetimes) — each bounded, wrong types and
  over-bound values refused naming the field, an unsupported field refused
  without echoing its (caller-controlled) name; unknown top-level keys are
  ignored. **Why the structured channel exists**: the request-text channel is
  rewritten by the framework's masking heuristics (any two Title Case words
  match the name pattern), so an event title carried only in text arrives at
  the calendar write as "[MASKED]" — real caller data corrupted on a
  successful run. Validated `event` fields travel over the unmasked context
  bridge instead and take precedence in field assembly. The pipeline carries
  no caller-controlled numeric field (inventory by grep: `input_context` is
  read only in `pre_process`; the hint, the `event` fields — all strings with
  bounded shapes — and the free-text request are the entire caller surface),
  so no numeric-bounds parser is required — if a numeric field is ever added,
  it must go through a finite+bounded parser.
- **Injection screen (template-owned)** — `pre_process` refuses
  prompt-injection content itself rather than relying on any upstream gate
  being active: chat-template control tokens (`<|im_start|>` / `<|im_end|>`,
  `[INST]` / `[/INST]`, `<<SYS>>` / `<</SYS>>`, forged `<system>`-style role
  tags) and high-confidence instruction/role-override phrasing. The request
  text is screened RAW (before the HTML strip, so a control token cannot be
  half-eaten into an ordinary-looking request); `input_context` is screened
  post-parse over every key and string value at any depth (a hostile field
  NAME or a JSON-escaped payload parses back to its real characters first).
  Text is normalized (URL-decoding, NFKC, zero-width strip) before scanning.
  Refusals are behavioural — error status, nothing carried forward — and name
  the pattern type only, never echoing the content. Patterns are limited to
  token forms and high-confidence phrases so legitimate scheduling text
  ("please disregard the earlier room booking", "system maintenance window")
  is unaffected — pinned both directions by tests.
- **Contact-identifier screen (structured channel)** — the framework's PII
  mask does not cover `input_context`, so the template screens that channel
  itself: free-text `event` fields are scanned with the platform PII detector
  and any contact identifier (email address, phone number, SSN/My Number,
  card number) is REFUSED naming the field — never silently stripped, never
  echoed. **Documented name-heuristic exclusion**: person-name findings do NOT
  refuse — a calendar title or location is legitimately two Title Case words
  ("Quarterly Business Review", "Osaka Innovation Center"), and the name
  heuristic matches any such bigram; refusing on it would block ordinary
  calendar data. Contact identifiers remain the refusal class because they are
  the values that would otherwise flow into the tenant calendar and the
  response unmasked.
- **Input flag-and-redact (text channel)** — `ValidateInputNode.execute()`
  runs a deterministic (regex, NOT LLM) scan for email addresses and
  access-token-like strings (`eyJ...`, `secret_...`, `sk-...`) and redacts
  them before any logging. A calendar request may legitimately mention people,
  so this is flag-and-redact for safe logging, not a hard reject; the
  framework input-side PII mask in `BaseNode.__call__` additionally masks
  emails/phones/names in `user_input`/`validated_input`. The only
  deterministic auto-reject is the empty/non-request guard. Consequence
  (documented): attendee email addresses are refused on the structured channel
  and redacted on the text channel, so **attendee management is out of scope
  for v1** (see Design Decision Record).
- **Secrets + output gate** — the integration token is read via
  `ctx.secrets.get("GOOGLE_CALENDAR_TOKEN")` (`InvocationContext.from_state(state)`),
  never `os.environ`, never stored in State. **The manifest declares
  `requires.secrets: []`** — a deliberate, visible contract choice: the token
  is read through the OPTIONAL `get()` accessor because the shipped
  network-free stub transport runs without a credential, and declaring a
  secret the runtime does not provision would fail the agent at compile time.
  A live deployment provisions `GOOGLE_CALENDAR_TOKEN` through the secrets
  provider without a manifest change; with a live transport injected, a
  missing token is a hard `status=error` — a real API is never called
  unauthenticated. The domain output gate is the **module-level**
  `_security_gate_output()` in `src/nodes/post_process_node.py`, called from
  `PostProcessNode.execute()`: it blocks any SUCCESS response that lacks event
  evidence (event_id/event_ref) and any credential-shaped string **anywhere in
  the nested output** — the gate walks dicts/lists/tuples, because the
  caller-facing output embeds the assembled event body as a nested mapping and
  a token one level down (e.g. inside the event description) must be caught
  exactly like a top-level one. Violations name the field path, never the
  value. The direct-execute error branch redacts credential-shaped substrings
  from the error surface for the same reason. No node defines
  `_extra_security_gate_input/_output` instance methods (framework hooks are
  @final / auto-wrapped — domain checks live inline or in module-level
  helpers).
- **Audit** — every node's `execute()` emits exactly one positional
  `emit_trace_event("<node>_complete", {small non-PII payload}, state)` on its
  SUCCESS path (intent / presence signals only — never request text, event
  content, or credentials). `__call__()` is never overridden; `_invoke_impl`
  is never defined on any node. Event names (documented for operations):

  | Node | Audit event |
  |------|-----------|
  | pre_process | `pre_process_complete` |
  | validate_input | `validate_input_complete` |
  | classify_intent | `classify_intent_complete` |
  | infer_calendar_fields | `infer_calendar_fields_complete` |
  | call_calendar_api | `call_calendar_api_complete` |
  | confirm | `confirm_complete` |
  | post_process | `post_process_complete` |

## Output Invariant — decision record

The template's stated output invariant is: **a SUCCESS response always carries
verifiable event evidence (`event_id`/`event_ref`), and no credential-shaped
string reaches the caller in any representation** — enforced by the
nested-walking gate above and pinned both ways by tests (nested leak probe AND
a top-level control that proves the scanner itself fires).

A numeric precision grid (rounding rendered aggregates) is **not applicable**
here, on evidence rather than assumption: the response renders no monetary or
aggregate numbers at all — its numeric content is calendar event ids (bounded
`[A-Za-z0-9][A-Za-z0-9_-]{3,63}` identifiers, which may be digit-heavy or
hyphenated-numeric) and ISO-like datetimes. A digit-grouping grammar would
have nothing legitimate to protect and would corrupt exactly these values (a
pure-numeric event id has no letters for an identifier guard to anchor on; a
datetime's minute field is a bare digit run). The identifier-integrity
property is pinned instead: the end-to-end suite asserts the response carries
no grouped-digit artifact (`\d,\d{3}`) anywhere and that a hyphenated-numeric
caller event id round-trips byte-identical.

## v1 Implementation Note — LLM synthesis

v1 is fully deterministic: intent classification (`ClassifyIntentNode`) uses a
keyword heuristic and field inference (`InferCalendarFieldsNode`) uses regex/
line-structure extraction, so the template runs and tests without a live LLM.
**No LLM client is constructed anywhere in v1** and no `system_prompt` is read
(no dead config) — the manifest accordingly declares
`generation_mode: "deterministic"` and `requires.extras: []`. LLM-backed
synthesis (richer intent classification, free-text date/time understanding,
natural-language event summaries) is a documented follow-up: an `llm:` section
in `config/config.yaml` — when declared — is already forwarded to the inner
graph by `_parent_config()`, so wiring an LLM in is additive and requires no
graph-shape change.

## v1 Limitation — Google Calendar client (documented)

`src/services/calendar_client.py` is a pure service-layer client (injectable
transport, `CalendarApiError`, per-call token, no framework imports) that
ships a **deterministic, network-free v1 stub** as its default transport: it
returns the documented Google Calendar API v3 Events resource shapes (an
event resource with an `id` for insert/patch, derived from the request; a
cancel receipt echoing the event id for delete) so the pipeline is runnable
and testable without a live Google Workspace tenant or the `requests`
package. It does **not** perform a live Google API call. In the deployed v1
graph, `CallCalendarApiNode` constructs the client from configuration only
(`base_url`, `calendar_id`) and deliberately injects **no** transports, so
every create/update/cancel resolves to the stub — **v1 does not write to a
real Google tenant**, and the audit event `call_calendar_api_complete`
records `stub_transport: true` on every v1 invoke (runtime evidence of the
mode). The design rule: never fake a live call — document the limitation.

The LIVE transport is a **construction-time injection point reserved for
deployment integration**: a deployment integration supplies real
`post`/`patch`/`delete` transport adapters at the point where
`CallCalendarApiNode` constructs the client. The method contracts and payload
shapes are already Google Calendar API v3 exact, so going live is a
deployment-integration change, not a business-logic change. (The stub also
runs without a live credential — see "Secrets + output gate" above; a live
transport requires `GOOGLE_CALENDAR_TOKEN`, an OAuth 2.0 access token with the
`https://www.googleapis.com/auth/calendar.events` scope, and the node refuses
to run a live transport unauthenticated.)

## Framework Utilization

### Shared Components Used
- [x] InvocationContext — read in `CallCalendarApiNode` via `InvocationContext.from_state(state)` (secrets + trust)
- [x] Trust gate — single external gate `PreProcessNode.required_trust_level = TrustLevel.VERIFIED_EXTERNAL`; inner domain nodes (incl. `CallCalendarApiNode`) declare `TrustLevel.ANONYMOUS` (caller `InvocationContext` forwarded unchanged into the subgraph)
- [x] Secrets — `ctx.secrets.get("GOOGLE_CALENDAR_TOKEN")` (optional accessor; see Security Design); entry-point `bound_secrets` / `secrets_factory` / `provision_secrets` in `src/api/server.py` (namespace/agent_name match the manifest values)
- [x] PII detector — `framework.security.pii_detector.detect_pii` reused in-template to screen the structured `input_context.event` channel (contact-identifier refusal; documented name-heuristic exclusion)
- [x] Audit — `emit_trace_event()`: one positional call per node on the SUCCESS path; framework lifecycle events (node_start/node_complete/node_error) NOT re-emitted

### Composition Pattern

- **Pattern**: GraphNode (subgraph) — Cat 2 outer/inner split.
- **Composition target**: inner `CalendarWorkflowGraph` (`BaseGraph`) via `CalendarWorkflowGraphNode.get_subgraph()`.
- **Config forwarding**: `CalendarWorkflowGraphNode._parent_config()` loads
  `config/config.yaml` and forwards `{google_calendar, llm (if declared)}`
  under `config["configurable"]` to the subgraph.
- **Caller-data forwarding**: validated event data crosses on the ContextVar
  bridge (`src/graph/context_bridge.py`) — the graph boundary does not forward
  outer state fields on its own.
- **Error propagation strategy**: `propagate` (default) — inner errors re-raised as
  `SubgraphError`; per-step `status=error` + `error_log` for API/validation failures
  (no silent pass).

## Import Isolation Confirmation
- [x] Template imports `framework/` and `shared/` only; no platform-internal SDK import anywhere
- [x] `src/services/calendar_client.py` and `src/services/security.py` have no
      framework imports (pure service layer, stdlib only)

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| Base type | AgentBaseGraph | AutonomousBaseGraph | AgentBaseGraph | Fixed multi-step pipeline (Cat 2), not an autonomous loop |
| Composition pattern | flat (MainNode) | GraphNode + inner subgraph | GraphNode + inner subgraph | 5 domain steps live in the inner graph; the outer backbone stays framework-shaped |
| LLM dependency | LLM client in v1 | deterministic v1, LLM as documented follow-up | deterministic v1 | template runs/tests without a live LLM; no dead prompt/config reads; see "v1 Implementation Note — LLM synthesis" |
| Calendar client | live `requests` call | injectable transport + documented v1 stub default | injectable + v1 stub default | never fake a live call; document the limitation; go-live is a transport injection, no logic change |
| Node configuration | ctor-arg dependency injection | no-arg nodes + runtime-file forwarding via `_parent_config()` -> `configurable` -> state | no-arg nodes | SDK v1 nodes are no-arg (ctor args TypeError at graph build); `config/config.yaml` stays the single runtime-config source |
| Structured caller data | ride inside the serialized request text | validated `input_context.event` + context bridge | context bridge | the text channel is rewritten by the framework mask (Title Case titles arrive "[MASKED]") — a successful run would write corrupted data; the bridge carries only validated fields |
| Compile-time secret declaration | declare GOOGLE_CALENDAR_TOKEN in `requires.secrets` | `requires.secrets: []` + optional `get()` accessor | `[]` + `get()` | the shipped stub transport runs without a credential; declaring an unprovisioned secret fails the agent at compile time; a live deployment provisions the token without a manifest change |
| Write target | infer event id from NL freely | caller-supplied/explicit id only; unresolved left empty | explicit only | never update/cancel the wrong event; unresolved id -> status=error, not invented |
| Low-confidence intent | fall back to create_event | status=error asking for an explicit operation | status=error | every intent is a write against the tenant calendar — a guessed write is never acceptable; unlike a read/write agent there is no read-only default to fall back to |
| Date/time inference | invent/complete missing datetimes | explicit ISO-like datetimes only; missing values omitted | explicit only | a wrong meeting time is a real-world failure; deterministic v1 accepts only what the caller wrote (free-text date understanding is the documented LLM follow-up) |
| Attendee management | parse attendee emails from the request | out of scope for v1 | out of scope | the input boundary refuses contact identifiers on the structured channel and redacts them on the text channel by design; attendee support needs a compatible channel and is a documented follow-up |
