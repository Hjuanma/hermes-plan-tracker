"""Tests for history management — one snapshot per content change, trim at 500.

Uses a temporary history directory to avoid touching real plugin data.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from core import (
    Checkbox,
    Milestone,
    PlanFile,
    _current_sha,
    ensure_history_dir,
    history_dir,
    parse_plan,
    read_history,
    record_snapshot,
)


# PlanFile uses __slots__ so we store tmp paths here for cleanup.
_tmp_plan_paths: dict = {}


def _make_plan(lines: list) -> PlanFile:
    """Create a PlanFile from lines via a temp file."""
    with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w") as f:
        for line in lines:
            f.write(line)
        f.flush()
        plan = parse_plan(f.name)
    _tmp_plan_paths[id(plan)] = f.name
    return plan


def _cleanup(plan: PlanFile) -> None:
    pid = id(plan)
    tmp = _tmp_plan_paths.pop(pid, None)
    if tmp and os.path.exists(tmp):
        os.unlink(tmp)


class TestHistorySha(unittest.TestCase):
    """SHA256 stability and change detection."""

    def test_sha_stable_for_same_content(self) -> None:
        plan_a = _make_plan(["# M\n", "- [x] a\n", "- [ ] b\n"])
        plan_b = _make_plan(["# M\n", "- [x] a\n", "- [ ] b\n"])
        try:
            self.assertEqual(_current_sha(plan_a), _current_sha(plan_b))
        finally:
            _cleanup(plan_a)
            _cleanup(plan_b)

    def test_sha_changes_when_checkboxes_change(self) -> None:
        plan_a = _make_plan(["# M\n", "- [x] a\n", "- [ ] b\n"])
        plan_b = _make_plan(["# M\n", "- [x] a\n", "- [x] b\n"])
        try:
            self.assertNotEqual(_current_sha(plan_a), _current_sha(plan_b))
        finally:
            _cleanup(plan_a)
            _cleanup(plan_b)


class TestRecordSnapshot(unittest.TestCase):
    """Snapshot recording and dedup."""

    def setUp(self) -> None:
        self._tmp_home = tempfile.mkdtemp()

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp_home, ignore_errors=True)

    def _plan_simple(self) -> PlanFile:
        return _make_plan(["# M\n", "- [x] a\n", "- [ ] b\n"])

    def test_records_first_snapshot(self) -> None:
        plan = self._plan_simple()
        try:
            written = record_snapshot(self._tmp_home, plan)
            self.assertTrue(written)
            hd = history_dir(self._tmp_home)
            hpath = os.path.join(hd, f"{plan.plan_id}.jsonl")
            self.assertTrue(os.path.exists(hpath))
            with open(hpath) as f:
                raw = f.read().strip()
            entry = json.loads(raw)
            self.assertEqual(entry["done"], plan.done)
            self.assertEqual(entry["total"], plan.total)
            self.assertIn("ts", entry)
            self.assertIn("sha", entry)
            self.assertIn("milestones", entry)
            self.assertIsInstance(entry["milestones"], dict)
            self.assertEqual(entry["milestones"], {"M": [1, 2]})
        finally:
            _cleanup(plan)

    def test_no_duplicate_snapshot(self) -> None:
        plan = self._plan_simple()
        try:
            record_snapshot(self._tmp_home, plan)
            written = record_snapshot(self._tmp_home, plan)
            self.assertFalse(written, "duplicate snapshot should be rejected")
        finally:
            _cleanup(plan)

    def test_records_on_content_change(self) -> None:
        plan_a = _make_plan(["# M\n", "- [x] a\n", "- [ ] b\n"])
        plan_b = _make_plan(["# M\n", "- [x] a\n", "- [x] b\n"])
        try:
            record_snapshot(self._tmp_home, plan_a)
            written = record_snapshot(self._tmp_home, plan_b)
            self.assertTrue(written, "changed content should create a snapshot")
        finally:
            _cleanup(plan_a)
            _cleanup(plan_b)

    def test_trim_at_500(self) -> None:
        """Produce 510 snapshots, verify only newest 500 remain."""
        plan = self._plan_simple()
        try:
            hd = ensure_history_dir(self._tmp_home)
            hpath = os.path.join(hd, f"{plan.plan_id}.jsonl")
            with open(hpath, "w") as f:
                for i in range(510):
                    entry = {
                        "ts": f"2026-01-01T00:{i:02d}:00",
                        "sha": f"sha_{i:03d}",
                        "done": i,
                        "total": 500,
                    }
                    f.write(json.dumps(entry, separators=(",", ":")) + "\n")

            # record_snapshot writes a new entry (done=1) then trims to 500,
            # so we get 500 entries with the last being the new one.
            record_snapshot(self._tmp_home, plan)
            with open(hpath) as f:
                raw = f.read().strip()
            lines = raw.split("\n")
            self.assertLessEqual(
                len(lines), 500,
                f"expected <=500 lines, got {len(lines)}",
            )
            # First entries (0-9) should be gone
            first = json.loads(lines[0])
            self.assertGreaterEqual(first["done"], 10,
                                    "first remaining entry should be >= 10")
        finally:
            _cleanup(plan)

    def test_read_history_limit(self) -> None:
        plan = self._plan_simple()
        try:
            hd = ensure_history_dir(self._tmp_home)
            hpath = os.path.join(hd, f"{plan.plan_id}.jsonl")
            with open(hpath, "w") as f:
                for i in range(100):
                    entry = {
                        "ts": f"2026-01-01T00:{i:02d}:00",
                        "sha": f"s{i:03d}",
                        "done": i,
                        "total": 100,
                    }
                    f.write(json.dumps(entry, separators=(",", ":")) + "\n")
            entries = read_history(self._tmp_home, plan.plan_id, limit=10)
            self.assertEqual(len(entries), 10)
            self.assertEqual(entries[0]["done"], 90)
        finally:
            _cleanup(plan)


if __name__ == "__main__":
    unittest.main()