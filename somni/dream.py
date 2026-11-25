"""Model-authored dreaming: turn a window of transcript into cited claims.

A dream pass shows a model (any ``complete(system, prompt) -> str`` callable)
a window of transcript records plus the current active claims, asks for a JSON
proposal of add/correct/update/retract operations, and submits it to
``SomniMemory.dream(..., partial=True)``.  Every claim must cite transcript IDs;
``verified`` claims must quote exact excerpts, which the store checks.  Invalid
operations are rejected individually and recorded in the pass, so the store's
dream log doubles as a learning trace.  If nothing in a proposal survives, the
model gets one retry with the rejection reasons.
"""
from __future__ import annotations

import json
import os

from .store import SomniError

DEFAULT_FOCUS = ("durable lessons that would help in future, similar situations: rules the environment "
                 "enforced, required order of steps or tool calls, checks and confirmations that mattered, "
                 "thresholds and calculations, and common traps or failure modes")

WIKI_LINK_GUIDE = ("Wiki links can point to pages created later in the same pass and can form cycles. "
                   "Every link must resolve at the end of the pass. " + 'Group pages in folders when a topic has several pages: a slug may be a path such as topic/page or topic/subtopic/page (at most 3 levels). Link with the full path, [[topic/page]].')

SYSTEM = """You consolidate an agent's memory offline ("dreaming").

You see numbered transcript records (IDs like t000123) and the current memory claims (IDs like n000004).
Propose operations as one JSON object:

{"schema": 1, "operations": [OP, ...]}

OP is one of:
  {"op": "add", "text": CLAIM, "status": "verified"|"uncertain", "salience": 1-5,
   "evidence": [{"ref": "t000123", "quote": "exact excerpt"}], "links": []}
  {"op": "correct", "claim_id": "n000004", "text": ..., "status": ..., "salience": ..., "evidence": [...], "links": []}
  {"op": "update", "claim_id": "n000004", "text": ..., "status": ..., "salience": ..., "evidence": [...], "links": []}
     (the world changed: n000004 was true until the newer evidence; it is kept as history, not as a mistake)
  {"op": "merge", "claim_ids": ["n000004", "n000007"], "text": "Combined guidance", "status": "uncertain", "salience": 3}
  {"op": "retract", "claim_id": "n000004", "reason": "why it is wrong or redundant", "evidence": [{"ref": "t000123"}]}
  {"op": "page", "slug": "lowercase-hyphenated or folder/page", "title": "...", "body": "markdown", "claims": ["n000004", ...]}
  {"op": "drop_page", "slug": "...", "reason": "merged into other-slug"}
  {"op": "promote", "claim_id": "n000004", "salience": 5}   (raise a broadly useful claim up the index)

Rules the store enforces (violations are rejected):
- Every evidence ref must be a transcript ID shown to you, at most once per claim, 1-16 refs.
- A quote must be copied EXACTLY (character for character) from the cited record. Keep quotes short (3-15 words).
- "verified" needs a quote for every ref; use "uncertain" for interpretations you cannot quote directly.
- links may only name existing claim IDs. correct/update/retract may only target active claims.
- update needs at least one evidence record newer than every record the old claim cites.
- merge synthesizes two or more distinct active claims with related guidance. The store inherits their
  evidence and keeps their provenance. Use a page when the claims are better kept as separate ideas.
- Pages (wiki): a page gathers what is known about ONE subject. Its body is markdown that cites claims
  inline as [n000004] (each cited ID must be in "claims", which must be active claims) and links pages
  as [[slug]]. """ + WIKI_LINK_GUIDE + """
  Writing a page with an existing slug replaces it (new version). When dropping a page, remove or update
  all links to it in the same pass.

Write claims that generalize. Omit names, identifiers and one-off details unless they matter in future
situations. One idea per claim, phrased as guidance ("Before X, do Y because Z."). Prefer correcting an
existing claim over adding a near-duplicate. Use salience 3 for ordinary reusable lessons, 1–2 for narrow
cases, and 4 for recurring situations. Reserve 5 for at most two claims that matter in most future runs.
Add, correct and merge may include a one-line gist of up to 160 UTF-8 bytes for the index. Keep the full
guidance in text.
""" + "\n" + "{time_guide}" + """
Return at most {max_ops} operations. Return only the JSON object."""


