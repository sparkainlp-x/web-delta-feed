# Copyright (C) 2026 Jean-François Brisson / Spark AI NLP. SPDX-License-Identifier: AGPL-3.0-only
"""Additional tests from the implementation review (the original spec tests are unchanged)."""

from __future__ import annotations

import ast
import contextlib
import hashlib
import io
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

import web_delta_feed as wdf  # noqa: E402

EXAMPLE_ARGS = [
    "examples/before.txt",
    "examples/after.txt",
    "--source-label", "Example status page",
    "--source-url", "https://example.test/status",
    "--question", "Has the release changed or maintenance been scheduled?",
    "--before-captured-at", "2026-09-28T09:00:00Z",
    "--after-captured-at", "2026-09-30T09:00:00Z",
    "--focus-term", "release",
    "--focus-term", "maintenance",
]


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.before = self.root / "before.txt"
        self.after = self.root / "after.txt"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def report(self, before_bytes: bytes, after_bytes: bytes, **overrides):
        self.before.write_bytes(before_bytes)
        self.after.write_bytes(after_bytes)
        options = {
            "source_label": "Label",
            "tracked_question": "Question?",
            "before_captured_at": "t0",
            "after_captured_at": "t1",
        }
        options.update(overrides)
        return wdf.build_report(self.before, self.after, **options)

    def run_main(self, argv):
        """Run main() in-process with a byte-backed stdout, like a real console."""
        raw_out = io.BytesIO()
        fake_stdout = io.TextIOWrapper(raw_out, encoding="ascii")
        err = io.StringIO()
        with mock.patch.object(sys, "stdout", fake_stdout), contextlib.redirect_stderr(err):
            code = wdf.main(argv)
            fake_stdout.flush()
        return code, raw_out.getvalue(), err.getvalue()

    def cli_args(self, *extra):
        return [str(self.before), str(self.after), "--source-label", "L", "--question", "Q",
                "--before-captured-at", "t0", "--after-captured-at", "t1", *extra]


class LineHandlingTests(_Base):
    def test_crlf_cr_and_missing_final_newline_are_the_same_logical_lines(self):
        for variant in (b"a\r\nb\r\n", b"a\rb\r", b"a\nb", b"a\r\nb"):
            with self.subTest(variant=variant):
                report = self.report(b"a\nb\n", variant)
                self.assertFalse(report["change"]["has_changes"])
                self.assertNotEqual(report["before"]["sha256"], report["after"]["sha256"])

    def test_bom_is_excluded_from_lines_but_included_in_hash(self):
        report = self.report(b"\xef\xbb\xbfhello\n", b"hello\n", focus_terms=["hello"])
        self.assertFalse(report["change"]["has_changes"])
        self.assertEqual(report["focus_term_matches"][0]["before_lines"], ["hello"])
        self.assertEqual(report["before"]["sha256"], hashlib.sha256(b"\xef\xbb\xbfhello\n").hexdigest())
        self.assertNotEqual(report["before"]["sha256"], report["after"]["sha256"])

    def test_only_lf_cr_crlf_split_lines(self):
        self.assertEqual(wdf.split_lines("a\x0cb\u2028c\x85d\n"), ["a\x0cb\u2028c\x85d"])
        self.assertEqual(wdf.split_lines(""), [])
        self.assertEqual(wdf.split_lines("\n"), [""])
        self.assertEqual(wdf.split_lines("a\n\nb"), ["a", "", "b"])
        self.assertEqual(wdf.split_lines("a\r\r\nb"), ["a", "", "b"])

    def test_empty_snapshot(self):
        report = self.report(b"", b"x\ny\n")
        self.assertEqual(report["change"]["added_lines"], ["x", "y"])
        self.assertEqual(report["change"]["removed_lines"], [])
        self.assertFalse(self.report(b"", b"")["change"]["has_changes"])

    def test_repeated_lines_and_order_are_retained(self):
        report = self.report(b"a\nb\n", b"x\na\nx\nb\nx\n")
        self.assertEqual(report["change"]["added_lines"], ["x", "x", "x"])
        report = self.report(b"dup\ndup\ndup\nend\n", b"dup\nend\n")
        self.assertEqual(report["change"]["removed_lines"], ["dup", "dup"])

    def test_whitespace_and_unicode_normalization_are_significant(self):
        report = self.report("café\nx \n".encode(), "cafe\u0301\nx\n".encode())
        self.assertEqual(report["change"]["removed_lines"], ["café", "x "])
        self.assertEqual(report["change"]["added_lines"], ["cafe\u0301", "x"])

    def test_casefold_focus_matching(self):
        report = self.report("Straße closed\n".encode(), b"STRASSE open\n", focus_terms=["strasse"])
        match = report["focus_term_matches"][0]
        self.assertEqual(match["before_lines"], ["Straße closed"])
        self.assertEqual(match["after_lines"], ["STRASSE open"])


