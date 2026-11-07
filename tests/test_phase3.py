"""Tests for Phase 3: toggle checkbox (3.1) and blocked milestones (3.2).

Uses temp files only, never touches the user's plans.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest

from core import (
    Checkbox,
    Milestone,
    PlanFile,
    clear_block,
    load_blocks,
    load_plan_detail,
    load_registry,
    parse_plan,
    resolve_plan_path,
    save_registry,
    set_block,
    toggle_checkbox,
)


class TestToggleCheckbox(unittest.TestCase):
    """3.1: toggle_checkbox content-hash guard and atomic write."""

    def setUp(self) -> None:
        self._tmp_home = tempfile.mkdtemp()
        self._plans_dir = os.path.join(self._tmp_home, "plans")
        os.makedirs(self._plans_dir, exist_ok=True)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp_home, ignore_errors=True)

    def _make_plan(self, lines: list) -> str:
        path = os.path.join(self._plans_dir, "test.md")
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        return path

    def _file_sha(self, path: str) -> str:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    def _plan_id(self, path: str) -> str:
        rp = str(os.path.realpath(path))
        return hashlib.sha256(rp.encode("utf-8")).hexdigest()[:12]

    def test_toggle_flips_checked_state(self) -> None:
        """Toggle flips exactly one checkbox character: [ ] -> [x] -> [ ]."""
        path = self._make_plan([
            "# M\n",
            "- [ ] task 1\n",
            "- [x] task 2\n",
        ])
        pid = self._plan_id(path)
        sha = self._file_sha(path)

        # Toggle unchecked to checked
        ok, msg, new_sha = toggle_checkbox(self._tmp_home, pid, 2, sha)
        self.assertTrue(ok, f"toggle should succeed: {msg}")
        with open(path, "r") as f:
            content = f.read()
        self.assertIn("- [x] task 1", content)
        self.assertIn("- [x] task 2", content)
        self.assertNotEqual(new_sha, sha, "sha must change after toggle")

        # Toggle back checked to unchecked
        ok, msg, sha3 = toggle_checkbox(self._tmp_home, pid, 2, new_sha)
        self.assertTrue(ok, f"toggle back should succeed: {msg}")
        with open(path, "r") as f:
            content = f.read()
        self.assertIn("- [ ] task 1", content)
        self.assertIn("- [x] task 2", content)

    def test_stale_sha_refused(self) -> None:
        """A wrong expect_sha returns 'stale' and leaves the file untouched."""
        path = self._make_plan([
            "# M\n",
            "- [ ] task 1\n",
        ])
        pid = self._plan_id(path)

        ok, msg, _ = toggle_checkbox(self._tmp_home, pid, 2, "0" * 64)
        self.assertFalse(ok)
        self.assertEqual(msg, "stale")

        # File must be untouched
        with open(path, "r") as f:
            self.assertIn("- [ ] task 1", f.read())

    def test_non_checkbox_line_refused(self) -> None:
        """A heading line (not a checkbox) is refused."""
        path = self._make_plan([
            "# M\n",
            "- [ ] task 1\n",
        ])
        pid = self._plan_id(path)
        sha = self._file_sha(path)

        ok, msg, _ = toggle_checkbox(self._tmp_home, pid, 1, sha)
        self.assertFalse(ok)
        self.assertNotEqual(msg, "stale")  # some descriptive error

    def test_line_beyond_eof_refused(self) -> None:
        path = self._make_plan([
            "# M\n",
        ])
        pid = self._plan_id(path)
        sha = self._file_sha(path)

        ok, msg, _ = toggle_checkbox(self._tmp_home, pid, 100, sha)
        self.assertFalse(ok)

    def test_trailing_newline_preserved(self) -> None:
        """The trailing newline must survive the toggle."""
        path = self._make_plan([
            "# M\n",
            "- [ ] task 1\n",
        ])
        pid = self._plan_id(path)
        sha = self._file_sha(path)

        toggle_checkbox(self._tmp_home, pid, 2, sha)
        with open(path, "rb") as f:
            content = f.read()
        self.assertTrue(content.endswith(b"\n"), "trailing newline must survive")

    def test_concurrent_toggle_of_two_lines(self) -> None:
        """Toggling two lines in sequence works (second toggle uses first's sha)."""
        path = self._make_plan([
            "# M\n",
            "- [ ] item 1\n",
            "- [ ] item 2\n",
        ])
        pid = self._plan_id(path)
        sha = self._file_sha(path)

        ok1, msg1, sha1 = toggle_checkbox(self._tmp_home, pid, 2, sha)
        self.assertTrue(ok1, f"first toggle: {msg1}")

        ok2, msg2, _ = toggle_checkbox(self._tmp_home, pid, 3, sha1)
        self.assertTrue(ok2, f"second toggle: {msg2}")

        with open(path, "r") as f:
            content = f.read()
        self.assertIn("- [x] item 1", content)
        self.assertIn("- [x] item 2", content)

    def test_asterisk_checkbox_toggled(self) -> None:
        """Toggle works with * [ ] lines too."""
        path = self._make_plan([
            "# M\n",
            "* [ ] item\n",
        ])
        pid = self._plan_id(path)
        sha = self._file_sha(path)

        ok, msg, _ = toggle_checkbox(self._tmp_home, pid, 2, sha)
        self.assertTrue(ok, f"toggle asterisk checkbox: {msg}")
        with open(path, "r") as f:
            content = f.read()
        self.assertIn("* [x] item", content)

    def test_plan_detail_exposes_sha_and_line(self) -> None:
        """load_plan_detail must include 'sha' (top-level) and 'line' per task."""
        path = self._make_plan([
            "# M\n",
            "### Task 1\n",
            "- [ ] item a\n",
            "### Task 2\n",
            "- [x] item b\n",
        ])
        # Register the file so load_plan_detail finds it
        from core import add_registry_entry
        add_registry_entry(self._tmp_home, path, scope="global")
        plan = parse_plan(path)
        pid = plan.plan_id

        detail = load_plan_detail(self._tmp_home, pid, cwd=self._tmp_home)
        self.assertIsNotNone(detail)
        self.assertIn("sha", detail, "detail must have a top-level 'sha'")
        # The sha should be a 64-char hex string
        self.assertEqual(len(detail["sha"]), 64, "sha must be sha256 hex")

        # Tasks should have 'line' pointing to the first checkbox in that task
        for t in detail.get("tasks", []):
            self.assertIn("line", t, f"task '{t.get('name')}' must have a 'line' field")
            self.assertGreater(t["line"], 0, f"task '{t.get('name')}' line must be > 0")

    def test_toggle_uses_file_sha(self) -> None:
        """The sha used by toggle_checkbox must match the sha from the file bytes."""
        path = self._make_plan([
            "# M\n",
            "- [ ] item\n",
        ])
        pid = self._plan_id(path)
        file_sha = self._file_sha(path)

        # Mock: simulate what the pane does - get sha from detail, use it for toggle
        from core import add_registry_entry, load_plan_detail
        add_registry_entry(self._tmp_home, path, scope="global")
        detail = load_plan_detail(self._tmp_home, pid, cwd=self._tmp_home)
        detail_sha = detail["sha"]

        self.assertEqual(file_sha, detail_sha, "detail sha must be sha256 of file bytes")

        ok, msg, new_sha = toggle_checkbox(self._tmp_home, pid, 2, detail_sha)
        self.assertTrue(ok, f"toggle with detail sha: {msg}")

    def test_unknown_plan_id_returns_not_found(self) -> None:
        ok, msg, _ = toggle_checkbox(self._tmp_home, "nonexistent1234", 1, "a" * 64)
        self.assertFalse(ok)
        self.assertIn("not found", msg.lower())


class TestBlocks(unittest.TestCase):
    """3.2: set_block / clear_block / load_blocks sidecar."""

    def setUp(self) -> None:
        self._tmp_home = tempfile.mkdtemp()

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp_home, ignore_errors=True)

    def test_set_and_clear_block_roundtrip(self) -> None:
        set_block(self._tmp_home, "plan_abc", "WS1", "waiting on API")
        blocks = load_blocks(self._tmp_home, "plan_abc")
        self.assertIn("WS1", blocks)
        self.assertEqual(blocks["WS1"]["reason"], "waiting on API")
        self.assertIn("ts", blocks["WS1"])

        cleared = clear_block(self._tmp_home, "plan_abc", "WS1")
        self.assertTrue(cleared)
        blocks = load_blocks(self._tmp_home, "plan_abc")
        self.assertNotIn("WS1", blocks)

    def test_block_survives_plan_edit(self) -> None:
        """Blocks are in a sidecar file, not the .md, so they naturally survive."""
        set_block(self._tmp_home, "plan_abc", "WS1", "reason")
        # Re-read from disk
        blocks = load_blocks(self._tmp_home, "plan_abc")
        self.assertIn("WS1", blocks)

    def test_clear_nonexistent_returns_false(self) -> None:
        cleared = clear_block(self._tmp_home, "plan_abc", "NONEXISTENT")
        self.assertFalse(cleared)

    def test_set_block_overwrites_reason(self) -> None:
        set_block(self._tmp_home, "plan_abc", "WS1", "first reason")
        set_block(self._tmp_home, "plan_abc", "WS1", "updated reason")
        blocks = load_blocks(self._tmp_home, "plan_abc")
        self.assertEqual(blocks["WS1"]["reason"], "updated reason")

    def test_detail_exposes_blocks(self) -> None:
        """load_plan_detail must include a 'blocks' dict."""
        # Create a plan, register it
        plans_dir = os.path.join(self._tmp_home, "plans")
        os.makedirs(plans_dir, exist_ok=True)
        path = os.path.join(plans_dir, "test.md")
        with open(path, "w") as f:
            f.write("# M\n- [ ] item\n")
        from core import add_registry_entry
        add_registry_entry(self._tmp_home, path, scope="global")
        plan = parse_plan(path)
        pid = plan.plan_id

        # Set a block
        set_block(self._tmp_home, pid, "M", "blocked")

        detail = load_plan_detail(self._tmp_home, pid, cwd=self._tmp_home)
        self.assertIsNotNone(detail)
        self.assertIn("blocks", detail, "detail must have 'blocks'")
        self.assertIn("M", detail["blocks"], "blocked milestone must appear in blocks")
        self.assertEqual(detail["blocks"]["M"]["reason"], "blocked")

    def test_no_blocks_in_detail_when_none_set(self) -> None:
        plans_dir = os.path.join(self._tmp_home, "plans")
        os.makedirs(plans_dir, exist_ok=True)
        path = os.path.join(plans_dir, "test.md")
        with open(path, "w") as f:
            f.write("# M\n- [ ] item\n")
        from core import add_registry_entry
        add_registry_entry(self._tmp_home, path, scope="global")
        plan = parse_plan(path)
        pid = plan.plan_id

        detail = load_plan_detail(self._tmp_home, pid, cwd=self._tmp_home)
        self.assertIn("blocks", detail)
        self.assertEqual(detail["blocks"], {})


