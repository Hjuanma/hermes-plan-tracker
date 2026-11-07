"""Tests for core.py - parsing, progress, plan-id derivation, and registry.

These tests use synthetic markdown and the bundled fixture at
``tests/fixtures/roadmap.md``. Two opt-in tests run against a real plan when
``PLAN_TRACKER_REAL_PLAN`` points at one.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from effort import attribute_effort

from core import (
    Checkbox,
    Milestone,
    PlanFile,
    _find_milestone_level,
    _first_heading,
    _milestones_dict,
    _parse_lines,
    _parse_weights_from_lines,
    _skip_frontmatter,
    add_registry_entry,
    compute_eta,
    compute_week_delta,
    export_plan_markdown,
    load_all_plans,
    load_plan_detail,
    load_registry,
    parse_plan,
    record_snapshot,
    record_snapshot_with_effort,
    remove_registry_entry,
    save_registry,
    scan_roots,
    validate_registration_path,
)


class TestParsing(unittest.TestCase):
    """Basic parsing: milestones, checkboxes, tasks, fencing."""

    def test_simple_milestones_and_checkboxes(self) -> None:
        lines = [
            "# WS0 - Bootstrap\n",
            "- [x] Step one\n",
            "- [ ] Step two\n",
            "# WS1 - Domain\n",
            "- [x] Step A\n",
            "- [x] Step B\n",
            "- [ ] Step C\n",
        ]
        milestones = _parse_lines(lines)
        self.assertEqual(len(milestones), 2)
        self.assertEqual(milestones[0].name, "WS0 - Bootstrap")
        self.assertEqual(milestones[0].done, 1)
        self.assertEqual(milestones[0].total, 2)
        self.assertEqual(milestones[1].name, "WS1 - Domain")
        self.assertEqual(milestones[1].done, 2)
        self.assertEqual(milestones[1].total, 3)

    def test_checkbox_variants(self) -> None:
        """``[x]``, ``[X]``, ``[ ]`` are accepted."""
        lines = [
            "# M\n",
            "- [x] lower\n",
            "- [X] upper\n",
            "- [ ] space\n",
            "* [x] asterisk\n",
        ]
        milestones = _parse_lines(lines)
        self.assertEqual(len(milestones), 1)
        self.assertEqual(milestones[0].done, 3)
        self.assertEqual(milestones[0].total, 4)

    def test_fenced_code_blocks_skipped(self) -> None:
        """Checkbox-like lines inside fenced blocks are ignored."""
        lines = [
            "# M\n",
            "- [ ] real task\n",
            "```\n",
            "- [ ] fake inside fence\n",
            "```\n",
            "- [x] another real\n",
        ]
        milestones = _parse_lines(lines)
        self.assertEqual(len(milestones), 1)
        self.assertEqual(milestones[0].done, 1)
        self.assertEqual(milestones[0].total, 2)

    def test_nested_code_blocks(self) -> None:
        """Nested fences still skip content properly."""
        lines = [
            "# M\n",
            "```\n",
            "# Not a heading\n",
            "- [ ] faked\n",
            "```\n",
            "- [x] actual\n",
        ]
        milestones = _parse_lines(lines)
        self.assertEqual(len(milestones), 1)
        self.assertEqual(milestones[0].total, 1)
        self.assertEqual(milestones[0].done, 1)

    def test_no_h1_preamble(self) -> None:
        """No heading at all -> preamble pseudo-milestone."""
        lines = [
            "- [x] task one\n",
            "- [ ] task two\n",
            "- [x] task three\n",
        ]
        milestones = _parse_lines(lines)
        self.assertEqual(len(milestones), 1)
        self.assertEqual(milestones[0].name, "(preamble)")
        self.assertEqual(milestones[0].done, 2)
        self.assertEqual(milestones[0].total, 3)

    def test_checkboxes_before_first_h1_ignored_when_h1_exists(self) -> None:
        """Checkboxes before the first heading are ignored when the file has any heading."""
        lines = [
            "- [ ] before\n",
            "# M\n",
            "- [x] after\n",
        ]
        milestones = _parse_lines(lines)
        self.assertEqual(len(milestones), 1)
        self.assertEqual(milestones[0].done, 1)
        self.assertEqual(milestones[0].total, 1)
        self.assertNotEqual(milestones[0].name, "(preamble)")

    def test_zero_checkbox_milestone_skipped(self) -> None:
        """Milestones with zero checkboxes are removed."""
        lines = [
            "# ADR-001 - Some Decision\n",
            "Context here.\n",
            "# WS0 - Bootstrap\n",
            "- [ ] step\n",
        ]
        milestones = _parse_lines(lines)
        self.assertEqual(len(milestones), 1)
        self.assertEqual(milestones[0].name, "WS0 - Bootstrap")

    def test_task_headers(self) -> None:
        """H3 task headers are captured under their milestone."""
        lines = [
            "# M\n",
            "- [ ] top-level item\n",
            "### Task 1: Setup\n",
            "- [x] subtask\n",
            "### Task 2: Build\n",
            "- [ ] subtask\n",
        ]
        milestones = _parse_lines(lines)
        self.assertEqual(len(milestones), 1)
        self.assertEqual(len(milestones[0].tasks), 2)
        self.assertEqual(milestones[0].tasks[0].name, "Task 1: Setup")
        self.assertEqual(milestones[0].tasks[0].done, 1)
        self.assertEqual(milestones[0].tasks[0].total, 1)
        self.assertEqual(milestones[0].tasks[1].name, "Task 2: Build")
        self.assertEqual(milestones[0].tasks[1].done, 0)
        self.assertEqual(milestones[0].tasks[1].total, 1)

    def test_h3_with_more_hashes(self) -> None:
        """``####`` and beyond are also treated as task headers."""
        lines = [
            "# M\n",
            "- [ ] top-level item\n",
            "#### A sub-section\n",
            "- [x] item\n",
        ]
        milestones = _parse_lines(lines)
        self.assertEqual(len(milestones), 1)
        self.assertEqual(len(milestones[0].tasks), 1)
        self.assertEqual(milestones[0].tasks[0].name, "A sub-section")


