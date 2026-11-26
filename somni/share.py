"""Sharing: turn what a memory learned about one topic into a skill other people's agents can load.

A share is a folder with a SKILL.md (the Agent Skills format: frontmatter `name` and `description`, then markdown) and
an EVIDENCE.md. The agent writes the guidelines in its own words and cites the claims each one rests on. Somni adds
what backs each guideline: how many records, over how many days, what the user said, what the tools returned and what
outside outcomes (CI, a grader, a benchmark) found. EVIDENCE.md lists each record's chain hash, so the author can later
show that a record is the one the evidence counted, without the share holding its text.

A draft is refused when its text holds something personal: home paths, the user's or the machine's name, the git
identity, emails, IP addresses, or anything shaped like a key. Nothing is published: the folder is written beside the
store for the user to read, edit and share as they choose.

This module doesn't import the store (store.py also runs as a script); the store passes its memory objects in.
"""
import datetime
import getpass
import json
import os
import pathlib
import re
import socket
import subprocess
from collections import Counter

NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
MAX_NAME = 64
MAX_DESCRIPTION = 1024
MAX_INTRO = 4000
MAX_GUIDELINES = 30
MAX_GUIDELINE = 600
MAX_CITED = 8
MATERIAL_CLAIMS = 60
SHARES_DIR = "shares"

KINDS = {"ordinary_observation": "user message", "agent_output": "agent reply", "tool_result": "tool result",
         "outcome": "outcome"}

GUIDE = (
    "Share pass. Write a skill that other people's agents can load, from what this memory learned about the topic "
    "below, and call somni_share once.\n"
    "- Pick the claims that are about the topic and would help someone else in their own projects. Leave out what "
    "fits only this user or this project: names, paths, hosts, accounts, private repos, customers. When the topic "
    "needs one of those, say it generally (\"the deploy host\", \"your CI\").\n"
    "- Write each guideline as an instruction to an agent, in your own words, general enough to apply elsewhere. "
    "Cite the claim IDs it rests on, as listed (p: ones too). A guideline may rest on several claims.\n"
    "- Prefer verified claims, claims an outside outcome confirmed (ci, grader, benchmark), and claims learned over "
    "many days. Leave out claims an outcome contradicted. Don't overstate: a guideline that rests on one record says "
    "so (\"in one case ...\"), or is left out.\n"
    "- name: lowercase-hyphenated, up to 64 characters. description: what the skill does and when to load it, for an "
    "agent deciding whether to (up to 1024 characters). title: an optional heading. intro: an optional short paragraph.\n"
    "- Somni adds each guideline's evidence and writes EVIDENCE.md with the records' hashes; don't write those "
    "yourself. It refuses a draft with personal details in it (home paths, the user's name or username, the machine's "
    "name, emails, IP addresses, anything shaped like a key); fix what it lists and call again.\n")


class ShareError(ValueError):
    pass


def _day(stamp):
    return stamp[:10] if isinstance(stamp, str) else None


def evidence(memory, claim, record_hash):
    """What backs one claim: its evidence records (kind, day, result, chain hash) and how the claim has fared."""
    records = []
    for item in claim["evidence"]:
        record = memory._records_by_id.get(item["ref"])
        if record is None:
            continue
        row = {"id": record["id"], "kind": KINDS.get(record["source"], record["source"]), "day": _day(record.get("at")),
               "sha256": record_hash(record)}
        if record["source"] == "tool_result" and record.get("tool_ok") is not None:
            row["result"] = "ok" if record["tool_ok"] else "failed"
        if record["source"] == "outcome":
            details = record.get("outcome", {})
            row["result"] = details.get("verdict", "no verdict")
            if details.get("by"):
                row["by"] = details["by"]
        if record["source"] == "ordinary_observation" and any(
                tag == "contributor" or tag.startswith("contributor:") for tag in record.get("tags", ())):
            row["kind"] = "sub-agent message"
        records.append(row)
    return {"claim": claim["id"], "status": claim["status"], "records": records,
            "revisions": len(memory.history(claim)), "uses": len(memory._uses(claim))}


