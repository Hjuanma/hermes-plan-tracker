"""Effort attribution: read state.db and attribute effort to milestone progress.

Pure Python stdlib + sqlite3. Opens state.db READ-ONLY. Never writes to it.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Dict, List


def effort_dir(hermes_home: str) -> str:
    """Full path to the effort data directory."""
    return str(Path(hermes_home) / "plugin-data" / "plan-tracker" / "effort")


def ensure_effort_dir(hermes_home: str) -> str:
    """Create and return the effort directory path."""
    ed = Path(effort_dir(hermes_home))
    ed.mkdir(parents=True, exist_ok=True)
    return str(ed)


def load_effort(hermes_home: str, plan_id: str) -> Dict[str, Any]:
    """Load effort data for a plan. Never raises, returns ``{\"milestones\": {}}`` on error."""
    path = Path(effort_dir(hermes_home)) / f"{plan_id}.json"
    if not path.exists():
        return {"milestones": {}}
    try:
        return json.loads(path.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"milestones": {}}


def save_effort(hermes_home: str, plan_id: str, data: Dict[str, Any]) -> None:
    """Write effort data atomically (temp file + os.replace)."""
    ed = Path(ensure_effort_dir(hermes_home))
    path = ed / f"{plan_id}.json"
    fd, tmp_path = tempfile.mkstemp(suffix=".json", dir=str(ed))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, separators=(",", ":"), default=str)
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_aggregated_effort(hermes_home: str, plan_id: str) -> Dict[str, Dict[str, Any]]:
    """Load effort data and aggregate per milestone.

    Returns ``{\"<milestone>\": {seconds, tokens_in, tokens_out, cost_usd, models, sessions}}``.
    """
    effort = load_effort(hermes_home, plan_id)
    milestones = effort.get("milestones", {})
    result: Dict[str, Dict[str, Any]] = {}
    for ms_name, events in milestones.items():
        agg: Dict[str, Any] = {
            "seconds": 0,
            "tokens_in": 0,
            "tokens_out": 0,
            "cost_usd": 0.0,
            "models": {},
            "sessions": [],
        }
        for event in events:
            agg["seconds"] += event.get("seconds", 0)
            agg["tokens_in"] += event.get("tokens_in", 0)
            agg["tokens_out"] += event.get("tokens_out", 0)
            agg["cost_usd"] += event.get("cost_usd", 0.0)
            for model, tok in event.get("models", {}).items():
                agg["models"][model] = agg["models"].get(model, 0) + tok
            for s in event.get("sessions", []):
                agg["sessions"].append(s)
        result[ms_name] = agg
    return result


#: Columns this module reads. A Hermes upgrade that renames one of them must
#: fail loudly in the tests instead of silently returning empty effort.
REQUIRED_COLUMNS = {
    "sessions": {
        "id", "input_tokens", "output_tokens", "estimated_cost_usd",
        "model", "parent_session_id", "title",
    },
    "messages": {"session_id", "timestamp", "active"},
    "session_model_usage": {"session_id", "model", "input_tokens", "output_tokens"},
}


def schema_ok(conn: sqlite3.Connection) -> bool:
    """True when every table and column this module reads exists."""
    for table, columns in REQUIRED_COLUMNS.items():
        try:
            have = {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}
        except sqlite3.Error:
            return False
        if not columns.issubset(have):
            return False
    return True


def _state_db_paths(hermes_home: str) -> List[str]:
    """Every state.db that can hold this machine's sessions, launch home first.

    The gateway runs under the default home while the chat may belong to a
    named profile, whose sessions live in profiles/<name>/state.db.
    """
    paths: List[str] = []
    root = os.path.join(hermes_home, "state.db")
    if os.path.exists(root):
        paths.append(root)
    profiles = os.path.join(hermes_home, "profiles")
    try:
        for name in sorted(os.listdir(profiles)):
            candidate = os.path.join(profiles, name, "state.db")
            if os.path.exists(candidate):
                paths.append(candidate)
    except OSError:
        pass
    return paths