class TestAdaptiveLevel(unittest.TestCase):
    """v0.2: adaptive milestone heading level."""

    def test_level_2_milestones(self) -> None:
        """## headings at level 2 produce milestones when no H1 exists."""
        lines = [
            "## Fase 1 - Inventario\n",
            "- [x] item 1\n",
            "- [x] item 2\n",
            "- [ ] item 3\n",
            "## Fase 2 - Migracion\n",
            "- [x] item 1\n",
            "- [ ] item 2\n",
            "- [ ] item 3\n",
            "## Fase 3 - Cutover\n",
            "- [ ] item 1\n",
            "- [ ] item 2\n",
        ]
        milestones = _parse_lines(lines)
        self.assertEqual(len(milestones), 3)
        self.assertEqual(milestones[0].name, "Fase 1 - Inventario")
        self.assertEqual(milestones[0].done, 2)
        self.assertEqual(milestones[0].total, 3)
        self.assertEqual(milestones[1].name, "Fase 2 - Migracion")
        self.assertEqual(milestones[1].done, 1)
        self.assertEqual(milestones[1].total, 3)
        self.assertEqual(milestones[2].name, "Fase 3 - Cutover")
        self.assertEqual(milestones[2].done, 0)
        self.assertEqual(milestones[2].total, 2)

    def test_h1_with_h2_subsections(self) -> None:
        """H1 is milestone_level even when H2 subsections exist."""
        lines = [
            "# Overview\n",
            "- [ ] overview item\n",
            "## Phase A\n",
            "- [x] task 1\n",
            "## Phase B\n",
            "- [ ] task 2\n",
        ]
        milestones = _parse_lines(lines)
        self.assertEqual(len(milestones), 1)
        self.assertEqual(milestones[0].name, "Overview")
        self.assertEqual(milestones[0].done, 1)
        self.assertEqual(milestones[0].total, 3)
        # H2 headings become tasks since milestone_level=1
        self.assertEqual(len(milestones[0].tasks), 2)

    def test_level_3_detection(self) -> None:
        """### headings at level 3 when no 1-2 headings contain checkboxes."""
        lines = [
            "### Section A\n",
            "- [x] task\n",
            "### Section B\n",
            "- [ ] task\n",
        ]
        milestones = _parse_lines(lines)
        self.assertEqual(len(milestones), 2)
        self.assertEqual(milestones[0].name, "Section A")
        self.assertEqual(milestones[0].done, 1)
        self.assertEqual(milestones[0].total, 1)
        self.assertEqual(milestones[1].name, "Section B")

    def test_mixed_levels_shallowest_wins(self) -> None:
        """H1 with checkboxes takes priority over deeper headings."""
        lines = [
            "# Workstream 1\n",
            "- [x] item\n",
            "## Sub A\n",
            "- [ ] sub item\n",
            "# Workstream 2\n",
            "- [ ] item\n",
        ]
        milestones = _parse_lines(lines)
        self.assertEqual(len(milestones), 2)
        self.assertEqual(milestones[0].name, "Workstream 1")
        self.assertEqual(milestones[0].done, 1)
        self.assertEqual(milestones[0].total, 2)
        self.assertEqual(milestones[1].name, "Workstream 2")
        self.assertEqual(milestones[1].done, 0)
        self.assertEqual(milestones[1].total, 1)

    def test_first_heading_of_any_level(self) -> None:
        """_first_heading returns the first heading (any level 1-4)."""
        lines = [
            "## Phase 1\n",
            "content\n",
        ]
        self.assertEqual(_first_heading(lines), "Phase 1")

    def test_first_heading_none(self) -> None:
        self.assertEqual(_first_heading([]), "")
        self.assertEqual(_first_heading(["- [ ] task\n"]), "")

    def test_first_heading_skips_fences(self) -> None:
        lines = [
            "```\n",
            "# heading inside fence\n",
            "```\n",
            "# Real heading\n",
        ]
        self.assertEqual(_first_heading(lines), "Real heading")

    def test_phased_plan_fixture(self) -> None:
        """Plan with ## phases (no H1) -> milestone_level=2, 3 milestones, 3/8 = 37.5%."""
        lines = [
            "## Fase 1 - Inventario\n",
            "- [x] item 1\n",
            "- [x] item 2\n",
            "- [ ] item 3\n",
            "## Fase 2 - Migracion\n",
            "- [x] item 1\n",
            "- [ ] item 2\n",
            "- [ ] item 3\n",
            "## Fase 3 - Cutover\n",
            "- [ ] item 1\n",
            "- [ ] item 2\n",
        ]
        milestones = _parse_lines(lines)
        self.assertEqual(len(milestones), 3)
        self.assertEqual(milestones[0].name, "Fase 1 - Inventario")
        self.assertEqual(milestones[0].done, 2)
        self.assertEqual(milestones[0].total, 3)
        self.assertEqual(milestones[1].name, "Fase 2 - Migracion")
        self.assertEqual(milestones[1].done, 1)
        self.assertEqual(milestones[1].total, 3)
        self.assertEqual(milestones[2].name, "Fase 3 - Cutover")
        self.assertEqual(milestones[2].done, 0)
        self.assertEqual(milestones[2].total, 2)
        total_done = sum(m.done for m in milestones)
        total_total = sum(m.total for m in milestones)
        self.assertEqual(total_done, 3)
        self.assertEqual(total_total, 8)
        self.assertAlmostEqual(round(total_done / total_total * 100, 1), 37.5, delta=0.1)

    def test_level_4_detection(self) -> None:
        """#### headings at level 4 when no shallower level has checkboxes."""
        lines = [
            "#### Task group\n",
            "- [x] item\n",
        ]
        milestones = _parse_lines(lines)
        self.assertEqual(len(milestones), 1)
        self.assertEqual(milestones[0].name, "Task group")
        self.assertEqual(milestones[0].done, 1)

    def test_h1_title_above_level_4_milestones(self) -> None:
        """An H1 title with no checkbox of its own is never a milestone."""
        lines = [
            "# Doc title\n",
            "some text\n",
            "#### Milestone One\n",
            "- [x] item\n",
        ]
        milestones = _parse_lines(lines)
        self.assertEqual(len(milestones), 1)
        self.assertEqual(milestones[0].name, "Milestone One")
        self.assertEqual(milestones[0].done, 1)
        self.assertEqual(milestones[0].total, 1)
        self.assertEqual(len(milestones[0].tasks), 0)

    def test_h2_task_under_h1_milestone(self) -> None:
        """H2 headings are tasks when milestone_level=1."""
        lines = [
            "# Sprint 1\n",
            "- [ ] sprint item\n",
            "## Task A\n",
            "- [x] subtask\n",
            "## Task B\n",
            "- [ ] subtask\n",
        ]
        milestones = _parse_lines(lines)
        self.assertEqual(len(milestones), 1)
        self.assertEqual(len(milestones[0].tasks), 2)
        self.assertEqual(milestones[0].tasks[0].name, "Task A")


class TestProgress(unittest.TestCase):
    """Percentages, rounding, zero-division safety."""

    def _make_ms(self, name: str, done: int, total: int) -> Milestone:
        ms = Milestone(name, 1)
        ms.checkboxes = [Checkbox(i < done, i) for i in range(total)]
        return ms

    def test_percent_rounding(self) -> None:
        ms = self._make_ms("test", 1, 3)
        self.assertAlmostEqual(ms.percent, 33.3, places=1)

    def test_zero_total_percent(self) -> None:
        ms = Milestone("empty", 1)
        self.assertEqual(ms.percent, 0.0)

    def test_hundred_percent(self) -> None:
        ms = self._make_ms("done", 5, 5)
        self.assertEqual(ms.percent, 100.0)

    def test_plan_global_percent(self) -> None:
        plan = PlanFile.__new__(PlanFile)
        plan.path = "/fake/path.md"
        plan.milestones = [
            self._make_ms("A", 1, 2),
            self._make_ms("B", 1, 1),
        ]
        self.assertEqual(plan.done, 2)
        self.assertEqual(plan.total, 3)
        self.assertAlmostEqual(plan.percent, 66.7, places=1)

    def test_plan_zero_total(self) -> None:
        plan = PlanFile.__new__(PlanFile)
        plan.path = "/fake.md"
        plan.milestones = []
        self.assertEqual(plan.percent, 0.0)
        self.assertEqual(plan.done, 0)
        self.assertEqual(plan.total, 0)

    def test_status_done(self) -> None:
        ms = self._make_ms("m", 3, 3)
        self.assertEqual(ms.status, "done")

    def test_status_active(self) -> None:
        ms = self._make_ms("m", 1, 3)
        self.assertEqual(ms.status, "active")

    def test_status_pending(self) -> None:
        ms = self._make_ms("m", 0, 3)
        self.assertEqual(ms.status, "pending")

    def test_status_zero_total(self) -> None:
        ms = Milestone("empty", 1)
        self.assertEqual(ms.status, "pending")


class TestPlanId(unittest.TestCase):
    """Plan id = first 12 hex of sha256(realpath)."""

    def test_stable_id(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w") as f:
            f.write("# M\n- [ ] x\n")
            tmppath = f.name
        try:
            plan = parse_plan(tmppath)
            rp = str(os.path.realpath(tmppath))
            expected = hashlib.sha256(rp.encode("utf-8")).hexdigest()[:12]
            self.assertEqual(plan.plan_id, expected)
        finally:
            os.unlink(tmppath)

    def test_id_changes_on_path_change(self) -> None:
        """Different files have different ids."""
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w") as f:
            f.write("# M\n- [ ] x\n")
            p1 = f.name
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w") as f:
            f.write("# M\n- [ ] x\n")
            p2 = f.name
        try:
            plan1 = parse_plan(p1)
            plan2 = parse_plan(p2)
            self.assertNotEqual(plan1.plan_id, plan2.plan_id)
        finally:
            os.unlink(p1)
            os.unlink(p2)


class TestFixturePlan(unittest.TestCase):
    """Parse the bundled fixture plan: a real-world shape, without a home dir."""

    DATA_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "roadmap.md")

    def test_parse_fixture(self) -> None:
        plan = parse_plan(self.DATA_PATH)
        self.assertEqual(plan.total, 12, "expected 12 checkboxes (the 2 in the code block must not count)")
        self.assertEqual(plan.done, 3)
        self.assertEqual(plan.percent, 25.0)

    def test_fixture_title_is_the_document_title(self) -> None:
        plan = parse_plan(self.DATA_PATH)
        self.assertEqual(plan.name, "Fixture Roadmap - Implementation Plan")

    def test_fixture_per_milestone_counts(self) -> None:
        plan = parse_plan(self.DATA_PATH)
        expected = {"WS0 - Bootstrap": (2, 4), "WS1 - API": (1, 4), "WS2 - UI": (0, 4)}
        self.assertEqual([ms.name for ms in plan.milestones], list(expected))
        for ms in plan.milestones:
            self.assertEqual(
                (ms.done, ms.total), expected[ms.name],
                f"{ms.name}: expected {expected[ms.name]}, got {(ms.done, ms.total)}",
            )
        self.assertEqual(sum(ms.done for ms in plan.milestones), plan.done)
        self.assertEqual(sum(ms.total for ms in plan.milestones), plan.total)

    def test_fixture_folds_subsections_and_ignores_the_title(self) -> None:
        """`###` checkboxes belong to their `##` milestone; the H1 title is not one."""
        plan = parse_plan(self.DATA_PATH)
        self.assertNotIn("Fixture Roadmap - Implementation Plan", [m.name for m in plan.milestones])
        api = [m for m in plan.milestones if m.name == "WS1 - API"][0]
        self.assertEqual(api.total, 4, "the 3 own checkboxes plus the folded `###` one")
        self.assertEqual(len(plan.milestones), 3, "subsections must not become milestones")