def summarize(items, *, notes=True):
    """One line on what backs a guideline, from the evidence of the claims it cites."""
    records = {row["id"] + ("" if not item.get("personal") else "p"): row for item in items for row in item["records"]}
    rows = list(records.values())
    days = sorted({row["day"] for row in rows if row["day"]})
    kinds = Counter(row["kind"] for row in rows)
    parts = [f"{len(rows)} record(s)" + (f" over {len({*days})} day(s), {days[0]} to {days[-1]}" if len(days) > 1 else
                                         f" on {days[0]}" if days else "")]
    said = kinds["user message"]
    if said:
        parts.append(f"the author said it {said}×" if said > 1 else "the author said it once")
    tools = [row for row in rows if row["kind"] == "tool result"]
    if tools:
        failed = sum(row.get("result") == "failed" for row in tools)
        parts.append(f"{len(tools)} tool result(s)" + (f", {failed} failed" if failed else ""))
    outcomes = [row for row in rows if row["kind"] == "outcome"]
    if outcomes:
        verdicts = Counter(row["result"] for row in outcomes)
        by = sorted({row["by"] for row in outcomes if row.get("by")})
        parts.append("outcomes " + ", ".join(f"{n} {v}" for v, n in sorted(verdicts.items())) +
                     (f" ({', '.join(by)})" if by else ""))
    unverified = sum(item["status"] != "verified" for item in items)
    if unverified and notes:
        parts.append(f"{unverified} of {len(items)} note(s) unverified")
    revised = sum(item["revisions"] for item in items)
    if revised:
        parts.append(f"revised {revised}×")
    return "; ".join(parts)


def _line(claim_id, claim, item, text):
    summary = summarize([item], notes=False)  # the line shows its status already
    return f"- {claim_id} [{claim['status']}; {summary}] {text}"


def material(memory, personal, topic, *, record_hash, personal_claim):
    """The claims a share pass picks from: those matching the topic (or the strongest, with no topic), with what
    backs each one. Returns {"guide", "claims", "n"}."""
    def pick(store):
        if store is None:
            return []
        active = [claim for claim in store.claims.values() if claim["state"] == "active"]
        if topic and topic.strip():
            hits = [hit for hit in store.recall(topic, limit=50) if hit["state"] == "active"]
            return [store.claims[hit["id"]] for hit in hits][:MATERIAL_CLAIMS]
        active.sort(key=lambda c: (c["status"] != "verified", -c["salience"], c["id"]))
        return active[:MATERIAL_CLAIMS]
    lines = []
    for store, prefix in ((memory, ""), (personal, "p:")):
        for claim in pick(store):
            item = evidence(store, claim, record_hash)
            lines.append(_line(prefix + claim["id"], claim, item, claim["text"]))
    return {"guide": GUIDE, "claims": "\n".join(lines), "n": len(lines)}


def _identity(cwd, deny="", home=None):
    """Words that name the user or this machine: never in a share."""
    words = set()
    home = home or str(pathlib.Path.home())
    for value in (getpass.getuser(), socket.gethostname().split(".")[0], pathlib.Path(home).name):
        if value and len(value) >= 3:
            words.add(value)
    for key in ("user.name", "user.email"):
        try:
            # The backend runs with a bare environment; git finds the user's global config through HOME.
            out = subprocess.run(["git", "config", "--get", key], cwd=cwd, capture_output=True, text=True, timeout=5,
                                 env={**os.environ, "HOME": home})
        except (OSError, subprocess.SubprocessError):
            continue
        value = out.stdout.strip()
        if value:
            words.add(value)
            if key == "user.name":
                words.update(part for part in value.split() if len(part) >= 3)
    for entry in (deny or os.environ.get("SOMNI_NEVER_SHARE", "")).split(","):
        if len(entry.strip()) >= 2:
            words.add(entry.strip())
            words.update(part for part in entry.split() if len(part) >= 3)
    return words


