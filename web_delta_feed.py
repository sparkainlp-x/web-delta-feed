#!/usr/bin/env python3
# Copyright (C) 2026 Jean-François Brisson / Spark AI NLP. SPDX-License-Identifier: AGPL-3.0-only
"""Compare two already-saved UTF-8 text snapshots and emit a deterministic JSON report.

This implementation was written to match the behaviour documented in README.md and
exercised by tests/test_web_delta_feed.py (the original module was not available).

Offline by design: only the two local snapshot files are read. A source URL is stored
verbatim as report metadata and is never fetched; there is no polling, monitoring,
scheduling, or service. The module imports no networking code.
"""

from __future__ import annotations

import argparse
import bisect
import difflib
import hashlib
import json
import os
import stat
import sys
import tempfile
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

__version__ = "1.0.0"

# Upper bound on each snapshot's size. Line diffs are not linear-time in the worst
# case, and this tool is meant for saved text pages, not bulk data.
MAX_SNAPSHOT_BYTES = 5 * 1024 * 1024

# Regions without anchor lines are aligned with SequenceMatcher, whose running time
# grows steeply with repeated lines. Above this many candidate line pairs in such a
# region, the tool stops with an error rather than running for minutes or guessing.
MAX_REPEATED_LINE_PAIRS = 100_000

_UTF8_BOM = b"\xef\xbb\xbf"
# Documents whose first non-whitespace text starts with one of these (case-insensitive)
# are treated as HTML and rejected. HTML fragments later in a file are not detected.
_HTML_PREFIXES = ("<!doctype html", "<html", "<head", "<body")


class SnapshotInputError(ValueError):
    """Raised for an unreadable or unsupported snapshot, or invalid report arguments."""


class _Snapshot:
    __slots__ = ("lines", "sha256")

    def __init__(self, lines: list[str], sha256: str) -> None:
        self.lines = lines
        self.sha256 = sha256


def _looks_like_html(text: str) -> bool:
    head = text.lstrip()[:64].lower()
    for prefix in _HTML_PREFIXES:
        if head.startswith(prefix):
            rest = head[len(prefix) : len(prefix) + 1]
            if rest == "" or rest in "> \t\r\n/":
                return True
    return False


def split_lines(text: str) -> list[str]:
    """Split text on LF, CRLF, or CR, excluding terminators.

    A final line terminator does not create an extra empty line. Other characters that
    ``str.splitlines`` treats as breaks (form feed, U+2028, etc.) stay inside lines.
    """
    if not text:
        return []
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines[-1] == "":
        lines.pop()
    return lines


def read_snapshot(path: str | os.PathLike[str]) -> _Snapshot:
    """Read one local snapshot: a regular UTF-8 text file (an empty file has no lines)."""
    path = Path(path)
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise SnapshotInputError(f"cannot read snapshot {path}: not a regular file")
        if info.st_size > MAX_SNAPSHOT_BYTES:
            raise SnapshotInputError(
                f"snapshot {path} is larger than the {MAX_SNAPSHOT_BYTES}-byte limit"
            )
        with path.open("rb") as handle:
            data = handle.read(MAX_SNAPSHOT_BYTES + 1)
    except SnapshotInputError:
        raise
    except OSError as exc:
        raise SnapshotInputError(f"cannot read snapshot {path}: {exc.strerror or exc}") from None
    if len(data) > MAX_SNAPSHOT_BYTES:  # the file grew after stat()
        raise SnapshotInputError(f"snapshot {path} is larger than the {MAX_SNAPSHOT_BYTES}-byte limit")

    digest = hashlib.sha256(data).hexdigest()  # raw bytes as read, including any BOM
    body = data[len(_UTF8_BOM) :] if data.startswith(_UTF8_BOM) else data
    if b"\x00" in body:
        raise SnapshotInputError(f"snapshot {path} contains NUL bytes; binary and UTF-16/32 files are not supported")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SnapshotInputError(
            f"snapshot {path} is not valid UTF-8 (byte offset {exc.start + len(data) - len(body)})"
        ) from None
    if _looks_like_html(text):
        raise SnapshotInputError(
            f"snapshot {path} looks like HTML, which is unsupported; save or extract plain text first"
        )
    return _Snapshot(split_lines(text), digest)