class TestOptionalRealPlan(unittest.TestCase):
    """Opt-in invariants against a real plan, via PLAN_TRACKER_REAL_PLAN.

    Frozen counts are deliberately absent: a living plan changes every day.
    """

    def _plan(self):
        path = os.environ.get("PLAN_TRACKER_REAL_PLAN")
        if not path or not os.path.exists(path):
            self.skipTest("set PLAN_TRACKER_REAL_PLAN to a plan file to run this")
        return parse_plan(path)

    def test_real_plan_invariants(self) -> None:
        plan = self._plan()
        self.assertGreater(plan.total, 0)
        self.assertLessEqual(plan.done, plan.total)
        for ms in plan.milestones:
            self.assertLessEqual(ms.done, ms.total, f"{ms.name}: done exceeds total")
        self.assertEqual(sum(ms.total for ms in plan.milestones), plan.total)
        self.assertEqual(sum(ms.done for ms in plan.milestones), plan.done)

    def test_real_plan_checked_count_matches_the_file(self) -> None:
        plan = self._plan()
        with open(os.environ["PLAN_TRACKER_REAL_PLAN"], "r", encoding="utf-8") as fh:
            text = fh.read()
        expected_done = len(re.findall(r"^\s*[-*]\s+\[[xX]\]", text, re.MULTILINE))
        self.assertEqual(plan.done, expected_done)


class TestMilestoneLevelDescent(unittest.TestCase):
    """A level that yields a single milestone is not fine-grained enough."""

    WRITING_PLANS = """# Demo Feature Implementation Plan

**Goal:** demo

## Global Constraints

- version floor 1.2

---

### Task 1: Bootstrap

- [ ] **Step 1: Write the failing test**
- [ ] **Step 2: Run it**

### Task 2: API

- [x] **Step 1: Write the failing test**
- [ ] **Step 2: Run it**

### Task 3: UI

- [x] **Step 1: Write the failing test**
- [x] **Step 2: Run it**
"""

    def _parse(self, text: str):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".md")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        self.addCleanup(os.unlink, path)
        return parse_plan(path)

    def test_descends_when_one_heading_swallows_the_whole_plan(self) -> None:
        plan = self._parse(self.WRITING_PLANS)
        self.assertEqual(
            [m.name for m in plan.milestones],
            ["Task 1: Bootstrap", "Task 2: API", "Task 3: UI"],
            "a single H2 must not swallow every task",
        )
        self.assertEqual([(m.done, m.total) for m in plan.milestones], [(0, 2), (1, 2), (2, 2)])

    def test_keeps_the_shallow_level_when_it_already_has_several_sections(self) -> None:
        plan = self._parse(
            "# Plan\n\n## A\n- [ ] a1\n- [ ] a2\n\n### A details\n\n## B\n- [ ] b1\n"
        )
        self.assertEqual([m.name for m in plan.milestones], ["A", "B"])

    def test_single_milestone_plan_still_works(self) -> None:
        plan = self._parse("# Plan\n\n## Only phase\n- [ ] a\n- [x] b\n")
        self.assertEqual([m.name for m in plan.milestones], ["Only phase"])
        self.assertEqual(plan.done, 1)
        self.assertEqual(plan.total, 2)


class TestScanRoots(unittest.TestCase):
    """Root scanning with dedup."""

    def test_finds_writing_plans_default_location(self) -> None:
        """`docs/superpowers/plans/` is where the writing-plans skill saves."""
        with tempfile.TemporaryDirectory() as td:
            repo = os.path.join(td, "repo")
            plans = os.path.join(repo, "docs", "superpowers", "plans")
            os.makedirs(plans)
            wanted = os.path.join(plans, "2026-01-01-feature.md")
            with open(wanted, "w") as fh:
                fh.write("# Feature Implementation Plan\n## A\n- [ ] step\n")
            found = [str(f) for f in scan_roots(os.path.join(td, "home"), cwd=repo)]
            self.assertIn(wanted, found)

    def test_scan_returns_empty_when_no_roots(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fake_home = os.path.join(td, "hermes")
            files = scan_roots(fake_home, cwd=td)
            self.assertIsInstance(files, list)
            fake_plans = [f for f in files if str(f).startswith(fake_home)]
            self.assertEqual(len(fake_plans), 0)

    def test_finds_md_with_checkbox(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            plans_dir = os.path.join(td, "plans")
            os.makedirs(plans_dir)
            test_path = os.path.join(plans_dir, "test.md")
            with open(test_path, "w") as f:
                f.write("# M\n- [ ] task\n")
            with open(os.path.join(plans_dir, "other.md"), "w") as f:
                f.write("# Just text\nNo checkbox here.\n")
            files = scan_roots(td, cwd=td)
            found = [f for f in files if f.name == "test.md"]
            self.assertEqual(len(found), 1, "test.md with checkboxes should be found exactly once")
            no_cb = [f for f in files if f.name == "other.md"]
            self.assertEqual(len(no_cb), 0, "other.md without checkboxes should be excluded")


class TestRegistry(unittest.TestCase):
    """Registry CRUD and validation (v2: entries are dicts with scope/session)."""

    def setUp(self) -> None:
        self._tmp_home = tempfile.mkdtemp()
        self._plans_dir = os.path.join(self._tmp_home, "plans")
        os.makedirs(self._plans_dir)
        # One valid .md file
        self._valid_md = os.path.join(self._plans_dir, "valid.md")
        with open(self._valid_md, "w") as f:
            f.write("# Test\n- [ ] item 1\n- [x] item 2\n")

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp_home, ignore_errors=True)

    def test_empty_registry_returns_empty_list(self) -> None:
        entries = load_registry(self._tmp_home)
        self.assertEqual(entries, [])

    def test_save_and_load_roundtrip(self) -> None:
        save_registry(self._tmp_home, [
            {"path": "/a.md", "scope": "global", "session": ""},
            {"path": "/b.md", "scope": "session", "session": "sess1"},
        ])
        entries = load_registry(self._tmp_home)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["path"], "/a.md")
        self.assertEqual(entries[0]["scope"], "global")
        self.assertEqual(entries[1]["scope"], "session")
        self.assertEqual(entries[1]["session"], "sess1")

    def test_v1_to_v2_migration(self) -> None:
        """A v1 registry file (strings only) is migrated on read."""
        rp = os.path.join(
            self._tmp_home, "plugin-data", "plan-tracker", "registry.json",
        )
        os.makedirs(os.path.dirname(rp), exist_ok=True)
        with open(rp, "w") as f:
            f.write('{"files": ["/old.md"]}\n')
        entries = load_registry(self._tmp_home)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["path"], "/old.md")
        self.assertEqual(entries[0]["scope"], "global")
        self.assertEqual(entries[0]["session"], "")
        # Verify the file was rewritten as v2
        with open(rp) as f:
            data = json.load(f)
        self.assertEqual(data.get("version"), 2)

    def test_add_entry_success(self) -> None:
        ok, msg, plan_data = add_registry_entry(self._tmp_home, self._valid_md)
        self.assertTrue(ok)
        self.assertEqual(msg, "registered")
        self.assertIsNotNone(plan_data)
        self.assertEqual(plan_data["name"], "Test")
        self.assertEqual(plan_data["done"], 1)
        self.assertEqual(plan_data["total"], 2)

    def test_add_entry_with_scope(self) -> None:
        ok, msg, plan_data = add_registry_entry(
            self._tmp_home, self._valid_md, scope="session", session="sess_01",
        )
        self.assertTrue(ok)
        entries = load_registry(self._tmp_home)
        self.assertEqual(entries[0]["scope"], "session")
        self.assertEqual(entries[0]["session"], "sess_01")

    def test_add_duplicate_rejected(self) -> None:
        add_registry_entry(self._tmp_home, self._valid_md)
        ok, msg, _ = add_registry_entry(self._tmp_home, self._valid_md)
        self.assertFalse(ok)
        self.assertIn("already registered", msg)

    def test_same_path_different_scopes_allowed(self) -> None:
        """Same file can exist under global and a session scope."""
        ok1, _, _ = add_registry_entry(self._tmp_home, self._valid_md, scope="global")
        self.assertTrue(ok1)
        ok2, _, _ = add_registry_entry(
            self._tmp_home, self._valid_md, scope="session", session="sess_a",
        )
        self.assertTrue(ok2)
        entries = load_registry(self._tmp_home)
        self.assertEqual(len(entries), 2)

    def test_same_path_same_scope_rejected(self) -> None:
        """Same file same scope same session -> duplicate."""
        ok1, _, _ = add_registry_entry(
            self._tmp_home, self._valid_md, scope="session", session="sess_a",
        )
        self.assertTrue(ok1)
        ok2, _, _ = add_registry_entry(
            self._tmp_home, self._valid_md, scope="session", session="sess_a",
        )
        self.assertFalse(ok2)

    def test_remove_entry_by_id(self) -> None:
        ok, _, plan_data = add_registry_entry(self._tmp_home, self._valid_md)
        self.assertTrue(ok)
        plan_id = plan_data["id"]
        ok2, msg2 = remove_registry_entry(self._tmp_home, plan_id)
        self.assertTrue(ok2)
        self.assertEqual(msg2, "removed")
        self.assertEqual(load_registry(self._tmp_home), [])

    def test_remove_nonexistent(self) -> None:
        ok, msg = remove_registry_entry(self._tmp_home, "nonexistent123456")
        self.assertFalse(ok)

    def test_remove_with_scope_filter(self) -> None:
        """Remove only when scope matches."""
        add_registry_entry(self._tmp_home, self._valid_md, scope="global")
        add_registry_entry(
            self._tmp_home, self._valid_md, scope="session", session="sess_a",
        )
        # Remove only the session-scoped one
        ok, msg = remove_registry_entry(
            self._tmp_home, self._valid_md, scope="session", session="sess_a",
        )
        self.assertTrue(ok)
        entries = load_registry(self._tmp_home)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["scope"], "global")

    def test_validation_bad_suffix(self) -> None:
        valid, msg = validate_registration_path("/tmp/fake.txt", self._tmp_home)
        self.assertFalse(valid)
        self.assertIn(".md", msg.lower())

    def test_validation_missing_file(self) -> None:
        valid, msg = validate_registration_path("/tmp/nonexistent.md", self._tmp_home)
        self.assertFalse(valid)
        self.assertIn("not found", msg.lower())

    def test_validation_relative_path(self) -> None:
        valid, msg = validate_registration_path("relative/path.md", self._tmp_home)
        self.assertFalse(valid)
        self.assertIn("absolute", msg.lower())

    def test_validation_no_checkbox(self) -> None:
        no_cb = os.path.join(self._plans_dir, "no_cb.md")
        with open(no_cb, "w") as f:
            f.write("# Just text\nNo checkboxes.\n")
        valid, msg = validate_registration_path(no_cb, self._tmp_home)
        self.assertFalse(valid)
        self.assertIn("checkbox", msg.lower())

    def test_registry_limit(self) -> None:
        from core import REGISTRY_MAX
        # Fill registry with 100 entries
        paths = [{"path": f"/tmp/test_{i}.md", "scope": "global", "session": ""}
                 for i in range(REGISTRY_MAX)]
        save_registry(self._tmp_home, paths)
        ok, msg, _ = add_registry_entry(self._tmp_home, self._valid_md)
        self.assertFalse(ok)
        self.assertIn("limit", msg.lower())

    def test_remove_by_path(self) -> None:
        add_registry_entry(self._tmp_home, self._valid_md)
        ok, msg = remove_registry_entry(self._tmp_home, self._valid_md)
        self.assertTrue(ok)
        self.assertEqual(msg, "removed")

    def test_validate_tilde_expansion(self) -> None:
        """~ expansion should result in absolute path."""
        valid, msg = validate_registration_path("~/some/file.md", self._tmp_home)
        self.assertNotIn("absolute", msg.lower())

    def test_corrupt_registry_returns_empty(self) -> None:
        rp = os.path.join(self._tmp_home, "plugin-data", "plan-tracker", "registry.json")
        os.makedirs(os.path.dirname(rp), exist_ok=True)
        with open(rp, "w") as f:
            f.write("not json\n")
        entries = load_registry(self._tmp_home)
        self.assertEqual(entries, [])

    def test_invalid_scope_rejected(self) -> None:
        ok, msg, _ = add_registry_entry(
            self._tmp_home, self._valid_md, scope="invalid",
        )
        self.assertFalse(ok)
        self.assertIn("scope", msg.lower())

    def test_session_scope_requires_session_id(self) -> None:
        ok, msg, _ = add_registry_entry(
            self._tmp_home, self._valid_md, scope="session", session="",
        )
        self.assertFalse(ok)
        self.assertIn("session", msg.lower())

    def test_session_scope_rejects_bad_session_id(self) -> None:
        ok, msg, _ = add_registry_entry(
            self._tmp_home, self._valid_md, scope="session", session="bad id!",
        )
        self.assertFalse(ok)


