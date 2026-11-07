"""Hermes plugin: plan-tracker - parse plan checkboxes and track progress.

A dockable desktop pane shows milestone progress bars and a status-bar chip
with the aggregate percentage.  v0.4 adds per-milestone snapshots on every
poll, effort attribution (time, tokens, cost, models, subagents) read from
state.db, ETA, week delta, and the pane UI for all of it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# Ensure this plugin's directory is on sys.path so core.py can be imported
_PLUGIN_ROOT = str(Path(__file__).resolve().parent)
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

from core import handle_plan_track  # type: ignore
from schemas import PLAN_TRACK_SCHEMA  # type: ignore

__all__ = ["register"]


def register(ctx: Any) -> None:
    """Wire the plugin into Hermes.

    v0.3 registers the ``plan_track`` agent tool with scope and session support
    for the registry of arbitrary markdown plan files.
    """
    ctx.register_tool(
        name="plan_track",
        toolset="plan-tracker",
        schema=PLAN_TRACK_SCHEMA,
        handler=handle_plan_track,
    )