def _longest_increasing_run(pairs: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Longest subsequence of (i, j) pairs (sorted by i) whose j values increase."""
    tails: list[int] = []  # tails[k] = index into pairs ending the best run of length k+1
    previous: list[int] = [-1] * len(pairs)
    tail_js: list[int] = []
    for index, (_, j) in enumerate(pairs):
        k = bisect.bisect_left(tail_js, j)
        if k > 0:
            previous[index] = tails[k - 1]
        if k == len(tails):
            tails.append(index)
            tail_js.append(j)
        else:
            tails[k] = index
            tail_js[k] = j
    run: list[tuple[int, int]] = []
    index = tails[-1] if tails else -1
    while index != -1:
        run.append(pairs[index])
        index = previous[index]
    run.reverse()
    return run


def _matched_pairs(before: Sequence[str], after: Sequence[str]) -> list[tuple[int, int]]:
    """Return matched (before_index, after_index) pairs, increasing in both indices.

    1. Common leading and trailing lines are matched.
    2. Lines occurring exactly once in both remaining regions anchor the alignment
       (the "patience diff" idea); regions between anchors are processed the same way.
    3. A region without such anchors is aligned with difflib.SequenceMatcher with
       automatic junk filtering disabled, provided it is small enough (see
       MAX_REPEATED_LINE_PAIRS); otherwise SnapshotInputError is raised.
    """
    matched: list[tuple[int, int]] = []
    stack = [(0, len(before), 0, len(after))]
    while stack:
        alo, ahi, blo, bhi = stack.pop()
        while alo < ahi and blo < bhi and before[alo] == after[blo]:
            matched.append((alo, blo))
            alo += 1
            blo += 1
        while alo < ahi and blo < bhi and before[ahi - 1] == after[bhi - 1]:
            ahi -= 1
            bhi -= 1
            matched.append((ahi, bhi))
        if alo == ahi or blo == bhi:
            continue

        positions_a: dict[str, list[int]] = {}
        for i in range(alo, ahi):
            positions_a.setdefault(before[i], []).append(i)
        positions_b: dict[str, list[int]] = {}
        for j in range(blo, bhi):
            positions_b.setdefault(after[j], []).append(j)
        unique = sorted(
            (found[0], positions_b[line][0])
            for line, found in positions_a.items()
            if len(found) == 1 and len(positions_b.get(line, ())) == 1
        )
        if unique:
            anchors = _longest_increasing_run(unique)
            start_a, start_b = alo, blo
            for i, j in anchors:
                matched.append((i, j))
                stack.append((start_a, i, start_b, j))
                start_a, start_b = i + 1, j + 1
            stack.append((start_a, ahi, start_b, bhi))
            continue

        candidate_pairs = sum(
            len(found) * len(positions_b[line]) for line, found in positions_a.items() if line in positions_b
        )
        if candidate_pairs == 0:
            continue  # nothing in common: every line in this region was removed or added
        if candidate_pairs > MAX_REPEATED_LINE_PAIRS:
            raise SnapshotInputError(
                "the snapshots differ in a region with too many repeated lines to align reliably "
                f"({candidate_pairs} candidate line pairs; limit {MAX_REPEATED_LINE_PAIRS})"
            )
        matcher = difflib.SequenceMatcher(None, before[alo:ahi], after[blo:bhi], autojunk=False)
        for block in matcher.get_matching_blocks():
            for offset in range(block.size):
                matched.append((alo + block.a + offset, blo + block.b + offset))
    matched.sort()
    return matched


def diff_lines(before: Sequence[str], after: Sequence[str]) -> tuple[list[str], list[str]]:
    """Return (added_lines, removed_lines), each in snapshot order, repeats retained."""
    matched = _matched_pairs(before, after)
    kept_a = {i for i, _ in matched}
    kept_b = {j for _, j in matched}
    removed = [line for i, line in enumerate(before) if i not in kept_a]
    added = [line for j, line in enumerate(after) if j not in kept_b]
    return added, removed


def _require_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SnapshotInputError(f"{name} must be a non-blank string")
    return value


def _check_focus_terms(focus_terms: Iterable[str]) -> list[str]:
    if isinstance(focus_terms, str):
        raise SnapshotInputError("focus_terms must be a collection of strings, not a single string")
    terms = list(focus_terms)
    for term in terms:
        _require_text(term, "each focus term")
        if "\n" in term or "\r" in term:
            raise SnapshotInputError("a focus term must not contain line breaks; terms match within one line")
    return terms


def build_report(
    before_path: str | os.PathLike[str],
    after_path: str | os.PathLike[str],
    *,
    source_label: str,
    tracked_question: str,
    before_captured_at: str,
    after_captured_at: str,
    source_url: str | None = None,
    focus_terms: Iterable[str] = (),
) -> dict[str, Any]:
    """Build the report dictionary for two local snapshot files.

    Metadata strings are kept verbatim (only blank values are rejected); the URL is not
    validated or contacted.
    """
    source: dict[str, str] = {"label": _require_text(source_label, "source label")}
    if source_url is not None:
        source["url"] = _require_text(source_url, "source URL")
    question = _require_text(tracked_question, "tracked question")
    before_time = _require_text(before_captured_at, "before capture timestamp")
    after_time = _require_text(after_captured_at, "after capture timestamp")
    terms = _check_focus_terms(focus_terms)

    before = read_snapshot(before_path)
    after = read_snapshot(after_path)
    added, removed = diff_lines(before.lines, after.lines)

    matches = []
    for term in terms:
        needle = term.casefold()
        matches.append(
            {
                "term": term,
                "before_lines": [line for line in before.lines if needle in line.casefold()],
                "after_lines": [line for line in after.lines if needle in line.casefold()],
            }
        )

    return {
        "source": source,
        "tracked_question": question,
        "before": {"captured_at": before_time, "sha256": before.sha256},
        "after": {"captured_at": after_time, "sha256": after.sha256},
        "change": {
            "has_changes": bool(added or removed),
            "added_lines": added,
            "removed_lines": removed,
        },
        "focus_term_matches": matches,
    }


def render_report(report: dict[str, Any]) -> str:
    """Canonical JSON text: sorted keys, 2-space indent, non-ASCII kept, final newline."""
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def write_report(path: str | os.PathLike[str], text: str) -> None:
    """Write UTF-8 text atomically (temporary file in the same directory, then rename).

    An existing file is replaced. On POSIX the result is readable only by its owner,
    because reports can contain sensitive snapshot excerpts.
    """
    destination = Path(path)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="\n", dir=destination.parent,
            prefix=f".{destination.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temp_name = handle.name
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, destination)
        temp_name = None
    finally:
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except OSError:
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="web-delta-feed",
        description=(
            "Compare two already-saved local UTF-8 text snapshots and print a JSON report "
            "of added/removed lines. Nothing is fetched; --source-url is metadata only."
        ),
    )
    parser.add_argument("before", help="earlier local snapshot (UTF-8 text file)")
    parser.add_argument("after", help="later local snapshot (UTF-8 text file)")
    parser.add_argument("--source-label", required=True, help="human-readable source name (kept verbatim)")
    parser.add_argument("--source-url", help="optional source URL, recorded as metadata and never fetched")
    parser.add_argument("--question", required=True, help="the question this comparison tracks")
    parser.add_argument("--before-captured-at", required=True, help="capture time of the earlier snapshot (verbatim)")
    parser.add_argument("--after-captured-at", required=True, help="capture time of the later snapshot (verbatim)")
    parser.add_argument(
        "--focus-term", action="append", default=[], metavar="TERM",
        help="literal, case-insensitive term to look for in each snapshot's lines (repeatable)",
    )
    parser.add_argument("--output", help="write the JSON report to this file instead of standard output")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.output is not None:
            output = Path(args.output)
            if any(_same_file(output, Path(p)) for p in (args.before, args.after)):
                raise SnapshotInputError("--output must not be one of the input snapshot files")
        report = build_report(
            args.before,
            args.after,
            source_label=args.source_label,
            source_url=args.source_url,
            tracked_question=args.question,
            before_captured_at=args.before_captured_at,
            after_captured_at=args.after_captured_at,
            focus_terms=args.focus_term,
        )
        text = render_report(report)
        if args.output is None:
            # Emit UTF-8 regardless of the console's locale encoding.
            buffer = getattr(sys.stdout, "buffer", None)
            if buffer is not None:
                sys.stdout.flush()
                buffer.write(text.encode("utf-8"))
                buffer.flush()
            else:
                sys.stdout.write(text)
        else:
            try:
                write_report(args.output, text)
            except OSError as exc:
                raise SnapshotInputError(f"cannot write report {args.output}: {exc.strerror or exc}") from None
    except SnapshotInputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