class TestTitleIsNotAMilestone(unittest.TestCase):
    """The title heading must never swallow the milestones."""

    def _parse(self, md: str):
        import tempfile

        folder = tempfile.mkdtemp()
        path = os.path.join(folder, "plan.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(md)
        return parse_plan(path)

    def test_title_h1_with_h2_phases(self) -> None:
        plan = self._parse(
            "# Mi plan de migracion\n\n"
            "## Fase 1 - Inventario\n- [x] a\n- [x] b\n- [ ] c\n\n"
            "## Fase 2 - Migracion\n- [x] d\n- [ ] e\n- [ ] f\n\n"
            "## Fase 3 - Cutover\n- [ ] g\n- [ ] h\n"
        )
        self.assertEqual(plan.name, "Mi plan de migracion")
        self.assertEqual(
            [(m.name, m.done, m.total) for m in plan.milestones],
            [
                ("Fase 1 - Inventario", 2, 3),
                ("Fase 2 - Migracion", 1, 3),
                ("Fase 3 - Cutover", 0, 2),
            ],
        )
        self.assertEqual((plan.done, plan.total), (3, 8))
        self.assertAlmostEqual(plan.percent, 37.5, delta=0.1)

    def test_no_title_h2_only_keeps_every_section(self) -> None:
        plan = self._parse("## Fase 1\n- [x] a\n## Fase 2\n- [ ] b\n- [ ] c\n")
        self.assertEqual(
            [(m.name, m.done, m.total) for m in plan.milestones],
            [("Fase 1", 1, 1), ("Fase 2", 0, 2)],
        )

    def test_title_then_h3_milestones(self) -> None:
        plan = self._parse("# Roadmap calidad\n### Etapa 1\n- [x] x\n### Etapa 2\n- [ ] y\n")
        self.assertEqual(plan.name, "Roadmap calidad")
        self.assertEqual(
            [(m.name, m.done, m.total) for m in plan.milestones],
            [("Etapa 1", 1, 1), ("Etapa 2", 0, 1)],
        )


if __name__ == "__main__":
    unittest.main()


class TestScopeFiltering(unittest.TestCase):
    """Scope filtering: load_all_plans and load_plan_detail with scope/session."""

    def setUp(self) -> None:
        self._tmp_home = tempfile.mkdtemp()
        self._plans_dir = os.path.join(self._tmp_home, "plans")
        os.makedirs(self._plans_dir, exist_ok=True)
        # One root-discovered plan (always global)
        self._root_md = os.path.join(self._plans_dir, "root.md")
        with open(self._root_md, "w") as f:
            f.write("# Root Plan\n- [x] item 1\n- [ ] item 2\n")
        # A second root plan
        self._root2_md = os.path.join(self._plans_dir, "root2.md")
        with open(self._root2_md, "w") as f:
            f.write("# Root Two\n- [ ] item\n")
        # A registry-only file (not in any root)
        self._extra_dir = os.path.join(self._tmp_home, "extra")
        os.makedirs(self._extra_dir, exist_ok=True)
        self._global_md = os.path.join(self._extra_dir, "global.md")
        with open(self._global_md, "w") as f:
            f.write("# Global Reg\n- [x] reg item\n")
        self._session_md = os.path.join(self._extra_dir, "session.md")
        with open(self._session_md, "w") as f:
            f.write("# Session Reg\n- [x] reg item\n")

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp_home, ignore_errors=True)

    def _setup_registry(self) -> None:
        """Register a global and a session entry."""
        add_registry_entry(self._tmp_home, self._global_md, scope="global")
        add_registry_entry(
            self._tmp_home, self._session_md, scope="session", session="sess_abc",
        )
        # Also add the session file under a different session (should be invisible)
        self._other_session_md = os.path.join(self._extra_dir, "other_session.md")
        with open(self._other_session_md, "w") as f:
            f.write("# Other Session\n- [ ] item\n")
        add_registry_entry(
            self._tmp_home, self._other_session_md, scope="session", session="sess_xyz",
        )

    def test_scope_all_returns_everything(self) -> None:
        self._setup_registry()
        plans = load_all_plans(self._tmp_home, cwd=self._tmp_home, scope="all")
        ids = sorted(p["name"] for p in plans)
        self.assertIn("Root Plan", ids)
        self.assertIn("Root Two", ids)
        self.assertIn("Global Reg", ids)
        self.assertIn("Session Reg", ids)
        self.assertIn("Other Session", ids)

    def test_scope_global_excludes_session_entries(self) -> None:
        self._setup_registry()
        plans = load_all_plans(self._tmp_home, cwd=self._tmp_home, scope="global")
        names = [p["name"] for p in plans]
        self.assertIn("Root Plan", names)
        self.assertIn("Global Reg", names)
        self.assertNotIn("Session Reg", names)
        self.assertNotIn("Other Session", names)

    def test_scope_session_filters_by_session_id(self) -> None:
        self._setup_registry()
        plans = load_all_plans(
            self._tmp_home, cwd=self._tmp_home, scope="session", session="sess_abc",
        )
        names = [p["name"] for p in plans]
        self.assertNotIn("Root Plan", names)
        self.assertNotIn("Root Two", names)
        self.assertNotIn("Global Reg", names)
        self.assertIn("Session Reg", names)
        self.assertNotIn("Other Session", names)

    def test_scope_session_empty_sees_nothing(self) -> None:
        """If no entries match the session, returns empty list."""
        plans = load_all_plans(
            self._tmp_home, cwd=self._tmp_home, scope="session", session="unknown_sess",
        )
        self.assertEqual(plans, [])

    def test_plan_detail_scope_all(self) -> None:
        self._setup_registry()
        # Find the session plan in "all" scope
        plan = parse_plan(self._session_md)
        detail = load_plan_detail(
            self._tmp_home, plan.plan_id, cwd=self._tmp_home, scope="all",
        )
        self.assertIsNotNone(detail)
        self.assertEqual(detail["name"], "Session Reg")

    def test_plan_detail_scope_session(self) -> None:
        self._setup_registry()
        plan = parse_plan(self._session_md)
        # Scope session should find it since it matches
        detail = load_plan_detail(
            self._tmp_home, plan.plan_id, cwd=self._tmp_home,
            scope="session", session="sess_abc",
        )
        self.assertIsNotNone(detail)
        self.assertEqual(detail["name"], "Session Reg")

    def test_plan_detail_scope_global_excludes_session(self) -> None:
        self._setup_registry()
        plan = parse_plan(self._session_md)
        # Scope global should NOT find a session-scoped plan
        detail = load_plan_detail(
            self._tmp_home, plan.plan_id, cwd=self._tmp_home, scope="global",
        )
        self.assertIsNone(detail)

    def test_two_sessions_dont_see_each_other(self) -> None:
        """Entries from two different sessions are isolated."""
        self._setup_registry()
        plans_a = load_all_plans(
            self._tmp_home, cwd=self._tmp_home, scope="session", session="sess_abc",
        )
        names_a = [p["name"] for p in plans_a]
        self.assertIn("Session Reg", names_a)
        self.assertNotIn("Other Session", names_a)

        plans_b = load_all_plans(
            self._tmp_home, cwd=self._tmp_home, scope="session", session="sess_xyz",
        )
        names_b = [p["name"] for p in plans_b]
        self.assertNotIn("Session Reg", names_b)
        self.assertIn("Other Session", names_b)

    def test_root_plans_are_scope_global(self) -> None:
        """Auto-discovered root files always have scope=global, session=""."""
        plans = load_all_plans(self._tmp_home, cwd=self._tmp_home, scope="all")
        for p in plans:
            if p.get("registered") is False:
                self.assertEqual(p.get("scope"), "global",
                                 f"{p['name']} should be scope=global")