class DiffAlgorithmTests(unittest.TestCase):
    def assert_valid_diff(self, before, after):
        pairs = wdf._matched_pairs(before, after)
        self.assertEqual(pairs, sorted(pairs))
        self.assertEqual(len({i for i, _ in pairs}), len(pairs))
        self.assertEqual(len({j for _, j in pairs}), len(pairs))
        self.assertTrue(all(b1 < b2 for (_, b1), (_, b2) in zip(pairs, pairs[1:], strict=False)))
        self.assertTrue(all(before[i] == after[j] for i, j in pairs))
        added, removed = wdf.diff_lines(before, after)
        self.assertEqual(len(before) - len(removed), len(pairs))
        self.assertEqual(len(after) - len(added), len(pairs))

    def test_random_inputs_produce_consistent_edit_scripts(self):
        rng = random.Random(20260930)
        for _ in range(300):
            alphabet = [f"line {k}" for k in range(rng.randint(1, 12))] + [""]
            before = [rng.choice(alphabet) for _ in range(rng.randint(0, 40))]
            after = [rng.choice(alphabet) for _ in range(rng.randint(0, 40))]
            with self.subTest(before=before, after=after):
                self.assert_valid_diff(before, after)

    def test_localized_edit_in_large_repetitive_file_is_exact_and_fast(self):
        before = ["" if i % 2 else f"row {i}" for i in range(100_000)]
        after = list(before)
        after[50_000] = "changed"
        after.insert(70_001, "inserted")
        added, removed = wdf.diff_lines(before, after)
        self.assertEqual(added, ["changed", "inserted"])
        self.assertEqual(removed, ["row 50000"])

    def test_regions_with_nothing_in_common(self):
        self.assertEqual(wdf.diff_lines(["a", "a"], ["b", "b"]), (["b", "b"], ["a", "a"]))

    def test_small_anchorless_region_uses_sequence_matcher(self):
        added, removed = wdf.diff_lines(["x", "a", "x", "a"], ["a", "x", "a", "a"])
        self.assertEqual(len(added), len(removed))
        self.assert_valid_diff(["x", "a", "x", "a"], ["a", "x", "a", "a"])

    def test_too_many_repeated_lines_is_an_error_not_a_guess(self):
        before = ["", "a"] * 400
        after = ["", "b"] * 400
        with self.assertRaisesRegex(wdf.SnapshotInputError, "too many repeated lines"):
            wdf.diff_lines(before, after)


