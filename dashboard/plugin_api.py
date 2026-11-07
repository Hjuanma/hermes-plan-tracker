"""Plan Tracker backend routes - what the desktop pane reads.

Mounted by the Hermes server at ``/api/plugins/plan-tracker/`` and reached from
the pane as ``ctx.rest(path)``::

    GET  /health                       liveness + scanned roots
    GET  /plans                        all plans (summary list, roots + registry)
    GET  /plan?id=<id>                 one plan (detail + history, roots + registry)
    POST /registry                     add a file to the registry
    DELETE /registry?id=<id>           remove a file from the registry

Reads plan markdown files through ``core.py`` - no model calls, no network.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, PlainTextResponse, Response

_PLUGIN_ROOT = str(Path(__file__).resolve().parent.parent)
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

try:
    from core import (
        add_registry_entry,
        clear_block,
        export_plan_markdown,
        get_plan_summary,
        load_all_plans,
        load_plan_detail,
        load_registry,
        parse_plan,
        record_snapshot,
        remove_registry_entry,
        read_history,
        scan_roots,
        set_block,
        status_of,
        toggle_checkbox,
    )
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from core import (
        add_registry_entry,
        clear_block,
        export_plan_markdown,
        get_plan_summary,
        load_all_plans,
        load_plan_detail,
        load_registry,
        parse_plan,
        record_snapshot,
        remove_registry_entry,
        read_history,
        scan_roots,
        set_block,
        status_of,
        toggle_checkbox,
    )

router = APIRouter(tags=["plan-tracker"])

def _home() -> str:
    """Resolve HERMES_HOME per request: the gateway can switch profile at runtime."""
    return os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))


def _roots() -> List[Dict[str, Any]]:
    """Return the list of root directories with stats."""
    cwd = os.environ.get("PWD", "")
    dirs: List[str] = []
    dirs.append(str(Path(_home()) / "plans"))
    dirs.append(str(Path.home() / ".hermes" / "plans"))
    if cwd:
        dirs.append(str(Path(cwd) / ".hermes" / "plans"))
    result: List[Dict[str, Any]] = []
    for d in dirs:
        dp = Path(d)
        info = {"path": d, "exists": dp.is_dir(), "plans": 0}
        if info["exists"]:
            info["plans"] = len(list(dp.glob("*.md")))
        result.append(info)
    return result


@router.get("/health")
def health() -> Dict[str, Any]:
    """Liveness check with root directory info."""
    return {"ok": True, "roots": _roots()}


@router.get("/plans")
def plans(
    scope_arg: str = Query("all", alias="scope"),
    session_arg: str = Query("", alias="session"),
) -> Dict[str, Any]:
    """List all plans with summary progress, filtered by scope."""
    cwd = os.environ.get("PWD", "")
    all_plans = load_all_plans(
        _home(), cwd=cwd, scope=scope_arg, session=session_arg,
    )
    return {"plans": all_plans}


@router.get("/plan")
def plan_detail(
    pid: str = Query(..., alias="id"),
    scope_arg: str = Query("all", alias="scope"),
    session_arg: str = Query("", alias="session"),
) -> JSONResponse:
    """Detail for one plan by its id, filtered by scope."""
    cwd = os.environ.get("PWD", "")
    detail = load_plan_detail(
        _home(), pid, cwd=cwd,
        scope=scope_arg, session=session_arg,
    )

    if detail is not None:
        # Activity of the last 24 h, so the effort is visible before any
        # milestone flips and a per-milestone window exists.
        try:
            import time as _time

            from effort import attribute_effort

            now = _time.time()
            detail["recent"] = attribute_effort(_home(), pid, now - 86400, now)
        except Exception:
            detail["recent"] = {}
        return JSONResponse({"ok": True, "plan": detail})

    return JSONResponse({"ok": False, "error": f"plan not found: {pid}"}, status_code=404)


@router.post("/registry")
def registry_add(body: Dict[str, Any]) -> JSONResponse:
    """Add a file to the registry.

    Accepts ``{"path": "/abs/path.md", "scope": "global"|"session", "session": "..."}``.
    ``scope`` defaults to ``global``, ``session`` defaults to ``""``.
    Returns 200 on success, 400 on validation failure, 409 on duplicate.
    """
    path_str = (body or {}).get("path", "")
    if not path_str:
        return JSONResponse(
            {"ok": False, "error": "path is required"},
            status_code=400,
        )

    scope = (body or {}).get("scope", "global")
    session = (body or {}).get("session", "")

    ok, msg, plan_data = add_registry_entry(_home(), path_str, scope=scope, session=session)
    if not ok:
        if msg == "file already registered":
            return JSONResponse(
                {"ok": False, "error": msg},
                status_code=409,
            )
        return JSONResponse(
            {"ok": False, "error": msg},
            status_code=400,
        )

    return JSONResponse({"ok": True, "message": msg, "plan": plan_data})


@router.delete("/registry")
def registry_remove(
    pid: str = Query(..., alias="id"),
    scope_arg: Optional[str] = Query(None, alias="scope"),
    session_arg: Optional[str] = Query(None, alias="session"),
) -> JSONResponse:
    """Remove a file from the registry by plan id.

    Optional ``scope`` and ``session`` query params narrow the removal.
    """
    ok, msg = remove_registry_entry(
        _home(), pid, scope=scope_arg, session=session_arg,
    )
    if not ok:
        return JSONResponse(
            {"ok": False, "error": msg},
            status_code=404,
        )
    return JSONResponse({"ok": True, "message": msg})


@router.post("/toggle")
def toggle(body: Dict[str, Any]) -> JSONResponse:
    """Toggle a checkbox on a given line in a plan file.

    Accepts ``{"id": "<plan_id>", "line": <int>, "expect_sha": "<sha256>"}``.
    Returns 200 with ``{ok: true, sha: "<new_sha>"}`` on success,
    409 when the content hash is stale, or 400 on other errors.
    """
    pid = (body or {}).get("id", "")
    line = (body or {}).get("line", 0)
    expect_sha = (body or {}).get("expect_sha", "")

    if not pid:
        return JSONResponse(
            {"ok": False, "error": "id is required"},
            status_code=400,
        )
    if not isinstance(line, int) or line < 1:
        return JSONResponse(
            {"ok": False, "error": "line must be a positive integer"},
            status_code=400,
        )
    if not expect_sha:
        return JSONResponse(
            {"ok": False, "error": "expect_sha is required"},
            status_code=400,
        )

    ok, msg, new_sha = toggle_checkbox(_home(), pid, line, expect_sha)
    if not ok:
        if msg == "stale":
            return JSONResponse(
                {"ok": False, "error": "stale"},
                status_code=409,
            )
        return JSONResponse(
            {"ok": False, "error": msg},
            status_code=400,
        )

    return JSONResponse({"ok": True, "sha": new_sha})


@router.post("/block")
def block(body: Dict[str, Any]) -> JSONResponse:
    """Mark a milestone as blocked.

    Accepts ``{"id": "<plan_id>", "milestone": "<name>", "reason": "<...>"}``.
    ``reason`` defaults to ``"blocked"`` when omitted.
    """
    pid = (body or {}).get("id", "")
    milestone = (body or {}).get("milestone", "")
    reason = (body or {}).get("reason", "blocked")

    if not pid:
        return JSONResponse(
            {"ok": False, "error": "id is required"},
            status_code=400,
        )
    if not milestone:
        return JSONResponse(
            {"ok": False, "error": "milestone is required"},
            status_code=400,
        )

    set_block(_home(), pid, milestone, reason)
    return JSONResponse({"ok": True})


@router.delete("/block")
def unblock(
    pid: str = Query(..., alias="id"),
    milestone: str = Query(..., alias="milestone"),
) -> JSONResponse:
    """Remove a block from a milestone.
    """
    removed = clear_block(_home(), pid, milestone)
    if not removed:
        return JSONResponse(
            {"ok": False, "error": "block not found"},
            status_code=404,
        )
    return JSONResponse({"ok": True})


@router.get("/export")
def export_plan(
    pid: str = Query(..., alias="id"),
    scope_arg: str = Query("all", alias="scope"),
    session_arg: str = Query("", alias="session"),
) -> Response:
    """Export a plan's summary as markdown.

    Returns text/markdown on success, 404 when the plan is not found.
    """
    cwd = os.environ.get("PWD", "")
    md = export_plan_markdown(
        _home(), pid, cwd=cwd, scope=scope_arg, session=session_arg,
    )
    if md is None:
        return JSONResponse(
            {"ok": False, "error": f"plan not found: {pid}"},
            status_code=404,
        )
    return PlainTextResponse(content=md, media_type="text/markdown")