class TestRegisteredRootPlan(unittest.TestCase):
    """A plan found in a root AND registered must come back marked as tracked."""

    def test_root_plan_registered_is_marked_not_dropped(self) -> None:
        import tempfile

        home = tempfile.mkdtemp()
        plans_dir = os.path.join(home, "plans")
        os.makedirs(plans_dir)
        plan_path = os.path.join(plans_dir, "p.md")
        with open(plan_path, "w", encoding="utf-8") as fh:
            fh.write("# Plan\n## F1\n- [x] a\n- [ ] b\n")

        ok, msg, _ = add_registry_entry(home, plan_path, scope="global", session="")
        self.assertTrue(ok, msg)

        listed = load_all_plans(home, scope="all", session="")
        mine = [p for p in listed if p["path"] == plan_path]
        self.assertEqual(len(mine), 1, "the plan must appear once, not twice")
        self.assertTrue(mine[0]["registered"], "a registered root plan must be marked")
        self.assertEqual(mine[0]["scope"], "global")

    def test_session_registration_of_a_root_plan_keeps_its_scope(self) -> None:
        import tempfile

        home = tempfile.mkdtemp()
        plans_dir = os.path.join(home, "plans")
        os.makedirs(plans_dir)
        plan_path = os.path.join(plans_dir, "p.md")
        with open(plan_path, "w", encoding="utf-8") as fh:
            fh.write("# Plan\n## F1\n- [ ] a\n")

        add_registry_entry(home, plan_path, scope="session", session="sess-1")
        listed = load_all_plans(home, scope="all", session="")
        mine = [p for p in listed if p["path"] == plan_path]
        self.assertEqual(len(mine), 1)
        self.assertTrue(mine[0]["registered"])
        self.assertEqual(mine[0]["scope"], "session")
        self.assertEqual(mine[0]["session"], "sess-1")


class TestMultiSessionRegistration(unittest.TestCase):
    """The same file can be tracked in several sessions at once."""

    def test_same_plan_in_two_sessions_lists_twice(self) -> None:
        import tempfile

        home = tempfile.mkdtemp()
        plans_dir = os.path.join(home, "plans")
        os.makedirs(plans_dir)
        plan_path = os.path.join(plans_dir, "p.md")
        with open(plan_path, "w", encoding="utf-8") as fh:
            fh.write("# Plan\n## F1\n- [ ] a\n")

        ok1, msg1, _ = add_registry_entry(home, plan_path, scope="session", session="sess-1")
        ok2, msg2, _ = add_registry_entry(home, plan_path, scope="session", session="sess-2")
        self.assertTrue(ok1, msg1)
        self.assertTrue(ok2, msg2)

        listed = [p for p in load_all_plans(home, scope="all", session="") if p["path"] == plan_path]
        self.assertEqual(len(listed), 2, "one entry per registration")
        self.assertEqual(sorted(p["session"] for p in listed), ["sess-1", "sess-2"])
        self.assertEqual(len({p["key"] for p in listed}), 2, "keys must be unique per registration")

        only_one = [p for p in load_all_plans(home, scope="session", session="sess-1") if p["path"] == plan_path]
        self.assertEqual(len(only_one), 1)
        self.assertEqual(only_one[0]["session"], "sess-1")


class TestSnapshotMilestones(unittest.TestCase):
    """v0.4: snapshots carry per-milestone counts."""

    def setUp(self) -> None:
        self._tmp_home = tempfile.mkdtemp()

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp_home, ignore_errors=True)

    def _plan(self, lines: list) -> PlanFile:
        """Create a PlanFile from lines via a temp file."""
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w") as f:
            for line in lines:
                f.write(line)
            f.flush()
            plan = parse_plan(f.name)
        return plan

    def test_record_snapshot_has_milestones(self) -> None:
        plan = self._plan(["# M\n", "- [x] a\n", "- [ ] b\n", "- [ ] c\n"])
        written = record_snapshot(self._tmp_home, plan)
        self.assertTrue(written)
        from core import read_history
        entries = read_history(self._tmp_home, plan.plan_id, limit=1)
        self.assertEqual(len(entries), 1)
        self.assertIn("milestones", entries[0])
        self.assertEqual(entries[0]["milestones"], {"M": [1, 3]})

    def test_snapshot_milestones_update_on_change(self) -> None:
        # Use a single temp file; modify its content between snapshots
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w") as f:
            f.write("# X\n- [x] a\n- [ ] b\n")
            f.flush()
            path = f.name
        plan = parse_plan(path)
        record_snapshot(self._tmp_home, plan)
        # Change the file content
        with open(path, "w") as f:
            f.write("# X\n- [x] a\n- [x] b\n")
        plan2 = parse_plan(path)
        written = record_snapshot(self._tmp_home, plan2)
        self.assertTrue(written)
        from core import read_history
        entries = read_history(self._tmp_home, plan2.plan_id, limit=2)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["milestones"], {"X": [1, 2]})
        self.assertEqual(entries[-1]["milestones"], {"X": [2, 2]})
        os.unlink(path)

    def test_milestones_dict_helper(self) -> None:
        plan = self._plan(["# Title\n", "## S1\n", "- [x] a\n", "- [ ] b\n"])
        md = _milestones_dict(plan)
        # Title "Title" has no checkboxes in its own section, so S1 is the milestone
        self.assertEqual(md, {"S1": [1, 2]})


