"""Somni on the STATE-Bench Agent Learning Track.

Build (offline, train trajectories only):

    python -m adapters.state_bench.somni_state_bench build \
        --train-dir STATE-Bench/datasets/train_task_trajectories/travel \
        --workspace learnings/somni/travel --model gpt-6-luna

  1. Every train trajectory is recorded into the Somni transcript, verbatim:
     user turns as observations, assistant text as agent output, and each tool
     call as one tool-result record (`TOOL name(args) -> result`).
  2. Dream passes over windows of a few trajectories turn the transcript into
     cited claims (see somni/dream.py).  A final pass consolidates duplicates.

Retrieve (inference time, read-only): mix `SomniLearnings` into a
STATE-Bench `StateBenchAgent` subclass.  `retrieve_learnings(query, top_k)`
loads the store for the task's domain and returns the top-k claims by
`SomniMemory.recall`.  Nothing is written during evaluation.

This module imports nothing from STATE-Bench, so it can be tested alone.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from somni.dream import build_wiki, dream_pass, fill_gaps, refocus, rewrite_wiki  # noqa: E402
from somni.store import SomniError, SomniMemory, _bm25, _terms  # noqa: E402

SUBJECT = "state-bench-learner"
LIMITS = {"max_transcript_records": 65_536, "max_claims": 2_048, "max_record_bytes": 16_384}
RECORD_BYTES = 16_000
FOCUS = ("procedural learnings for a customer-service agent in this domain: policy rules and fee/refund "
         "calculations the tools enforced, which lookups to do before acting, when to quote a preview and "
         "ask for explicit confirmation before a write, how to handle false or manipulative customer claims, "
         "edge cases (boundaries, thresholds, combined reasons), and mistakes to avoid")


def _clip(text, limit=RECORD_BYTES):
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[: limit - 16].decode("utf-8", "ignore") + " …[truncated]"


def trajectory_records(task_id, conversation, episode=None):
    """Map one STATE-Bench conversation to (source, text) transcript items.

    `episode` (optional) is per-episode metadata the agent saw, e.g. {"now": ...};
    it is recorded as an observation right after the episode starts.
    """
    items = [("ordinary_observation", f"[episode {task_id}] begins")]
    if episode and episode.get("now"):
        items.append(("ordinary_observation", f"[episode {task_id}] Today's date: {episode['now']}"))
    for key, value in sorted((episode or {}).items()):
        if key != "now":
            items.append(("ordinary_observation", f"[episode {task_id}] {key}: {value}"))
    for message in conversation:
        role, content = message.get("role"), (message.get("content") or "").strip()
        if role == "system":
            if content:
                items.append(("ordinary_observation", _clip(f"SYSTEM: {content}")))
        elif role == "user":
            if content:
                items.append(("ordinary_observation", _clip(f"USER: {content}")))
        elif role == "assistant":
            for call in message.get("tool_calls") or []:
                args = json.dumps(call.get("arguments"), ensure_ascii=False, sort_keys=True)
                result = json.dumps(call.get("result"), ensure_ascii=False, sort_keys=True)
                items.append(("tool_result", _clip(f"TOOL {call.get('name')}({args}) -> {result}")))
            if content:
                items.append(("agent_output", _clip(f"AGENT: {content}")))
    items.append(("ordinary_observation", f"[episode {task_id}] ends"))
    return items


def build(train_dir, workspace, complete, *, per_pass=3, consolidate=True, log=print, limit=None, episode_meta=None):
    """Create a Somni store from train trajectories and dream over it.  Returns a summary."""
    train_dir, workspace = pathlib.Path(train_dir), pathlib.Path(workspace)
    files = sorted(train_dir.glob("*.json"), key=lambda p: (int(p.stem.split("-")[0]) if p.stem.split("-")[0].isdigit() else 0, p.stem))
    if limit:
        files = files[:limit]
    workspace.mkdir(parents=True, exist_ok=True)
    memory = SomniMemory.create(workspace, SUBJECT, "state-bench", train_dir.name, limits=LIMITS)
    memory.set_phase("open")
    spans = []
    for path in files:
        conversation = json.loads(path.read_text()).get("conversation") or []
        ids = memory.record_many(trajectory_records(path.stem, conversation, _episode_for(path.stem, episode_meta)))
        spans.append((path.stem, ids[0], ids[-1]))
    memory.set_phase("closed")
    memory.load()
    log(f"recorded {len(memory.transcript)} transcript records from {len(files)} trajectories")
    by_id = {record["id"]: index for index, record in enumerate(memory.transcript)}
    passes = []
    started = time.time()
    for start in range(0, len(spans), per_pass):
        group = spans[start:start + per_pass]
        window = memory.transcript[by_id[group[0][1]]: by_id[group[-1][2]] + 1]
        trace = dream_pass(memory, window, complete, focus=FOCUS)
        trace["episodes"] = [name for name, _, _ in group]
        passes.append(trace)
        memory.load()
        active = sum(c["state"] == "active" for c in memory.claims.values())
        log(f"dream {len(passes)}: episodes {trace['episodes'][0]}..{trace['episodes'][-1]} -> "
            f"{[a.get('accepted') for a in trace['attempts']]} accepted, {active} active claims "
            f"({time.time() - started:.0f}s)")
    if consolidate:
        passes.extend(_consolidate(memory, complete, log))
    memory.load()
    summary = {"trajectories": len(files), "episode_meta": bool(episode_meta), "metrics": memory.metrics(), "passes": passes}
    (workspace / "build-trace.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    return summary


CONSOLIDATE_FOCUS = ("CONSOLIDATION ONLY: do not add new topics. Merge near-duplicate claims by correcting the "
                     "best one (combine their evidence, max 16 refs) and retracting the others with reason "
                     "'duplicate of nXXXXXX'. Retract claims that are episode-specific trivia. Raise salience of "
                     "broadly useful rules, lower it for narrow ones")


def _consolidate(memory, complete, log, rounds=2):
    traces = []
    for round_number in range(rounds):
        memory.load()
        active = [c for c in memory.claims.values() if c["state"] == "active"]
        if len(active) < 30:
            break
        refs = sorted({e["ref"] for c in active for e in c["evidence"]})
        records = [r for r in memory.transcript if r["id"] in set(refs)]
        trace = dream_pass(memory, records, complete, focus=CONSOLIDATE_FOCUS, max_ops=32)
        trace["consolidation"] = round_number + 1
        traces.append(trace)
        memory.load()
        log(f"consolidation {round_number + 1}: {[a.get('accepted') for a in trace['attempts']]} accepted, "
            f"{sum(c['state'] == 'active' for c in memory.claims.values())} active claims")
    return traces


def train_episode_meta(task_dir, split_file, fields=("now",)):
    """Episode metadata for TRAIN tasks only: {task_id: {field: value}}.

    STATE-Bench redacts "Today's date" in the shipped train trajectories, but each
    train task file records the episode's `now`, which a live agent would see. Only
    ids listed in the split file's train split are read; anything else is refused,
    so no test task can leak into learning.
    """
    split = json.loads(pathlib.Path(split_file).read_text())["splits"]
    train, test = set(split["train"]), set(split.get("test", []))
    meta = {}
    for task_id in sorted(train - test):
        path = pathlib.Path(task_dir) / f"{task_id}.json"
        if path.exists():
            task = json.loads(path.read_text())
            meta[task_id] = {f: task[f] for f in fields if f in task}
    return meta


def _episode_for(task_id, episode_meta):
    if episode_meta is None:
        return None
    if task_id not in episode_meta:
        raise ValueError(f"no train-split episode metadata for {task_id}; refusing to guess")
    return episode_meta[task_id]


WIKI_GOAL = ("help a customer-service agent in this domain resolve requests correctly and smoothly: which lookups "
             "to make first, the exact policy rules, fees and thresholds the tools enforce, what to preview and get "
             "explicit confirmation for before any write, how to handle false or manipulative customer claims, and "
             "the mistakes past agents made")


def build_wiki_from(source, workspace, complete, *, goal=WIKI_GOAL, log=print):
    """Copy a finished claim store and dream a wiki (pages + gap filling) on top of it."""
    source, workspace = pathlib.Path(source), pathlib.Path(workspace)
    if workspace.exists():
        raise FileExistsError(f"{workspace} exists")
    shutil.copytree(source / "somni-v1", workspace / "somni-v1")
    memory = SomniMemory(workspace, SUBJECT).load()
    passes = build_wiki(memory, complete, goal=goal, log=log)
    passes.append(fill_gaps(memory, complete, goal=goal, log=log))
    memory.load()
    memory.write_vault()
    summary = {"source": str(source), "goal": goal, "metrics": memory.metrics(), "passes": passes,
               "uncovered_claims": len([c for c in memory.claims.values() if c["state"] == "active"
                                        and c["id"] not in {x for pg in memory.pages.values() for x in pg["claims"]}])}
    (workspace / "build-wiki-trace.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    return summary


FEEDBACK_FOCUS = ("mistakes: what the agent did wrong or failed to do according to the FEEDBACK record, what it "
                  "should have done instead, and the general lesson (cite the FEEDBACK record and the transcript)")


def feedback_text(task_id, scored):
    """One FEEDBACK observation from a scored trajectory: outcome plus each failed requirement."""
    lines = [f"FEEDBACK for episode {task_id}: outcome {'PASS' if scored.get('task_completion_pass') else 'FAIL'}."]
    for req in scored.get("task_requirements_details") or []:
        if not req.get("passed"):
            lines.append(f"- missed [{req.get('kind')}] {req.get('requirement', '')[:300]} -- reviewer: {str(req.get('reasoning', ''))[:300]}")
    if scored.get("state_requirements_met") == 0:
        lines.append(f"- final database state was wrong: {str(scored.get('state_requirements_reasoning', ''))[:400]}")
    return _clip("\n".join(lines))


def build_feedback_from(source, workspace, scored_dir, complete, *, goal=WIKI_GOAL, log=print):
    """Focus memory on the goal using feedback from the agent's own scored TRAIN episodes.

    Records each scored train episode and its FEEDBACK into the transcript, dreams cited
    claims about the failed ones, reflects on the failures to revise the wiki instructions,
    rewrites the wiki, and fills gaps.
    """
    source, workspace = pathlib.Path(source), pathlib.Path(workspace)
    if workspace.exists():
        raise FileExistsError(f"{workspace} exists")
    shutil.copytree(source / "somni-v1", workspace / "somni-v1")
    memory = SomniMemory(workspace, SUBJECT).load()
    memory.set_phase("open")
    failed, summary_lines = [], []
    for path in sorted(pathlib.Path(scored_dir).glob("*.json")):
        scored = json.loads(path.read_text())
        if scored.get("task_completion_pass") is None:
            continue
        items = trajectory_records(f"review-{path.stem}", scored.get("conversation") or [])
        items.insert(-1, ("ordinary_observation", feedback_text(path.stem, scored)))
        ids = memory.record_many(items)
        if not scored.get("task_completion_pass"):
            failed.append((path.stem, ids[0], ids[-1]))
            summary_lines.append(feedback_text(path.stem, scored))
    memory.set_phase("closed")
    memory.load()
    log(f"recorded {len(failed)} failed review episodes")
    by_id = {r["id"]: i for i, r in enumerate(memory.transcript)}
    passes = []
    for start in range(0, len(failed), 2):
        group = failed[start:start + 2]
        window = memory.transcript[by_id[group[0][1]]: by_id[group[-1][2]] + 1]
        trace = dream_pass(memory, window, complete, focus=FEEDBACK_FOCUS)
        trace["review_episodes"] = [g[0] for g in group]
        passes.append(trace)
        memory.load()
    instructions, reflect = refocus(memory, complete, goal=goal, failures="\n".join(summary_lines), log=log)
    passes.append(reflect)
    if instructions:
        passes.extend(rewrite_wiki(memory, complete, goal=goal, instructions=instructions, log=log))
    passes.extend(build_wiki(memory, complete, goal=goal, max_passes=3, log=log))
    passes.append(fill_gaps(memory, complete, goal=goal, log=log))
    memory.load()
    memory.write_vault()
    summary = {"source": str(source), "scored_dir": str(scored_dir), "failed_episodes": len(failed), "goal": goal,
               "instructions": instructions, "metrics": memory.metrics(), "passes": passes}
    (workspace / "build-feedback-trace.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    return summary


class SomniLearnings:
    """Mixin for a STATE-Bench StateBenchAgent subclass (read-only retrieval).

    The store for a domain is ``$SOMNI_STATE_BENCH_ROOT/<domain>`` unless the
    subclass sets ``somni_root``.
    """

    somni_root = None
    _cache = {}

    def _somni_memory(self):
        domain = getattr(getattr(self, "runtime_context", None), "domain", None) or os.environ.get("STATE_BENCH_DOMAIN")
        root = pathlib.Path(self.somni_root or os.environ["SOMNI_STATE_BENCH_ROOT"])
        workspace = root / domain
        key = str(workspace)
        if key not in SomniLearnings._cache:
            SomniLearnings._cache[key] = SomniMemory(workspace, SUBJECT).load()
        return SomniLearnings._cache[key]

    def retrieve_learnings(self, query: str, top_k: int = 3) -> list[str]:
        try:
            claims = self._somni_memory().recall(query[:2000], limit=top_k)
        except SomniError:
            return []
        return [f"{c['text']} (memory {c['id']}; {c['status']}; {len(c['evidence'])} cited records)" for c in claims]


class SomniWikiLearnings(SomniLearnings):
    """Retrieval over the wiki: the best-matching pages (whole), then claims if fewer than top_k pages."""

    def retrieve_learnings(self, query: str, top_k: int = 3) -> list[str]:
        try:
            memory = self._somni_memory()
            pages = memory.recall_pages(query[:2000], limit=top_k)
            out = [f"# {p['title']}\n{p['body']}\n(Somni wiki page `{p['slug']}`, built from {len(p['claims'])} cited claims)"
                   for p in pages]
            if len(out) < top_k:
                out += [f"{c['text']} (memory {c['id']}; {c['status']})" for c in memory.recall(query[:2000], limit=top_k - len(out))]
            return out
        except SomniError:
            return []


EPISODE = re.compile(r"^\[episode ([^\]]+)\] begins$")


def episodes(memory):
    """Split the transcript into {episode_id: [records]} using the adapter's episode markers."""
    out, current = {}, None
    for record in memory.transcript:
        match = EPISODE.match(record["text"])
        if match:
            current = match.group(1)
            out[current] = []
        if current is not None:
            out[current].append(record)
    return out