PERSONAL_PATTERNS = [
    ("a home path", re.compile(r"(?:/home/|/Users/|[A-Za-z]:\\+Users\\+)[^\s/\\]+")),
    ("an email address", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    ("an IP address", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("a key or token", re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,}|"
                                  r"AKIA[0-9A-Z]{16}|xox[abprs]-[\w-]{10,}|AIza[\w-]{30,})")),
    ("a private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY")),
    ("a long random string", re.compile(r"(?<![\w/+=-])(?=[A-Za-z0-9+/_=-]*\d)(?=[A-Za-z0-9+/_=-]*[A-Za-z])"
                                        r"[A-Za-z0-9+/_=-]{40,}(?![\w/+=-])")),
]


def scan(texts, *, identity, paths):
    """Personal details in the share's text: a list of "<field>: <what> (<found>)"."""
    found = []
    for field, text in texts:
        for what, pattern in PERSONAL_PATTERNS:
            for match in pattern.finditer(text):
                found.append(f"{field}: {what} ({match[0][:40]})")
        for path in paths:
            if path and len(path) > 1 and path in text:
                found.append(f"{field}: a local path ({path})")
        for word in identity:
            if re.search(rf"(?<![\w]){re.escape(word)}(?![\w])", text, re.IGNORECASE):
                found.append(f"{field}: a name that identifies the user or this machine ({word})")
    return sorted(set(found))


def _resolve(claim_id, memory, personal, personal_prefix):
    if claim_id.startswith(personal_prefix):
        store, local = personal, claim_id[len(personal_prefix):]
        if store is None:
            raise ShareError(f"{claim_id}: personal memory is off in this session")
    else:
        store, local = memory, claim_id
    claim = store.claims.get(local)
    if claim is None:
        raise ShareError(f"{claim_id}: no such claim")
    if claim["state"] != "active":
        raise ShareError(f"{claim_id}: only current claims can back a share (this one is {claim['state']})")
    return store, claim


