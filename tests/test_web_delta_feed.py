from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from web_delta_feed import SnapshotInputError, build_report  # noqa: E402


class WebDeltaFeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.before = self.root / "before.txt"
        self.after = self.root / "after.txt"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def make_report(self, before: Path | None = None, after: Path | None = None, **overrides):
        options = {
            "source_label": "Status sample",
            "source_url": "https://example.test/status",
            "tracked_question": "Did the status change?",
            "before_captured_at": "2026-09-28T09:00:00Z",
            "after_captured_at": "2026-09-30T09:00:00Z",
            "focus_terms": (),
        }
        options.update(overrides)
        return build_report(before or self.before, after or self.after, **options)

    def test_exact_added_and_removed_lines(self) -> None:
        self.before.write_text("same\nremove me\nkeep\nreplace old\n", encoding="utf-8")
        self.after.write_text("same\nadd me\nkeep\nreplace new\n", encoding="utf-8")

        change = self.make_report()["change"]
        self.assertTrue(change["has_changes"])
        self.assertEqual(change["removed_lines"], ["remove me", "replace old"])
        self.assertEqual(change["added_lines"], ["add me", "replace new"])

    def test_identical_snapshots_report_no_change(self) -> None:
        self.before.write_text("first\nsecond\n", encoding="utf-8")
        self.after.write_bytes(self.before.read_bytes())

        report = self.make_report()
        self.assertFalse(report["change"]["has_changes"])
        self.assertEqual(report["change"]["added_lines"], [])
        self.assertEqual(report["change"]["removed_lines"], [])
        self.assertEqual(report["before"]["sha256"], report["after"]["sha256"])

    def test_metadata_is_preserved(self) -> None:
        self.before.write_text("old\n", encoding="utf-8")
        self.after.write_text("new\n", encoding="utf-8")

        report = self.make_report()
        self.assertEqual(report["source"], {"label": "Status sample", "url": "https://example.test/status"})
        self.assertEqual(report["tracked_question"], "Did the status change?")
        self.assertEqual(report["before"]["captured_at"], "2026-09-28T09:00:00Z")
        self.assertEqual(report["after"]["captured_at"], "2026-09-30T09:00:00Z")

    def test_optional_source_url_is_omitted_when_absent(self) -> None:
        self.before.write_text("x\n", encoding="utf-8")
        self.after.write_text("x\n", encoding="utf-8")
        report = self.make_report(source_url=None)
        self.assertEqual(report["source"], {"label": "Status sample"})

    def test_json_serialization_is_deterministic(self) -> None:
        self.before.write_text("release 1\n", encoding="utf-8")
        self.after.write_text("release 2\n", encoding="utf-8")
        report_a = self.make_report(focus_terms=["release"])
        report_b = self.make_report(focus_terms=["release"])
        json_a = json.dumps(report_a, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        json_b = json.dumps(report_b, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        self.assertEqual(json_a, json_b)

    def test_repeated_focus_terms_match_case_insensitive_substrings(self) -> None:
        self.before.write_text("Release 2.4\nNo issue\nMAINTENANCE soon\n", encoding="utf-8")
        self.after.write_text("release 2.5\nMaintenance later\n", encoding="utf-8")

        matches = self.make_report(focus_terms=["release", "MAINTENANCE", "release"])["focus_term_matches"]
        self.assertEqual([item["term"] for item in matches], ["release", "MAINTENANCE", "release"])
        self.assertEqual(matches[0]["before_lines"], ["Release 2.4"])
        self.assertEqual(matches[0]["after_lines"], ["release 2.5"])
        self.assertEqual(matches[1]["before_lines"], ["MAINTENANCE soon"])
        self.assertEqual(matches[1]["after_lines"], ["Maintenance later"])
        self.assertEqual(matches[2], matches[0])

    def test_invalid_utf8_is_reported_as_malformed_input(self) -> None:
        self.before.write_bytes(b"valid\n\xff\n")
        self.after.write_text("valid\n", encoding="utf-8")
        with self.assertRaisesRegex(SnapshotInputError, "not valid UTF-8"):
            self.make_report()

    def test_missing_and_non_file_inputs_are_reported(self) -> None:
        self.after.write_text("ok\n", encoding="utf-8")
        with self.assertRaisesRegex(SnapshotInputError, "cannot read snapshot"):
            self.make_report(before=self.root / "missing.txt")

        self.before.mkdir()
        with self.assertRaisesRegex(SnapshotInputError, "cannot read snapshot"):
            self.make_report()

    def test_unicode_lines_hashes_and_focus_terms(self) -> None:
        self.before.write_text("Café déjà vu\n", encoding="utf-8")
        self.after.write_text("Café déjà vu\n新しい行\n", encoding="utf-8")

        report = self.make_report(focus_terms=["CAFÉ", "新しい"])
        self.assertEqual(report["change"]["added_lines"], ["新しい行"])
        self.assertEqual(report["focus_term_matches"][0]["before_lines"], ["Café déjà vu"])
        self.assertEqual(report["focus_term_matches"][1]["after_lines"], ["新しい行"])
        self.assertEqual(len(report["after"]["sha256"]), 64)
        rendered = json.dumps(report, ensure_ascii=False)
        self.assertIn("新しい行", rendered)

    def test_cli_prints_json_and_writes_identical_report_file(self) -> None:
        self.before.write_text("old line\n", encoding="utf-8")
        self.after.write_text("new line\n", encoding="utf-8")
        output_path = self.root / "report.json"
        command = [
            sys.executable,
            str(PROJECT_DIR / "web_delta_feed.py"),
            str(self.before),
            str(self.after),
            "--source-label",
            "CLI test",
            "--question",
            "What changed?",
            "--before-captured-at",
            "before-time",
            "--after-captured-at",
            "after-time",
        ]
        printed = subprocess.run(command, check=False, capture_output=True, text=True)
        self.assertEqual(printed.returncode, 0, printed.stderr)
        self.assertEqual(json.loads(printed.stdout)["change"]["added_lines"], ["new line"])

        written = subprocess.run(command + ["--output", str(output_path)], check=False, capture_output=True, text=True)
        self.assertEqual(written.returncode, 0, written.stderr)
        self.assertEqual(written.stdout, "")
        self.assertEqual(output_path.read_text(encoding="utf-8"), printed.stdout)


if __name__ == "__main__":
    unittest.main()