def episode_digest(task_id, records, limit=3_000):
    """A compact case: the request, the tool calls in order (with arguments), and the final answer."""
    lines = [f"Similar past case ({task_id}):"]
    users = [r["text"] for r in records if r["text"].startswith("USER: ")]
    if users:
        lines.append(users[0][:500])
    for r in records:
        if r["source"] == "tool_result" and r["text"].startswith("TOOL "):
            lines.append("- " + r["text"].split(" -> ", 1)[0][5:][:220])
    agents = [r["text"] for r in records if r["source"] == "agent_output"]
    if agents:
        lines.append("Final answer: " + agents[-1][7:][:700])
    text = "\n".join(lines)
    return text if len(text) <= limit else text[: limit - 1] + "…"


class SomniWikiSearchLearnings(SomniWikiLearnings):
    """The essay's best configuration: wiki pages plus transcript search.

    Returns the top (top_k - 1) wiki pages and one digest of the most similar past episode,
    found by BM25 over whole episodes in the transcript.
    """

    def retrieve_learnings(self, query: str, top_k: int = 3) -> list[str]:
        pages = SomniWikiLearnings.retrieve_learnings(self, query, max(1, top_k - 1))[: max(1, top_k - 1)]
        try:
            memory = self._somni_memory()
            key = id(memory)
            if SomniWikiSearchLearnings._episode_cache.get("key") != key:
                eps = episodes(memory)
                SomniWikiSearchLearnings._episode_cache = {"key": key, "eps": eps, "docs": {
                    k: _terms_for(" ".join(r["text"] for r in v)) for k, v in eps.items()}}
            cache = SomniWikiSearchLearnings._episode_cache
            ids = list(cache["eps"])
            ranked = _rank(ids, [cache["docs"][i] for i in ids], query)
            if ranked:
                pages.append(episode_digest(ranked[0], cache["eps"][ranked[0]]))
        except SomniError:
            pass
        return pages[:top_k]

    _episode_cache = {}