def build(memory, personal, proposal, *, record_hash, personal_prefix, version, cwd, deny="", home=None):
    """Validate a share proposal, check it for personal details and write SKILL.md and EVIDENCE.md.
    Returns {"path", "name", "guidelines", "replaced"}."""
    if not isinstance(proposal, dict):
        raise ShareError("a share is an object with name, description, intro and guidelines")
    name, description = proposal.get("name"), proposal.get("description")
    intro, guidelines = proposal.get("intro") or "", proposal.get("guidelines")
    title = proposal.get("title") or name.replace("-", " ").capitalize()
    if not isinstance(title, str) or not title.strip() or len(title) > 80 or "\n" in title:
        raise ShareError("title must be one line up to 80 characters")
    if not isinstance(name, str) or not NAME.fullmatch(name) or len(name) > MAX_NAME:
        raise ShareError("name must be lowercase words joined by hyphens, up to 64 characters")
    if not isinstance(description, str) or not description.strip() or len(description) > MAX_DESCRIPTION:
        raise ShareError("description must be text up to 1024 characters")
    if not isinstance(intro, str) or len(intro) > MAX_INTRO:
        raise ShareError("intro must be text up to 4000 characters")
    if not isinstance(guidelines, list) or not 1 <= len(guidelines) <= MAX_GUIDELINES:
        raise ShareError(f"guidelines must be a list of 1 to {MAX_GUIDELINES}")
    rows = []
    for number, guideline in enumerate(guidelines, 1):
        text, cited = (guideline.get("text"), guideline.get("claims")) if isinstance(guideline, dict) else (None, None)
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_GUIDELINE:
            raise ShareError(f"guideline {number}: text must be up to {MAX_GUIDELINE} characters")
        if not isinstance(cited, list) or not 1 <= len(cited) <= MAX_CITED or not all(isinstance(c, str) for c in cited):
            raise ShareError(f"guideline {number}: cite 1 to {MAX_CITED} claim IDs in claims")
        items = []
        for claim_id in dict.fromkeys(cited):
            store, claim = _resolve(claim_id, memory, personal, personal_prefix)
            item = evidence(store, claim, record_hash)
            item["claim"] = claim_id
            item["personal"] = store is personal
            items.append(item)
        rows.append({"text": " ".join(text.split()), "items": items})
    texts = [("name", name), ("title", title), ("description", description), ("intro", intro)]
    texts += [(f"guideline {n}", row["text"]) for n, row in enumerate(rows, 1)]
    texts += [(f"guideline {n} evidence", " ".join(item.get("by", "") for i in row["items"] for item in i["records"]))
              for n, row in enumerate(rows, 1)]
    paths = {str(pathlib.Path(cwd).resolve()), str(memory.workspace.resolve()), home or str(pathlib.Path.home())}
    problems = scan(texts, identity=_identity(cwd, deny, home), paths=paths)
    if problems:
        raise ShareError("this draft has personal details in it; rewrite these and call again: " + "; ".join(problems))

    folder = memory.workspace / SHARES_DIR / name
    replaced = folder.exists()
    folder.mkdir(parents=True, exist_ok=True)
    today = datetime.date.today().isoformat()
    body = [f"# {title.strip()}", ""]
    if intro.strip():
        body += [intro.strip(), ""]
    body += ["## Guidelines", ""]
    for n, row in enumerate(rows, 1):
        body += [f"{n}. {row['text']}", f"   *Evidence: {summarize(row['items'])}. [E{n}](EVIDENCE.md#e{n})*", ""]
    days = sorted({r["day"] for row in rows for i in row["items"] for r in i["records"] if r["day"]})
    span = f" between {days[0]} and {days[-1]}" if len(days) > 1 else f" on {days[0]}" if days else ""
    body += ["---", "", f"Learned by pi-somni from the author's own work{span}, and exported on {today}. Each guideline "
             "rests on notes its author's agent made from records of that work: what the author said, what tools "
             "returned, and what outside checks found. [EVIDENCE.md](EVIDENCE.md) lists them.", ""]
    front = ["---", f"name: {name}", f"description: {json.dumps(' '.join(description.split()), ensure_ascii=False)}",
             "metadata:", f"  generated-by: pi-somni {version}", f"  exported: {today}", "---", ""]
    (folder / "SKILL.md").write_text("\n".join(front + body), encoding="utf-8")

    heads = [f"- Project memory: transcript head `{memory.state['transcript_head']}` "
             f"({memory.state['transcript_count']} records)"]
    if personal is not None and any(i["personal"] for row in rows for i in row["items"]):
        heads.append(f"- Personal memory: transcript head `{personal.state['transcript_head']}` "
                     f"({personal.state['transcript_count']} records); its record IDs start with p:")
    out = [f"# Evidence for {name}", "",
           f"Exported on {today} by pi-somni {version}. Every guideline in SKILL.md rests on notes (claims) in the "
           "author's Somni memory, and every note on records of their sessions. Records are hash-chained: each one "
           "holds the hash of the record before it. A record's hash below is SHA-256 over its canonical JSON (sorted "
           "keys, no spaces), so the author can show a record to anyone who asks and they can check it is the one "
           "counted here. The share holds no record's text.", "",
           "When the share was made, the chains ended at:", *heads, "",
           "Kinds of record: a *user message* is something the author typed; a *tool result* is what a tool returned, "
           "ok or failed; an *outcome* is a verdict recorded afterwards from outside the agent (CI, a grader, a "
           "reviewer, a benchmark). *Verified* notes cite exact quotes from their records; *unverified* ones were "
           "inferred. *Revised* counts the times a note was updated because things changed. *Uses* counts the "
           "records and sessions that cited or recalled the note.", ""]
    for n, row in enumerate(rows, 1):
        out += [f'<a id="e{n}"></a>', f"## E{n}. {row['text']}", "", f"{summarize(row['items'])}.", ""]
        for item in row["items"]:
            out.append(f"- Note `{item['claim']}`: {item['status']}, revised {item['revisions']}×, {item['uses']} use(s)")
        out += ["", "| Record | Kind | Day | Result | SHA-256 |", "|---|---|---|---|---|"]
        for item in row["items"]:
            prefix = personal_prefix if item["personal"] else ""
            for record in item["records"]:
                result = record.get("result", "") + (f" ({record['by']})" if record.get("by") else "")
                out.append(f"| {prefix}{record['id']} | {record['kind']} | {record['day'] or ''} | {result} | "
                           f"`{record['sha256']}` |")
        out.append("")
    (folder / "EVIDENCE.md").write_text("\n".join(out), encoding="utf-8")
    return {"path": str(folder), "name": name, "guidelines": len(rows), "replaced": replaced}
