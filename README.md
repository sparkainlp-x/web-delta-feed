# Web Delta Feed

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.23069164.svg)](https://doi.org/10.5281/zenodo.23069164)
[![tests](https://github.com/sparkainlp-x/web-delta-feed/actions/workflows/tests.yml/badge.svg)](https://github.com/sparkainlp-x/web-delta-feed/actions/workflows/tests.yml)

A compact, offline prototype for comparing two **already-saved UTF-8 text snapshots**. It reads local files only: a supplied source URL is recorded as metadata and is never fetched, and the program does not poll, monitor, or start a service. HTML input is intentionally unsupported; save or extract the relevant content as plain text before comparing it.

## Install

Requires Python 3.10 or newer. The implementation and tests use only the Python standard library. Run the single script from this directory, or install it from a clone to get a `web-delta-feed` command (which can replace `python3 web_delta_feed.py` below):

```bash
python3 -m pip install .
web-delta-feed --version
```

## Run it

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

Omit `--output` to print the JSON report to standard output. The two positional arguments are the earlier and later local snapshot paths. `--source-label`, `--question`, and both capture timestamps are required; timestamps and labels are preserved verbatim (they are not parsed or checked, only rejected if blank). `--source-url` is optional metadata and is omitted from the report when not given. Repeat `--focus-term` to check multiple literal terms; repeated terms produce repeated entries, in the order given.

`--output` replaces an existing file atomically (temporary file, then rename). On POSIX systems the report file is readable only by its owner, because reports can contain snapshot excerpts. The tool refuses to write the report over either input snapshot.

The exit status is 0 on success and 2 on any error (unreadable or unsupported input, invalid arguments, or a failed write), with a message on standard error and no report output.

## Accepted input

Each snapshot must be a regular file of at most 5 MiB containing UTF-8 text.

- A leading UTF-8 byte-order mark is ignored for line comparison (it is still included in the SHA-256 hash).
- Lines end at LF, CRLF, or a lone CR. A missing final line ending does not matter. Other characters such as form feed or U+2028 stay inside a line.
- Invalid UTF-8 (the error gives the byte offset) and files containing NUL bytes, such as binary or UTF-16 files, are rejected.
- A file whose first non-whitespace text starts with `<!doctype html`, `<html`, `<head`, or `<body` (any case) is rejected as HTML. This is a simple check: HTML fragments further into a file are not detected and are compared as ordinary text.
- Whitespace and Unicode normalization are significant: `café` written with a combining accent is a different line from the precomposed form.

## Report behavior

The JSON contains source metadata (`source.label`, optional `source.url`) and the `tracked_question`, each capture timestamp and SHA-256 hash (`before`/`after`), exact added and removed **line contents** with a `has_changes` flag (`change`), and before/after focus-term matches (`focus_term_matches`). JSON keys are sorted, indentation is two spaces, the output ends with one newline, and it is written as UTF-8 even when the console uses another encoding. Repeated runs over the same files and arguments are byte-for-byte identical. Local file paths are not included in the report.

Line terminators are excluded from reported line strings; ordering and repeated lines are retained. A line is added or removed when its content changes relative to the other snapshot. If the logical lines are identical, `has_changes` is false and both change lists are empty, even though metadata or raw-byte hashes can still differ (for example, if line-ending bytes differ).

How lines are aligned: identical leading and trailing lines are matched first; lines that occur exactly once in both remaining parts then anchor the alignment (the "patience diff" idea); any region left without such anchors is aligned with Python's standard-library `SequenceMatcher` with automatic junk filtering disabled. The added and removed lists always form a correct line-level edit between the two snapshots, but, as with other diff tools, they are not guaranteed to be the smallest possible edit. `SequenceMatcher` becomes very slow when a region has many repeated lines (for example, two heavily rewritten files with many blank lines), so if such a region has more than 100,000 candidate line pairs the tool stops with an error rather than running for minutes or guessing.

**Exact diffs are not semantic interpretation.** A changed line does not say whether the change is important or what it means. Focus-term matching is a case-insensitive (Unicode case-folded) literal substring check on each line of each snapshot, not AI judgment; matching lines are returned verbatim and in snapshot order. Focus terms must not be blank or contain line breaks.

SHA-256 hashes establish integrity of the bytes read after capture: they can help detect whether a saved snapshot later changed, but **do not establish that the snapshot is authentic or came from the labeled source**. The tool cannot verify capture claims because it does not fetch or authenticate sources. Snapshot files, URLs, focus terms, and report excerpts may contain sensitive content; store and share them accordingly.

## Example

The synthetic input snapshots are `examples/before.txt` and `examples/after.txt`. The checked-in `examples/example-report.json` is generated with the command above (a test checks that it is reproduced byte-for-byte). The source URL and timestamps in this example are illustrative metadata only.

## Tests

Run from the project directory:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile web_delta_feed.py
```

`tests/test_web_delta_feed.py` holds the original specification tests: exact additions/removals, unchanged snapshots, metadata preservation, deterministic JSON, repeated focus terms and case-insensitive matching, invalid/unreadable inputs, Unicode text, and the CLI. `tests/test_review_additions.py` adds line-ending and BOM handling, HTML/binary/size/FIFO rejection, the alignment rules (including randomized consistency checks and the repeated-line limit), output-file safety, exit codes, reproduction of the example report, and checks that the module imports no networking code and runs with networking disabled. CI also runs `ruff check .` and requires at least 90% branch coverage (`coverage run -m unittest discover -s tests` then `coverage report`, with `coverage[toml]` installed).

## Implementation note

The original `web_delta_feed.py` did not come through with the rest of the project, so the current module was written afterwards to match this README and the original tests, which served as the specification. Where the README was silent (line terminators, BOM, HTML detection, size limits, exit codes, output-file behavior), the choices are documented above.

## Related work / when to use something else

- **`diff` / Python's `difflib`:** `diff -u before.txt after.txt` or `difflib.unified_diff` give a human-readable diff with context. Use them when you want to read changes, not a JSON record with metadata and hashes.
- **`git diff --no-index before.txt after.txt`:** richer diff algorithms (`--patience`, `--histogram`), word-level diffs (`--word-diff`), and colored output, for ad-hoc comparisons or snapshots kept in a Git repository.
- **[changedetection.io](https://changedetection.io/) and [urlwatch](https://thp.io/2008/urlwatch/):** these fetch pages on a schedule, filter HTML, and send notifications. Use them if you actually want live website monitoring, which this tool deliberately does not do.

Web Delta Feed is a small, dependency-free, offline step for recording what changed between two text snapshots you already have, in a stable JSON form that is easy to archive or pass to other tools.

## Citation

See [CITATION.cff](CITATION.cff).

## License

This software is available under the GNU Affero General Public License v3.0 only (AGPL-3.0-only); see [LICENSE](LICENSE).

Organizations that want to use it in proprietary products or services without AGPL obligations can contact the author about a commercial license via https://sparkainlpx.xyz.
