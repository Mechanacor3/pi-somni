# Settings and `somni doctor`

Settings live in a YAML-style file. `~/.config/somni/somni.conf` (or `$XDG_CONFIG_HOME/somni/somni.conf`) holds your
personal defaults, and `.pi/somni.conf` in the directory where you start Pi holds settings for one project:

```yaml
# .pi/somni.conf
actions:
  enabled: true
  tools: [read, grep, mcp__tracker__get_issue]
daydreams:
  every_minutes: 30
wiki.link_style: wikilink      # a dotted key works too
```

For each setting, an environment variable that is set wins, then the project file, then the user file, then the
default. `SOMNI_CONFIG=/path/file` reads that one file instead of both, and `SOMNI_CONFIG=none` reads none. The
format is a strict subset of YAML: nested `key: value` maps indented with spaces, `#` comments, `[a, b]` and `- item`
lists, `{a: 1}` maps, quoted strings, and `|` or `>` blocks for long text. A misspelled key, a value of the wrong
type, or a section used as a setting is reported with its file and line, and that setting keeps its previous value.
Pi warns about these at session start.

`/somni-doctor` (or `/somni-doctor all`) checks everything offline, without a model call. The same check runs outside
Pi as `bin/somni doctor` from the package directory, and `--json` gives a machine-readable report. It shows:
- the config files it read, and each problem with its file and line;
- the backend Python (3.10+), and Pillow when thumbnails are on;
- the store: verified, its counts, a subject mismatch, a full transcript, failed dreams, and a store inside a Git
  repository that doesn't ignore it;
- what a feature change needs. Examples: action words that may call a shell tool, an allowlist naming a tool no
  session offered, words in the store that aren't loaded, a word file that won't parse, daydreams without recording,
  a vault that will switch link style at the next pass;
- every setting that differs from its default, with where it came from.

It changes nothing. A store with an interrupted write is reported, not repaired; the next Pi session recovers it.
`bin/somni config template` prints a commented file listing every setting at its default, and
`bin/somni config show` prints each setting's value and source.

