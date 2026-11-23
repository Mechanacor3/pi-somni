# Headless runs and the store CLI

Use Pi's normal model and login configuration. Point each task and its dream at the same store.

```bash
export SOMNI_WORKSPACE=/path/to/memory
SOMNI_RUN_LABEL='test 001' SOMNI_TAGS='["world A", "harvest"]' \
  pi --mode json -p 'Complete the task.'
pi -p '/somni-dream what should carry over to the next world?'
python3 somni/store.py --workspace "$SOMNI_WORKSPACE" --subject-id "${SOMNI_SUBJECT:-pi}" status --json
```

The one-shot dream waits for the pass and prints a line such as
`Somni dream d000001: 1 added, 0 rejected, 0 merged.` It exits nonzero with a diagnostic if
there is no committed pass. Use `--mode json -p` for the same dream with Pi's JSON events.
In RPC mode, send `/somni-dream` as a `prompt` and wait for `agent_settled` before reading
the store status. The status JSON reports the transcript head and count, active claims by
status, and the last pass ID and counts. `added_count` includes corrections that create a
replacement claim. For older passes that didn't record added and merged counts, those counts
can be `null`. Claims inherit the union of run labels and tags from the records they cite.


Every setting is listed in [Settings](settings.md). Harnesses usually set them as environment
variables, which override any config file; `SOMNI_CONFIG=none` ignores the files entirely.

## What gets recorded

Somni records finalized assistant text blocks before, between, and after tool calls. Large tool
results get a bounded preview with the original byte count and SHA-256. These policies apply to
the Somni transcript. Pi itself receives the original tool result. The store keeps up to four
PNG, JPEG, GIF, or WebP images per result in `somni-v1/images/` and deduplicates them by content.
The image budget is 64 MiB per store. Records count images omitted by the size, format, or
storage limits. If configured, Pillow converts downscaled images to JPEG before hashing,
with a 4 MiB input limit. Keep the store out of version control. Set `SOMNI_RECORD=0` before
launching Pi to pause automatic recording while retaining memory retrieval.

## The store CLI

The store also has a CLI: `python3 somni/store.py --workspace W --subject-id S
{init,session-open,session-close,record,note,dream,search,inject,report,metrics,status}`.
Use `inject --query 'your prompt'` to preview a prompt-ranked index. Pi uses up to the
first 64 KiB of the prompt for ranking.
