# Evaluating Somni

The evaluation harness lives under [`evals/state_bench/`](evals/state_bench/). The STATE-Bench
Agent Learning Track measures task completion with stored learnings. The offline replay measures
the cost of Somni's Python backend. [Pi integration tests](#verification-tests) exercise the
extension with a scripted model.

## Inputs and work directory

Use this Somni checkout and an upstream [STATE-Bench](https://github.com/microsoft/STATE-Bench)
checkout. The offline replay was verified with STATE-Bench commit
[`5644b18`](https://github.com/microsoft/STATE-Bench/tree/5644b1838d96bc4483da29642d058ecaa6f80f7f).
The replay selects events directly from `datasets/train_task_trajectories/<domain>/` in
deterministic order. Generate memory stores and scored trajectories locally under
`evals/state_bench/work/`, which Git ignores.

To prepare a fresh data checkout next to Somni, run:

```bash
git clone https://github.com/microsoft/STATE-Bench.git ../STATE-Bench
git -C ../STATE-Bench checkout --detach 5644b1838d96bc4483da29642d058ecaa6f80f7f
```

From the Somni checkout:

```bash
STATE_BENCH_ROOT="$(cd ../STATE-Bench && pwd)"
EVAL_WORK="$PWD/evals/state_bench/work"
mkdir -p "$EVAL_WORK"
```

Python 3.10+ is sufficient for replay and synthetic tests. Scored runs additionally use
STATE-Bench's Python 3.12+ environment. Set `OPENAI_API_KEY` in your shell for model-backed
learning builds and OpenAI agent runs. For the stock scorer, configure the simulator and judge
credentials described in the [upstream learning track guide](https://github.com/microsoft/STATE-Bench/blob/5644b1838d96bc4483da29642d058ecaa6f80f7f/docs/AGENT_LEARNING_TRACK.md).

## Offline backend replay

```bash
python3 -m evals.state_bench.replay --state-bench-root "$STATE_BENCH_ROOT" \
  --domain travel --events 100 --persistent > "$EVAL_WORK/replay-travel.json"
```

The persistent mode sends events one at a time to the same Python child, in training episode
order. The replay records user prompts, tool results, and assistant output. It measures index
generation on user turns and omits the training adapter's episode markers and system messages.
The JSON report includes per-event timings, medians, and p95 values. Set `TMPDIR`
to a directory on the filesystem you want to measure. On Linux, set `TMPDIR=/dev/shm`
to time a disposable RAM-backed store. Omit `--persistent` to measure one process per
event, or add `--separate-user-events` to time separate recording and index calls on
each user turn. `--somni-root` selects another Somni checkout for comparisons.

## STATE-Bench Agent Learning Track

### Build a memory store from training data

- **`build`** records the training conversations, then dreams over windows of three trajectories.
- **`build-wiki`** builds a wiki on a finished claim store, fills gaps, and promotes claims.
- **`build-feedback`** refocuses a wiki using a scored **training** run. Add
  `--balanced` to include successful and failed episodes together.

```bash
TRACK_MODEL=gpt-6-luna   # choose a model available to your account
python3 -m evals.state_bench.somni_state_bench build \
  --train-dir "$STATE_BENCH_ROOT/datasets/train_task_trajectories/travel" \
  --workspace "$EVAL_WORK/claims/travel" --model "$TRACK_MODEL"
python3 -m evals.state_bench.somni_state_bench build-wiki \
  --from "$EVAL_WORK/claims/travel" --workspace "$EVAL_WORK/wiki/travel" --model "$TRACK_MODEL"
```

Each output workspace is create-only. Use a new directory for another model or build.
STATE-Bench redacts the episode date in its training trajectories. Provide `--task-dir` and
`--split-file` with the paths to the domain's `tasks/` directory and `splits/train_test.json`
to recover the `now` field for train IDs only. The published wiki + search setup uses
the supplied train trajectories without the added dates.

### Score held-out episodes

The included [agent](evals/state_bench/agents/somni.py) subclasses STATE-Bench's
`StateBenchAgent` and adds its read-only `retrieve_learnings(query, top_k=3)` hook. It reads
`$SOMNI_STATE_BENCH_ROOT/<domain>/somni-v1`. Run the stock baseline and the Somni condition on
the same test split and record the agent model, reasoning effort, scorer, Somni commit,
and store provenance. The example runs one replicate in travel. Use separate output
directories for additional conditions or model configurations.

```bash
(cd "$STATE_BENCH_ROOT" && uv sync --locked --no-dev)
export STATE_BENCH_AGENT_MODEL="$TRACK_MODEL"
export SOMNI_STATE_BENCH_ROOT="$EVAL_WORK/wiki"
benchmark_args=(
  --domain travel --split test --agent-provider openai --agent-api-key-var OPENAI_API_KEY
  --agent-model-name "$TRACK_MODEL" --agent-model-reasoning-level medium
  --num-runs 1 --num-workers 1 --retrieve-learnings-top-k 3
)
"$STATE_BENCH_ROOT/.venv/bin/python" -m evals.state_bench.run_track -- \
  "${benchmark_args[@]}" --output-dir "$EVAL_WORK/baseline/travel"
"$STATE_BENCH_ROOT/.venv/bin/python" -m evals.state_bench.run_track -- \
  "${benchmark_args[@]}" --agent-class SomniWikiSearchAgent --output-dir "$EVAL_WORK/somni/travel"
```

The wrapper uses STATE-Bench's locked scoring configuration by default. Place
`--scorer-model gpt-6-luna` before the `--` to use the unofficial historical
configuration. The wrapper records that override in each trajectory. For a model sweep, keep
the scorer and test split fixed. For scoring details and the metrics command, see the
[upstream run_batch guide](https://github.com/microsoft/STATE-Bench/blob/5644b1838d96bc4483da29642d058ecaa6f80f7f/docs/eval/run-batch.md).

### Retrieval variants

| Mixin | `retrieve_learnings(query, top_k=3)` returns |
|---|---|
| `SomniLearnings` | Top-k claims |
| `SomniWikiLearnings` | Top-k pages, then claims |
| `SomniWikiSearchLearnings` | Top pages plus a digest of the most similar past episode |
| `SomniLayeredLearnings` | Promoted index first, then content pages |
| `SomniBriefingLearnings` | Model-written briefing for the situation, then the best pages |

The historical `adapters.state_bench.somni_state_bench` entry point forwards to the evaluation
module.

## Offline dreaming over trajectories

`somni/dream.py` holds the model-authored dreaming operations. Each takes a
`complete(system, prompt) -> json_text` function. `somni/llm.py` supplies one for the OpenAI
Responses API using only the standard library. Every pass records accepted operations and
rejection reasons in the learning trace, `dreams.jsonl`.

| Function | Essay operation | What it does |
|---|---|---|
| `dream_pass` | correct memory | Turns a transcript window into cited claims; can correct, retract or promote |
| `build_wiki` | build new representations | Gathers claims not yet on a page into linked subject pages, until nearly all are covered |
| `fill_gaps` | fill gaps, promote | Merges duplicate pages, repairs stale ones, writes an overview page, promotes key claims |
| `refocus` + `rewrite_wiki` | focus on a goal | Reflects on recorded feedback, revises the wiki instructions, and rewrites pages under them |
| `brief` | (reading the wiki) | At retrieval time, writes a short situation-specific briefing from the index and best pages |

## Published results

The Python adapter was evaluated on the [STATE-Bench](https://github.com/microsoft/STATE-Bench) Agent Learning Track
(v0.8.1, 50 held-out test tasks per domain, three runs per condition). Learnings were built from
the shipped train trajectories only.

`gpt-6-luna` played agent, user simulator and judge. The runs used an **unofficial** configuration
because the leaderboard protocol locks the simulator and judge to GPT-5.4. Compare these
conditions within this experiment. Δ is percentage points of pass@1 against the same agent
without memory, paired by task, with 95% bootstrap intervals.

| Configuration | Travel | Customer support | Shopping | Pooled |
|---|---:|---:|---:|---:|
| No memory (pass@1) | 56.7% | 62.7% | 53.3% | 57.6% |
| **Somni wiki + search** (recommended) | **+9.3** [+1.3, +17.3] | +1.3 [−9.3, +12.0] | +3.3 [−4.0, +10.7] | **+4.7** [−0.4, +9.8] |
| Somni claims only | +8.0 [−0.7, +16.7] | +1.3 [−8.0, +10.0] | +4.7 [−0.7, +10.7] | +4.7 [+0.2, +9.1] |
| Somni wiki (pages only) | +6.7 | +1.3 | +3.3 | +3.8 |
| Somni wiki refocused on train feedback | +4.0 | +3.3 | +2.7 | +3.3 |

- **Completion:** the recommended setup raises pooled pass@1 from 57.6% to 62.2%. That's about
  8% relative, and it fixes 11% of the baseline's failures. The gain is largest in travel, small
  in shopping, and not detectable in customer support.
- **Conversation quality:** the judge's 1–5 UX score improves in travel (+0.23 [+0.04, +0.45])
  and customer support (+0.68 [+0.37, +0.99]). Claims-only memory gets about half of that.
- **Cost:** about 2× the baseline's agent tokens per task, because whole pages are retrieved.
  Claims only costs about 1.4×. The one-time build used about 130 model calls (≈1.5M tokens)
  for all three domains.
- **Not detected:** in customer support, memory made the agent better at the conversational
  requirements (asking, confirming, explaining) but sometimes over-cautious. Its final-state
  accuracy dropped slightly, so completion stayed flat.
- **What didn't help:** recording the episode date. Dreaming only over failures also made the
  agent over-cautious, consistent with the essay's warning about a "record only failures" rule.

The historical wiki + search results used the adapter at `c319416`. The task-paired bootstrap
resampled 4,000 sets of per-task score differences, after averaging each task over repeats.
Only complete scored runs entered the comparison.

### Recommended setup

1. **Build claims:** `build` over your trajectories, dreaming in windows of about three episodes.
2. **Build the wiki:** `build-wiki` gathers the claims into linked pages, then `fill_gaps` merges
   them, adds an overview and promotes key claims. Open `somni-v1/vault/` in Obsidian to review
   what was learned.
3. **Retrieve:** use `SomniWikiSearchLearnings`, which returns the two best pages plus the most
   similar past case.

For interactive use, Pi exposes `/somni-dream`, `/somni-wiki`, and `somni_recall`.

`SomniLayeredLearnings`, `SomniBriefingLearnings` and `build-feedback --balanced` are
implemented and tested, but were not fully evaluated: the evaluation budget ran out.

## Verification tests

From the Somni repository root:

```bash
python3 -m unittest discover -s tests -t .
python3 -m unittest discover -s evals/state_bench/tests -t .
# Run the Pi client test using the local Pi executable and a scripted model:
PI_PLUGIN_E2E_BINARY="$(command -v pi)" python3 -m unittest tests.test_extension_e2e -v
# Or use the test image:
docker build -t pi-plugin-e2e:pi-1.0.0 tests/e2e && python3 -m unittest tests.test_extension_e2e
```
