# Fishbowl lineage feedback

- [x] Retain bounded, content-addressed screenshots and accept image citations in dreams. The scripted provider received the actual PNG bytes. A dream cited an attached frame and another opened an older frame with `somni_image`.
- [x] Make one-shot dreaming work. Text and JSON mode wait for the pass. Text mode exits nonzero with a diagnostic when the model skips submission.
- [x] Expose `status --json` and an authoritative final summary line. The tests check the transcript head, active claim counts, pass ID, and operation counts.
- [x] Carry run labels and tags into claims and recall. Claims inherit context from the records they cite, and an exact tag match outranks shared terms.
- [x] Bound tool results. A 2 MiB `stream_read` result becomes arguments, a byte count, and a SHA-256 digest. Large ordinary results retain a bounded UTF-8 preview and add a truncation marker.
- [x] Verify writer recovery after `SIGKILL`. The next Pi session appended to the original transcript head using the existing lock file.
- [x] Record assistant text before, between, and after tool calls. The one-shot transcript test verifies the order of text and tool results.
- [x] Verify a read-only plugin install. The tests record into a separate store and render a wiki without altering the install.

The [verification commands](../EVALUATION.md#verification-tests) run these cases against a scripted model. The packaged extension passed on Pi 1.0.0 and 1.5.2.

A separate synthetic check timed a full load and verification of the same 2,500-record,
500-claim store seven times per version. The median fell from 229 ms to 91 ms after reusing
one transcript ID index across claim checks. This fixture measured local store reads.
