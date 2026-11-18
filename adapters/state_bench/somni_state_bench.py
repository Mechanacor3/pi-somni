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
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from somni.dream import dream_pass  # noqa: E402
from somni.store import SomniError, SomniMemory  # noqa: E402

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
    r = sub.add_parser("recall")
    r.add_argument("--workspace", required=True)
    r.add_argument("query")
    r.add_argument("--top-k", type=int, default=3)
    args = ap.parse_args(argv)
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