def _attribute_from_db(
    db_path: str, since_ts: float, until_ts: float,
) -> Dict[str, Any]:
    """Attribute one state.db in the window. Returns {} on any sqlite error."""
    try:
        conn = sqlite3.connect("file:" + db_path + "?mode=ro", timeout=2, uri=True)
    except (sqlite3.Error, OSError):
        return {}

    if not schema_ok(conn):
        conn.close()
        return {}

    try:
        rows = conn.execute(
            """SELECT s.id, COALESCE(s.input_tokens, 0), COALESCE(s.output_tokens, 0),
                      COALESCE(s.estimated_cost_usd, 0.0),
                      s.model, s.parent_session_id IS NOT NULL,
                      COALESCE(s.title, ''), COUNT(m.id),
                      MIN(m.timestamp), MAX(m.timestamp)
               FROM messages m
               JOIN sessions s ON s.id = m.session_id
               WHERE m.timestamp >= ? AND m.timestamp <= ?
                 AND m.active = 1
               GROUP BY s.id""",
            (since_ts, until_ts),
        ).fetchall()

        if not rows:
            return {}

        total_seconds = 0
        total_tokens_in = 0
        total_tokens_out = 0
        total_cost = 0.0
        models_map: Dict[str, int] = {}
        sessions_list: List[Dict[str, Any]] = []

        for row in rows:
            (
                sid, inp_tok, out_tok, cost, model,
                subagent, title, msg_in_window, first_ts, last_ts,
            ) = row

            total_msgs = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = ? AND active = 1",
                (sid,),
            ).fetchone()[0]

            share = msg_in_window / total_msgs if total_msgs > 0 else 0.0

            prorated_in = round(inp_tok * share) if inp_tok else 0
            prorated_out = round(out_tok * share) if out_tok else 0
            prorated_cost = round(cost * share, 6) if cost else 0.0

            seconds = (last_ts - first_ts) if last_ts and first_ts else 0

            total_seconds += seconds
            total_tokens_in += prorated_in
            total_tokens_out += prorated_out
            total_cost += prorated_cost

            model_rows = conn.execute(
                """SELECT model, COALESCE(input_tokens, 0), COALESCE(output_tokens, 0)
                   FROM session_model_usage
                   WHERE session_id = ?""",
                (sid,),
            ).fetchall()
            for mrow in model_rows:
                m_model, m_in, m_out = mrow
                m_tokens = round(m_in * share) + round(m_out * share)
                if m_tokens > 0:
                    models_map[m_model] = models_map.get(m_model, 0) + m_tokens

            if not model_rows and model:
                mtok = prorated_in + prorated_out
                if mtok > 0:
                    models_map[model] = models_map.get(model, 0) + mtok

            sessions_list.append({
                "id": sid,
                "title": title,
                "model": model or "",
                "subagent": bool(subagent),
                "tokens": prorated_in + prorated_out,
                "seconds": seconds,
            })

        return {
            "seconds": total_seconds,
            "tokens_in": total_tokens_in,
            "tokens_out": total_tokens_out,
            "cost_usd": total_cost,
            "models": models_map,
            "sessions": sessions_list,
        }

    except sqlite3.Error:
        return {}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def attribute_effort(
    hermes_home: str, plan_id: str, since_ts: float, until_ts: float,
) -> Dict[str, Any]:
    """Attribute effort in [since_ts, until_ts] across every profile's state.db.

    Read-only. Missing or locked databases are skipped, never raised.
    """
    del plan_id  # kept for future use
    merged: Dict[str, Any] = {
        "seconds": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0,
        "models": {}, "sessions": [],
    }
    seen_sessions: set = set()
    found = False

    for db_path in _state_db_paths(hermes_home):
        part = _attribute_from_db(db_path, since_ts, until_ts)
        if not part:
            continue
        found = True
        merged["seconds"] += part.get("seconds", 0)
        merged["tokens_in"] += part.get("tokens_in", 0)
        merged["tokens_out"] += part.get("tokens_out", 0)
        merged["cost_usd"] = round(merged["cost_usd"] + part.get("cost_usd", 0.0), 6)
        for model, tokens in (part.get("models") or {}).items():
            merged["models"][model] = merged["models"].get(model, 0) + tokens
        for session in part.get("sessions") or []:
            if session.get("id") in seen_sessions:
                continue
            seen_sessions.add(session.get("id"))
            merged["sessions"].append(session)

    return merged if found else {}


def update_effort_for_snapshot(
    hermes_home: str, plan_id: str, prev_snapshot: Dict, new_snapshot: Dict,
) -> None:
    """Update effort data when a snapshot shows milestone done increased.

    Reads the milestone lists from prev and new, attributes effort in the
    time window, and appends to the per-milestone event list.
    """
    from datetime import datetime

    prev_ms = prev_snapshot.get("milestones", {})
    new_ms = new_snapshot.get("milestones", {})

    try:
        since_ts = datetime.fromisoformat(prev_snapshot["ts"]).timestamp()
        until_ts = datetime.fromisoformat(new_snapshot["ts"]).timestamp()
    except (ValueError, KeyError):
        return

    effort = load_effort(hermes_home, plan_id)
    if "milestones" not in effort:
        effort["milestones"] = {}

    has_change = False
    for ms_name, new_counts in new_ms.items():
        prev_counts = prev_ms.get(ms_name, [0, 0])
        delta = new_counts[0] - prev_counts[0]
        if delta > 0:
            attr = attribute_effort(hermes_home, plan_id, since_ts, until_ts)
            if not attr:
                continue
            attr["ts"] = new_snapshot["ts"]
            attr["delta"] = delta
            if ms_name not in effort["milestones"]:
                effort["milestones"][ms_name] = []
            effort["milestones"][ms_name].append(attr)
            has_change = True

    if has_change:
        save_effort(hermes_home, plan_id, effort)