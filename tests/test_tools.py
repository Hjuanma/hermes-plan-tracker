"""Tests for the plan_track tool handler - called directly, no server."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from core import handle_plan_track, load_registry


class TestPlanTrackHandler(unittest.TestCase):
    """Call handle_plan_track directly with various actions."""

    def setUp(self) -> None:
        self._tmp_home = tempfile.mkdtemp()
        self._plans_dir = os.path.join(self._tmp_home, "plans")
        os.makedirs(self._plans_dir)
        self._valid_md = os.path.join(self._plans_dir, "test.md")
        with open(self._valid_md, "w") as f:
            f.write("# Test Plan\n- [ ] item 1\n- [x] item 2\n")
        # Point HERMES_HOME at our temp dir
        self._old_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = self._tmp_home

    def tearDown(self) -> None:
        if self._old_home is not None:
            os.environ["HERMES_HOME"] = self._old_home
        else:
            os.environ.pop("HERMES_HOME", None)
        import shutil
        shutil.rmtree(self._tmp_home, ignore_errors=True)

    def test_tool_add(self) -> None:
        result = json.loads(handle_plan_track({"action": "add", "path": self._valid_md}))
        self.assertTrue(result["ok"])
        self.assertEqual(result["message"], "registered")
        # Default scope should be global (no session_id injected)
        entries = load_registry(self._tmp_home)
        self.assertEqual(entries[0]["scope"], "global")

    def test_tool_add_missing_path(self) -> None:
        result = json.loads(handle_plan_track({"action": "add"}))
        self.assertFalse(result["ok"])

    def test_tool_add_invalid_path(self) -> None:
        result = json.loads(handle_plan_track({"action": "add", "path": "/nonexistent.md"}))
        self.assertFalse(result["ok"])

    def test_tool_add_with_session_id(self) -> None:
        """When session_id is injected, default scope is session."""
        result = json.loads(handle_plan_track(
            {"action": "add", "path": self._valid_md},
            session_id="sess_abc",
        ))
        self.assertTrue(result["ok"])
        entries = load_registry(self._tmp_home)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["scope"], "session")
        self.assertEqual(entries[0]["session"], "sess_abc")

    def test_tool_add_explicit_global_overrides_session_id(self) -> None:
        """Explicit scope='global' wins even when session_id is injected."""
        result = json.loads(handle_plan_track(
            {"action": "add", "path": self._valid_md, "scope": "global"},
            session_id="sess_abc",
        ))
        self.assertTrue(result["ok"])
        entries = load_registry(self._tmp_home)
        self.assertEqual(entries[0]["scope"], "global")
        self.assertEqual(entries[0]["session"], "")

    def test_tool_remove(self) -> None:
        # First add
        add_result = json.loads(handle_plan_track({"action": "add", "path": self._valid_md}))
        self.assertTrue(add_result["ok"])
        plan_id = add_result["plan"]["id"]
        # Then remove
        result = json.loads(handle_plan_track({"action": "remove", "id": plan_id}))
        self.assertTrue(result["ok"])
        self.assertEqual(result["message"], "removed")

    def test_tool_remove_missing_key(self) -> None:
        result = json.loads(handle_plan_track({"action": "remove"}))
        self.assertFalse(result["ok"])

    def test_tool_list(self) -> None:
        result = json.loads(handle_plan_track({"action": "list"}))
        self.assertTrue(result["ok"])
        self.assertIsInstance(result["paths"], list)
        self.assertEqual(len(result["paths"]), 0)

    def test_tool_list_after_add(self) -> None:
        handle_plan_track({"action": "add", "path": self._valid_md})
        result = json.loads(handle_plan_track({"action": "list"}))
        self.assertEqual(len(result["paths"]), 1)
        # Each path entry is a dict with scope/session
        self.assertIn("scope", result["paths"][0])
        self.assertIn("session", result["paths"][0])

    def test_tool_list_reports_scopes(self) -> None:
        """list returns entries with their scope and session."""
        handle_plan_track(
            {"action": "add", "path": self._valid_md, "scope": "session", "session": "sess_a"},
        )
        result = json.loads(handle_plan_track({"action": "list"}))
        self.assertEqual(result["paths"][0]["scope"], "session")
        self.assertEqual(result["paths"][0]["session"], "sess_a")

    def test_tool_unknown_action(self) -> None:
        result = json.loads(handle_plan_track({"action": "unknown"}))
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()