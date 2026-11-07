"""
Plan Tracker Reminder - warn when a tracked plan hasn't moved in N days.

Usage:
    python3 scripts/reminder.py [--days N] [--dry-run]

Stdlib only.  Reads the registry and history, prints a line per stalled plan.
Exit 0 always.

Documented hermes cron command (DO NOT create the cron job here):
    cd $HERMES_HOME/plugins/plan-tracker
    $HERMES_HOME/hermes-agent/.venv/bin/python3 scripts/reminder.py --days 3
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


def _hermes_home() -> str:
    return os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))


def _history_dir(hh: str) -> Path:
    return Path(hh) / "plugin-data" / "plan-tracker" / "history"


def _registry_path(hh: str) -> Path:
    return Path(hh) / "plugin-data" / "plan-tracker" / "registry.json"


def load_registry(hh: str) -> List[Dict[str, Any]]:
    """Load registry entries; never raises."""
    rp = _registry_path(hh)
    if not rp.exists():
        return []
    try:
        data = json.loads(rp.read_text("utf-8"))
        return [
            {"path": e["path"], "scope": e.get("scope", "global"), "session": e.get("session", "")}
            for e in data.get("files", [])
            if isinstance(e, dict)
        ]
    except (json.JSONDecodeError, KeyError, OSError):
        return []


def _plan_id(path: str) -> str:
    """First 12 hex of sha256(realpath)."""
    import hashlib
    rp = str(Path(path).resolve())
    return hashlib.sha256(rp.encode("utf-8")).hexdigest()[:12]


def _oldest_ts_in_window(hh: str, plan_id: str, days: int) -> float:
    """Earliest timestamp of a snapshot within *days* ago, or -1 if none.

    If the newest snapshot is older than *days*, returns -1 (plan is stalled).
    """
    hpath = _history_dir(hh) / f"{plan_id}.jsonl"
    if not hpath.exists():
        return -1
    try:
        raw = hpath.read_text("utf-8").strip()
        if not raw:
            return -1
        lines = raw.split("\n")
        # Check the newest line first
        newest_line = json.loads(lines[-1])
        newest_ts = datetime.fromisoformat(newest_line["ts"]).timestamp()
        now = datetime.now(timezone.utc).timestamp()
        if now - newest_ts > days * 86400:
            return -1  # no activity within the window
        # Find the oldest snapshot within the window
        cutoff = now - days * 86400
        for line in lines:
            entry = json.loads(line)
            entry_ts = datetime.fromisoformat(entry["ts"]).timestamp()
            if entry_ts >= cutoff:
                return entry_ts
        return -1
    except Exception:
        return -1


def reminder(days: int = 3, dry_run: bool = False) -> None:
    """Print stalled plans."""
    hh = _hermes_home()
    entries = load_registry(hh)
    if not entries:
        print("No tracked plans found.")
        return

    stalled: List[str] = []
    for ent in entries:
        pid = _plan_id(ent["path"])
        ts = _oldest_ts_in_window(hh, pid, days)
        if ts < 0:
            name = Path(ent["path"]).stem
            stalled.append(f"{name}  ({ent['path']})")

    if not stalled:
        print(f"All {len(entries)} tracked plans have moved in the last {days} days.")
        return

    if dry_run:
        print(f"[dry-run] {len(stalled)} stalled plan(s):")
        for s in stalled:
            print(f"  {s}")
    else:
        for s in stalled:
            print(s)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Warn when tracked plans haven't moved in N days.",
    )
    parser.add_argument("--days", type=int, default=3, help="Stall threshold in days (default: 3)")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be said, no output")
    args = parser.parse_args()
    reminder(days=args.days, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
    sys.exit(0)