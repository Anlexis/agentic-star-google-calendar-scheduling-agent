"""AgentCore Platform v1.0 - inner workflow Step 2: ClassifyIntent.

Classifies the (redacted) request into one of create_event / update_event /
cancel_event using a deterministic keyword heuristic, so the template is
testable and runnable without a live LLM (v1 Implementation Note - LLM
synthesis, docs/02_design.md). Every intent in this write agent mutates the
tenant calendar, so - unlike a read/write agent with a read-only fallback - a
low-confidence / unknown request is a hard status=error asking for an explicit
operation: a guessed write is never acceptable (docs/02 Design Decision
Record).
"""

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

_VALID_INTENTS = ("create_event", "update_event", "cancel_event")

# Deterministic keyword signals (checked in priority order: cancel is the most
# explicit destructive signal; update before create so "reschedule" never
# matches create's "schedule" first).
_KEYWORDS = (
    (
        "cancel_event",
        ("cancel", "call off", "delete", "remove", "scrap", "キャンセル", "取り消", "取消", "中止", "削除"),
    ),
    (
        "update_event",
        (
            "update",
            "change",
            "edit",
            "move",
            "reschedule",
            "postpone",
            "shift",
            "rename",
            "extend",
            "shorten",
            "変更",
            "更新",
            "修正",
            "延期",
            "リスケ",
            "移動",
        ),
    ),
    (
        "create_event",
        (
            "create",
            "schedule",
            "book",
            "add a",
            "add an",
            "set up",
            "new event",
            "new meeting",
            "arrange",
            "plan",
            "organize",
            "作成",
            "登録",
            "追加",
            "予約",
            "新規",
            "設定",
        ),
    ),
)


class ClassifyIntentNode(FunctionNode):
    """Classify the request into a Google Calendar write-operation intent."""

    # Inner domain node, read-only classification of already-redacted text -
    # the external gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: "dict[str, Any]") -> "dict[str, Any]":
        text = state.get("validated_input", "") or ""
        if not text:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ClassifyIntentNode: missing validated_input"],
            }

        intent = self._classify_via_keywords(text)

        if intent not in _VALID_INTENTS:
            # Fail closed: every valid intent is a write against the tenant
            # calendar - never guess one (docs/02 Design Decision Record).
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [
                    "ClassifyIntentNode: could not determine the calendar "
                    "operation - please state explicitly whether to create, "
                    "update, or cancel an event"
                ],
            }

        # Audit the classification decision - intent label only, never the text.
        emit_trace_event(
            "classify_intent_complete",
            {"intent": intent},
            state,
        )

        return {"intent": intent, "status": AgentStatus.SUCCESS.value}

    # -- classification -------------------------------------------------------

    def _classify_via_keywords(self, text: str) -> str:
        low = text.lower()
        for intent, words in _KEYWORDS:
            if any(w in low for w in words):
                return intent
        # No signal at all: fall through to the fail-closed guard in execute()
        # (returns a sentinel outside _VALID_INTENTS).
        return "unknown"