def render_pages(memory, body_chars=400):
    if not memory.pages:
        return "(no pages yet)"
    out = []
    for slug, page in sorted(memory.pages.items()):
        body = page["body"].replace("\n", " ")
        out.append(f"[[{slug}]] {page['title']} (v{page['version']}; claims {','.join(page['claims'])}; links {','.join(page['links']) or '-'}): "
                   f"{body[:body_chars]}{'…' if len(body) > body_chars else ''}")
    return "\n".join(out)


TIME_GUIDE = ("Records are dated (day headers in the listing). When a record disagrees with a claim, decide what "
              "happened. If the world changed, the claim was true until then: use update, which keeps it as history. "
              "If the claim was wrong all along, use correct or retract. Never leave two active claims that disagree; "
              "when both were true at different times, update the older one and say since when in the text. Age is "
              "not doubt: never write 'verify before relying', 'may be stale' or 'check the files first' into a claim "
              "that nothing has contradicted. Date it instead ('as of 2026-01-12'); it stays the best information "
              "until a newer record changes it.")


OUTCOME_GUIDE = ("OUTCOME records come from outside the agent, after the fact: a grader, CI or tests, a reviewer, the "
                 "user. About whether something worked they outrank the agent's own reports and its counters; when they "
                 "disagree, the outcome is right, and a claim about the result should cite the outcome (an exact quote "
                 "from it can be verified). Each outcome shows the agent's report it judges. When the agent said it was "
                 "done and the outcome says it failed, that pair is the most useful thing to learn from: write what the "
                 "agent should have checked before reporting success, citing both records.")


def describe_outcome(record, report=None):
    """The label and the paired agent report for an outcome record in a dream listing."""
    details = record.get("outcome", {})
    label = "; ".join(filter(None, ["OUTCOME, from outside the agent", details.get("by") and f"by {details['by']}",
                                    details.get("verdict") and f"verdict {details['verdict']}"]))
    pair = ""
    if report:
        pair = f"\n    judges the agent's report [{report['ref']}]: {report['text']}"
        if report.get("overclaim"):
            pair += "\n    The agent reported success; this outcome says it failed."
    return label, pair


def render_records(records, max_chars=2_400, memory=None):
    lines = []
    day = None
    for record in records:
        if record.get("at") and record["at"][:10] != day:
            day = record["at"][:10]
            lines.append(f"--- {day} ---")
        text = record["text"]
        if len(text) > max_chars:
            text = text[:max_chars] + " …[truncated]"
        source, pair = record["source"], ""
        if source == "outcome":
            report = record.get("report") or (memory.outcome_report(record) if memory is not None else None)
            source, pair = describe_outcome(record, report)
        run = f" [run: {record['run_label']}]" if record.get("run_label") else ""
        lines.append(f"[{record['id']}] ({source}){run} {text}{pair}")
    return "\n".join(lines)


def render_claims(memory, limit=120):
    active = [c for c in memory.claims.values() if c["state"] == "active"]
    active.sort(key=lambda c: (-c["salience"], c["id"]))

    def timing(claim):
        clause = memory.describe_time(claim)
        return f"; {clause}" if clause else ""
    lines = [f"[{c['id']}] (salience {c['salience']}, {c['status']}, evidence "
             f"{','.join(e['ref'] for e in c['evidence'])}{timing(c)}) {c['text']}" for c in active[:limit]]
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


def dream_pass(memory, records, complete, *, focus=DEFAULT_FOCUS, max_ops=12, retries=1, prompt_override=None):
    """Run one model-authored pass over ``records``.  Returns a trace dict.

    The session must be closed (dreaming is offline).  Never raises for a bad
    proposal; the trace says what happened.
    """
    prompt = prompt_override or (f"Focus: {focus}.\n\n## Current claims\n{render_claims(memory)}\n\n"
                                 f"## Transcript window\n{render_records(records, memory=memory)}\n")
    guide = TIME_GUIDE + ("\n" + OUTCOME_GUIDE if any(r.get("source") == "outcome" for r in records) else "")
    system = SYSTEM.replace("{max_ops}", str(max_ops)).replace("{time_guide}", guide)
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


