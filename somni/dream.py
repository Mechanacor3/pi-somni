"""Model-authored dreaming: turn a window of transcript into cited claims.

A dream pass shows a model (any ``complete(system, prompt) -> str`` callable)
a window of transcript records plus the current active claims, asks for a JSON
proposal of add/correct/retract operations, and submits it to
``SomniMemory.dream(..., partial=True)``.  Every claim must cite transcript IDs;
``verified`` claims must quote exact excerpts, which the store checks.  Invalid
operations are rejected individually and recorded in the pass, so the store's
dream log doubles as a learning trace.  If nothing in a proposal survives, the
model gets one retry with the rejection reasons.
"""
from __future__ import annotations

import json

from .store import SomniError

DEFAULT_FOCUS = ("durable lessons that would help in future, similar situations: rules the environment "
                 "enforced, required order of steps or tool calls, checks and confirmations that mattered, "
                 "thresholds and calculations, and common traps or failure modes")

SYSTEM = """You consolidate an agent's memory offline ("dreaming").

You see numbered transcript records (IDs like t000123) and the current memory claims (IDs like n000004).
Propose operations as one JSON object:

{"schema": 1, "operations": [OP, ...]}

OP is one of:
  {"op": "add", "text": CLAIM, "status": "verified"|"uncertain", "salience": 1-5,
   "evidence": [{"ref": "t000123", "quote": "exact excerpt"}], "links": []}
  {"op": "correct", "claim_id": "n000004", "text": ..., "status": ..., "salience": ..., "evidence": [...], "links": []}
  {"op": "retract", "claim_id": "n000004", "reason": "why it is wrong or redundant", "evidence": [{"ref": "t000123"}]}

Rules the store enforces (violations are rejected):
- Every evidence ref must be a transcript ID shown to you, at most once per claim, 1-16 refs.
- A quote must be copied EXACTLY (character for character) from the cited record. Keep quotes short (3-15 words).
- "verified" needs a quote for every ref; use "uncertain" for interpretations you cannot quote directly.
- links may only name existing claim IDs. correct/retract may only target active claims.

Write claims that generalize: no customer names, booking IDs, dates or one-off amounts unless the amount is a
policy constant. One idea per claim, phrased as guidance ("Before X, do Y because Z."). Prefer correcting an
existing claim over adding a near-duplicate. Salience 5 = applies to most future situations; 1 = rare edge case.
Return at most {max_ops} operations. Return only the JSON object."""


def render_records(records, max_chars=2_400):
    lines = []
    for record in records:
        text = record["text"]
        if len(text) > max_chars:
            text = text[:max_chars] + " …[truncated]"
        lines.append(f"[{record['id']}] ({record['source']}) {text}")
    return "\n".join(lines)


def render_claims(memory, limit=120):
    active = [c for c in memory.claims.values() if c["state"] == "active"]
    active.sort(key=lambda c: (-c["salience"], c["id"]))
    lines = [f"[{c['id']}] (salience {c['salience']}, {c['status']}, evidence "
             f"{','.join(e['ref'] for e in c['evidence'])}) {c['text']}" for c in active[:limit]]
    if len(active) > limit:
        lines.append(f"… {len(active) - limit} lower-salience claims not shown")
    return "\n".join(lines) or "(no claims yet)"


def _parse(text):
    try:
        value = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise SomniError(f"proposal is not JSON: {exc}") from exc
    if isinstance(value, dict) and "operations" in value:
        value = {"schema": 1, "operations": value["operations"]}
    return value


def dream_pass(memory, records, complete, *, focus=DEFAULT_FOCUS, max_ops=12, retries=1):
    """Run one model-authored pass over ``records``.  Returns a trace dict.

    The session must be closed (dreaming is offline).  Never raises for a bad
    proposal; the trace says what happened.
    """
    prompt = (f"Focus: {focus}.\n\n## Current claims\n{render_claims(memory)}\n\n"
              f"## Transcript window\n{render_records(records)}\n")
    system = SYSTEM.replace("{max_ops}", str(max_ops))
    trace = {"window": [records[0]["id"], records[-1]["id"]] if records else None, "attempts": []}
    for attempt in range(retries + 1):
        try:
            raw = complete(system, prompt)
        except Exception as exc:  # a refused or failed model call skips this pass, not the build
            trace["attempts"].append({"accepted": 0, "model_error": str(exc)[:300]})
            return trace
        try:
            proposal = _parse(raw)
            if isinstance(proposal, dict) and isinstance(proposal.get("operations"), list):
                proposal["operations"] = proposal["operations"][:max_ops]
            record = memory.dream(proposal, partial=True)
            trace["attempts"].append({"accepted": record["operation_count"], "rejected": record.get("rejected", [])})
            trace["dream_seq"] = record["seq"]
            return trace
        except SomniError as exc:
            trace["attempts"].append({"accepted": 0, "error": str(exc)[:600]})
            prompt += (f"\n\n## Your previous proposal was rejected\n{str(exc)[:1500]}\n"
                       "Fix the problems (copy quotes exactly, cite only shown IDs) and return a new JSON object.")
    return trace