class InputValidationTests(_Base):
    def test_html_documents_are_rejected(self):
        for text in (b"<!DOCTYPE html>\n<p>x</p>\n", b"  \n<html lang='en'>\n", b"\xef\xbb\xbf<HTML>\n",
                     b"<head>\n", b"<body>", b"<html"):
            with self.subTest(text=text):
                with self.assertRaisesRegex(wdf.SnapshotInputError, "looks like HTML"):
                    self.report(text, b"ok\n")

    def test_plain_text_mentioning_tags_is_accepted(self):
        report = self.report(b"Use <html> tags carefully\n<header> is fine\n", b"<headline>\n")
        self.assertTrue(report["change"]["has_changes"])

    def test_binary_and_utf16_files_are_rejected(self):
        for data in (b"abc\x00def\n", "hello\n".encode("utf-16")):
            with self.subTest(data=data):
                with self.assertRaisesRegex(wdf.SnapshotInputError, "NUL bytes"):
                    self.report(data, b"ok\n")

    def test_invalid_utf8_reports_byte_offset(self):
        with self.assertRaisesRegex(wdf.SnapshotInputError, r"not valid UTF-8 \(byte offset 5\)"):
            self.report(b"\xef\xbb\xbfab\xc3(", b"ok\n")
        with self.assertRaisesRegex(wdf.SnapshotInputError, "not valid UTF-8"):
            self.report(b"\xed\xa0\x80\n", b"ok\n")  # encoded surrogate

    def test_size_limit(self):
        with mock.patch.object(wdf, "MAX_SNAPSHOT_BYTES", 8):
            self.report(b"12345678", b"ok")
            with self.assertRaisesRegex(wdf.SnapshotInputError, "larger than the 8-byte limit"):
                self.report(b"123456789", b"ok")

    def test_file_that_grows_after_stat_is_still_bounded(self):
        self.before.write_bytes(b"123456789")
        self.after.write_bytes(b"ok")
        real_stat = Path.stat

        def small_stat(path, *args, **kwargs):
            result = real_stat(path, *args, **kwargs)
            values = list(result)
            values[6] = 1  # st_size
            return os.stat_result(values)

        with mock.patch.object(wdf, "MAX_SNAPSHOT_BYTES", 8), mock.patch.object(Path, "stat", small_stat):
            with self.assertRaisesRegex(wdf.SnapshotInputError, "larger than"):
                wdf.build_report(self.before, self.after, source_label="L", tracked_question="Q",
                                 before_captured_at="a", after_captured_at="b")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFOs")
    def test_fifo_is_rejected_without_blocking(self):
        fifo = self.root / "pipe"
        os.mkfifo(fifo)
        self.after.write_text("ok\n", encoding="utf-8")
        with self.assertRaisesRegex(wdf.SnapshotInputError, "not a regular file"):
            wdf.build_report(fifo, self.after, source_label="L", tracked_question="Q",
                             before_captured_at="a", after_captured_at="b")

    def test_unreadable_file(self):
        self.before.write_text("x\n", encoding="utf-8")
        self.after.write_text("x\n", encoding="utf-8")
        with mock.patch.object(Path, "open", side_effect=PermissionError(13, "Permission denied")):
            with self.assertRaisesRegex(wdf.SnapshotInputError, "cannot read snapshot .*Permission denied"):
                wdf.build_report(self.before, self.after, source_label="L", tracked_question="Q",
                                 before_captured_at="a", after_captured_at="b")

    def test_blank_metadata_and_bad_focus_terms_are_rejected(self):
        for key in ("source_label", "tracked_question", "before_captured_at", "after_captured_at", "source_url"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(wdf.SnapshotInputError, "non-blank"):
                    self.report(b"a\n", b"b\n", **{key: "  "})
        for terms, pattern in ((["ok", ""], "non-blank"), (["two\nlines"], "line breaks"),
                               ("release", "not a single string"), ([7], "non-blank")):
            with self.subTest(terms=terms):
                with self.assertRaisesRegex(wdf.SnapshotInputError, pattern):
                    self.report(b"a\n", b"b\n", focus_terms=terms)

    def test_metadata_is_kept_verbatim(self):
        report = self.report(b"a\n", b"a\n", source_label="  Padded label ", source_url="not even a URL",
                             before_captured_at="yesterday-ish")
        self.assertEqual(report["source"], {"label": "  Padded label ", "url": "not even a URL"})
        self.assertEqual(report["before"]["captured_at"], "yesterday-ish")


class CliTests(_Base):
    def test_stdout_is_utf8_bytes_regardless_of_console_encoding(self):
        self.before.write_text("Café\n", encoding="utf-8")
        self.after.write_text("Café 新\n", encoding="utf-8")
        code, out, err = self.run_main(self.cli_args("--focus-term", "新"))
        self.assertEqual((code, err), (0, ""))
        text = out.decode("utf-8")
        self.assertIn("Café 新", text)
        self.assertEqual(text, wdf.render_report(json.loads(text)))

    def test_stdout_without_buffer_falls_back_to_text(self):
        self.before.write_text("a\n", encoding="utf-8")
        self.after.write_text("b\n", encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(wdf.main(self.cli_args()), 0)
        self.assertEqual(json.loads(out.getvalue())["change"]["added_lines"], ["b"])

    def test_output_file_is_written_and_replaced(self):
        self.before.write_text("a\n", encoding="utf-8")
        self.after.write_text("b\n", encoding="utf-8")
        target = self.root / "report.json"
        target.write_text("old", encoding="utf-8")
        code, out, err = self.run_main(self.cli_args("--output", str(target)))
        self.assertEqual((code, out, err), (0, b"", ""))
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["change"]["removed_lines"], ["a"])
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["after.txt", "before.txt", "report.json"])

    def test_output_must_not_overwrite_an_input(self):
        self.before.write_text("a\n", encoding="utf-8")
        self.after.write_text("b\n", encoding="utf-8")
        link = self.root / "link.txt"
        try:
            link.symlink_to(self.after)
        except (OSError, NotImplementedError):
            link = self.after
        for target in (self.before, link):
            with self.subTest(target=target.name):
                code, out, err = self.run_main(self.cli_args("--output", str(target)))
                self.assertEqual(code, 2)
                self.assertIn("must not be one of the input", err)
        self.assertEqual(self.before.read_text(encoding="utf-8"), "a\n")
        self.assertEqual(self.after.read_text(encoding="utf-8"), "b\n")

    def test_write_failure_exits_2_and_leaves_no_temp_file(self):
        self.before.write_text("a\n", encoding="utf-8")
        self.after.write_text("b\n", encoding="utf-8")
        code, _, err = self.run_main(self.cli_args("--output", str(self.root / "missing-dir" / "r.json")))
        self.assertEqual(code, 2)
        self.assertIn("cannot write report", err)
        with mock.patch.object(wdf.os, "replace", side_effect=OSError(28, "No space left on device")):
            code, _, err = self.run_main(self.cli_args("--output", str(self.root / "r.json")))
        self.assertEqual(code, 2)
        self.assertIn("No space left", err)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["after.txt", "before.txt"])

    def test_input_errors_exit_2_with_message_and_no_stdout(self):
        self.after.write_text("b\n", encoding="utf-8")
        code, out, err = self.run_main(self.cli_args())
        self.assertEqual((code, out), (2, b""))
        self.assertTrue(err.startswith("error: cannot read snapshot"))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            wdf.main([str(self.before), str(self.after)])
        self.assertEqual(raised.exception.code, 2)

    def test_version_matches_citation_and_packaging(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            wdf.main(["--version"])
        self.assertEqual(out.getvalue().strip(), f"web-delta-feed {wdf.__version__}")
        citation = (PROJECT_DIR / "CITATION.cff").read_text(encoding="utf-8")
        self.assertEqual(re.search(r"^version: (\S+)$", citation, re.M).group(1), wdf.__version__)
        pyproject = (PROJECT_DIR / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('version = { attr = "web_delta_feed.__version__" }', pyproject)

    def test_checked_in_example_report_is_reproducible(self):
        result = subprocess.run([sys.executable, "web_delta_feed.py", *EXAMPLE_ARGS], cwd=PROJECT_DIR,
                                capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = (PROJECT_DIR / "examples" / "example-report.json").read_bytes()
        self.assertEqual(result.stdout, expected)
        self.assertTrue(expected.endswith(b"}\n"))


class OfflineTests(unittest.TestCase):
    def test_module_imports_only_offline_standard_library_modules(self):
        tree = ast.parse((PROJECT_DIR / "web_delta_feed.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        allowed = {"__future__", "argparse", "bisect", "collections", "difflib", "hashlib", "json", "os",
                   "pathlib", "stat", "sys", "tempfile", "typing"}
        self.assertLessEqual(imported, allowed)
        self.assertFalse(imported & {"socket", "urllib", "http", "ssl", "requests", "asyncio", "ftplib"})

    def test_cli_runs_with_networking_disabled_and_does_not_fetch_the_url(self):
        probe = (
            "import socket, sys\n"
            "def refuse(*a, **k): raise RuntimeError('network access attempted')\n"
            "socket.socket = refuse; socket.create_connection = refuse; socket.getaddrinfo = refuse\n"
            "sys.argv = ['web_delta_feed.py'] + sys.argv[1:]\n"
            "import runpy; runpy.run_path('web_delta_feed.py', run_name='__main__')\n"
        )
        result = subprocess.run([sys.executable, "-c", probe, *EXAMPLE_ARGS], cwd=PROJECT_DIR,
                                capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["source"]["url"], "https://example.test/status")


if __name__ == "__main__":
    unittest.main()
