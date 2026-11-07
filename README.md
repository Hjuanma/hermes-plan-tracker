# Plan Tracker for Hermes

Track the markdown plans you already write. Plan Tracker reads the checkboxes in
your `.md` plan files and shows milestone progress in a dockable pane plus a
status-bar chip, with per-milestone effort, an ETA and a history chart.

No new format to learn: if a file has headings and `- [ ]` checkboxes, it is a
plan.

```text
▤ 37%                                  <- status-bar chip, click to open/close

TRACKED 2/7                    refresh  all
> G  AI Agent Roadmap…   92/248 37%   unpin
v S  Mi plan de migración 3/8 37%    unpin
   ▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬
   Fase 1 - Inventario   2/3  66%   42m · 18k tok · $0.12 · sonnet-4.5 +2 sub
   Fase 2 - Migración    1/3  33%
   Fase 3 - Cutover      0/2   0%
   [history chart]  ETA 12 dic · 3.2 t/día · +3 hitos esta semana
```

## What it does

- **Milestones from headings.** The milestone level is detected per file: the
  shallowest heading level (H1-H4) whose sections hold checkboxes. A document
  title is never a milestone. A plan written with `## Phase` works as well as one
  written with `# Workstream`.
- **Scopes.** A plan is tracked globally or in one session, and the same file can
  be tracked in several sessions at once. The pane follows the focused chat.
- **Pin / unpin** from the pane, or with the `plan_track` agent tool.
- **Tick a task from the pane.** The only write path, guarded by a content hash:
  if the file changed since the pane read it, the write is refused and the pane
  refreshes instead of overwriting.
- **Blocked milestones** with a reason, stored in a sidecar, never in your plan.
- **Effort per milestone**: time, tokens, cost, models and subagents, attributed
  from Hermes' own session store over the window in which the milestone moved.
- **ETA and week delta** from the recorded history.
- **Weights** (optional) so a milestone can count more than another.
- **Export** the current state as markdown.

## Install

```bash
hermes plugins install <catalog-name>     # from the catalog
hermes plugins enable plan-tracker
```

Then, in the Desktop app: **Settings -> Plugins -> Plan Tracker -> Desktop** on.
The desktop half is opt-in, like every unified package.

## Usage

1. Open the **Plan** tab in the right rail (or click the `▤` chip).
2. Pick a plan in the dropdown and press **pin**, choosing `Session` or `Global`
   as the target.
3. Expand a tracked plan to see its milestones, tick tasks, block a milestone,
   read the effort and export.

Plans are discovered in `$HERMES_HOME/plans/`, `$HERMES_DEFAULT_HOME/plans/`
(default `~/.hermes/plans/`) and `<cwd>/.hermes/plans/`. Anything else can be
pinned by absolute path, or added to `PLAN_TRACKER_ROOTS`.

### Agent tool

`plan_track` with `add` (path, optional scope), `remove` (id or path) and `list`.
When the agent calls it inside a chat, the plan lands in that session's scope
unless `scope="global"` is passed.

### Weights

Optional YAML frontmatter at the top of the plan:

```markdown
---
weights:
  Phase 1: 3
  Phase 2: 1
---
```

A weighted milestone contributes its weight to the bar instead of its checkbox
count, and the global percentage becomes the weighted average. Milestones without
a weight keep their checkbox count.

## Compatible with `writing-plans` and `executing-plans`

A plan written by the `writing-plans` skill needs no conversion:

| In the plan | Becomes |
|---|---|
| `# [Feature] Implementation Plan` | the document title (never a milestone) |
| `### Task N: [name]` | one milestone per task |
| `- [ ] **Step N**` | one task inside that milestone |

Milestones are the shallowest heading level that holds checkboxes, with one
refinement: a heading that only *contains* deeper sections (a lone
`## Global Constraints` wrapping every task) owns no checkbox of its own and the
parser descends past it. Without that rule the whole plan collapsed into a single
milestone called after the container.

`executing-plans` ticks the plan's checkboxes as each task closes, so the pane
moves with the execution and nothing has to be re-imported. Register the plan
once at the start (`plan_track` with `action="add"`) and it follows the run.

Plans are found automatically in `~/.hermes/plans/` and, inside a repository,
`docs/superpowers/plans/` (the `writing-plans` default) - the latter only when the
plugin is asked about the current working directory.

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `HERMES_HOME` | `~/.hermes` | Read per request, so a gateway profile switch is picked up |
| `HERMES_DEFAULT_HOME` | `~/.hermes` | Second plan root (`<home>/plans`) |
| `PLAN_TRACKER_ROOTS` | empty | Extra plan directories, `os.pathsep` separated |

## Disclosures

What this plugin reads and writes, so you can decide before installing:

- **Reads your plan files** (`.md`) wherever they live, and **writes to them in
  exactly one case**: ticking or unticking a checkbox from the pane. That write
  changes one character, is atomic, and is refused when the file changed since
  the pane last read it (content hash). Nothing else in a plan file is ever
  modified.
- **Reads Hermes' session store** (`state.db`, and `profiles/*/state.db`) to
  attribute effort: session model, token counts, estimated cost, message
  timestamps and whether a session is a subagent. Read-only, opened with
  `mode=ro`. It never writes to `state.db`.
- **Stores its own state** under `$HERMES_HOME/plugin-data/plan-tracker/`:
  the registry of tracked plans, per-plan history snapshots, effort events and
  blocked milestones.
- **No network calls, no shell commands, no subprocesses, no model calls, no
  stored credentials.** It does not read your API keys.

Effort attribution is temporal, not causal: it sums the activity of the window
between two snapshots, so two plans that moved in the same window each receive
that activity. It is labelled as activity in the window, not as exact cost.

## Requirements

- Hermes `>=0.23.0` (the desktop half uses `ctx.storage`, `Tip`, `useValue` and
  `ctx.os.writeClipboard`).
- The Desktop app for the pane; the backend and the `plan_track` tool work
  without it.

## Development

```bash
python3 -m unittest discover -s tests -v
node --test tests/desktop.test.mjs
hermes plugins validate . --install-deps
```

The Python suite must run with an interpreter that has FastAPI, or the API tests
skip silently. Parsing is tested against the bundled fixture
`tests/fixtures/roadmap.md`, so the suite needs no personal files.

To run the invariant checks against one of your own plans:

```bash
PLAN_TRACKER_REAL_PLAN=~/path/to/plan.md python3 -m unittest discover -s tests
```

## License

MIT. See [LICENSE](LICENSE).
