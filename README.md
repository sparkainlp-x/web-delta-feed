# Web Delta Feed

A compact, offline prototype for comparing two **already-saved UTF-8 text snapshots**. It reads local files only: a supplied source URL is recorded as metadata and is never fetched, and the program does not poll, monitor, or start a service. HTML input is intentionally unsupported; save or extract the relevant content as plain text before comparing it.

## Run it

Python 3.10+ is recommended. The implementation and tests use only the Python standard library.

```bash
python3 web_delta_feed.py \
  examples/before.txt examples/after.txt \
  --source-label "Example status page" \
  --source-url "https://example.test/status" \
  --question "Has the release changed or maintenance been scheduled?" \
  --before-captured-at "2026-09-28T09:00:00Z" \
  --after-captured-at "2026-09-30T09:00:00Z" \
  --focus-term release \
  --focus-term maintenance \
  --output examples/example-report.json
```

Omit `--output` to print the JSON report to standard output. The two positional arguments are the earlier and later local snapshot paths. `--source-label`, `--question`, and both capture timestamps are required; timestamps and labels are preserved verbatim. `--source-url` is optional metadata. Repeat `--focus-term` to check multiple literal terms.

## Report behavior

The JSON contains source metadata and the tracked question, each capture timestamp and SHA-256 hash, exact added and removed **line contents**, a `has_changes` flag, and before/after focus-term matches. JSON keys are sorted and output is UTF-8, making repeated runs over the same files and arguments byte-for-byte deterministic.

Line changes use Python's standard-library `SequenceMatcher` with automatic junk filtering disabled. Line terminators are excluded from reported line strings; ordering and repeated lines are retained. A line is added or removed when its content changes relative to the other snapshot. If the logical lines are identical, `has_changes` is false and both change lists are empty, even though metadata or raw-byte hashes can still differ (for example, if line-ending bytes differ).

**Exact diffs are not semantic interpretation.** A changed line does not say whether the change is important or what it means. Focus-term matching is a case-insensitive literal substring check on each line, not AI judgment; matching lines are returned verbatim and in snapshot order.

SHA-256 hashes establish integrity of the bytes read after capture: they can help detect whether a saved snapshot later changed, but **do not establish that the snapshot is authentic or came from the labeled source**. The tool cannot verify capture claims because it does not fetch or authenticate sources. Snapshot files, URLs, focus terms, and report excerpts may contain sensitive content; store and share them accordingly.

## Example

The synthetic input snapshots are `examples/before.txt` and `examples/after.txt`. The checked-in `examples/example-report.json` is generated with the command above. The source URL and timestamps in this example are illustrative metadata only.

## Tests

Run from the project directory:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile web_delta_feed.py
```

The tests cover exact additions/removals, unchanged snapshots, metadata preservation, deterministic JSON, repeated focus terms and case-insensitive matching, invalid/unreadable inputs, and Unicode text.
