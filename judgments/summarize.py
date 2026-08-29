"""Generating holdings for judgments that have no published headnote.

The Supreme Court publishes an official headnote, so its holdings are quoted
verbatim (see :mod:`judgments.holding`). The Bombay High Court publishes none,
which leaves a real gap: 42,646 of the 42,788 judgments in the 2026 digest have
no statement of what was decided.

This module fills that gap with a model reading the judgment. The result is
stored under :attr:`Provenance.GENERATED` and is never mixed with headnote text,
because the difference matters: a headnote is the court's own words and can be
cited; this is a reading of the judgment and cannot. The database keeps them in
separate provenance so a user always knows which they are looking at.

Two consequences shape the design. Reading is fallible, so the model is given an
explicit way to say a document states no holding — very many of these are
procedural orders that decide nothing — and that answer is recorded rather than
replaced by an invented summary. And reading costs money per judgment, so every
call reports its token usage and the caller can price a run before committing to
it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .holding import Holding, Provenance

#: Per-million-token prices for the default model, used to price a run.
#: Cache reads bill at roughly a tenth of the input rate.
PRICE_PER_MTOK = {"claude-opus-5": (5.0, 25.0)}

DEFAULT_MODEL = "claude-opus-5"

TOOL_NAME = "record_holding"

#: Stable across every judgment in a run, so it is the right cache breakpoint.
SYSTEM_PROMPT = """You summarise judgments of Indian courts for a legal research database.

For each judgment you are given, state what the court HELD — the point of law or
the determination it made, and the reason for it. You are writing the headnote
the court did not publish.

What matters:

- Report the holding, not the facts. A recitation of who sued whom is not a
  holding. If the court decided a question, say what it decided and on what
  basis.
- Name the provision or principle the decision turned on, but only if the
  judgment names it. Do not supply a section number the text does not contain.
- Many of these documents decide nothing: adjournments, directions to file an
  affidavit, orders listing a matter for a later date, disposal in terms of a
  settlement. That is not a failure to summarise — set `states_a_holding` to
  false and say in one line what the order did instead.
- The text is extracted from PDFs and is often mangled: words may be spaced out
  letter by letter, lines interleaved, headers repeated. Read through it. If it
  is too corrupted to follow, set `states_a_holding` to false and say so.
- Never state a holding the judgment does not support. An accurate "this order
  decides nothing" is far more useful here than a plausible invention, because
  a reader will rely on this without opening the judgment."""

HOLDING_SCHEMA = {
    "type": "object",
    "properties": {
        "states_a_holding": {
            "type": "boolean",
            "description": (
                "True if the document decides a question of law or fact. False "
                "for procedural orders, adjournments, and unreadable text."
            ),
        },
        "holding": {
            "type": "string",
            "description": (
                "What the court held, in two to four sentences. When "
                "states_a_holding is false, one line saying what the order did."
            ),
        },
    },
    "required": ["states_a_holding", "holding"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Usage:
    """Token usage for one call, so a run can be priced from real numbers."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
        )

    def cost(self, model: str = DEFAULT_MODEL) -> float:
        """Estimated USD for this usage."""
        rate_in, rate_out = PRICE_PER_MTOK.get(model, PRICE_PER_MTOK[DEFAULT_MODEL])
        return (
            self.input_tokens * rate_in
            + self.cache_read_tokens * rate_in * 0.1
            + self.output_tokens * rate_out
        ) / 1_000_000


@dataclass(frozen=True)
class Summary:
    holding: Holding
    usage: Usage
    #: False when the model judged the document to decide nothing.
    states_a_holding: bool = True


class Summariser:
    """Wraps the Claude API call that reads one judgment."""

    def __init__(
        self,
        client: object | None = None,
        model: str = DEFAULT_MODEL,
        effort: str = "high",
        max_chars: int = 60_000,
    ):
        # Constructed lazily so importing this module -- and therefore the whole
        # package -- does not require the SDK or credentials to be present.
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self._client = client
        self.model = model
        self.effort = effort
        self.max_chars = max_chars

    def summarise(self, text: str, title: str = "") -> Summary:
        """Read one judgment and return its holding."""
        if not text or not text.strip():
            return Summary(
                Holding("", Provenance.NONE), Usage(), states_a_holding=False
            )

        body = text
        note = ""
        if len(body) > self.max_chars:
            # Truncation is disclosed rather than silent: the holding usually
            # sits at the end of a judgment, so the tail is kept over the middle.
            head, tail = body[: self.max_chars // 3], body[-(2 * self.max_chars // 3):]
            body = f"{head}\n\n[... middle of judgment omitted for length ...]\n\n{tail}"
            note = " (long judgment; middle omitted)"

        message = self._client.messages.create(
            model=self.model,
            max_tokens=4000,
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort},
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[
                {
                    "name": TOOL_NAME,
                    "description": "Record what this judgment held.",
                    "strict": True,
                    "input_schema": HOLDING_SCHEMA,
                }
            ],
            tool_choice={"type": "tool", "name": TOOL_NAME},
            messages=[
                {
                    "role": "user",
                    "content": f"Judgment: {title}{note}\n\n<judgment>\n{body}\n</judgment>",
                }
            ],
        )

        usage = _usage_of(message)

        # A safety decline is not a holding. Recording it as one would put the
        # refusal text into the database as if the court had said it.
        if getattr(message, "stop_reason", None) == "refusal":
            return Summary(Holding("", Provenance.NONE), usage, states_a_holding=False)

        call = next(
            (
                b
                for b in message.content
                if getattr(b, "type", None) == "tool_use" and b.name == TOOL_NAME
            ),
            None,
        )
        if call is None:
            return Summary(Holding("", Provenance.NONE), usage, states_a_holding=False)

        data = call.input if isinstance(call.input, dict) else {}
        states = bool(data.get("states_a_holding"))
        text_out = str(data.get("holding") or "").strip()

        if not text_out:
            return Summary(Holding("", Provenance.NONE), usage, states_a_holding=False)

        return Summary(
            Holding(text_out, Provenance.GENERATED), usage, states_a_holding=states
        )


def _usage_of(message: object) -> Usage:
    u = getattr(message, "usage", None)
    if u is None:
        return Usage()
    return Usage(
        input_tokens=getattr(u, "input_tokens", 0) or 0,
        output_tokens=getattr(u, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
    )


def credentials_available() -> bool:
    """Whether the SDK will find a credential without prompting.

    Checked before a run starts so a long job fails immediately with a clear
    message rather than after the first fetch.
    """
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    from pathlib import Path

    return (Path.home() / ".config" / "anthropic").exists()
