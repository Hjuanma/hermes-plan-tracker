"""Tests for the dashboard FastAPI routes - called directly (no server).

Each test constructs calls the route functions in-process. If fastapi is not
available the tests are skipped gracefully.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

# fastapi may not be installed or may be broken in this Python. We gate all
# tests behind a try/except import to keep the stdlib-only test suite clean.
try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    _HAVE_FASTAPI = True
except ImportError:
    _HAVE_FASTAPI = False
except Exception:
    _HAVE_FASTAPI = False


# Store the client and sample dir for the class
_CLIENT = None
_SAMPLE_DIR = None


def _setup_client() -> None:
    global _CLIENT, _SAMPLE_DIR
    if not _HAVE_FASTAPI:
        return
    if _CLIENT is not None:
        return
    _SAMPLE_DIR = tempfile.mkdtemp()
    plans_sub = os.path.join(_SAMPLE_DIR, "plans")
    os.makedirs(plans_sub, exist_ok=True)
    with open(os.path.join(plans_sub, "test-plan.md"), "w") as f:
        f.write("# My Plan\n- [x] done task\n- [ ] pending task\n")

    # Create a registry entry
    registry_md = os.path.join(_SAMPLE_DIR, "extra.md")
    with open(registry_md, "w") as f:
        f.write("# Extra\n- [ ] item\n")

    os.environ["HERMES_HOME"] = _SAMPLE_DIR
    # Import after setting env
    import importlib
    import dashboard.plugin_api
    importlib.reload(dashboard.plugin_api)
    app = FastAPI()
    app.include_router(dashboard.plugin_api.router, prefix="/api/plugins/plan-tracker")
    _CLIENT = TestClient(app)


def _teardown_client() -> None:
    global _CLIENT, _SAMPLE_DIR
    if _SAMPLE_DIR:
        import shutil
        shutil.rmtree(_SAMPLE_DIR, ignore_errors=True)
    _CLIENT = None
    _SAMPLE_DIR = None


@unittest.skipIf(not _HAVE_FASTAPI, "fastapi not available")
class TestApiRoutes(unittest.TestCase):
    """Call the router's route handlers through TestClient."""

    @classmethod
    def setUpClass(cls) -> None:
        _setup_client()

    @classmethod
    def tearDownClass(cls) -> None:
        # Only teardown after all tests in this class
        pass

    def test_health_returns_ok_and_roots(self) -> None:
        resp = _CLIENT.get("/api/plugins/plan-tracker/health")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertIn("roots", body)
        self.assertIsInstance(body["roots"], list)
        roots_with_plans = [r for r in body["roots"] if r["exists"]]
        self.assertGreater(len(roots_with_plans), 0)

    def test_plans_returns_list_with_registered(self) -> None:
        resp = _CLIENT.get("/api/plugins/plan-tracker/plans")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("plans", body)
        self.assertIsInstance(body["plans"], list)
        for p in body["plans"]:
            self.assertIn("registered", p)

    def test_plan_detail_found(self) -> None:
        resp = _CLIENT.get("/api/plugins/plan-tracker/plans")
        plans = resp.json().get("plans", [])
        if not plans:
            self.skipTest("no plans found to test detail")
        plan_id = plans[0]["id"]
        resp2 = _CLIENT.get(f"/api/plugins/plan-tracker/plan?id={plan_id}")
        self.assertEqual(resp2.status_code, 200)
        body = resp2.json()
        self.assertTrue(body["ok"])
        self.assertIn("plan", body)
        detail = body["plan"]
        self.assertEqual(detail["id"], plan_id)
        self.assertIn("milestones", detail)
        self.assertIn("tasks", detail)
        self.assertIn("history", detail)
        self.assertIn("registered", detail)
        self.assertIn("effort", detail)
        self.assertIn("eta", detail)
        self.assertIn("week", detail)

    def test_plan_detail_not_found(self) -> None:
        resp = _CLIENT.get("/api/plugins/plan-tracker/plan?id=nonexistent12345")
        self.assertEqual(resp.status_code, 404)
        body = resp.json()
        self.assertFalse(body["ok"])

    def test_registry_post_success(self) -> None:
        # Create a valid .md in our temp dir
        new_md = os.path.join(_SAMPLE_DIR, "new.md")
        with open(new_md, "w") as f:
            f.write("# New\n- [x] item\n")
        resp = _CLIENT.post(
            "/api/plugins/plan-tracker/registry",
            json={"path": new_md},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["message"], "registered")
        self.assertIn("plan", body)

    def test_registry_post_validation_failure(self) -> None:
        resp = _CLIENT.post(
            "/api/plugins/plan-tracker/registry",
            json={"path": "/nonexistent/file.md"},
        )
        self.assertEqual(resp.status_code, 400)

    def test_registry_post_duplicate(self) -> None:
        # First registration should succeed
        dup_md = os.path.join(_SAMPLE_DIR, "dup.md")
        with open(dup_md, "w") as f:
            f.write("# Dup\n- [ ] item\n")
        _CLIENT.post(
            "/api/plugins/plan-tracker/registry",
            json={"path": dup_md},
        )
        # Second should conflict
        resp = _CLIENT.post(
            "/api/plugins/plan-tracker/registry",
            json={"path": dup_md},
        )
        self.assertEqual(resp.status_code, 409, f"expected 409, got {resp.status_code}: {resp.json()}")

    def test_registry_post_missing_path(self) -> None:
        resp = _CLIENT.post(
            "/api/plugins/plan-tracker/registry",
            json={},
        )
        self.assertEqual(resp.status_code, 400)

    def test_registry_delete_success(self) -> None:
        # Add a file
        rm_md = os.path.join(_SAMPLE_DIR, "rm.md")
        with open(rm_md, "w") as f:
            f.write("# Rm\n- [ ] item\n")
        add_resp = _CLIENT.post(
            "/api/plugins/plan-tracker/registry",
            json={"path": rm_md},
        )
        self.assertEqual(add_resp.status_code, 200)
        plan_id = add_resp.json()["plan"]["id"]
        # Delete it
        resp = _CLIENT.delete(f"/api/plugins/plan-tracker/registry?id={plan_id}")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])

    def test_registry_delete_not_found(self) -> None:
        resp = _CLIENT.delete("/api/plugins/plan-tracker/registry?id=nonexistent12345")
        self.assertEqual(resp.status_code, 404)

    def test_plans_scope_global(self) -> None:
        resp = _CLIENT.get("/api/plugins/plan-tracker/plans?scope=global")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        for p in body["plans"]:
            self.assertIn("scope", p)
            self.assertIn("session", p)

    def test_plans_scope_session(self) -> None:
        resp = _CLIENT.get(
            "/api/plugins/plan-tracker/plans?scope=session&session=test_sess",
        )
        self.assertEqual(resp.status_code, 200)
        # Should be empty since no session-scoped entries exist
        self.assertEqual(len(resp.json()["plans"]), 0)

    def test_registry_post_with_scope(self) -> None:
        scope_md = os.path.join(_SAMPLE_DIR, "scope_test.md")
        with open(scope_md, "w") as f:
            f.write("# Scope Test\n- [x] task\n")
        resp = _CLIENT.post(
            "/api/plugins/plan-tracker/registry",
            json={"path": scope_md, "scope": "session", "session": "sess_a"},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])

    def test_registry_delete_with_scope(self) -> None:
        scope_md = os.path.join(_SAMPLE_DIR, "scope_del.md")
        with open(scope_md, "w") as f:
            f.write("# Scope Del\n- [x] task\n")
        add_resp = _CLIENT.post(
            "/api/plugins/plan-tracker/registry",
            json={"path": scope_md, "scope": "session", "session": "sess_b"},
        )
        self.assertEqual(add_resp.status_code, 200)
        plan_id = add_resp.json()["plan"]["id"]
        resp = _CLIENT.delete(
            f"/api/plugins/plan-tracker/registry?id={plan_id}&scope=session&session=sess_b",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])

    def test_plan_detail_with_scope(self) -> None:
        """GET /plan with scope=global should still find root-discovered plans."""
        resp = _CLIENT.get("/api/plugins/plan-tracker/plans")
        plans = resp.json().get("plans", [])
        if not plans:
            self.skipTest("no plans to test")
        plan_id = plans[0]["id"]
        resp2 = _CLIENT.get(
            f"/api/plugins/plan-tracker/plan?id={plan_id}&scope=global",
        )
        self.assertEqual(resp2.status_code, 200)
        self.assertIn("scope", resp2.json()["plan"])

    def test_plan_detail_has_sha_and_blocks(self) -> None:
        """Detail must include 'sha' and 'blocks' per Phase 3."""
        resp = _CLIENT.get("/api/plugins/plan-tracker/plans")
        plans = resp.json().get("plans", [])
        if not plans:
            self.skipTest("no plans to test")
        plan_id = plans[0]["id"]
        resp2 = _CLIENT.get(f"/api/plugins/plan-tracker/plan?id={plan_id}")
        self.assertEqual(resp2.status_code, 200)
        detail = resp2.json()["plan"]
        self.assertIn("sha", detail, "detail must have a top-level sha")
        self.assertEqual(len(detail["sha"]), 64, "sha must be sha256 hex")
        self.assertIn("blocks", detail, "detail must have a blocks dict")
        self.assertIsInstance(detail["blocks"], dict)

    def test_toggle_success(self) -> None:
        """POST /toggle toggles a checkbox and returns the new sha."""
        toggle_md = os.path.join(_SAMPLE_DIR, "toggle_success.md")
        with open(toggle_md, "w") as f:
            f.write("# Toggle Test\n- [ ] item 1\n- [x] item 2\n")
        # Register it to appear in the detail
        reg_resp = _CLIENT.post(
            "/api/plugins/plan-tracker/registry",
            json={"path": toggle_md},
        )
        self.assertEqual(reg_resp.status_code, 200, reg_resp.json())
        plan_id = reg_resp.json()["plan"]["id"]

        # Get detail to obtain sha
        detail_resp = _CLIENT.get(
            f"/api/plugins/plan-tracker/plan?id={plan_id}",
        )
        self.assertEqual(detail_resp.status_code, 200)
        sha = detail_resp.json()["plan"]["sha"]

        # Toggle line 2 (unchecked) -> checked
        toggle_resp = _CLIENT.post(
            "/api/plugins/plan-tracker/toggle",
            json={"id": plan_id, "line": 2, "expect_sha": sha},
        )
        self.assertEqual(toggle_resp.status_code, 200, toggle_resp.json())
        body = toggle_resp.json()
        self.assertTrue(body["ok"])
        self.assertIn("sha", body)
        self.assertNotEqual(body["sha"], sha)

    def test_toggle_stale(self) -> None:
        """Wrong expect_sha returns 409."""
        toggle_md = os.path.join(_SAMPLE_DIR, "toggle_stale.md")
        with open(toggle_md, "w") as f:
            f.write("# Stale\n- [ ] item\n")
        reg_resp = _CLIENT.post(
            "/api/plugins/plan-tracker/registry",
            json={"path": toggle_md},
        )
        self.assertEqual(reg_resp.status_code, 200)
        plan_id = reg_resp.json()["plan"]["id"]

        toggle_resp = _CLIENT.post(
            "/api/plugins/plan-tracker/toggle",
            json={"id": plan_id, "line": 2, "expect_sha": "0" * 64},
        )
        self.assertEqual(toggle_resp.status_code, 409)
        body = toggle_resp.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], "stale")

    def test_toggle_bad_line(self) -> None:
        """Toggling a non-checkbox line returns 400."""
        toggle_md = os.path.join(_SAMPLE_DIR, "toggle_bad.md")
        with open(toggle_md, "w") as f:
            f.write("# Bad\n- [ ] item\n")
        reg_resp = _CLIENT.post(
            "/api/plugins/plan-tracker/registry",
            json={"path": toggle_md},
        )
        self.assertEqual(reg_resp.status_code, 200)
        plan_id = reg_resp.json()["plan"]["id"]
        detail_resp = _CLIENT.get(
            f"/api/plugins/plan-tracker/plan?id={plan_id}",
        )
        sha = detail_resp.json()["plan"]["sha"]

        # Line 1 is a heading, not a checkbox
        toggle_resp = _CLIENT.post(
            "/api/plugins/plan-tracker/toggle",
            json={"id": plan_id, "line": 1, "expect_sha": sha},
        )
        self.assertEqual(toggle_resp.status_code, 400)

    def test_toggle_missing_id(self) -> None:
        toggle_resp = _CLIENT.post(
            "/api/plugins/plan-tracker/toggle",
            json={"line": 1, "expect_sha": "a" * 64},
        )
        self.assertEqual(toggle_resp.status_code, 400)

    def test_block_success(self) -> None:
        """POST /block marks a milestone as blocked."""
        block_md = os.path.join(_SAMPLE_DIR, "block_test.md")
        with open(block_md, "w") as f:
            f.write("# M\n- [ ] item\n")
        reg_resp = _CLIENT.post(
            "/api/plugins/plan-tracker/registry",
            json={"path": block_md},
        )
        self.assertEqual(reg_resp.status_code, 200)
        plan_id = reg_resp.json()["plan"]["id"]

        # Set a block
        block_resp = _CLIENT.post(
            "/api/plugins/plan-tracker/block",
            json={"id": plan_id, "milestone": "M", "reason": "waiting"},
        )
        self.assertEqual(block_resp.status_code, 200)
        self.assertTrue(block_resp.json()["ok"])

        # Verify via detail
        detail_resp = _CLIENT.get(
            f"/api/plugins/plan-tracker/plan?id={plan_id}",
        )
        self.assertEqual(detail_resp.status_code, 200)
        blocks = detail_resp.json()["plan"]["blocks"]
        self.assertIn("M", blocks)
        self.assertEqual(blocks["M"]["reason"], "waiting")

    def test_unblock_success(self) -> None:
        """DELETE /block removes a block."""
        block_md = os.path.join(_SAMPLE_DIR, "unblock_test.md")
        with open(block_md, "w") as f:
            f.write("# M\n- [ ] item\n")
        reg_resp = _CLIENT.post(
            "/api/plugins/plan-tracker/registry",
            json={"path": block_md},
        )
        self.assertEqual(reg_resp.status_code, 200)
        plan_id = reg_resp.json()["plan"]["id"]

        # Set a block
        _CLIENT.post(
            "/api/plugins/plan-tracker/block",
            json={"id": plan_id, "milestone": "M", "reason": "test"},
        )

        # Unblock
        unblock_resp = _CLIENT.delete(
            f"/api/plugins/plan-tracker/block?id={plan_id}&milestone=M",
        )
        self.assertEqual(unblock_resp.status_code, 200)
        self.assertTrue(unblock_resp.json()["ok"])

        # Verify block is gone
        detail_resp = _CLIENT.get(
            f"/api/plugins/plan-tracker/plan?id={plan_id}",
        )
        self.assertEqual(detail_resp.status_code, 200)
        self.assertNotIn("M", detail_resp.json()["plan"]["blocks"])

    def test_unblock_nonexistent(self) -> None:
        """DELETE /block on a milestone without a block returns 404."""
        unblock_resp = _CLIENT.delete(
            "/api/plugins/plan-tracker/block?id=nonexistent1234&milestone=X",
        )
        self.assertEqual(unblock_resp.status_code, 404)

    def test_block_missing_milestone(self) -> None:
        """POST /block without a milestone returns 400."""
        block_resp = _CLIENT.post(
            "/api/plugins/plan-tracker/block",
            json={"id": "abc", "reason": "test"},
        )
        self.assertEqual(block_resp.status_code, 400)

    def test_tasks_have_line_field(self) -> None:
        """Tasks in the detail must include 'line'."""
        task_md = os.path.join(_SAMPLE_DIR, "task_line.md")
        with open(task_md, "w") as f:
            f.write("# M\n### Task One\n- [ ] sub\n")
        reg_resp = _CLIENT.post(
            "/api/plugins/plan-tracker/registry",
            json={"path": task_md},
        )
        self.assertEqual(reg_resp.status_code, 200)
        plan_id = reg_resp.json()["plan"]["id"]
        detail_resp = _CLIENT.get(
            f"/api/plugins/plan-tracker/plan?id={plan_id}",
        )
        self.assertEqual(detail_resp.status_code, 200)
        tasks = detail_resp.json()["plan"].get("tasks", [])
        for t in tasks:
            self.assertIn("line", t, f"task '{t.get('name')}' missing 'line'")
            self.assertIsInstance(t["line"], int)

    def test_export_returns_markdown(self) -> None:
        """GET /export returns markdown for a known plan."""
        resp = _CLIENT.get("/api/plugins/plan-tracker/plans")
        plans = resp.json().get("plans", [])
        if not plans:
            self.skipTest("no plans to export")
        plan_id = plans[0]["id"]
        export_resp = _CLIENT.get(f"/api/plugins/plan-tracker/export?id={plan_id}")
        self.assertEqual(export_resp.status_code, 200)
        self.assertIn("text/markdown", export_resp.headers.get("content-type", ""))

    def test_export_not_found(self) -> None:
        """GET /export with unknown id returns 404."""
        resp = _CLIENT.get("/api/plugins/plan-tracker/export?id=nonexistent123456")
        self.assertEqual(resp.status_code, 404)
        body = resp.json()
        self.assertFalse(body["ok"])


if __name__ == "__main__":
    _setup_client()
    try:
        unittest.main()
    finally:
        _teardown_client()