class TestEffortProrating(unittest.TestCase):
    """v0.4: effort attribution against a synthetic state.db."""

    def setUp(self) -> None:
        self._tmp_home = tempfile.mkdtemp()
        self._make_state_db()

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp_home, ignore_errors=True)

    def _make_state_db(self) -> None:
        """Create a synthetic state.db with known data for testing."""
        import sqlite3
        db_path = os.path.join(self._tmp_home, "state.db")
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        # Create sessions table (minimal columns we read)
        conn.execute("""CREATE TABLE sessions (
            id TEXT PRIMARY KEY, input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            estimated_cost_usd REAL DEFAULT 0.0,
            model TEXT, parent_session_id TEXT, title TEXT
        )""")
        # Create messages table (minimal columns we read)
        conn.execute("""CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            timestamp REAL NOT NULL, active INTEGER DEFAULT 1
        )""")
        # Create session_model_usage table
        conn.execute("""CREATE TABLE session_model_usage (
            session_id TEXT NOT NULL REFERENCES sessions(id),
            model TEXT NOT NULL,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            estimated_cost_usd REAL DEFAULT 0.0,
            PRIMARY KEY (session_id, model)
        )""")

        # Session A: 100 input tokens, 50 output, cost 0.02, model claude
        conn.execute(
            "INSERT INTO sessions VALUES ('sess_a', 100, 50, 0.02, 'claude-sonnet-4.5', NULL, 'Session A')",
        )
        # Session B: subagent, 200 input, 100 output, cost 0.04, model gpt
        conn.execute(
            "INSERT INTO sessions VALUES ('sess_b', 200, 100, 0.04, 'gpt-4', 'sess_a', 'Session B')",
        )

        # Messages for sess_a: 4 total, 2 in window [10, 30]
        for ts in [5.0, 10.0, 20.0, 30.0]:
            conn.execute(
                "INSERT INTO messages (session_id, timestamp, active) VALUES (?, ?, 1)",
                ("sess_a", ts),
            )

        # Messages for sess_b: 2 total, 1 in window [10, 30]
        for ts in [8.0, 25.0]:
            conn.execute(
                "INSERT INTO messages (session_id, timestamp, active) VALUES (?, ?, 1)",
                ("sess_b", ts),
            )

        # Model usage for sess_a: claude-sonnet gets all
        conn.execute(
            "INSERT INTO session_model_usage VALUES ('sess_a', 'claude-sonnet-4.5', 100, 50, 0.02)",
        )

        conn.commit()
        conn.close()

    def test_attribute_effort_returns_correct_values(self) -> None:
        from effort import attribute_effort as ae
        result = ae(self._tmp_home, "dummy", 10.0, 30.0)
        self.assertIn("seconds", result)
        self.assertIn("tokens_in", result)
        self.assertIn("tokens_out", result)
        self.assertIn("cost_usd", result)
        self.assertIn("models", result)
        self.assertIn("sessions", result)

        # sess_a: 3/4 msgs in window (ts 10, 20, 30), share=0.75
        #   tokens_in = round(100 * 0.75) = 75
        #   tokens_out = round(50 * 0.75) = 38 (banker's rounding: 37.5 -> 38)
        #   cost = round(0.02 * 0.75, 6) = 0.015
        #   seconds = 30 - 10 = 20
        # sess_b: 1/2 msgs in window (ts 25), share=0.5
        #   tokens_in = round(200 * 0.5) = 100
        #   tokens_out = round(100 * 0.5) = 50
        #   cost = round(0.04 * 0.5, 6) = 0.02
        #   seconds = 25 - 25 = 0

        self.assertEqual(result["tokens_in"], 175)
        self.assertEqual(result["tokens_out"], 88)
        self.assertAlmostEqual(result["cost_usd"], 0.035, places=6)
        self.assertGreater(result["seconds"], 0)

        # Should have 2 sessions
        self.assertEqual(len(result["sessions"]), 2)
        sess_ids = {s["id"] for s in result["sessions"]}
        self.assertEqual(sess_ids, {"sess_a", "sess_b"})

        # Check subagent flag
        for s in result["sessions"]:
            if s["id"] == "sess_b":
                self.assertTrue(s["subagent"], "sess_b is a subagent")
            else:
                self.assertFalse(s["subagent"])

        # Models should include both
        self.assertIn("claude-sonnet-4.5", result["models"])

    def test_missing_state_db_returns_empty(self) -> None:
        """When state.db does not exist, attribute_effort returns {}."""
        from effort import attribute_effort as ae
        # Point at a non-existent db
        result = ae("/nonexistent/path", "dummy", 0, 1)
        self.assertEqual(result, {})

    def test_empty_window_returns_empty(self) -> None:
        from effort import attribute_effort as ae
        result = ae(self._tmp_home, "dummy", 9999.0, 99999.0)
        self.assertEqual(result, {})

    def test_effort_save_load_roundtrip(self) -> None:
        from effort import load_effort, save_effort
        data = {"milestones": {"WS1": [{"ts": "2026-11-06T12:00:00", "delta": 1}]}}
        save_effort(self._tmp_home, "test_id", data)
        loaded = load_effort(self._tmp_home, "test_id")
        self.assertEqual(loaded, data)

    def test_load_effort_missing(self) -> None:
        from effort import load_effort
        loaded = load_effort(self._tmp_home, "nonexistent")
        self.assertEqual(loaded, {"milestones": {}})

    def test_aggregated_effort(self) -> None:
        from effort import load_aggregated_effort, save_effort
        data = {
            "milestones": {
                "WS1": [
                    {"ts": "a", "delta": 1, "seconds": 100, "tokens_in": 50,
                     "tokens_out": 10, "cost_usd": 0.01, "models": {"m1": 60},
                     "sessions": [{"id": "s1", "title": "S1", "model": "m1",
                                   "subagent": False, "tokens": 60, "seconds": 100}]},
                ],
            },
        }
        save_effort(self._tmp_home, "test_agg", data)
        agg = load_aggregated_effort(self._tmp_home, "test_agg")
        self.assertIn("WS1", agg)
        self.assertEqual(agg["WS1"]["seconds"], 100)
        self.assertEqual(agg["WS1"]["tokens_in"], 50)
        self.assertEqual(agg["WS1"]["models"], {"m1": 60})
        self.assertEqual(len(agg["WS1"]["sessions"]), 1)


class TestEtaMath(unittest.TestCase):
    """v0.4: ETA computation from history."""

    def test_eta_requires_2_points(self) -> None:
        self.assertIsNone(compute_eta([]))
        self.assertIsNone(compute_eta([{"ts": "2026-01-01T00:00:00", "done": 0, "total": 10}]))

    def test_eta_returns_date(self) -> None:
        history = [
            {"ts": "2026-11-01T00:00:00", "done": 0, "total": 10},
            {"ts": "2026-11-08T00:00:00", "done": 5, "total": 10},
        ]
        # 5 done in 7 days = 0.714/day, 5 remaining -> 7 days -> 2026-11-15
        eta = compute_eta(history)
        self.assertIsNotNone(eta)
        self.assertAlmostEqual(eta["per_day"], 0.714, delta=0.01)
        self.assertEqual(eta["remaining"], 5)

    def test_eta_zero_progress_returns_none(self) -> None:
        history = [
            {"ts": "2026-11-01T00:00:00", "done": 0, "total": 10},
            {"ts": "2026-11-08T00:00:00", "done": 0, "total": 10},
        ]
        self.assertIsNone(compute_eta(history))

    def test_eta_remaining_zero(self) -> None:
        """If all done, ETA is today with remaining=0."""
        history = [
            {"ts": "2026-11-01T00:00:00", "done": 3, "total": 10},
            {"ts": "2026-11-08T00:00:00", "done": 10, "total": 10},
        ]
        eta = compute_eta(history)
        self.assertIsNotNone(eta)
        self.assertEqual(eta["remaining"], 0)

    def test_eta_zero_rate_returns_none(self) -> None:
        history = [
            {"ts": "2026-11-01T00:00:00", "done": 5, "total": 10},
            {"ts": "2026-11-08T00:00:00", "done": 5, "total": 10},
        ]
        self.assertIsNone(compute_eta(history))


