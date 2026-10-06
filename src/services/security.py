"""Input sanitizer + content screens for CMN-C2-235 (outer backbone pre_process).

Pure, stateless domain helpers (NOT framework gate methods). Three concerns:

- ``sanitize_query``: strip HTML markup and cap length before the raw caller
  text is JSON-serialized and handed to the inner calendar workflow graph.
- ``EVENT_ID_RE`` / ``DATETIME_VALUE_RE``: the bounded shapes a caller-supplied
  event id / event datetime must match before they are accepted (fail closed).
- ``find_injection``: the template-owned prompt-injection screen. The template
  owns this guarantee itself rather than relying on any upstream gate being
  active: chat-template control tokens (``<|im_start|>``, ``[INST]``,
  ``<<SYS>>``, forged role tags) and instruction/role-override phrasing are
  refused in the node that owns the caller contract. Text is normalized first
  (URL-decoding, NFKC, zero-width strip) so escaped or homoglyph variants of
  the same payload do not slip past the patterns.
"""

from __future__ import annotations

import re
import unicodedata
import urllib.parse

_HTML_TAG_RE = re.compile(r"<[^>]+>")

DEFAULT_MAX_LENGTH = 4000

# The bounded shape of a calendar event id: URL-safe identifier (no spaces),
# 4-64 chars. Caller-supplied event-id hints must match this shape exactly;
# anything else is refused (fail closed).
EVENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{3,63}$")

# Explicit ISO-like datetime value ("2026-08-01 14:00" / "2026-08-01T14:00:00")
# or bare date ("2026-08-01"). The only shapes a caller-supplied event start /
# end may take - nothing else is accepted, nothing is ever invented.
DATETIME_VALUE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[T ](\d{1,2}:\d{2})(?::\d{2})?)?$")

_ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\ufeff\u00ad]")

# Template-owned injection patterns. Token forms first: a chat-template control
# token is an attack marker regardless of surrounding phrasing, and phrase-only
# screens miss it. Phrases are limited to high-confidence forms so legitimate
# scheduling text ("please disregard the earlier room booking") is unaffected.
_INJECTION_PATTERNS: "tuple[tuple[str, re.Pattern[str]], ...]" = (
    ("chat_template_token", re.compile(r"<\|im_(?:start|end)\|>", re.IGNORECASE)),
    ("chat_template_token", re.compile(r"\[/?(?:INST|SYS)\]", re.IGNORECASE)),
    ("chat_template_token", re.compile(r"<</?SYS>>", re.IGNORECASE)),
    ("chat_template_token", re.compile(r"<\s*/?(?:system|assistant)\s*>", re.IGNORECASE)),
    (
        "instruction_override",
        re.compile(
            r"(?:ignore|disregard)\s+(?:all\s+|the\s+)?(?:previous|above|prior)\s+"
            r"(?:instructions?|prompts?|context|rules?)",
            re.IGNORECASE,
        ),
    ),
    (
        "instruction_override",
        re.compile(r"\bignore\s+all\s+(?:rules?|instructions?)\b", re.IGNORECASE),
    ),
    (
        "role_override",
        re.compile(
            r"\bact\s+as\s+(?:a|an)\s+(?:different|new|unrestricted|unfiltered|evil|"
            r"jailbroken|dan|god|admin|root|superuser|hacker)\b",
            re.IGNORECASE,
        ),
    ),
)


def _normalize(text: str) -> str:
    """Undo common obfuscation (URL-encoding, homoglyphs, zero-width chars) before scanning."""
    text = urllib.parse.unquote(text)
    text = unicodedata.normalize("NFKC", text)
    return _ZERO_WIDTH_RE.sub("", text)


def find_injection(text: str) -> "list[str]":
    """Return the injection pattern types found in ``text`` ([] = clean).

    Callers refuse on any finding; the finding NAMES the pattern type only -
    the matched text is never included, so nothing hostile is ever echoed.
    """
    normalized = _normalize(text)
    found: list[str] = []
    for pattern_type, pattern in _INJECTION_PATTERNS:
        if pattern_type not in found and pattern.search(normalized):
            found.append(pattern_type)
    return found


def sanitize_query(query: str, max_length: int = DEFAULT_MAX_LENGTH) -> str:
    """Strip HTML tags (injection/markup guard) and cap length."""
    cleaned = _HTML_TAG_RE.sub("", query)
    return cleaned[:max_length]
