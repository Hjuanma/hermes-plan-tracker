"""plan-tracker core engine: parse plans, compute progress, manage history.

Pure Python stdlib: no FastAPI, no network, no subprocesses.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+)$")
_CB_RE = re.compile(r"^\s*[-*]\s+\[([ xX])\]", re.MULTILINE)
_FENCE_RE = re.compile(r"^```")
_FM_RE = re.compile(r"^---\s*$")

REGISTRY_MAX = 100
REGISTRY_VERSION = 2

# --- frontmatter helpers ---


def _skip_frontmatter(lines: List[str]) -> int:
    """Number of leading lines consumed by YAML frontmatter (0 when none)."""
    if not lines or not _FM_RE.match(lines[0].rstrip()):
        return 0
    for i in range(1, len(lines)):
        if _FM_RE.match(lines[i].rstrip()):
            return i + 1
    return 0


def _parse_weights_from_lines(lines: List[str]) -> Dict[str, int]:
    """Parse milestone weights from YAML frontmatter in *lines*.

    Returns ``{milestone_name: weight}`` or ``{}`` when no frontmatter or
    no weights block.
    """
    if not lines or not _FM_RE.match(lines[0].rstrip()):
        return {}
    end = -1
    for i in range(1, len(lines)):
        if _FM_RE.match(lines[i].rstrip()):
            end = i
            break
    if end < 0:
        return {}
    weights: Dict[str, int] = {}
    in_weights = False
    for raw in lines[1:end]:
        line = raw.rstrip()
        stripped = line.strip()
        if stripped == "weights:":
            in_weights = True
            continue
        if in_weights:
            if not stripped:
                continue
            if line[0] in (" ", "\t"):
                m = re.match(r"^\s+(.+?)\s*:\s*(\d+)\s*$", line)
                if m:
                    weights[m.group(1).strip()] = int(m.group(2))
            else:
                in_weights = False
    return weights


# --- data objects ---


class Checkbox:
    """One checkbox line with its checked state."""

    __slots__ = ("checked", "line_number")

    def __init__(self, checked: bool, line_number: int) -> None:
        self.checked = checked
        self.line_number = line_number


class TaskHeader:
    """A task-level heading under a milestone."""

    __slots__ = ("name", "line_number", "checkboxes")

    def __init__(self, name: str, line_number: int) -> None:
        self.name = name
        self.line_number = line_number
        self.checkboxes: List[Checkbox] = []

    @property
    def done(self) -> int:
        return sum(1 for cb in self.checkboxes if cb.checked)

    @property
    def total(self) -> int:
        return len(self.checkboxes)


class Milestone:
    """One section (milestone / workstream) with its checkboxes and tasks."""

    __slots__ = ("name", "line_number", "tasks", "checkboxes")

    def __init__(self, name: str, line_number: int) -> None:
        self.name = name
        self.line_number = line_number
        self.tasks: List[TaskHeader] = []
        self.checkboxes: List[Checkbox] = []

    @property
    def done(self) -> int:
        return sum(1 for cb in self.checkboxes if cb.checked)

    @property
    def total(self) -> int:
        return len(self.checkboxes)

    @property
    def percent(self) -> float:
        if self.total == 0:
            return 0.0
        return round(self.done / self.total * 100, 1)

    @property
    def status(self) -> str:
        """One of 'done', 'active', 'pending'."""
        if self.total == 0:
            return "pending"
        if self.done == self.total:
            return "done"
        if self.done > 0:
            return "active"
        return "pending"


class PlanFile:
    """Parsed representation of one markdown plan file."""

    __slots__ = ("path", "milestones", "title", "weights")

    def __init__(self, path: str) -> None:
        self.path = path
        self.milestones: List[Milestone] = []
        self.title = ""
        self.weights: Dict[str, int] = {}

    @property
    def name(self) -> str:
        """Plan name = the document title (first heading), else the file stem."""
        return self.title or Path(self.path).stem

    @property
    def done(self) -> int:
        return sum(m.done for m in self.milestones)

    @property
    def total(self) -> int:
        return sum(m.total for m in self.milestones)

    @property
    def percent(self) -> float:
        if self.total == 0:
            return 0.0
        return round(self.done / self.total * 100, 1)

    @property
    def weighted_percent(self) -> float:
        """Weighted progress: sum(done/total * weight) / sum(weight) * 100.

        Milestones without an explicit weight use their checkbox count.
        """
        total_weight = 0
        weighted_progress = 0.0
        for ms in self.milestones:
            w = self.weights.get(ms.name, ms.total)
            if ms.total > 0:
                weighted_progress += (ms.done / ms.total) * w
            total_weight += w
        if total_weight == 0:
            return 0.0
        return round(weighted_progress / total_weight * 100, 1)

    @property
    def plan_id(self) -> str:
        """First 12 hex chars of sha256(resolved path)."""
        rp = str(Path(self.path).resolve())
        return hashlib.sha256(rp.encode("utf-8")).hexdigest()[:12]


# --- adaptive parser ---


def _first_heading(lines: List[str]) -> str:
    """First heading (level 1..4) outside a fenced block; empty string when none.

    Skips YAML frontmatter (--- delimited block at line 0).
    """
    in_code = False
    skip = _skip_frontmatter(lines)
    for i, raw in enumerate(lines):
        if i < skip:
            continue
        line = raw.rstrip("\n")
        if _FENCE_RE.match(line):
            in_code = not in_code
            continue
        if in_code:
            continue
        m = _HEADING_RE.match(line)
        if m:
            return m.group(2).strip()
    return ""


def _title_line(
    headings: List[Tuple[int, str, int]],
    checkboxes: List[Tuple[bool, int]],
) -> int:
    """Line of the document title, or 0 when the file has no title heading.

    The first heading is the title when its own body (up to the next heading of
    any level) holds no checkbox. A title is never a milestone candidate.
    """
    if not headings:
        return 0
    first_ln = min(h[2] for h in headings)
    next_ln = min((h[2] for h in headings if h[2] > first_ln), default=2**63 - 1)
    if any(first_ln <= cb_ln < next_ln for _, cb_ln in checkboxes):
        return 0
    return first_ln


def _sections_with_checkboxes(
    headings: List[Tuple[int, str, int]],
    checkboxes: List[Tuple[bool, int]],
    level: int,
    skip_line: int = 0,
) -> List[Tuple[str, int]]:
    """Return the (name, line) of every level-*level* section holding a checkbox.

    A section at level L starts at a heading of level L and ends at the next
    heading of level <= L (or end of file). The heading at *skip_line* (the
    document title) never counts.
    """
    found: List[Tuple[str, int]] = []
    for lvl, name, h_ln in headings:
        if lvl != level or h_ln == skip_line:
            continue
        next_ln = _section_end(h_ln, headings, level)
        if any(h_ln <= cb_ln < next_ln for _, cb_ln in checkboxes):
            found.append((name, h_ln))
    return found


def _first_deeper_line(
    h_ln: int, headings: List[Tuple[int, str, int]], level: int
) -> int:
    """Return the first heading line below h_ln whose level is deeper than *level*."""
    for lvl, _, line in headings:
        if line > h_ln and lvl > level:
            return line
    return 1 << 30


def _has_own_checkbox(
    h_ln: int,
    level: int,
    headings: List[Tuple[int, str, int]],
    checkboxes: List[Tuple[bool, int]],
) -> bool:
    """True when the section has a checkbox before any deeper heading.

    A container heading (``## Global Constraints`` in a writing-plans plan)
    carries prose only: every checkbox that falls inside it belongs to a deeper
    subsection, so the container is not a milestone of its own.
    """
    stop = _first_deeper_line(h_ln, headings, level)
    return any(h_ln < cb_ln < stop for _, cb_ln in checkboxes)


def _find_milestone_level(
    headings: List[Tuple[int, str, int]],
    checkboxes: List[Tuple[bool, int]],
    skip_line: int = 0,
) -> int:
    """Return the heading level (1..4) that defines the milestones.

    Start at the shallowest level holding a checkbox. If that level has exactly
    ONE such section and it owns no checkbox of its own - a pure container that
    spans the whole document, as in a writing-plans plan - descend to the next
    level that holds checkboxes and try again. A grouping heading that owns a
    checkbox directly keeps its level, so a single-milestone plan still reports
    one milestone.
    """
    level = 0
    for candidate in range(1, 5):
        if _sections_with_checkboxes(headings, checkboxes, candidate, skip_line):
            level = candidate
            break
    if not level:
        return 1

    while level < 4:
        sections = _sections_with_checkboxes(headings, checkboxes, level, skip_line)
        if len(sections) != 1:
            break
        _, h_ln = sections[0]
        if _has_own_checkbox(h_ln, level, headings, checkboxes):
            break
        deeper = [
            candidate
            for candidate in range(level + 1, 5)
            if _sections_with_checkboxes(headings, checkboxes, candidate, skip_line)
        ]
        if not deeper:
            break
        level = deeper[0]
    return level


def _section_end(
    h_ln: int, headings: List[Tuple[int, str, int]], up_to_level: int
) -> int:
    """Return the line number where the section starting at h_ln ends.

    The section ends at the next heading of level <= up_to_level, or at
    end-of-file (sys.maxsize).
    """
    next_ln: int = 2**63 - 1  # practically infinite
    for lvl2, _, h2_ln in headings:
        if h2_ln > h_ln and lvl2 <= up_to_level:
            next_ln = h2_ln
            break
    return next_ln


def _build_milestones(
    headings: List[Tuple[int, str, int]],
    checkboxes: List[Tuple[bool, int]],
    milestone_level: int,
    skip_line: int = 0,
) -> List[Milestone]:
    """Build Milestone objects from sections at milestone_level.

    Sections with zero checkboxes are skipped.  Task rows are headings
    deeper than milestone_level that hold checkboxes. The heading at
    *skip_line* (the document title) is never a milestone.
    """
    milestones: List[Milestone] = []
    ms_headings = [
        (name, h_ln)
        for (lvl, name, h_ln) in headings
        if lvl == milestone_level and h_ln != skip_line
    ]

    for name, h_ln in ms_headings:
        ms_end = _section_end(h_ln, headings, milestone_level)

        # All checkboxes in this section
        section_cbs = [
            Checkbox(checked, cb_ln)
            for (checked, cb_ln) in checkboxes
            if h_ln <= cb_ln < ms_end
        ]
        if not section_cbs:
            continue  # skip sections with zero checkboxes

        ms = Milestone(name, h_ln)
        ms.checkboxes = section_cbs

        # Task rows: deeper headings that hold checkboxes within this section
        task_headings = [
            (tlvl, tname, t_ln)
            for (tlvl, tname, t_ln) in headings
            if tlvl > milestone_level and h_ln < t_ln < ms_end
        ]
        for tlvl, tname, t_ln in task_headings:
            t_end = _section_end(t_ln, headings, tlvl)
            task_cbs = [
                Checkbox(checked, cb_ln)
                for (checked, cb_ln) in checkboxes
                if t_ln <= cb_ln < t_end
            ]
            if task_cbs:
                th = TaskHeader(tname, t_ln)
                th.checkboxes = task_cbs
                ms.tasks.append(th)

        milestones.append(ms)

    return milestones


def _parse_lines(lines: List[str]) -> List[Milestone]:
    """Parse a list of text lines into milestones using adaptive level detection.

    Rules from the spec:
      - Milestone level is the shallowest level (1..4) that has at least one
        section containing a checkbox.
      - Sections with zero checkboxes are skipped.
      - Task rows = headings deeper than the milestone level that hold checkboxes.
      - Fenced code blocks are skipped entirely.
      - YAML frontmatter (--- delimited) is skipped entirely.
      - Checkboxes before the first heading are ignored UNLESS the file has
        no heading at all, in which case they become a single ``(preamble)``
        milestone.
    """
    headings: List[Tuple[int, str, int]] = []  # (level, name, line_number)
    checkboxes: List[Tuple[bool, int]] = []  # (checked, line_number)
    in_code = False
    seen_any_heading = False
    fm_skip = _skip_frontmatter(lines)

    for i, raw in enumerate(lines):
        if i < fm_skip:
            continue
        line = raw.rstrip("\n")
        ln = i + 1  # 1-indexed

        # fence detection
        if _FENCE_RE.match(line):
            in_code = not in_code
            continue
        if in_code:
            continue

        hm = _HEADING_RE.match(line)
        if hm:
            level = len(hm.group(1))
            if 1 <= level <= 4:
                headings.append((level, hm.group(2).strip(), ln))
                seen_any_heading = True
            continue

        cb = _CB_RE.match(line)
        if cb:
            checked = cb.group(1) in "xX"
            if not seen_any_heading:
                # Checkbox before any heading; will become preamble only if
                # the file has zero headings.
                checkboxes.append((checked, ln))
            else:
                checkboxes.append((checked, ln))

    if not headings and not seen_any_heading:
        # No heading at all -> preamble pseudo-milestone
        if checkboxes:
            preamble = Milestone(name="(preamble)", line_number=1)
            for checked, ln in checkboxes:
                preamble.checkboxes.append(Checkbox(checked=checked, line_number=ln))
            return [preamble]
        return []

    title_line = _title_line(headings, checkboxes)
    milestone_level = _find_milestone_level(headings, checkboxes, title_line)
    return _build_milestones(headings, checkboxes, milestone_level, title_line)


def parse_plan(path: str) -> PlanFile:
    """Parse a single markdown file into a PlanFile."""
    plan = PlanFile(path)
    with open(path, "r", encoding="utf-8") as fh:
        lines = fh.readlines()
    plan.milestones = _parse_lines(lines)
    plan.title = _first_heading(lines)
    plan.weights = _parse_weights_from_lines(lines)
    return plan


# --- root scanning ---


def scan_roots(
    hermes_home: str,
    cwd: str = "",
    extra_roots: Optional[List[str]] = None,
) -> List[Path]:
    """Return deduped list of plan markdown file paths from all roots.

    Roots, in order, deduped by realpath:
      1. ``$HERMES_HOME/plans/``
      2. ``$HERMES_DEFAULT_HOME/plans/`` (default ``~/.hermes``)
      3. ``<cwd>/.hermes/plans/`` (if cwd is given)
      4. ``extra_roots`` (if given)
      5. ``$PLAN_TRACKER_ROOTS`` (os.pathsep separated, if set)

    Returns ``*.md`` files that contain at least one checkbox.
    """
    seen: set[str] = set()
    result: List[Path] = []
    dirs: List[str] = []

    hh = Path(hermes_home)
    dirs.append(str(hh / "plans"))
    default_home = os.environ.get("HERMES_DEFAULT_HOME", "").strip()
    dirs.append(str(Path(default_home).expanduser() / "plans") if default_home
                else str(Path.home() / ".hermes" / "plans"))
    if cwd:
        dirs.append(str(Path(cwd) / ".hermes" / "plans"))
        dirs.append(str(Path(cwd) / "docs" / "superpowers" / "plans"))
    if extra_roots:
        dirs.extend(extra_roots)
    env_roots = os.environ.get("PLAN_TRACKER_ROOTS", "").strip()
    if env_roots:
        dirs.extend(part for part in env_roots.split(os.pathsep) if part.strip())

    for d in dirs:
        dp = Path(d)
        if not dp.is_dir():
            continue
        for fp in sorted(dp.glob("*.md")):
            rp = str(fp.resolve())
            if rp in seen:
                continue
            seen.add(rp)
            text = fp.read_text("utf-8", errors="replace")
            if _CB_RE.search(text):
                result.append(fp)

    return result


# --- progress helpers ---


def status_of(
    plan: PlanFile, milestone_index: int, previous_milestones: List[Milestone]
) -> str:
    """Derive status for a milestone in context of prior milestones."""
    del previous_milestones  # kept for backward compatibility
    ms = plan.milestones[milestone_index]
    for prev in plan.milestones[:milestone_index]:
        if prev.status == "active":
            return "pending"
    return ms.status


# --- history ---


def history_dir(hermes_home: str) -> str:
    """Full path to the history directory."""
    return str(Path(hermes_home) / "plugin-data" / "plan-tracker" / "history")


def ensure_history_dir(hermes_home: str) -> str:
    """Create and return the history directory path."""
    hd = Path(history_dir(hermes_home))
    hd.mkdir(parents=True, exist_ok=True)
    return str(hd)


def _current_sha(plan: PlanFile) -> str:
    """SHA256 of the canonical representation (milestones + checkboxes)."""
    parts: List[str] = []
    for ms in plan.milestones:
        parts.append(ms.name)
        parts.append(str(ms.done))
        parts.append(str(ms.total))
    raw = "\n".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _milestones_dict(plan: PlanFile) -> Dict[str, List[int]]:
    """Return {milestone_name: [done, total]} for the plan."""
    return {ms.name: [ms.done, ms.total] for ms in plan.milestones}


def record_snapshot(hermes_home: str, plan: PlanFile) -> bool:
    """Append a snapshot JSON line if content changed. Returns True if written."""
    hd = ensure_history_dir(hermes_home)
    pid = plan.plan_id
    path = Path(hd) / f"{pid}.jsonl"
    sha = _current_sha(plan)

    last_sha: Optional[str] = None
    if path.exists():
        raw = path.read_text("utf-8").strip()
        if raw:
            last_line = raw.rsplit("\n", 1)[-1]
            try:
                last_sha = json.loads(last_line).get("sha")
            except (json.JSONDecodeError, KeyError):
                pass

    if last_sha == sha:
        return False

    now = datetime.now(timezone.utc).isoformat()
    entry = {
        "ts": now, "sha": sha, "done": plan.done, "total": plan.total,
        "milestones": _milestones_dict(plan),
    }
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, separators=(",", ":")) + "\n")

    _trim_history(path)
    return True


def _trim_history(path: Path, max_lines: int = 500) -> None:
    """Keep only the newest N lines in the JSONL file."""
    if not path.exists():
        return
    raw = path.read_text("utf-8").strip()
    if not raw:
        return
    lines = raw.split("\n")
    if len(lines) <= max_lines:
        return
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines[-max_lines:]) + "\n")


def read_history(
    hermes_home: str, plan_id: str, limit: int = 50
) -> List[Dict[str, Any]]:
    """Return the last N history entries for a plan."""
    hd = Path(history_dir(hermes_home))
    path = hd / f"{plan_id}.jsonl"
    if not path.exists():
        return []
    raw = path.read_text("utf-8").strip()
    if not raw:
        return []
    lines = raw.split("\n")
    entries: List[Dict[str, Any]] = []
    for line in lines[-limit:]:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


# --- ETA and week delta ---


def compute_eta(history: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Compute ETA from history.

    Returns ``{\"per_day\": float, \"remaining\": int, \"date\": \"YYYY-MM-DD\"}``
    or ``None`` when there is no rate (fewer than 2 data points, zero rate,
    or no progress in the window).
    """
    if len(history) < 2:
        return None

    first = history[0]
    last = history[-1]

    try:
        first_ts = datetime.fromisoformat(first["ts"])
        last_ts = datetime.fromisoformat(last["ts"])
    except (ValueError, KeyError):
        return None

    days = (last_ts - first_ts).total_seconds() / 86400.0
    if days <= 0:
        return None

    progress = last.get("done", 0) - first.get("done", 0)
    if progress <= 0:
        return None

    per_day = progress / days
    remaining = last.get("total", 0) - last.get("done", 0)
    if remaining <= 0:
        return {"per_day": round(per_day, 2), "remaining": 0, "date": last_ts.date().isoformat()}

    eta_days = remaining / per_day
    from datetime import timedelta

    eta_date = last_ts + timedelta(days=eta_days)
    return {
        "per_day": round(per_day, 2),
        "remaining": remaining,
        "date": eta_date.date().isoformat(),
    }


def compute_week_delta(history: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Compare the oldest snapshot within 7 days with the latest.

    Returns ``{\"since_ts\": \"ISO\", \"milestones\": {\"<name>\": delta}}``
    or ``None`` when there is no data in the window.
    """
    if len(history) < 2:
        return None

    now = datetime.now(timezone.utc)
    cutoff = now.timestamp() - 7 * 86400

    window: List[Dict[str, Any]] = []
    for h in history:
        try:
            h_ts = datetime.fromisoformat(h["ts"]).timestamp()
        except (ValueError, KeyError):
            continue
        if h_ts >= cutoff:
            window.append(h)

    if len(window) < 2:
        return None

    first = window[0]
    last = window[-1]

    first_ms = first.get("milestones", {})
    last_ms = last.get("milestones", {})

    deltas: Dict[str, int] = {}
    for ms_name, last_counts in last_ms.items():
        first_counts = first_ms.get(ms_name, [0, 0])
        delta = last_counts[0] - first_counts[0]
        if delta > 0:
            deltas[ms_name] = delta

    return {"since_ts": first["ts"], "milestones": deltas}


# --- content-hash guard for toggle checkbox ---


def resolve_plan_path(
    hermes_home: str, plan_id: str, cwd: str = "",
) -> Optional[str]:
    """Find a plan file path by its plan_id (12 hex chars).

    Scans root directories and registry entries until the id matches.
    Returns the resolved path or None.
    """
    for fp in scan_roots(hermes_home, cwd):
        rp = str(Path(fp).resolve())
        pid = hashlib.sha256(rp.encode("utf-8")).hexdigest()[:12]
        if pid == plan_id:
            return rp

    for entry in load_registry(hermes_home):
        try:
            rp = str(Path(entry["path"]).resolve())
            pid = hashlib.sha256(rp.encode("utf-8")).hexdigest()[:12]
            if pid == plan_id:
                return rp
        except Exception:
            continue

    return None


_CB_TOGGLE_RE = re.compile(r"^(\s*[-*]\s+\[)([ xX])(\]\s*.*)$")


def toggle_checkbox(
    hermes_home: str, plan_id: str, line_number: int, expect_sha: str,
) -> Tuple[bool, str, str]:
    """Toggle a checkbox character on *line_number* in the plan file.

    Reads the file, compares sha256 of its current bytes with *expect_sha*,
    and rewrites ONLY the ``[ ]`` / ``[x]`` character when they match.
    The write is atomic (temp file + ``os.replace``).

    Returns ``(ok, message, new_sha)``.
    ``ok`` is False and ``message`` is ``'stale'`` when the hash mismatches.
    """
    path_str = resolve_plan_path(hermes_home, plan_id)
    if path_str is None:
        return False, "plan not found", ""
    path = Path(path_str)

    try:
        raw = path.read_bytes()
    except OSError as e:
        return False, f"cannot read: {e}", ""

    current_sha = hashlib.sha256(raw).hexdigest()
    if current_sha != expect_sha:
        return False, "stale", ""

    lines = raw.split(b"\n")
    if line_number < 1 or line_number > len(lines):
        return False, f"line number {line_number} out of range (1..{len(lines)})", ""

    line = lines[line_number - 1].decode("utf-8", errors="replace")
    m = _CB_TOGGLE_RE.match(line)
    if not m:
        return False, "line is not a checkbox", ""

    prefix = m.group(1)  # e.g. "- ["
    inner = m.group(2)   # " " | "x" | "X"
    suffix = m.group(3)  # e.g. "] ..."

    if inner == " ":
        new_inner = "x"
    else:
        new_inner = " "

    new_line = (prefix + new_inner + suffix).encode("utf-8")
    raw_lines = raw.split(b"\n")
    # Preserve whether the original line ended with \n or not
    if line_number - 1 < len(raw_lines):
        raw_lines[line_number - 1] = new_line

    new_raw = b"\n".join(raw_lines)

    # Atomic write: temp file then os.replace
    fd, tmp_path = tempfile.mkstemp(suffix=".md", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(new_raw)
        os.chmod(tmp_path, path.stat().st_mode)
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    new_sha = hashlib.sha256(new_raw).hexdigest()
    return True, "", new_sha


# --- blocked milestones sidecar ---


def _blocks_dir(hermes_home: str) -> str:
    """Full path to the blocks storage directory."""
    return str(Path(hermes_home) / "plugin-data" / "plan-tracker" / "blocks")


def _ensure_blocks_dir(hermes_home: str) -> str:
    """Create and return the blocks directory path."""
    bd = Path(_blocks_dir(hermes_home))
    bd.mkdir(parents=True, exist_ok=True)
    return str(bd)


def _blocks_path(hermes_home: str, plan_id: str) -> Path:
    return Path(_ensure_blocks_dir(hermes_home)) / f"{plan_id}.json"


def load_blocks(hermes_home: str, plan_id: str) -> Dict[str, Any]:
    """Load blocked milestones for a plan. Never raises; returns {} on error."""
    bp = _blocks_path(hermes_home, plan_id)
    if not bp.exists():
        return {}
    try:
        return json.loads(bp.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_blocks(hermes_home: str, plan_id: str, data: Dict[str, Any]) -> None:
    """Write blocks data atomically (temp file + os.replace)."""
    bp = _blocks_path(hermes_home, plan_id)
    fd, tmp_path = tempfile.mkstemp(suffix=".json", dir=str(bp.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, separators=(",", ":"))
        os.replace(tmp_path, str(bp))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def set_block(
    hermes_home: str, plan_id: str, milestone: str, reason: str,
) -> None:
    """Mark a milestone as blocked with a reason.

    Persisted to ``blocks/<plan_id>.json``. Does not touch the plan file.
    """
    blocks = load_blocks(hermes_home, plan_id)
    from datetime import datetime, timezone
    blocks[milestone] = {
        "reason": reason,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    _save_blocks(hermes_home, plan_id, blocks)


def clear_block(
    hermes_home: str, plan_id: str, milestone: str,
) -> bool:
    """Remove a block from a milestone. Returns True if removed, False if absent."""
    blocks = load_blocks(hermes_home, plan_id)
    if milestone not in blocks:
        return False
    del blocks[milestone]
    _save_blocks(hermes_home, plan_id, blocks)
    return True


# --- effort-attribution glue ---


def record_snapshot_with_effort(hermes_home: str, plan: PlanFile) -> bool:
    """Record a snapshot and attribute effort if milestones progressed."""
    written = record_snapshot(hermes_home, plan)
    if written:
        _update_effort_on_snapshot(hermes_home, plan)
    return written


def _update_effort_on_snapshot(hermes_home: str, plan: PlanFile) -> None:
    """Read the two most recent snapshots and attribute effort for changed milestones."""
    pid = plan.plan_id
    history = read_history(hermes_home, pid, limit=2)
    if len(history) < 2:
        return
    prev = history[0]
    new = history[-1]
    if "milestones" in new and "milestones" in prev:
        try:
            from effort import update_effort_for_snapshot as _update

            _update(hermes_home, plan.plan_id, prev, new)
        except ImportError:
            pass


def load_all_plans(
    hermes_home: str,
    cwd: str = "",
    extra_roots: Optional[List[str]] = None,
    scope: str = "all",
    session: str = "",
) -> List[Dict[str, Any]]:
    """Return plan summaries filtered by scope.

    One entry per REGISTRATION: the same file tracked in two sessions (or in a
    session and globally) appears once per registration, each with its own
    ``scope``/``session``. A root file with no registration appears once with
    ``registered: False``.

    Each dict includes ``id``, ``name``, ``done``, ``total``, ``percent``,
    ``mtime``, ``path``, ``scope``, ``session``, ``registered`` and ``key``
    (unique per registration, safe as a React key).
    """
    result: List[Dict[str, Any]] = []
    registered_paths: set = set()

    for rent in load_registry(hermes_home):
        ent_scope = rent.get("scope", "global")
        ent_session = rent.get("session", "")

        if scope == "global" and ent_scope != "global":
            continue
        if scope == "session" and (ent_scope != "session" or ent_session != session):
            continue

        try:
            plan = parse_plan(rent["path"])
        except (OSError, UnicodeDecodeError):
            continue
        record_snapshot_with_effort(hermes_home, plan)
        result.append({
            "id": plan.plan_id,
            "name": plan.name,
            "done": plan.done,
            "total": plan.total,
            "percent": plan.percent,
            "mtime": os.path.getmtime(plan.path),
            "path": plan.path,
            "registered": True,
            "scope": ent_scope,
            "session": ent_session,
            "key": "%s:%s:%s" % (plan.plan_id, ent_scope, ent_session),
        })
        try:
            registered_paths.add(str(Path(plan.path).resolve()))
        except Exception:
            pass

    if scope in ("all", "global"):
        for fp in scan_roots(hermes_home, cwd, extra_roots):
            try:
                real = str(Path(fp).resolve())
            except Exception:
                continue
            if real in registered_paths:
                continue
            try:
                plan = parse_plan(str(fp))
            except (OSError, UnicodeDecodeError):
                continue
            record_snapshot_with_effort(hermes_home, plan)
            result.append({
                "id": plan.plan_id,
                "name": plan.name,
                "done": plan.done,
                "total": plan.total,
                "percent": plan.percent,
                "mtime": os.path.getmtime(plan.path),
                "path": plan.path,
                "scope": "global",
                "session": "",
                "registered": False,
                "key": "%s:root" % plan.plan_id,
            })

    return result


def get_plan_summary(
    plan_path: str, registered: bool = False
) -> Optional[Dict[str, Any]]:
    """Parse a single plan file and return a summary dict, or None on error."""
    try:
        plan = parse_plan(plan_path)
        return {
            "id": plan.plan_id,
            "name": plan.name,
            "done": plan.done,
            "total": plan.total,
            "percent": plan.percent,
            "mtime": os.path.getmtime(plan.path),
            "path": plan.path,
            "registered": registered,
        }
    except (OSError, UnicodeDecodeError):
        return None


def load_plan_detail(
    hermes_home: str,
    plan_id: str,
    cwd: str = "",
    extra_roots: Optional[List[str]] = None,
    extra_paths: Optional[List[str]] = None,
    known_registered_paths: Optional[set] = None,
    scope: str = "all",
    session: str = "",
) -> Optional[Dict[str, Any]]:
    """Return a full plan detail dict or None if not found.

    Searches root-scanned files plus registry entries filtered by *scope* and
    *session*.  The *extra_paths* and *known_registered_paths* params are
    retained for backward compat but superseded by the registry-scope logic.
    """
    # Build the candidate-file list with scope filtering
    all_paths: List[Tuple[str, bool]] = []  # (path, is_registered)
    seen_real: set = set()

    # Root-scanned files (always scope "global")
    if scope in ("all", "global"):
        for fp in scan_roots(hermes_home, cwd, extra_roots):
            fp_str = str(fp)
            all_paths.append((fp_str, False))
            try:
                seen_real.add(str(Path(fp_str).resolve()))
            except Exception:
                pass

    # Registry entries, filtered by scope/session
    for rent in load_registry(hermes_home):
        rent_scope = rent.get("scope", "global")
        rent_session = rent.get("session", "")
        if scope == "global" and rent_scope != "global":
            continue
        if scope == "session" and (rent_scope != "session" or rent_session != session):
            continue
        try:
            real = str(Path(rent["path"]).resolve())
            if real not in seen_real:
                all_paths.append((rent["path"], True))
                seen_real.add(real)
        except Exception:
            all_paths.append((rent["path"], True))

    # Also merge extra_paths for backward compat
    if extra_paths:
        for p in extra_paths:
            try:
                real = str(Path(p).resolve())
                if real not in seen_real:
                    all_paths.append((p, False))
                    seen_real.add(real)
            except Exception:
                all_paths.append((p, False))

    for path_str, is_registered in all_paths:
        try:
            plan = parse_plan(path_str)
        except (OSError, UnicodeDecodeError):
            continue
        if plan.plan_id == plan_id:
            record_snapshot_with_effort(hermes_home, plan)

            # Determine registered status
            actual_registered = is_registered
            if not actual_registered and known_registered_paths:
                try:
                    actual_registered = str(Path(plan.path).resolve()) in known_registered_paths
                except Exception:
                    pass

            tasks_list: List[Dict[str, Any]] = []
            for ms in plan.milestones:
                for t in ms.tasks:
                    first_line = t.checkboxes[0].line_number if t.checkboxes else 0
                    tasks_list.append({
                        "name": t.name, "done": t.done, "total": t.total,
                        "line": first_line,
                    })

            milestones_list: List[Dict[str, Any]] = []
            for i, ms in enumerate(plan.milestones):
                ms_weight = plan.weights.get(ms.name, ms.total)
                milestones_list.append({
                    "name": ms.name,
                    "done": ms.done,
                    "total": ms.total,
                    "percent": ms.percent,
                    "status": status_of(plan, i, plan.milestones),
                    "weight": ms_weight,
                })

            history = read_history(hermes_home, plan_id)

            # Effort, ETA, week delta
            effort_agg = {}
            eta = None
            week = None
            try:
                from effort import load_aggregated_effort as _agg
                effort_agg = _agg(hermes_home, plan_id)
                eta = compute_eta(history)
                week = compute_week_delta(history)
            except ImportError:
                pass

            # File sha for content-hash guard
            try:
                detail_sha = hashlib.sha256(
                    Path(plan.path).read_bytes()
                ).hexdigest()
            except OSError:
                detail_sha = ""

            # Blocked milestones sidecar
            detail_blocks = load_blocks(hermes_home, plan_id)

            detail = {
                "id": plan.plan_id,
                "name": plan.name,
                "path": plan.path,
                "percent": plan.percent,
                "weighted_percent": plan.weighted_percent,
                "done": plan.done,
                "total": plan.total,
                "milestones": milestones_list,
                "tasks": tasks_list,
                "history": history,
                "effort": effort_agg,
                "eta": eta,
                "week": week,
                "mtime": os.path.getmtime(plan.path),
                "registered": actual_registered,
                "sha": detail_sha,
                "blocks": detail_blocks,
            }

            # Derive scope/session for the returned detail
            for rent in load_registry(hermes_home):
                try:
                    if str(Path(rent["path"]).resolve()) == str(Path(plan.path).resolve()):
                        detail["scope"] = rent.get("scope", "global")
                        detail["session"] = rent.get("session", "")
                        break
                except Exception:
                    pass
            else:
                detail["scope"] = "global"
                detail["session"] = ""

            return detail

    return None


# --- registry ---


def _registry_path(hermes_home: str) -> str:
    """Full path to the registry JSON file."""
    return str(Path(hermes_home) / "plugin-data" / "plan-tracker" / "registry.json")


def load_registry(hermes_home: str) -> List[Dict[str, Any]]:
    """Load registry entries. Returns list of dicts with path, scope, session.

    Auto-migrates v1 (string list) to v2 (dict entries) format.  Never raises.
    """
    rp = Path(_registry_path(hermes_home))
    if not rp.exists():
        return []
    try:
        data = json.loads(rp.read_text("utf-8"))
        files_raw = data.get("files", [])
        version = data.get("version", 1)

        if version == 1:
            # Migrate v1: ["/path.md"] -> [{"path":..., "scope":"global", "session":""}]
            entries: List[Dict[str, Any]] = []
            for f in files_raw:
                if isinstance(f, str):
                    entries.append({"path": f, "scope": "global", "session": ""})
                else:
                    entries.append(dict(f))
            save_registry(hermes_home, entries)  # rewrite as v2
            return entries
        else:
            return [
                {
                    "path": e["path"],
                    "scope": e.get("scope", "global"),
                    "session": e.get("session", ""),
                }
                for e in files_raw
                if isinstance(e, dict)
            ]
    except (json.JSONDecodeError, KeyError, OSError):
        return []


def save_registry(hermes_home: str, entries: List[Dict[str, Any]]) -> None:
    """Write the registry atomically in v2 format (temp file + os.replace)."""
    rp = Path(_registry_path(hermes_home))
    rp.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(suffix=".json", dir=str(rp.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(
                {"version": REGISTRY_VERSION, "files": entries},
                fh, separators=(",", ": "),
            )
        os.replace(tmp_path, str(rp))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def validate_registration_path(path_str: str, hermes_home: str) -> Tuple[bool, str]:
    """Validate a path for registry registration.

    Returns ``(is_valid, message)``.  Checks: exists, regular file, .md suffix
    (case-insensitive), absolute (or ~-expandable), contains >= 1 checkbox.
    """
    expanded = os.path.expanduser(path_str)

    if not os.path.isabs(expanded):
        return False, "path must be absolute (use ~/ or full /path)"

    p = Path(expanded)

    if p.suffix.lower() != ".md":
        return False, "path must end with .md"

    if not p.exists():
        return False, f"file not found: {expanded}"

    if not p.is_file():
        return False, f"not a regular file: {expanded}"

    try:
        text = p.read_text("utf-8", errors="replace")
        if not _CB_RE.search(text):
            return False, "file contains no checkbox items"
    except (OSError, UnicodeDecodeError):
        return False, f"cannot read file: {expanded}"

    return True, "valid"


def add_registry_entry(
    hermes_home: str, path_str: str, scope: str = "global", session: str = ""
) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """Add a file to the registry after validation.

    ``scope`` is ``global`` or ``session``.  When ``scope`` is ``session`` the
    ``session`` id must be a non-empty ``[A-Za-z0-9_-]{1,128}`` string.
    Duplicate = same realpath AND same scope AND same session.

    Returns ``(success, message, plan_dict_or_None)``.
    """
    expanded = os.path.expanduser(path_str)
    valid, msg = validate_registration_path(expanded, hermes_home)
    if not valid:
        return False, msg, None

    if scope not in ("global", "session"):
        return False, 'scope must be "global" or "session"', None
    if scope == "session" and (not session or not re.match(r"^[A-Za-z0-9_-]{1,128}$", session)):
        return False, "session id required (alphanumeric, 1-128 chars)", None

    real = str(Path(expanded).resolve())
    entries = load_registry(hermes_home)

    if len(entries) >= REGISTRY_MAX:
        return False, f"registry limit ({REGISTRY_MAX}) reached", None

    # Duplicate = same realpath AND same scope AND same session
    for ent in entries:
        try:
            ent_real = str(Path(ent["path"]).resolve())
        except (OSError, RuntimeError):
            ent_real = ent["path"]
        if ent_real == real and ent.get("scope") == scope and ent.get("session") == session:
            return False, "file already registered", None

    entries.append({"path": expanded, "scope": scope, "session": session})
    save_registry(hermes_home, entries)

    plan_data: Optional[Dict[str, Any]] = None
    try:
        plan = parse_plan(expanded)
        plan_data = {
            "id": plan.plan_id,
            "name": plan.name,
            "path": plan.path,
            "done": plan.done,
            "total": plan.total,
            "percent": plan.percent,
            "scope": scope,
            "session": session,
        }
    except (OSError, UnicodeDecodeError):
        pass

    return True, "registered", plan_data


def remove_registry_entry(
    hermes_home: str, plan_id_or_path: str,
    scope: Optional[str] = None, session: Optional[str] = None,
) -> Tuple[bool, str]:
    """Remove a file from the registry by plan_id (12 hex) or path.

    When *scope* or *session* is given the entry is only removed if all
    non-None filters match the entry's fields.

    Returns ``(success, message)``.
    """
    entries = load_registry(hermes_home)
    if not entries:
        return False, "registry is empty"

    remaining: List[Dict[str, Any]] = []
    removed = False
    for ent in entries:
        try:
            rp = str(Path(ent["path"]).resolve())
            pid = hashlib.sha256(rp.encode("utf-8")).hexdigest()[:12]
            matches_target = (
                pid == plan_id_or_path
                or ent["path"] == plan_id_or_path
                or rp == plan_id_or_path
            )
            if matches_target:
                # Apply optional scope/session filters
                if scope is not None and ent.get("scope") != scope:
                    remaining.append(ent)
                    continue
                if session is not None and ent.get("session") != session:
                    remaining.append(ent)
                    continue
                removed = True
                continue
        except Exception:
            pass
        remaining.append(ent)

    if not removed:
        return False, f"no entry found: {plan_id_or_path}"

    save_registry(hermes_home, remaining)
    return True, "removed"


# --- export ---


def export_plan_markdown(
    hermes_home: str, plan_id: str, cwd: str = "",
    scope: str = "all", session: str = "",
) -> Optional[str]:
    """Build a markdown summary of a plan for export.

    Returns the markdown text, or ``None`` when the plan is not found.
    """
    detail = load_plan_detail(hermes_home, plan_id, cwd=cwd, scope=scope, session=session)
    if detail is None:
        return None

    lines: List[str] = []
    lines.append(f"# {detail['name']}")
    lines.append("")

    pct_val = detail.get("weighted_percent") if detail.get("weighted_percent") is not None else detail["percent"]
    lines.append(f"**Progress:** {detail['done']}/{detail['total']} ({pct_val}%)")
    if detail.get("weighted_percent") is not None and detail["weighted_percent"] != detail["percent"]:
        lines.append(f"_Weighted: {detail['weighted_percent']}%_")
    lines.append("")

    lines.append("## Milestones")
    for ms in detail.get("milestones", []):
        w = ms.get("weight", ms["total"])
        weight_suffix = f" (weight={w})" if w != ms["total"] else ""
        lines.append(
            f"- **{ms['name']}:** {ms['done']}/{ms['total']} ({ms['percent']}%){weight_suffix}"
        )

    blocks = detail.get("blocks", {})
    if blocks:
        lines.append("")
        lines.append("### Blocked")
        for ms_name, blk in blocks.items():
            lines.append(f"- **{ms_name}:** {blk.get('reason', 'blocked')}")

    eta = detail.get("eta")
    if eta:
        lines.append("")
        lines.append(f"**ETA:** {eta.get('date', '?')} ({eta.get('per_day', 0)}/day, "
                      f"{eta.get('remaining', 0)} remaining)")

    effort = detail.get("effort", {})
    if effort:
        total_sec = sum(e.get("seconds", 0) for e in effort.values())
        total_tok = sum(e.get("tokens_in", 0) for e in effort.values()) + sum(
            e.get("tokens_out", 0) for e in effort.values()
        )
        if total_sec or total_tok:
            lines.append("")
            lines.append(f"**Effort:** {total_sec // 60}m, {total_tok // 1000}k tokens")

    lines.append("")
    lines.append("---")
    lines.append("_Exported from Plan Tracker_")
    return "\n".join(lines)


# --- plan_track tool handler ---


def handle_plan_track(params: dict, **kwargs: Any) -> str:
    """Handler for the ``plan_track`` agent tool.

    Parameters are passed as ``params`` per the Hermes tool-calling
    convention.  Supports ``session_id`` from kwargs: when *add* omits
    ``scope``, it defaults to ``session`` if ``session_id`` is present,
    else ``global``.

    Returns a compact JSON string.
    """
    hermes_home = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
    action = params.get("action", "")
    injected_session = kwargs.get("session_id", "")

    if action == "add":
        path = params.get("path", "")
        if not path:
            return json.dumps({"ok": False, "message": "path is required"})
        # When scope is omitted: session if session_id injected, else global
        scope = params.get("scope", "")
        if not scope:
            scope = "session" if injected_session else "global"
        session = params.get("session", "")
        if not session:
            session = injected_session if scope == "session" else ""
        ok, msg, plan_data = add_registry_entry(hermes_home, path, scope=scope, session=session)
        return json.dumps({"ok": ok, "message": msg, "plan": plan_data}, default=str)

    if action == "remove":
        remove_key = params.get("id") or params.get("path")
        if not remove_key:
            return json.dumps({"ok": False, "message": "id or path is required"})
        scope = params.get("scope")
        session = params.get("session", injected_session) if params.get("scope") else None
        ok, msg = remove_registry_entry(hermes_home, remove_key, scope=scope, session=session)
        return json.dumps({"ok": ok, "message": msg})

    if action == "list":
        entries = load_registry(hermes_home)
        return json.dumps({"ok": True, "paths": entries})

    return json.dumps({"ok": False, "message": f"unknown action: {action}"})