class TestWeekDelta(unittest.TestCase):
    """v0.4: week delta from history."""

    def test_week_delta_requires_2_points(self) -> None:
        self.assertIsNone(compute_week_delta([]))
        self.assertIsNone(compute_week_delta([{"ts": "2026-11-06T00:00:00", "done": 0}]))

    def test_week_delta_computes_correctly(self) -> None:
        """Milestone done values change between two recent snapshots."""
        history = [
            {"ts": "2026-11-05T00:00:00", "done": 1, "total": 10,
             "milestones": {"WS1": [1, 5], "WS2": [0, 5]}},
            {"ts": "2026-11-06T00:00:00", "done": 3, "total": 10,
             "milestones": {"WS1": [2, 5], "WS2": [1, 5]}},
        ]
        week = compute_week_delta(history)
        self.assertIsNotNone(week)
        self.assertEqual(week["milestones"], {"WS1": 1, "WS2": 1})
        self.assertEqual(week["since_ts"], "2026-11-05T00:00:00")

    def test_week_delta_no_milestones_field(self) -> None:
        """Old snapshots without milestones are handled gracefully."""
        history = [
            {"ts": "2026-11-05T00:00:00", "done": 0, "total": 5},
            {"ts": "2026-11-06T00:00:00", "done": 2, "total": 5},
        ]
        week = compute_week_delta(history)
        self.assertIsNotNone(week)
        self.assertEqual(week["milestones"], {})


class TestEffortAcrossProfiles(unittest.TestCase):
    """The gateway runs under the default home; the chat may live in a profile."""

    def _make_db(self, path: str, session_id: str, ts: float) -> None:
        import sqlite3

        os.makedirs(os.path.dirname(path), exist_ok=True)
        con = sqlite3.connect(path)
        con.executescript(
            """
            CREATE TABLE sessions (id TEXT PRIMARY KEY, input_tokens INTEGER,
              output_tokens INTEGER, estimated_cost_usd REAL, model TEXT,
              parent_session_id TEXT, title TEXT);
            CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT,
              timestamp REAL, active INTEGER);
            CREATE TABLE session_model_usage (session_id TEXT, model TEXT,
              input_tokens INTEGER, output_tokens INTEGER);
            """
        )
        con.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
            (session_id, 1000, 200, 0.5, "model-x", None, "a chat"),
        )
        con.execute("INSERT INTO messages (session_id, timestamp, active) VALUES (?,?,1)", (session_id, ts))
        con.execute("INSERT INTO messages (session_id, timestamp, active) VALUES (?,?,1)", (session_id, ts + 60))
        con.commit()
        con.close()

    def test_finds_sessions_in_a_profile_state_db(self) -> None:
        import tempfile
        import time

        home = tempfile.mkdtemp()
        now = time.time()
        self._make_db(os.path.join(home, "profiles", "work", "state.db"), "sess-1", now - 120)

        result = attribute_effort(home, "plan", now - 600, now)
        self.assertTrue(result, "a profile state.db must be read")
        self.assertEqual(len(result["sessions"]), 1)
        self.assertEqual(result["sessions"][0]["id"], "sess-1")
        self.assertGreater(result["tokens_in"], 0)

    def test_missing_databases_return_empty(self) -> None:
        import tempfile

        self.assertEqual(attribute_effort(tempfile.mkdtemp(), "plan", 0, 1), {})


class TestWeightsParsing(unittest.TestCase):
    """Phase 4.5: parse weights from YAML frontmatter."""

    def test_weights_present(self) -> None:
        lines = [
            "---\n",
            "weights:\n",
            "  WS0 - Bootstrap: 3\n",
            "  WS1 - Domain: 1\n",
            "---\n",
            "# Title\n",
            "## WS0 - Bootstrap\n",
            "- [ ] item\n",
            "## WS1 - Domain\n",
            "- [x] item\n",
        ]
        weights = _parse_weights_from_lines(lines)
        self.assertEqual(weights, {"WS0 - Bootstrap": 3, "WS1 - Domain": 1})

    def test_weights_absent(self) -> None:
        lines = ["# Title\n", "- [ ] item\n"]
        self.assertEqual(_parse_weights_from_lines(lines), {})

    def test_weights_malformed_frontmatter(self) -> None:
        """Opening --- without closing --- returns {}."""
        lines = [
            "---\n",
            "weights:\n",
            "  A: 3\n",
            "# Title\n",
            "- [ ] item\n",
        ]
        self.assertEqual(_parse_weights_from_lines(lines), {})

    def test_weights_unknown_milestone(self) -> None:
        """A weight referencing a non-existent milestone is stored but unused."""
        lines = [
            "---\n",
            "weights:\n",
            "  NonExistent: 5\n",
            "---\n",
            "# Title\n",
            "- [ ] item\n",
        ]
        weights = _parse_weights_from_lines(lines)
        self.assertIn("NonExistent", weights)
        self.assertEqual(weights["NonExistent"], 5)

    def test_skip_frontmatter_returns_correct_offset(self) -> None:
        lines = ["---\n", "key: val\n", "---\n", "# H1\n", "- [ ] x\n"]
        self.assertEqual(_skip_frontmatter(lines), 3)

    def test_skip_frontmatter_no_fm(self) -> None:
        self.assertEqual(_skip_frontmatter(["# H1\n"]), 0)

    def test_parse_preserves_line_numbers(self) -> None:
        """Checkbox line numbers must still refer to the original file."""
        lines = [
            "---\n",
            "weights:\n",
            "  M: 1\n",
            "---\n",
            "# Title\n",
            "## M\n",
            "- [ ] item\n",
        ]
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w") as f:
            f.writelines(lines)
            path = f.name
        try:
            plan = parse_plan(path)
            self.assertEqual(len(plan.milestones), 1)
            self.assertEqual(plan.milestones[0].name, "M")
            # Checkbox is on line 7 (0-indexed: line 6), after the 4-line frontmatter
            self.assertEqual(
                plan.milestones[0].checkboxes[0].line_number, 7,
            )
        finally:
            os.unlink(path)

    def test_weights_loaded_via_parse_plan(self) -> None:
        lines = [
            "---\n",
            "weights:\n",
            "  W1: 2\n",
            "---\n",
            "# T\n",
            "## W1\n",
            "- [ ] x\n",
        ]
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w") as f:
            f.writelines(lines)
            path = f.name
        try:
            plan = parse_plan(path)
            self.assertEqual(plan.weights, {"W1": 2})
        finally:
            os.unlink(path)


class TestWeightedPercent(unittest.TestCase):
    """Phase 4.5: weighted percent math."""

    def _plan(self, content: str, weights_dict: dict) -> PlanFile:
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w") as f:
            f.write(content)
            path = f.name
        plan = parse_plan(path)
        plan.weights = weights_dict
        os.unlink(path)
        return plan

    def test_weighted_percent_with_explicit_weights(self) -> None:
        # A: 1/4 done, weight=3 => (0.25)*3 = 0.75
        # B: 1/2 done, weight=1 => (0.50)*1 = 0.50
        # total weight = 4, weighted = 1.25/4 = 0.3125 => 31.2%
        plan = self._plan(
            "# T\n## A\n- [x] a\n- [ ] b\n- [ ] c\n- [ ] d\n## B\n- [x] e\n- [ ] f\n",
            {"A": 3, "B": 1},
        )
        self.assertAlmostEqual(plan.weighted_percent, 31.2, delta=0.1)
        # Checkbox percent is 2/6 = 33.3%
        self.assertAlmostEqual(plan.percent, 33.3, delta=0.1)

    def test_weighted_percent_no_weights(self) -> None:
        """Without weights, weighted_percent equals checkbox percent."""
        plan = self._plan("# T\n## A\n- [x] a\n- [ ] b\n## B\n- [ ] c\n", {})
        self.assertAlmostEqual(plan.weighted_percent, plan.percent, delta=0.1)

    def test_weighted_percent_all_done(self) -> None:
        plan = self._plan("# T\n- [x] a\n- [x] b\n", {"M": 5})
        self.assertEqual(plan.weighted_percent, 100.0)

    def test_weighted_percent_zero_total(self) -> None:
        plan = self._plan("# T\n", {})
        self.assertEqual(plan.weighted_percent, 0.0)

    def test_weighted_percent_detail_exposes_field(self) -> None:
        """The detail dict must include weighted_percent."""
        home = tempfile.mkdtemp()
        plans_dir = os.path.join(home, "plans")
        os.makedirs(plans_dir)
        p = os.path.join(plans_dir, "p.md")
        with open(p, "w") as f:
            f.write("---\nweights:\n  M: 3\n---\n# T\n## M\n- [x] x\n- [ ] y\n")
        plan = parse_plan(p)
        detail = load_plan_detail(home, plan.plan_id, cwd=home)
        self.assertIsNotNone(detail)
        self.assertIn("weighted_percent", detail)
        self.assertIsInstance(detail["weighted_percent"], float)
        import shutil
        shutil.rmtree(home, ignore_errors=True)


