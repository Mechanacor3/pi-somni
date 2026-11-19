#!/usr/bin/env python3
"""Replay STATE-Bench training events through Somni without a model or an API key."""

import argparse
from collections import defaultdict
from contextlib import ExitStack
import importlib.util
import json
from itertools import islice
import math
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[2]


def elapsed_ms(call, *args, **kwargs):
    start = time.perf_counter()
    value = call(*args, **kwargs)
    return value, round((time.perf_counter() - start) * 1000, 3)


def summarize(values):
    ordered = sorted(values)
    return {
        "calls": len(values),
        "median_ms": round(statistics.median(values), 2),
        "p95_ms": round(ordered[math.ceil(0.95 * len(ordered)) - 1], 2),
        "total_ms": round(sum(values), 2),
    }


def clipped(text):
    data = text.encode()
    if len(data) <= 8000:
        return text
    return data[:7968].decode(errors="replace") + " …[truncated]"


def load_adapter(somni):
    # Older Somni revisions put the adapter under adapters/.
    path = somni / "evals/state_bench/somni_state_bench.py"
    if not path.is_file():
        path = somni / "adapters/state_bench/somni_state_bench.py"
    if not path.is_file():
        raise ValueError(f"no STATE-Bench adapter found in {somni}")
    sys.path.insert(0, str(somni))
    try:
        spec = importlib.util.spec_from_file_location("_replay_adapter", path)
        if spec is None or spec.loader is None:
            raise ValueError(f"no STATE-Bench adapter found in {somni}")
        adapter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(adapter)
    finally:
        sys.path.pop(0)
    return adapter


def train_events(directory, trajectory_records):
    def trajectory_order(path):
        prefix = path.stem.split("-", 1)[0]
        return int(prefix) if prefix.isdigit() else 0, path.stem

    for file in sorted(directory.glob("*.json"), key=trajectory_order):
        conversation = json.loads(file.read_text(encoding="utf-8"))["conversation"]
        for source, content in trajectory_records(file.stem, conversation):
            if source != "ordinary_observation" or content.startswith("USER: "):
                yield file.stem, source, clipped(content)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-bench-root", type=Path, required=True, help="path to a STATE-Bench checkout")
    parser.add_argument("--somni-root", type=Path, default=ROOT, help="Somni checkout or source snapshot to replay")
    parser.add_argument("--domain", default="travel", choices=["travel", "customer_support", "shopping_assistant"])
    parser.add_argument("--events", type=int, default=100)
    parser.add_argument("--separate-user-events", action="store_true",
                        help="reproduce the old record-then-inject extension behavior")
    parser.add_argument("--persistent", action="store_true", help="replay through one persistent Python child")
    args = parser.parse_args()
    if args.events < 1:
        parser.error("--events must be positive")
    train = args.state_bench_root / "datasets/train_task_trajectories" / args.domain
    if not train.is_dir():
        parser.error(f"STATE-Bench training directory does not exist: {train}")
    somni = args.somni_root.resolve()
    adapter = load_adapter(somni)
    events = list(islice(train_events(train, adapter.trajectory_records), args.events))
    if len(events) < args.events:
        parser.error(f"found only {len(events)} training events in {train}")
    durations, steps = defaultdict(list), []

    def invoke(*cmd):
        return subprocess.run(cmd, capture_output=True, check=True)

    with ExitStack() as cleanup:
        tmp = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="somni-profile-"))
        cli = [sys.executable, "-I", str(somni / "somni/store.py"), "--workspace", tmp, "--subject-id", "pi"]
        invoke(*cli, "init", "--lineage-id", "latency", "--attempt-id", f"{args.domain}-train")
        invoke(*cli, "session-open")
        sequence = 0
        if args.persistent:
            start = time.perf_counter()
            child = cleanup.enter_context(subprocess.Popen(
                [*cli, "serve", "--record"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            ))
            cleanup.callback(child.stdin.close)
            ready = json.loads(child.stdout.readline())
            if not ready.get("result", {}).get("writable"):
                raise RuntimeError(f"could not start writable backend: {ready}")
            durations["backend_startup"].append((time.perf_counter() - start) * 1000)

            def request(method, **fields):
                nonlocal sequence
                sequence += 1
                child.stdin.write(json.dumps({"id": sequence, "method": method, "args": fields}) + "\n")
                child.stdin.flush()
                reply = json.loads(child.stdout.readline())
                if reply["id"] != sequence or "error" in reply:
                    raise RuntimeError(f"backend request failed: {reply}")
                return reply["result"]

        for position, (episode, source, text) in enumerate(events, 1):
            user = source == "ordinary_observation"
            combined = user and not args.separate_user_events
            if args.persistent:
                _, ms = elapsed_ms(request, "record", source=source, text=text, index_bytes=4096 if combined else None)
            else:
                extra = ("--inject-max-bytes", "4096") if combined else ()
                _, ms = elapsed_ms(invoke, *cli, "record", "--source", source, "--text", text, *extra)
            metric = "record_and_inject" if combined else "record"
            step = {"step": position, "episode": episode, "source": source, f"{metric}_ms": ms}
            durations[metric].append(ms)
            durations[f"{metric}_{source}"].append(ms)
            total = ms
            if user and not combined:
                if args.persistent:
                    _, ms = elapsed_ms(request, "inject", index_bytes=4096)
                else:
                    _, ms = elapsed_ms(invoke, *cli, "inject", "--max-bytes", "4096")
                step["inject_ms"] = ms
                total += ms
                durations["inject_after_user"].append(ms)
            durations["user_event" if user else "non_user_event"].append(total)
            durations["all_events"].append(total)
            steps.append(step)
        memory = adapter.SomniMemory(tmp, "pi").load()
        if len(memory.transcript) != args.events:
            raise RuntimeError("replay did not persist every event")
        with tempfile.TemporaryDirectory(prefix="somni-profile-probe-") as probe_root:
            shutil.copytree(memory.store, Path(probe_root) / "somni-v1")
            probe = adapter.SomniMemory(probe_root, "pi")
            for _ in range(12):
                _, ms = elapsed_ms(invoke, sys.executable, "-I", "-c", "pass")
                durations["python_startup"].append(ms)
                _, ms = elapsed_ms(invoke, sys.executable, "-I", "-c",
                                   "import sys; sys.path.insert(0, sys.argv[1]); import store",
                                   str(somni / "somni"))
                durations["python_startup_and_store_import"].append(ms)
                _, ms = elapsed_ms(memory.load)
                durations["load_and_verify_final_store"].append(ms)
                _, ms = elapsed_ms(memory.inject, 4096)
                durations["render_index_on_loaded_store"].append(ms)
                _, ms = elapsed_ms(probe.record, "ordinary_observation", "latency probe")
                durations["in_process_record_after_replay"].append(ms)
    print(json.dumps({
        "domain": args.domain, "split": "train", "events": len(events),
        "combined_user_events": not args.separate_user_events,
        "persistent": args.persistent,
        "summary": {name: summarize(values) for name, values in durations.items()},
        "steps": steps,
    }, indent=2))


if __name__ == "__main__":
    main()