PAGE_GUIDE = ("Write a practical playbook for one subject. Explain when it applies, which steps and checks matter, "
              "and how to avoid common mistakes. Include details the cited claims support and that help a future "
              "agent act. Keep each page under about 5 KB.")


def page_guide(override=None):
    guide = os.environ.get("SOMNI_PAGE_GUIDE", "") if override is None else override
    if not isinstance(guide, str):
        raise SomniError("wiki page guide must be text")
    return guide.strip() or PAGE_GUIDE


def _uncovered(memory):
    covered = {c for p in memory.pages.values() for c in p["claims"]}
    active = [c for c in memory.claims.values() if c["state"] == "active" and c["id"] not in covered]
    return sorted(active, key=lambda c: (-c["salience"], c["id"]))


def build_wiki(memory, complete, *, goal, max_passes=8, pages_per_pass=6, log=print):
    """Dreaming that builds representations: gather active claims into linked subject pages.

    Repeats until (almost) every active claim is cited by some page or `max_passes` is reached.
    Returns a list of pass traces.
    """
    traces = []
    for number in range(max_passes):
        memory.load()
        uncovered = _uncovered(memory)
        active = sum(c["state"] == "active" for c in memory.claims.values())
        if not uncovered or len(uncovered) <= max(2, active // 20):
            break
        claims = "\n".join(f"[{c['id']}] (salience {c['salience']}) {c['text']}" for c in uncovered[:120])
        prompt = (f"GOAL for this memory: {goal}\n\nBUILD THE WIKI. {page_guide()} Write or extend at most {pages_per_pass} pages "
                  f"so that the claims below are gathered under the subjects they belong to; extend an existing page (same slug) "
                  f"when it already covers the subject. Use only page and correct operations.\n\n"
                  f"## Existing pages\n{render_pages(memory)}\n\n## Claims not yet on any page\n{claims}\n")
        trace = dream_pass(memory, [], complete, prompt_override=prompt, max_ops=pages_per_pass + 4)
        trace["wiki_pass"] = number + 1
        traces.append(trace)
        memory.load()
        log(f"wiki pass {number + 1}: {[a.get('accepted') for a in trace['attempts']]} accepted; "
            f"{len(memory.pages)} pages, {len(_uncovered(memory))} claims uncovered")
    return traces


def fill_gaps(memory, complete, *, goal, log=print):
    """One review pass: merge duplicate pages, repair stale ones, add an overview, promote key claims."""
    memory.load()
    stale = memory.stale_pages()
    top = sorted((c for c in memory.claims.values() if c["state"] == "active"), key=lambda c: (-c["salience"], c["id"]))[:40]
    prompt = (f"GOAL for this memory: {goal}\n\nFILL GAPS AND TIDY THE WIKI. {page_guide()}\n"
              "1. Merge pages that cover the same subject: rewrite the better one with both pages' content and claims, "
              "drop the redundant page and update any links to it in the same pass.\n"
              "2. Repair pages that cite claims which are no longer active (listed below): rewrite them on active claims.\n"
              "3. Write or update an `overview` page that links the main pages ([[slug]]) with one line on when to use each.\n"
              "4. Promote: for up to two claims that apply to most situations, use promote to raise their salience to 5.\n\n"
              f"## Pages\n{render_pages(memory, body_chars=250)}\n\n## Stale pages\n{stale or '(none)'}\n\n"
              "## Highest-salience claims\n" + "\n".join(f"[{c['id']}] (salience {c['salience']}) {c['text']}" for c in top))
    trace = dream_pass(memory, [], complete, prompt_override=prompt, max_ops=24)
    memory.load()
    log(f"fill-gaps: {[a.get('accepted') for a in trace['attempts']]} accepted; {len(memory.pages)} pages")
    trace["fill_gaps"] = True
    return trace


def refocus(memory, complete, *, goal, failures, log=print):
    """Focus memory on a goal (the essay's loop): reflect on failures, revise the instructions.

    `failures` is a short text describing what went wrong in past sessions (for example the
    requirements a reviewer said were missed).  The model reflects and returns revised wiki
    instructions; the caller then rebuilds the wiki with them.  Returns (instructions, trace).
    """
    memory.load()
    prompt = (f"GOAL for this memory: {goal}\n\nREFLECT ON FAILURES. Past sessions failed in the ways listed below, "
              "even though the memory existed. Diagnose what the memory should say, emphasise or warn about so these "
              "failures do not recur, then write revised instructions for rebuilding the wiki. Return JSON "
              '{"diagnosis": "...", "instructions": "concrete guidance for which pages to write and what they must contain"}.\n\n'
              f"## Current pages\n{render_pages(memory, body_chars=300)}\n\n## Failures\n{failures[:24000]}\n")
    trace = {"refocus": True}
    try:
        value = json.loads(complete("You improve an agent's long-term memory. Return only a JSON object.", prompt))
        instructions = str(value.get("instructions", ""))[:4000]
        trace["diagnosis"] = str(value.get("diagnosis", ""))[:2000]
    except Exception as exc:  # a refused call or bad JSON leaves the goal unchanged
        trace["error"] = str(exc)[:300]
        instructions = ""
    trace["instructions"] = instructions
    log(f"refocus: {'new instructions' if instructions else 'unchanged'}")
    return instructions, trace


def rewrite_wiki(memory, complete, *, goal, instructions, max_passes=4, log=print):
    """Rewrite existing pages (same slugs) and add new ones under revised instructions."""
    traces = []
    for number in range(max_passes):
        memory.load()
        active = sorted((c for c in memory.claims.values() if c["state"] == "active"), key=lambda c: (-c["salience"], c["id"]))
        claims = "\n".join(f"[{c['id']}] (salience {c['salience']}) {c['text']}" for c in active[:160])
        prompt = (f"GOAL for this memory: {goal}\n\nREVISED INSTRUCTIONS (from reflecting on failures): {instructions}\n\n"
                  f"REWRITE THE WIKI pass {number + 1}/{max_passes}. {page_guide()} Rewrite the pages that the instructions "
                  "say are missing or misleading (reuse the slug to replace a page) and add pages for uncovered subjects. "
                  "At most 6 page operations.\n\n"
                  f"## Pages\n{render_pages(memory, body_chars=600)}\n\n## Active claims\n{claims}\n")
        trace = dream_pass(memory, [], complete, prompt_override=prompt, max_ops=10)
        trace["rewrite_pass"] = number + 1
        traces.append(trace)
        log(f"rewrite pass {number + 1}: {[a.get('accepted') for a in trace['attempts']]} accepted")
        if not any(a.get("accepted") for a in trace["attempts"]):
            break
    return traces


BRIEF_SYSTEM = ("You are the memory of an agent that is about to handle a live situation. From the memory pages "
                "provided, write a short briefing for THIS situation only: the steps and checks in order, exact numbers "
                "and thresholds that apply, what must be asked or confirmed (and what need not be), and traps to avoid. "
                "Cite the page slug for each point, e.g. (returns-and-refunds). Use at most 250 words. If the pages "
                "contain nothing relevant, say so in one sentence. Return JSON {\"briefing\": \"...\"}.")


def brief(memory, query, complete, *, pages=4, index_bytes=2_000):
    """Retrieval-time reading of the wiki: a situation-specific briefing (read-only)."""
    candidates = [p for p in memory.recall_pages(query, limit=pages + 3) if p["claims"]][:pages]
    index = memory.inject(index_bytes)
    body = "\n\n".join(f"## {p['title']} ({p['slug']})\n{p['body']}" for p in candidates) or "(no pages)"
    raw = complete(BRIEF_SYSTEM, f"SITUATION: {query}\n\n## Memory index\n{index}\n\n## Pages\n{body}")
    return str(json.loads(raw).get("briefing", "")).strip(), candidates
