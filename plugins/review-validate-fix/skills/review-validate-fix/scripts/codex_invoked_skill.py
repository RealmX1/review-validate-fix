# Vendored from https://github.com/RealmX1/codex-invoked-skill
#   path: codex_invoked_skill/core.py @ commit c39934d
# Single-file copy so the RVF plugin stays self-contained (no pip dependency).
# Do not edit here — change upstream and re-vendor. Tests live in that repo.
"""Recover which skill / custom prompt a Codex user explicitly invoked.

Background
----------
OpenAI's Codex CLI (verified against 0.135.x) has a Claude-Code-style hook
system, but its ``UserPromptSubmit`` hook delivers only the raw ``prompt``
string. Unlike Claude Code's dedicated ``UserPromptExpansion`` event, there is
**no** structured field telling a hook which slash command / skill / custom
prompt was explicitly invoked. The official docs state plainly: *"No fields
identify slash commands, skills, or custom prompts — only the raw prompt text
is provided."*

The identity is, however, recorded in Codex's session **rollout** JSONL. Each
submitted prompt produces an ``event_msg`` record of type ``user_message``
whose ``text_elements`` array marks each explicit ``$``-invocation with a
``byte_range`` and a ``placeholder`` (e.g. ``$rvf:review-validate-fix``). A
Codex hook receives ``transcript_path`` pointing at that rollout, so the
identity can be read **structurally** — without regex-matching the free prompt
text.

This module turns that rollout data into a clean, typed signal.

Scope & caveats (empirically verified against real rollouts)
------------------------------------------------------------
* Only ``$name`` / ``$namespace:name`` mentions land in ``text_elements``.
  Custom prompts invoked via the ``/prompts:<name>`` menu are expanded to plain
  markdown before being stored and leave **no** ``text_elements`` marker — use
  :func:`match_invocation_in_text` as a best-effort fallback for those.
* The observed element shape is exactly
  ``{"byte_range": {"start": int, "end": int}, "placeholder": str}``. Any extra
  fields a future Codex build emits are preserved verbatim on
  :attr:`InvokedSkill.raw`.
* Timing: depending on when your hook fires relative to the rollout flush, the
  *current* turn's ``user_message`` may not be written yet. ``which="latest"``
  returns the most recent ``user_message``'s elements; pass the hook's
  ``prompt`` as ``match_prompt=`` to pin the exact record by its stored text.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

__all__ = [
    "InvokedSkill",
    "parse_placeholder",
    "invoked_skills_from_transcript",
    "invoked_skills_from_event",
    "was_skill_invoked",
    "match_invocation_in_text",
]

# Hook-event keys that may carry the path to the Codex rollout JSONL.
_TRANSCRIPT_KEYS = ("transcript_path", "conversation_path", "session_path")

# Placeholder prefix -> coarse element kind. Only "$" is observed in practice;
# the rest are forward-compatible guesses kept deliberately conservative.
_PREFIX_KIND = {"$": "skill", "@": "mention"}


@dataclass(frozen=True)
class InvokedSkill:
    """One explicitly-invoked skill / custom prompt found in a user message.

    Attributes
    ----------
    name:
        The bare skill/command name, e.g. ``review-validate-fix``.
    namespace:
        The source/plugin namespace if the placeholder was ``$ns:name``
        (e.g. ``rvf`` or ``review-validate-fix``), else ``None``.
    kind:
        Coarse classification from the placeholder prefix: ``"skill"`` for
        ``$`` (the only form Codex currently records), ``"mention"`` for ``@``,
        otherwise ``"text"``.
    placeholder:
        The raw placeholder string, e.g. ``$rvf:review-validate-fix``.
    byte_range:
        ``(start, end)`` byte offsets of the placeholder within the message.
    raw:
        The original ``text_elements`` element dict (forward-compatibility).
    """

    name: str
    namespace: "str | None"
    kind: str
    placeholder: str
    byte_range: "tuple[int, int] | None"
    raw: "dict[str, Any]" = field(default_factory=dict, repr=False, compare=False)

    @property
    def qualified(self) -> str:
        """``namespace:name`` if namespaced, else just ``name``."""
        return f"{self.namespace}:{self.name}" if self.namespace else self.name

    def matches(self, name: str, *, namespace: "str | None" = None) -> bool:
        """True if this invocation is ``name`` (and ``namespace`` if given)."""
        if self.name != name:
            return False
        return namespace is None or self.namespace == namespace


def parse_placeholder(placeholder: str) -> InvokedSkill:
    """Parse a single ``text_elements`` ``placeholder`` into an :class:`InvokedSkill`.

    ``$ns:name`` -> namespace ``ns``, name ``name``; ``$name`` -> name ``name``.
    The leading sigil (``$``/``@``) determines :attr:`InvokedSkill.kind`.
    """
    return _build(placeholder, byte_range=None, raw={"placeholder": placeholder})


def _build(placeholder: str, *, byte_range: "tuple[int, int] | None", raw: "dict[str, Any]") -> InvokedSkill:
    text = placeholder or ""
    prefix = text[:1]
    kind = _PREFIX_KIND.get(prefix, "text")
    body = text[1:] if prefix in _PREFIX_KIND else text
    if ":" in body:
        namespace, name = body.split(":", 1)
        namespace = namespace or None
    else:
        namespace, name = None, body
    return InvokedSkill(
        name=name,
        namespace=namespace,
        kind=kind,
        placeholder=text,
        byte_range=byte_range,
        raw=raw,
    )


def _element_to_skill(element: "dict[str, Any]") -> "InvokedSkill | None":
    if not isinstance(element, dict):
        return None
    placeholder = element.get("placeholder")
    if not isinstance(placeholder, str) or not placeholder:
        return None
    br = element.get("byte_range")
    byte_range: "tuple[int, int] | None" = None
    if isinstance(br, dict):
        start, end = br.get("start"), br.get("end")
        if isinstance(start, int) and isinstance(end, int):
            byte_range = (start, end)
    return _build(placeholder, byte_range=byte_range, raw=dict(element))


def _iter_user_messages(transcript_path: "str | Path") -> "Iterator[dict[str, Any]]":
    """Yield each ``user_message`` payload dict from a Codex rollout JSONL.

    Best-effort: a missing/unreadable file yields nothing; malformed lines are
    skipped. Order matches file order (chronological).
    """
    path = Path(transcript_path).expanduser()
    try:
        handle = path.open(encoding="utf-8")
    except OSError:
        return
    with handle:
        for line in handle:
            line = line.strip()
            if not line or '"user_message"' not in line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict) or record.get("type") != "event_msg":
                continue
            payload = record.get("payload")
            if isinstance(payload, dict) and payload.get("type") == "user_message":
                yield payload


def _elements(payload: "dict[str, Any]") -> "list[InvokedSkill]":
    out: "list[InvokedSkill]" = []
    for element in payload.get("text_elements") or []:
        skill = _element_to_skill(element)
        if skill is not None:
            out.append(skill)
    return out


def invoked_skills_from_transcript(
    transcript_path: "str | Path",
    *,
    which: str = "latest",
    match_prompt: "str | None" = None,
) -> "list[InvokedSkill]":
    """Structured ``$``-invocations recorded in a Codex rollout.

    Parameters
    ----------
    transcript_path:
        Path to the rollout JSONL (the ``transcript_path`` a hook receives).
    which:
        ``"latest"`` (default) reads the most recent ``user_message``'s
        elements; ``"all"`` flattens elements across every ``user_message``.
    match_prompt:
        If given, ignore ``which`` and instead return the elements of the most
        recent ``user_message`` whose stored ``message`` equals this string —
        use the hook's ``prompt`` to pin the exact turn regardless of flush
        ordering.
    """
    payloads = list(_iter_user_messages(transcript_path))
    if match_prompt is not None:
        for payload in reversed(payloads):
            if payload.get("message") == match_prompt:
                return _elements(payload)
        return []
    if which == "all":
        out: "list[InvokedSkill]" = []
        for payload in payloads:
            out.extend(_elements(payload))
        return out
    if which == "latest":
        return _elements(payloads[-1]) if payloads else []
    raise ValueError(f"which must be 'latest' or 'all', got {which!r}")


def _transcript_from_event(event: "dict[str, Any]") -> "str | None":
    for key in _TRANSCRIPT_KEYS:
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def invoked_skills_from_event(
    event: "dict[str, Any]",
    *,
    which: str = "latest",
    match_prompt: "str | bool | None" = None,
) -> "list[InvokedSkill]":
    """Like :func:`invoked_skills_from_transcript`, but reads ``transcript_path``
    from a Codex hook event dict.

    ``match_prompt`` may be ``True`` to use the event's own ``prompt`` as the
    pin (convenience), a string to pin an explicit value, or ``None`` to use
    ``which``. Returns ``[]`` if the event carries no transcript path.
    """
    transcript = _transcript_from_event(event)
    if transcript is None:
        return []
    pin: "str | None"
    if match_prompt is True:
        prompt = event.get("prompt")
        pin = prompt if isinstance(prompt, str) else None
    elif isinstance(match_prompt, str):
        pin = match_prompt
    else:
        pin = None
    return invoked_skills_from_transcript(transcript, which=which, match_prompt=pin)


def was_skill_invoked(
    source: "str | Path | dict[str, Any]",
    name: str,
    *,
    namespace: "str | None" = None,
    which: str = "latest",
) -> bool:
    """Convenience boolean: was ``name`` explicitly invoked?

    ``source`` may be a rollout path or a hook event dict.
    """
    if isinstance(source, dict):
        skills = invoked_skills_from_event(source, which=which)
    else:
        skills = invoked_skills_from_transcript(source, which=which)
    return any(skill.matches(name, namespace=namespace) for skill in skills)


def match_invocation_in_text(prompt: str, known: Iterable[str]) -> "list[str]":
    """Best-effort fallback: detect ``$name`` / ``/name`` / ``:name`` invocations
    of *known* commands by anchored text matching.

    Use this only for invocation forms that do **not** appear in rollout
    ``text_elements`` (notably the ``/prompts:<name>`` menu). Matching is
    anchored to line-start or whitespace with a trailing word boundary to avoid
    false positives on quoted/embedded literals (the same discipline a robust
    hand-rolled detector needs). Returns the matched names in declaration order,
    de-duplicated.
    """
    if not prompt:
        return []
    found: "list[str]" = []
    for name in known:
        if not name:
            continue
        pattern = re.compile(r"(?:^|\s)[$/:]" + re.escape(name) + r"\b", re.MULTILINE)
        if pattern.search(prompt) and name not in found:
            found.append(name)
    return found