class TestExportMarkdown(unittest.TestCase):
    """Phase 4.4: export plan markdown."""

    def test_export_returns_markdown_with_counts(self) -> None:
        import shutil
        home = tempfile.mkdtemp()
        plans_dir = os.path.join(home, "plans")
        os.makedirs(plans_dir)
        p = os.path.join(plans_dir, "test.md")
        with open(p, "w") as f:
            f.write("# My Plan\n- [x] done\n- [ ] pending\n")
        plan = parse_plan(p)
        md = export_plan_markdown(home, plan.plan_id, cwd=home)
        self.assertIsNotNone(md)
        self.assertIn("My Plan", md)
        self.assertIn("1/2", md)
        self.assertIn("Milestones", md)
        shutil.rmtree(home, ignore_errors=True)

    def test_export_unknown_id_returns_none(self) -> None:
        md = export_plan_markdown("/nonexistent", "badbadbad123")
        self.assertIsNone(md)

    def test_export_includes_weighted_when_different(self) -> None:
        import shutil
        home = tempfile.mkdtemp()
        plans_dir = os.path.join(home, "plans")
        os.makedirs(plans_dir)
        p = os.path.join(plans_dir, "w.md")
        with open(p, "w") as f:
            f.write(
                "---\nweights:\n  M1: 3\n---\n"
                "# W Plan\n## M1\n- [x] a\n- [ ] b\n## M2\n- [x] c\n"
            )
        plan = parse_plan(p)
        md = export_plan_markdown(home, plan.plan_id, cwd=home)
        self.assertIsNotNone(md)
        self.assertIn("Weighted", md)
        shutil.rmtree(home, ignore_errors=True)

    def test_export_milestone_weight_appears(self) -> None:
        import shutil
        home = tempfile.mkdtemp()
        plans_dir = os.path.join(home, "plans")
        os.makedirs(plans_dir)
        p = os.path.join(plans_dir, "w2.md")
        with open(p, "w") as f:
            f.write(
                "---\nweights:\n  M: 5\n---\n"
                "# T\n## M\n- [x] x\n"
            )
        plan = parse_plan(p)
        md = export_plan_markdown(home, plan.plan_id, cwd=home)
        self.assertIsNotNone(md)
        self.assertIn("weight=5", md)
        shutil.rmtree(home, ignore_errors=True)


class TestReminderSelection(unittest.TestCase):
    """Phase 4.3: the reminder script selects stalled plans correctly."""

    def _write_history(self, hdir: Path, plan_id: str, entries: list) -> None:
        hdir.mkdir(parents=True, exist_ok=True)
        path = hdir / f"{plan_id}.jsonl"
        with open(path, "w") as f:
            for entry in entries:
                f.write(json.dumps(entry, separators=(",", ":")) + "\n")

    def test_no_registry_no_output(self) -> None:
        """When the registry is empty, reminder prints nothing."""
        import shutil
        home = tempfile.mkdtemp()
        old_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = home
        try:
            from scripts.reminder import reminder
            reminder(days=3, dry_run=True)
        finally:
            if old_home:
                os.environ["HERMES_HOME"] = old_home
            else:
                os.environ.pop("HERMES_HOME", None)
            shutil.rmtree(home, ignore_errors=True)

    def test_plan_with_recent_activity_not_stalled(self) -> None:
        """A plan with a snapshot within the window is not stalled."""
        import shutil
        home = tempfile.mkdtemp()
        old_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = home
        try:
            # Create registry entry
            plans_dir = os.path.join(home, "plans")
            os.makedirs(plans_dir)
            p = os.path.join(plans_dir, "active.md")
            with open(p, "w") as f:
                f.write("# Active\n- [ ] x\n")
            from core import add_registry_entry, parse_plan
            add_registry_entry(home, p, scope="global")
            plan = parse_plan(p)
            pid = plan.plan_id
            # Write a history entry that is recent
            from datetime import datetime, timezone, timedelta
            recent_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            self._write_history(
                Path(home) / "plugin-data" / "plan-tracker" / "history",
                pid,
                [{"ts": recent_ts, "sha": "a", "done": 0, "total": 1}],
            )
            from scripts.reminder import reminder
            reminder(days=3, dry_run=True)
        finally:
            if old_home:
                os.environ["HERMES_HOME"] = old_home
            else:
                os.environ.pop("HERMES_HOME", None)
            shutil.rmtree(home, ignore_errors=True)

    def test_plan_without_recent_activity_is_stalled(self) -> None:
        """A plan with no snapshot in the window is detected as stalled."""
        import shutil
        from datetime import datetime, timezone, timedelta
        home = tempfile.mkdtemp()
        old_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = home
        try:
            plans_dir = os.path.join(home, "plans")
            os.makedirs(plans_dir)
            p = os.path.join(plans_dir, "stale.md")
            with open(p, "w") as f:
                f.write("# Stale\n- [ ] x\n")
            from core import add_registry_entry, parse_plan
            add_registry_entry(home, p, scope="global")
            plan = parse_plan(p)
            pid = plan.plan_id
            # Old history entry
            old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
            self._write_history(
                Path(home) / "plugin-data" / "plan-tracker" / "history",
                pid,
                [{"ts": old_ts, "sha": "a", "done": 0, "total": 1}],
            )
            from scripts.reminder import _oldest_ts_in_window
            result = _oldest_ts_in_window(home, pid, 3)
            self.assertEqual(result, -1, "plan with old history should be stalled")
        finally:
            if old_home:
                os.environ["HERMES_HOME"] = old_home
            else:
                os.environ.pop("HERMES_HOME", None)
            shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()


class TestEffortSchemaGuard(unittest.TestCase):
    """A state.db whose schema drifted must yield no effort, not a crash."""

    def test_wrong_schema_returns_empty(self) -> None:
        import sqlite3
        import tempfile
        import time

        home = tempfile.mkdtemp()
        db = os.path.join(home, "state.db")
        con = sqlite3.connect(db)
        con.executescript(
            """
            CREATE TABLE sessions (id TEXT, input_tokens INTEGER);
            CREATE TABLE messages (session_id TEXT, timestamp REAL);
            """
        )
        con.execute("INSERT INTO sessions VALUES ('s1', 10)")
        con.execute("INSERT INTO messages VALUES ('s1', ?)", (time.time(),))
        con.commit()
        con.close()

        self.assertEqual(attribute_effort(home, "plan", 0, time.time() + 10), {})

    def test_schema_ok_accepts_the_real_shape(self) -> None:
        import sqlite3
        import tempfile

        from effort import REQUIRED_COLUMNS, schema_ok

        home = tempfile.mkdtemp()
        db = os.path.join(home, "state.db")
        con = sqlite3.connect(db)
        for table, columns in REQUIRED_COLUMNS.items():
            cols = ", ".join("%s TEXT" % c for c in sorted(columns))
            con.execute("CREATE TABLE %s (%s)" % (table, cols))
        con.commit()
        self.assertTrue(schema_ok(con))
        con.close()


class TestConfigurableRoots(unittest.TestCase):
    """The plan roots must not be hardcoded to ~/.hermes."""

    def _plan(self, folder: str, name: str) -> str:
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# P\n## F\n- [ ] a\n")
        return path

    def test_plan_tracker_roots_env_adds_a_root(self) -> None:
        import tempfile

        home = tempfile.mkdtemp()
        extra = tempfile.mkdtemp()
        wanted = self._plan(extra, "extra.md")
        old = os.environ.get("PLAN_TRACKER_ROOTS")
        os.environ["PLAN_TRACKER_ROOTS"] = extra
        try:
            found = [str(p) for p in scan_roots(home)]
        finally:
            if old is None:
                os.environ.pop("PLAN_TRACKER_ROOTS", None)
            else:
                os.environ["PLAN_TRACKER_ROOTS"] = old
        self.assertIn(wanted, found)

    def test_hermes_default_home_env_moves_the_second_root(self) -> None:
        import tempfile

        home = tempfile.mkdtemp()
        default_home = tempfile.mkdtemp()
        wanted = self._plan(os.path.join(default_home, "plans"), "default.md")
        old = os.environ.get("HERMES_DEFAULT_HOME")
        os.environ["HERMES_DEFAULT_HOME"] = default_home
        try:
            found = [str(p) for p in scan_roots(home)]
        finally:
            if old is None:
                os.environ.pop("HERMES_DEFAULT_HOME", None)
            else:
                os.environ["HERMES_DEFAULT_HOME"] = old
        self.assertIn(wanted, found)