def _terms_for(text):
    return _terms(text)


def _rank(ids, docs, query):
    return [item for _, item in _bm25(docs, ids, query)]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build Somni learnings from STATE-Bench train trajectories")
    sub = ap.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--train-dir", required=True)
    b.add_argument("--workspace", required=True)
    b.add_argument("--model", default="gpt-6-luna")
    b.add_argument("--reasoning-effort", default="medium")
    b.add_argument("--per-pass", type=int, default=3)
    b.add_argument("--limit", type=int, default=None, help="only the first N trajectories (smoke tests)")
    b.add_argument("--task-dir", help="STATE-Bench domain tasks/ dir: add each TRAIN episode's `now` (needs --split-file)")
    b.add_argument("--split-file", help="STATE-Bench splits/train_test.json; only its train ids are read")
    w = sub.add_parser("build-wiki", help="dream a wiki on top of a finished claim store")
    w.add_argument("--from", dest="source", required=True, help="workspace of a finished build")
    w.add_argument("--workspace", required=True)
    w.add_argument("--model", default="gpt-6-luna")
    w.add_argument("--reasoning-effort", default="medium")
    f = sub.add_parser("build-feedback", help="refocus a wiki store on the agent's own scored TRAIN episodes")
    f.add_argument("--from", dest="source", required=True)
    f.add_argument("--workspace", required=True)
    f.add_argument("--scored-dir", required=True, help="scored trajectories of TRAIN tasks only")
    f.add_argument("--model", default="gpt-6-luna")
    f.add_argument("--reasoning-effort", default="medium")
    r = sub.add_parser("recall")
    r.add_argument("--workspace", required=True)
    r.add_argument("query")
    r.add_argument("--top-k", type=int, default=3)
    args = ap.parse_args(argv)
    if args.command == "build-feedback":
        from somni.llm import openai_completer
        complete = openai_completer(args.model, reasoning_effort=args.reasoning_effort)
        summary = build_feedback_from(args.source, args.workspace, args.scored_dir, complete)
        summary["model"], summary["model_usage"] = args.model, complete.usage.as_dict()
        (pathlib.Path(args.workspace) / "build-feedback-trace.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
        print(json.dumps({k: v for k, v in summary.items() if k != "passes"}, indent=2))
        return 0
    if args.command == "build-wiki":
        from somni.llm import openai_completer
        complete = openai_completer(args.model, reasoning_effort=args.reasoning_effort)
        summary = build_wiki_from(args.source, args.workspace, complete)
        summary["model"], summary["model_usage"] = args.model, complete.usage.as_dict()
        (pathlib.Path(args.workspace) / "build-wiki-trace.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
        print(json.dumps({k: v for k, v in summary.items() if k != "passes"}, indent=2))
        return 0
    if args.command == "build":
        from somni.llm import openai_completer
        complete = openai_completer(args.model, reasoning_effort=args.reasoning_effort)
        meta = train_episode_meta(args.task_dir, args.split_file) if args.task_dir else None
        summary = build(args.train_dir, args.workspace, complete, per_pass=args.per_pass, limit=args.limit, episode_meta=meta)
        summary["model_usage"] = complete.usage.as_dict()
        print(json.dumps({k: v for k, v in summary.items() if k != "passes"}, indent=2))
        trace_path = pathlib.Path(args.workspace) / "build-trace.json"
        trace = json.loads(trace_path.read_text())
        trace["model"], trace["model_usage"] = args.model, complete.usage.as_dict()
        trace_path.write_text(json.dumps(trace, indent=2, ensure_ascii=False) + "\n")
    else:
        memory = SomniMemory(args.workspace, SUBJECT).load()
        for claim in memory.recall(args.query, limit=args.top_k):
            print(f"{claim['id']} [{claim['score']}] {claim['text']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