| Config key | Environment variable | Default | Meaning |
|---|---|---|---|
| `store.path` | `SOMNI_WORKSPACE` | `.pi/somni` | Where the memory store lives. Relative paths are relative to the directory Pi starts in |
| `store.subject` | `SOMNI_SUBJECT` | `pi` | Identity bound into the store's manifest |
| `store.record` | `SOMNI_RECORD` | on | Record sessions automatically. `false` keeps memory retrieval but records nothing (a testbed driver may record instead) |
| `store.python` | `SOMNI_PYTHON` | `python3` | Interpreter for the backend. The config files themselves are read with `SOMNI_PYTHON` from the environment, or `python3` |
| `index.bytes` | `SOMNI_INJECT_BYTES` | `4096` | Size of the memory index injected before each prompt (256–8192) |
| `recording.tool_result_bytes` | `SOMNI_TOOL_RESULT_BYTES` | `4096` | Tool result text kept per record, in UTF-8 bytes (512–8000) |
| `recording.digest_tools` | `SOMNI_DIGEST_TOOLS` | `stream_read` | Tools whose text output is recorded as a byte count and SHA-256 beside their arguments. An empty list disables this |
| `recording.image_bytes` | `SOMNI_IMAGE_BYTES` | `262144` | Stored bytes per image. `0` disables image capture (0–2097152) |
| `recording.image_max_edge` | `SOMNI_IMAGE_MAX_EDGE` | `0` | Optional thumbnail edge in pixels. Needs Pillow in the backend's Python (0–4096) |
| `dreams.records` | `SOMNI_DREAM_RECORDS` | `120` | Recent transcript records sent to `/somni-dream` (1–120) |
| `dreams.record_bytes` | `SOMNI_DREAM_RECORD_BYTES` | `1500` | Text excerpt per dream record, in UTF-8 bytes, including the truncation marker (64–8192) |
| `dreams.max_reads` | `SOMNI_DREAM_MAX_READS` | `16` | Somni evidence reads per dream or wiki turn (1–64) |
| `dreams.max_images` | `SOMNI_DREAM_MAX_IMAGES` | `16` | Distinct images a dream may view, including those attached to its prompt (0–64) |
| `dreams.max_submissions` | `SOMNI_DREAM_MAX_SUBMISSIONS` | `3` | `somni_dream` attempts per dream (1–10) |
| `dreams.cache_minutes` | `SOMNI_DREAM_CACHE_MINUTES` | `60` | How long an interrupted dream's evidence reads are offered to a retry on the same records. `0` disables (0–10080) |
| `dreams.lenient_image_evidence` | `SOMNI_LENIENT_IMAGE_EVIDENCE` | off | Keep a citation's `{ref}` when only its image is wrong, downgrading the claim to uncertain if needed |
| `wiki.link_style` | `SOMNI_LINK_STYLE` | `markdown` | Vault links: `markdown` (relative `[Title](path.md)`) or `wikilink` (Obsidian `[[slug\|Title]]`) |
| `wiki.page_guide` | `SOMNI_PAGE_GUIDE` | practical playbook guidance | Replaces the default wiki-writing guidance |
| `actions.enabled` | `SOMNI_ACTIONS` | off | [Action words](action-words.md): dreamed Python functions that call session tools, registered as Pi tools |
| `actions.tools` | `SOMNI_ACTION_TOOLS` | all non-Somni tools | The tools action words may call |
| `actions.max_calls` | `SOMNI_ACTION_MAX_CALLS` | `16` | Tool calls per action word run (1–64) |
| `daydreams.every_turns` | `SOMNI_DAYDREAM_EVERY_TURNS` | `0` | Start a daydream (see [Daydreams](long-sessions.md#daydreams-experimental)) after this many model turns. `0` is off (0–10000) |
| `daydreams.every_minutes` | `SOMNI_DAYDREAM_EVERY_MINUTES` | `0` | Or start one on this interval. `0` is off (0–1440) |
| `daydreams.per_hour` | `SOMNI_DAYDREAM_PER_HOUR` | `4` | At most this many daydreams start per hour (1–60) |
| `daydreams.model` | `SOMNI_DAYDREAM_MODEL` | the session's model | Model for daydreams, as `provider/id` |
| `daydreams.thinking` | `SOMNI_DAYDREAM_THINKING` | Pi's default | Thinking level for daydreams |
| `daydreams.timeout_seconds` | `SOMNI_DAYDREAM_TIMEOUT_SECONDS` | `300` | Stop a daydream that runs longer (10–7200) |
| `daydreams.fork` | `SOMNI_DAYDREAM_FORK` | off | Run each daydream as a fork of the session, so it starts from the session's own context and prompt cache instead of a cold prompt. Needs a saved session and the session's model |
| `daydreams.prompt_bytes` | `SOMNI_DAYDREAM_PROMPT_BYTES` | `16384` | Size of the record listing in a daydream's prompt, newest first. Tool results appear as one-line stubs the daydream can read in full with `somni_search "t000123"` (2048–131072) |
| `daydreams.max_reads` | `SOMNI_DAYDREAM_MAX_READS` | `6` | Somni evidence reads per daydream (at most `dreams.max_reads`) (1–64) |
| `daydreams.focus` | `SOMNI_DAYDREAM_FOCUS` | lessons from the newest records: what you said, facts found, steps that worked or failed | Focus text passed to the daydream's `/somni-dream` |
| `daydreams.command` | `SOMNI_DAYDREAM_COMMAND` | the running Pi | Command used to start the daydream's Pi |
| | `SOMNI_CONFIG` | unset | Config file to read instead of the user and project files; `none` reads none |
| | `SOMNI_RUN_LABEL` | unset | Label on each recorded event, up to 96 UTF-8 bytes |
| | `SOMNI_TAGS` | unset | JSON array of up to eight printable tags, each up to 96 UTF-8 bytes |
| | `SOMNI_DAYDREAM` | unset | Set by Somni in a daydream worker |
| | `SOMNI_DAYDREAM_FORK_OF` | unset | Set by Somni in a forked daydream worker: the parent session's ID |
| | `SOMNI_STORE` | unset | Path to an alternative `store.py` (development) |

`SOMNI_RUN_LABEL`, `SOMNI_TAGS`, `SOMNI_CONFIG`, `SOMNI_DAYDREAM`, `SOMNI_DAYDREAM_FORK_OF` and `SOMNI_STORE` are environment-only: they are set
per run or by Somni itself.