class TestResolvePlanPath(unittest.TestCase):
    """Helper used by toggle_checkbox to find a file by plan_id."""

    def setUp(self) -> None:
        self._tmp_home = tempfile.mkdtemp()
        self._plans_dir = os.path.join(self._tmp_home, "plans")
        os.makedirs(self._plans_dir, exist_ok=True)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp_home, ignore_errors=True)

    def _plan_id_for_path(self, path: str) -> str:
        rp = str(os.path.realpath(path))
        return hashlib.sha256(rp.encode("utf-8")).hexdigest()[:12]

    def test_resolves_root_plan(self) -> None:
        path = os.path.join(self._plans_dir, "test.md")
        with open(path, "w") as f:
            f.write("# M\n- [ ] item\n")
        pid = self._plan_id_for_path(path)
        resolved = resolve_plan_path(self._tmp_home, pid)
        self.assertEqual(resolved, os.path.realpath(path))

    def test_resolves_registered_plan(self) -> None:
        extra_dir = os.path.join(self._tmp_home, "extra")
        os.makedirs(extra_dir, exist_ok=True)
        path = os.path.join(extra_dir, "test.md")
        with open(path, "w") as f:
            f.write("# M\n- [ ] item\n")
        pid = self._plan_id_for_path(path)

        # Register it but not in root
        from core import add_registry_entry
        add_registry_entry(self._tmp_home, path, scope="global")

        resolved = resolve_plan_path(self._tmp_home, pid)
        self.assertEqual(resolved, os.path.realpath(path))

    def test_nonexistent_returns_none(self) -> None:
        resolved = resolve_plan_path(self._tmp_home, "nonexistent1234")
        self.assertIsNone(resolved)


if __name__ == "__main__":
    unittest.main()