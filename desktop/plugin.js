/**
 * Plan Tracker - desktop half.
 *
 * A right-rail pane (a tab beside the conversation) that shows every plan's
 * milestone progress bars
 * and a 40 px SVG sparkline of history, plus a status-bar chip with the
 * aggregate percentage.  v0.3 adds a Session/Global/All scope selector and
 * per-session plan isolation.
 *
 * Design notes:
 *   - theme variables only (``--ui-*``, ``--foreground``);
 *   - one visual idea per state: dim strikethrough = done, accent = active,
 *     faint = pending;
 *   - no JSX syntax: jsx()/jsxs() calls only;
 *   - imports limited to @hermes/plugin-sdk, react and react/jsx-runtime.
 */

import {
  cn,
  host,
  STATUSBAR_AREAS,
  Tip,
  useQuery,
  useQueryClient,
  useValue,
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useState } from 'react'

const ID = 'plan-tracker'

// ctx.rest captured at register-time.
let ctxRest = async () => {
  throw new Error('plan-tracker: not registered')
}
let getStorage = (key, fallback) => fallback
let setStorage = () => {}
// The pane is registered on demand so the chip can close it completely and
// bring it back: ctx.register returns the disposer that removes it.
let paneDisposer = null
let paneCtx = null
let paneOpen = true

function openPane() {
  if (paneDisposer || !paneCtx) return
  paneDisposer = paneCtx.register({
    id: 'plan-tracker-pane',
    area: 'panes',
    title: 'Plan',
    data: { placement: 'right', width: '330px' },
    render: () => jsx(PlanPane, {}),
  })
  paneOpen = true
}

function closePane() {
  if (paneDisposer) {
    try { paneDisposer() } catch (e) { /* already gone */ }
    paneDisposer = null
  }
  paneOpen = false
}

function togglePane() {
  if (paneOpen) closePane()
  else openPane()
  setStorage('paneOpen', paneOpen)
}

/* ---------------- helpers ---------------- */

function pct(done, total) {
  return total ? Math.round((done / total) * 100) : 0
}

/* ---------------- query ---------------- */

function usePlans(scope, session) {
  const queryKey = [ID, 'plans', scope, session || '']
  const qs = scope === 'all' || !scope
    ? '/plans'
    : `/plans?scope=${encodeURIComponent(scope)}&session=${encodeURIComponent(session || '')}`
  return useQuery({
    queryKey,
    queryFn: () => ctxRest(qs, { timeoutMs: 8000 }),
    refetchInterval: 3000,
    retry: 1,
  })
}

function useOnePlan(planId, scope, session) {
  const qs = scope === 'all' || !scope
    ? `/plan?id=${encodeURIComponent(planId || '')}`
    : `/plan?id=${encodeURIComponent(planId || '')}&scope=${encodeURIComponent(scope)}&session=${encodeURIComponent(session || '')}`
  return useQuery({
    queryKey: [ID, 'plan', planId || '', scope, session || ''],
    queryFn: () => ctxRest(qs, { timeoutMs: 8000 }),
    enabled: Boolean(planId),
    refetchInterval: 3000,
    retry: 1,
  })
}

/* ---------------- pieces ---------------- */

function SegmentedBar({ milestones }) {
  const total = milestones.reduce((s, m) => s + m.total, 0)
  if (!total) return null
  const segments = milestones.map((m) => ({
    name: m.name,
    width: Math.round((m.total / total) * 100),
    done: m.done,
    total: m.total,
    status: m.status,
  }))
  return jsx('div', {
    className: 'flex h-2 overflow-hidden rounded-full',
    children: segments.map((seg) =>
      jsx('div', {
        key: seg.name,
        className: cn(
          'h-full transition-all duration-500',
          seg.status === 'done'
            ? 'bg-(--ui-accent)'
            : seg.status === 'active'
              ? 'bg-(--ui-accent)/[0.5]'
              : 'bg-(--ui-stroke-secondary)/40',
        ),
        style: { width: `${seg.width}%` },
        title: `${seg.name}: ${seg.done}/${seg.total} (${pct(seg.done, seg.total)}%)`,
      }),
    ),
  })
}

function MilestoneRow({ name, done, total, percent, status, effort }) {
  const isDone = status === 'done'
  const isActive = status === 'active'
  const effortParts = []
  if (effort) {
    if (effort.seconds) {
      const mins = Math.round(effort.seconds / 60)
      effortParts.push(`${mins}m`)
    }
    if (effort.tokens_in) {
      effortParts.push(`${Math.round(effort.tokens_in / 1000)}k tok`)
    }
    if (effort.cost_usd && effort.cost_usd > 0.001) {
      effortParts.push(`$${effort.cost_usd.toFixed(2)}`)
    }
    const modelNames = Object.keys(effort.models || {})
    if (modelNames.length) {
      effortParts.push(modelNames.join('/'))
    }
    const subCount = (effort.sessions || []).filter((s) => s.subagent).length
    if (subCount) {
      effortParts.push(`+${subCount} sub`)
    }
  }
  return jsxs('div', {
    className: 'flex items-center gap-2 text-[0.6875rem] leading-5',
    children: [
      jsx('span', {
        className: cn(
          'min-w-0 flex-1 truncate',
          isDone
            ? 'text-(--ui-text-quaternary) line-through decoration-(--ui-stroke-secondary)'
            : isActive
              ? 'text-(--foreground) font-medium'
              : 'text-(--ui-text-tertiary)',
        ),
        children: name,
      }),
      jsx('span', {
        className: 'font-mono tabular-nums text-(--ui-text-quaternary)',
        children: `${done}/${total}`,
      }),
      jsx('span', {
        className: 'font-mono tabular-nums text-(--ui-text-quaternary)',
        children: `${percent}%`,
      }),
      effortParts.length
        ? jsx('span', {
            className: 'font-mono text-[0.5rem] text-(--ui-text-tertiary)',
            children: ` · ${effortParts.join(' · ')}`,
          })
        : null,
    ],
  })
}

function Sparkline({ history, week }) {
  if (!history || history.length < 2) return null
  const values = history.map((h) => h.done / Math.max(h.total, 1))
  const max = Math.max(...values, 0.01)
  const min = Math.min(...values, 0)
  const points = values.map((v) => Math.round((v / max) * 36))
  const w = Math.max(values.length - 1, 1)
  const coords = points.map((y, i) => ({
    x: Math.round((i / w) * 100),
    y: 38 - y,
  }))
  const d = coords
    .map((p, i) => `${i === 0 ? 'M' : 'L'}${p.x},${p.y}`)
    .join(' ')

  const minPct = Math.round(min * 100)
  const maxPct = Math.round(max * 100)
  const firstTs = history.length ? history[0].ts.slice(0, 10) : ''
  const lastTs = history.length ? history[history.length - 1].ts.slice(0, 10) : ''

  const weekParts = []
  if (week && week.milestones) {
    const totalDelta = Object.values(week.milestones).reduce((s, v) => s + v, 0)
    if (totalDelta > 0) {
      weekParts.push(`+${totalDelta} hitos esta semana`)
    }
  }

  return jsxs('div', {
    className: 'flex items-start gap-1',
    children: [
      jsxs('div', {
        className: 'flex flex-col text-[0.5rem] font-mono leading-3 text-(--ui-text-tertiary)',
        children: [
          jsx('span', { children: `${maxPct}%` }),
          jsx('span', { children: `${minPct}%` }),
        ],
      }),
      jsxs('div', {
        className: 'flex flex-col gap-0.5',
        children: [
          jsx('svg', {
            width: '100',
            height: '40',
            viewBox: '0 0 100 40',
            className: 'overflow-visible',
            children: jsx('path', {
              d,
              fill: 'none',
              stroke: 'var(--ui-accent)',
              strokeWidth: 1.5,
              strokeLinecap: 'round',
              strokeLinejoin: 'round',
            }),
          }),
          jsxs('div', {
            className: 'flex items-center justify-between text-[0.5rem] font-mono text-(--ui-text-tertiary)',
            children: [
              jsx('span', { children: firstTs }),
              jsx('span', { children: lastTs }),
            ],
          }),
          weekParts.length
            ? jsx('div', {
                className: 'text-[0.5rem] font-mono text-(--ui-accent)',
                children: weekParts.join(' '),
              })
            : null,
        ],
      }),
    ],
  })
}

function Empty({ error }) {
  return jsxs('div', {
    className: 'flex flex-col gap-2 px-2 py-6 text-center',
    children: [
      jsx('div', {
        className: 'font-mono text-[0.6875rem] tracking-widest text-(--ui-text-quaternary)',
        children: 'NO PLANS FOUND',
      }),
      jsx('div', {
        className: 'text-xs leading-5 text-(--ui-text-tertiary)',
        children: error
          ? 'Plan Tracker backend unreachable - enable the plugin in Capabilities.'
          : 'Write a markdown plan file with checkboxes in .hermes/plans/ and it appears here.',
      }),
    ],
  })
}

function PlanDetail({ plan, planId, qc, scope, session }) {
  const milestones = plan.milestones || []
  const tasks = plan.tasks || []
  const history = plan.history || []
  const pctVal = pct(plan.done, plan.total)
  const effortMap = plan.effort || {}
  const eta = plan.eta
  const week = plan.week
  const recent = plan.recent || null
  const sha = plan.sha || ''
  const blocks = plan.blocks || {}

  async function onToggleCheckbox(tline) {
    try {
      const res = await ctxRest('/toggle', {
        method: 'POST',
        body: { id: planId, line: tline, expect_sha: sha },
        timeoutMs: 8000,
      })
      if (res && res.ok) {
        qc.invalidateQueries({ queryKey: [ID] })
      } else if (res && res.error === 'stale') {
        qc.invalidateQueries({ queryKey: [ID] })
        host.notify({ kind: 'error', message: 'plan changed, retry' })
      }
    } catch (e) { /* ignore */ }
  }

  async function onExport() {
    try {
      const md = await ctxRest(`/export?id=${encodeURIComponent(planId)}`, { timeoutMs: 8000 })
      if (md && typeof md === 'string') {
        if (ctx.os && typeof ctx.os.writeClipboard === 'function') {
          await ctx.os.writeClipboard(md)
          host.notify({ kind: 'info', message: 'plan exported to clipboard' })
        } else {
          host.notify({ kind: 'info', message: 'clipboard unavailable' })
        }
      }
    } catch (e) {
      host.notify({ kind: 'error', message: 'export failed' })
    }
  }

  async function onBlockMilestone(mname, isBlocked) {
    if (isBlocked) {
      try {
        await ctxRest(`/block?id=${encodeURIComponent(planId)}&milestone=${encodeURIComponent(mname)}`, { method: 'DELETE', timeoutMs: 5000 })
        qc.invalidateQueries({ queryKey: [ID] })
      } catch (e) { /* ignore */ }
    } else {
      try {
        await ctxRest('/block', {
          method: 'POST',
          body: { id: planId, milestone: mname, reason: 'blocked' },
          timeoutMs: 5000,
        })
        qc.invalidateQueries({ queryKey: [ID] })
      } catch (e) { /* ignore */ }
    }
  }

  return jsxs('div', {
    className: 'flex flex-col gap-1.5',
    children: [
      jsxs('div', {
              className: 'flex items-center justify-between gap-2',
              children: [
                jsx('span', {
                  className: 'font-medium text-xs text-(--foreground) truncate',
                  children: plan.name,
                }),
                jsxs('span', {
                  className: 'font-mono text-[0.5625rem] text-(--ui-text-tertiary)',
                  children: [
                    eta
                      ? `ETA ${eta.date} \u00b7 ${eta.per_day}/d`
                      : 'no rate yet',
                  ],
                }),
                jsx('button', {
                  className: 'font-mono text-[0.5rem] px-1 rounded text-(--ui-text-quaternary) hover:text-(--ui-accent)',
                  title: 'export this plan as markdown',
                  onClick: () => onExport(),
                  children: 'export',
                }),
              ],
            }),
      recent && recent.sessions && recent.sessions.length
        ? jsx('div', {
            className: 'font-mono text-[0.5625rem] text-(--ui-text-quaternary)',
            children: `24h: ${recent.sessions.length} ses · ${Math.round(((recent.tokens_in || 0) + (recent.tokens_out || 0)) / 1000)}k tok · $${(recent.cost_usd || 0).toFixed(2)}`,
          })
        : null,
      jsx(SegmentedBar, { milestones }),
      jsxs('div', {
        className: 'flex items-center gap-2 font-mono text-[0.5625rem] text-(--ui-text-quaternary)',
        children: [
          jsx('span', { children: `${plan.done}/${plan.total}` }),
          jsx('span', { className: 'text-(--ui-text-tertiary)', children: 'complete' }),
          jsx('span', {
            className: 'font-mono tabular-nums text-(--ui-accent)',
            children: `${pctVal}%`,
          }),
        ],
      }),
      jsx('div', {
        className: 'flex flex-col gap-px',
        children: milestones.map((ms) => {
          const blocked = blocks[ms.name] || null
          const msBlocked = blocked ? blocked.reason : null
          return jsxs('div', {
            className: 'flex items-center gap-1',
            key: ms.name,
            children: [
              jsx('span', {
                className: cn(
                  'cursor-pointer font-mono text-[0.5rem]',
                  msBlocked ? 'text-(--ui-accent)' : 'text-(--ui-text-quaternary) opacity-40',
                ),
                title: msBlocked ? `blocked: ${msBlocked}` : 'click to block',
                onClick: () => onBlockMilestone(ms.name, Boolean(msBlocked)),
                children: msBlocked ? '\u25cf' : '\u25cb',
              }),
              jsx(MilestoneRow, { ...ms, key: ms.name, effort: effortMap[ms.name] }),
            ],
          })
        }),
      }),
      tasks.length
        ? jsx('div', {
            className: 'flex flex-col gap-px',
            children: tasks.map((t) => {
              const tline = t.line || 0
              return tline > 0
                ? jsxs('div', {
                    className: 'flex items-center gap-1 text-[0.6875rem] leading-5',
                    key: t.name,
                    children: [
                      jsx('span', {
                        className: 'cursor-pointer font-mono text-[0.5625rem]',
                        onClick: () => onToggleCheckbox(tline),
                        children: '\u2611',
                      }),
                      jsx('span', {
                        className: 'text-(--ui-text-tertiary)',
                        children: `${t.name} (${t.done}/${t.total})`,
                      }),
                    ],
                  })
                : jsx('span', {
                    className: 'text-[0.5625rem] text-(--ui-text-tertiary)',
                    children: `${t.name} (${t.done}/${t.total})`,
                    key: t.name,
                  })
            }),
          })
        : null,
      jsx('div', {
        className: 'mt-1',
        children: jsx(Sparkline, { history, week }),
      }),
    ],
  })
}

function AddRow({ qc, scope, session }) {
  const [inputPath, setInputPath] = useState('')
  const [errorMsg, setErrorMsg] = useState('')
  const [loading, setLoading] = useState(false)

  async function onSubmit() {
    const path = inputPath.trim()
    if (!path) {
      setErrorMsg('paste an absolute path first')
      return
    }
    setLoading(true)
    setErrorMsg('')
    try {
      // ctx.rest takes a plain object body; a JSON string reaches FastAPI as a
      // string and the route answers 422.
      const body = { path }
      if (scope === 'session' && session) {
        body.scope = 'session'
        body.session = session
      } else {
        body.scope = 'global'
      }
      const res = await ctxRest('/registry', {
        method: 'POST',
        body,
        timeoutMs: 8000,
      })
      if (res && res.ok) {
        setInputPath('')
        setErrorMsg('registered')
        host.notify({ kind: 'info', message: 'plan registered' })
        qc.invalidateQueries({ queryKey: [ID] })
      } else {
        const msg = (res && (res.error || res.message)) || 'registration failed'
        setErrorMsg(msg)
        host.notify({ kind: 'error', message: msg })
      }
    } catch (e) {
      setErrorMsg('request failed')
      const why = e && e.message ? e.message : 'request failed'
      host.notify({ kind: 'error', message: `add failed (POST /registry): ${why}` })
    } finally {
      setLoading(false)
    }
  }

  return jsxs('div', {
    className: 'flex flex-col gap-0.5',
    children: [
      jsxs('div', {
        className: 'flex items-center gap-1',
        children: [
          jsx('input', {
            className: 'flex-1 min-w-0 text-xs bg-transparent border-none text-(--ui-text-secondary)',
            placeholder: '/abs/path/to/plan.md',
            value: inputPath,
            onChange: (e) => { setInputPath(e.target.value); setErrorMsg('') },
            onKeyDown: (e) => { if (e.key === 'Enter') onSubmit() },
          }),
          jsx('button', {
            className: cn(
              'text-xs px-1.5 py-0.5 rounded text-(--ui-text-secondary)',
              loading ? 'opacity-50' : '',
            ),
            onClick: onSubmit,
            disabled: loading,
            children: loading ? '...' : 'add',
          }),
        ],
      }),
      jsx('div', {
        className: 'text-[0.5625rem] text-(--ui-text-quaternary) leading-4',
        children: `scope ${scope}${scope === 'session' ? ` (${session || 'no session id'})` : ''}${errorMsg ? ` - ${errorMsg}` : ''}`,
      }),
    ],
  })
}

function PlanRow({ plan, qc, scope, session, expanded, onToggle, detail }) {
  const registered = plan.registered
  const planScope = plan.scope || 'global'
  const badge = planScope === 'session' ? 'S' : 'G'
  const value = pct(plan.done, plan.total)
  return jsxs('div', {
    className: 'flex flex-col gap-0.5',
    children: [
      jsxs('div', {
        className: cn(
          'group flex items-center gap-1.5 rounded px-1 py-0.5 cursor-pointer',
          expanded ? 'bg-(--ui-accent)/[0.08]' : 'hover:bg-(--ui-stroke-secondary)/25',
        ),
        onClick: () => onToggle(plan.id),
        children: [
          jsx('span', {
            className: 'font-mono text-[0.5rem] text-(--ui-text-quaternary)',
            children: expanded ? 'v' : '>',
          }),
          jsx('span', {
            className: cn(
              'font-mono text-[0.5625rem] min-w-[1rem] text-center',
              planScope === 'session' ? 'text-(--ui-accent)' : 'text-(--ui-text-tertiary)',
            ),
            children: badge,
          }),
          jsx('span', {
            className: cn(
              'flex-1 min-w-0 truncate text-[0.6875rem] font-mono',
              expanded ? 'text-(--ui-text-primary)' : 'text-(--ui-text-secondary)',
            ),
            children: planScope === 'session' && plan.session
              ? `${plan.name} ·${String(plan.session).slice(-6)}`
              : plan.name,
          }),
          jsx('span', {
            className: 'font-mono text-[0.5625rem] tabular-nums text-(--ui-text-tertiary)',
            children: `${plan.done}/${plan.total} ${value}%`,
          }),
          registered
            ? jsx('button', {
                className: 'text-[0.5625rem] px-1 text-(--ui-text-quaternary) opacity-0 group-hover:opacity-100 hover:text-(--ui-accent)',
                onClick: (e) => {
                  if (e && e.stopPropagation) e.stopPropagation()
                  let query = `/registry?id=${encodeURIComponent(plan.id)}`
                  query += `&scope=${encodeURIComponent(planScope)}`
                  if (planScope === 'session' && plan.session) {
                    query += `&session=${encodeURIComponent(plan.session)}`
                  }
                  ctxRest(query, { method: 'DELETE', timeoutMs: 5000 })
                    .then(() => qc.invalidateQueries({ queryKey: [ID] }))
                    .catch(() => {})
                },
                children: 'unpin',
              })
            : null,
        ],
      }),
      jsx('div', {
        className: 'h-0.5 w-full overflow-hidden rounded-full bg-(--ui-stroke-secondary)/60',
        children: jsx('div', {
          className: 'h-full rounded-full bg-(--ui-accent)',
          style: { width: `${value}%` },
        }),
      }),
      expanded && detail ? detail : null,
    ],
  })
}

function ScopeSelector({ currentScope, onChange, hasSession }) {
  return jsx('select', {
    className: 'shrink-0 text-[0.625rem] bg-transparent border-none text-(--ui-text-secondary)',
    value: currentScope,
    title: 'where a pinned plan is tracked',
    onChange: (e) => onChange(e.target.value),
    children: [
      jsx('option', { value: 'session', children: hasSession ? 'Session' : 'Session (no id)' }),
      jsx('option', { value: 'global', children: 'Global' }),
    ],
  })
}

function PlanPane() {
  const qc = useQueryClient()
  // Subscribed, not read once: switching chats must re-render the pane now,
  // not on the next poll.
  const durableSession = useValue(host.state.focusedStoredSessionId)
  const activeSession = useValue(host.state.activeSessionId)
  const sessionId = durableSession || activeSession || ''
  const [pinScope, setPinScope] = useState(() => {
    const saved = getStorage('pinScope', '')
    if (saved === 'session' || saved === 'global') return saved
    return sessionId ? 'session' : 'global'
  })
  const [searchQuery, setSearchQuery] = useState('')

  const { data: allData, isError: plansError } = usePlans('all', '')
  const allPlans = (allData && allData.plans) || []
  const pinned = allPlans.filter((p) => {
    const mine = p.scope === 'global' || (p.scope === 'session' && p.session === sessionId)
    return mine && p.registered
  })
  // A plan can be tracked in several sessions at once: offer it whenever the
  // TARGET scope has no registration for it yet.
  const registeredInTarget = new Set(
    allPlans
      .filter((p) => p.registered && p.scope === pinScope && (pinScope === 'global' || p.session === sessionId))
      .map((p) => p.path),
  )
  const seenPaths = new Set()
  const unpinned = allPlans.filter((p) => {
    if (seenPaths.has(p.path)) return false
    seenPaths.add(p.path)
    return !registeredInTarget.has(p.path)
  })

  const [showAll, setShowAll] = useState(() => getStorage('showAll', false) === true)
  const baseList = showAll ? allPlans : pinned
  const filteredList = searchQuery
    ? baseList.filter((p) => {
        const q = searchQuery.toLowerCase()
        return p.name.toLowerCase().includes(q)
          || (p.path && p.path.toLowerCase().includes(q))
      })
    : baseList
  const [pinTarget, setPinTarget] = useState(null)
  const [pinning, setPinning] = useState(false)
  const [expandedId, setExpandedId] = useState(null)
  const expandedPlan = pinned.find((p) => p.id === expandedId) || null
  const detailScope = expandedPlan ? (expandedPlan.scope || 'global') : 'global'
  const detailSession = expandedPlan && expandedPlan.scope === 'session' ? expandedPlan.session : ''
  // Hooks must run unconditionally: a conditional useQuery here is React #310.
  const { data: detailData } = useOnePlan(expandedId, detailScope, detailSession)
  const detail = detailData && detailData.plan

  const toggle = (id) => setExpandedId(expandedId === id ? null : id)

  if (plansError || !allData) {
    return jsx('div', {
      className: 'flex h-full flex-col overflow-auto p-2',
      children: jsx(Empty, { error: plansError }),
    })
  }

  return jsxs('div', {
    className: 'flex h-full flex-col gap-1.5 overflow-auto p-2',
    children: [
      jsx(AddRow, { qc, scope: pinScope, session: pinScope === 'session' ? sessionId : '' }),
      unpinned.length
        ? jsxs('div', {
            className: 'flex items-center gap-1',
            children: [
              jsx('select', {
                className: 'flex-1 min-w-0 text-xs bg-transparent border-none text-(--ui-text-secondary)',
                value: pinTarget || unpinned[0].id,
                onChange: (e) => setPinTarget(e.target.value),
                children: unpinned.map((p) =>
                  jsx('option', { key: p.id, value: p.id, children: p.name }),
                ),
              }),
              jsx(ScopeSelector, {
                currentScope: pinScope,
                hasSession: Boolean(sessionId),
                onChange: (v) => { setPinScope(v); setStorage('pinScope', v) },
              }),
              jsx('button', {
                className: 'shrink-0 text-[0.5625rem] px-1 rounded text-(--ui-text-tertiary) hover:text-(--ui-accent)',
                title: 'pin this plan for tracking',
                onClick: async () => {
                  const target = unpinned.find((p) => p.id === (pinTarget || unpinned[0].id))
                  if (!target) return
                  const body = { path: target.path }
                  if (pinScope === 'session' && sessionId) {
                    body.scope = 'session'
                    body.session = sessionId
                  } else {
                    body.scope = 'global'
                  }
                  setPinning(true)
                  try {
                    const res = await ctxRest('/registry', { method: 'POST', body, timeoutMs: 8000 })
                    if (res && res.ok) {
                      host.notify({ kind: 'info', message: `pinned: ${target.name}` })
                      qc.invalidateQueries({ queryKey: [ID] })
                    } else {
                      host.notify({ kind: 'error', message: (res && (res.error || res.message)) || 'pin failed' })
                    }
                  } catch (e) {
                    const why = e && e.message ? e.message : 'request error'
                    host.notify({ kind: 'error', message: `pin failed (POST /registry): ${why}` })
                  } finally {
                    setPinning(false)
                  }
                },
                children: pinning ? '...' : 'pin',
              }),
            ],
          })
        : null,
      jsxs('div', {
        className: 'mt-1 flex flex-col gap-1 border-t border-(--ui-stroke-secondary)/50 pt-1',
        children: [
          jsxs('div', {
            className: 'flex items-center justify-between gap-1',
            children: [
              jsx('div', {
                className: 'font-mono text-[0.5625rem] tracking-widest text-(--ui-text-quaternary)',
                children: showAll
                  ? `ALL PLANS ${filteredList.length}/${allPlans.length}`
                  : `TRACKED ${filteredList.length}/${pinned.length}`,
              }),
              jsxs('div', {
                className: 'flex items-center gap-1',
                children: [
                  jsx('button', {
                    className: 'text-[0.5625rem] px-1 rounded text-(--ui-text-quaternary) hover:text-(--ui-accent)',
                    title: 're-read the plans from disk now',
                    onClick: () => qc.invalidateQueries({ queryKey: [ID] }),
                    children: 'refresh',
                  }),
                  jsx('button', {
                    className: 'text-[0.5625rem] px-1 rounded text-(--ui-text-quaternary) hover:text-(--ui-accent)',
                    title: showAll ? 'show only the plans tracked here' : 'show every plan, including other sessions',
                    onClick: () => {
                      const next = !showAll
                      setShowAll(next)
                      setStorage('showAll', next)
                    },
                    children: showAll ? 'tracked' : 'all',
                  }),
                ],
              }),
            ],
          }),
          jsx('input', {
            className: 'w-full text-xs bg-transparent border-none border-b border-(--ui-stroke-secondary)/40 px-1 py-0.5 mb-0.5 text-(--ui-text-secondary)',
            placeholder: 'filter by name or path ...',
            value: searchQuery,
            onChange: (e) => setSearchQuery(e.target.value),
          }),
          filteredList.length
            ? filteredList.map((p) =>
                jsx(PlanRow, {
                  plan: p,
                  qc,
                  scope: p.scope === 'session' ? 'session' : 'global',
                  session: p.scope === 'session' ? p.session : '',
                  expanded: p.id === expandedId,
                  onToggle: toggle,
                  detail: p.id === expandedId && detail
                    ? jsx(PlanDetail, { plan: detail, planId: p.id, qc, scope: detailScope, session: detailSession })
                    : null,
                  key: p.key || p.id,
                }),
              )
            : jsx(Empty, { error: false }),
        ],
      }),
    ],
  })
}

function Chip() {
  const durableSession = useValue(host.state.focusedStoredSessionId)
  const activeSession = useValue(host.state.activeSessionId)
  const sessionId = durableSession || activeSession || ''
  const { data: allData } = usePlans('all', '')
  const allPlans = (allData && allData.plans) || []
  const tracked = allPlans.filter((p) => {
    const mine = p.scope === 'global' || (p.scope === 'session' && p.session === sessionId)
    return mine && p.registered
  })

  // Notify when a tracked plan crosses to 100% (once per crossing)
  tracked.forEach((p) => {
    const prevPct = Number(getStorage(`prevPct:${p.id}`, -1))
    if (prevPct >= 0 && prevPct < 100 && p.percent === 100) {
      host.notify({ kind: 'info', message: `${p.name} complete!` })
    }
    if (p.percent !== prevPct) {
      setStorage(`prevPct:${p.id}`, p.percent)
    }
  })

  if (tracked.length === 0) {
    return jsx(Tip, {
      label: jsxs('div', {
        className: 'flex flex-col gap-0.5',
        children: [
          jsx('div', { className: 'text-[0.625rem]', children: 'no tracked plans' }),
          jsx('div', { className: 'text-[0.5625rem] opacity-60', children: 'pin one in the pane' }),
        ],
      }),
      children: jsx('span', {
        className: 'flex items-center gap-1.5 px-1.5 font-mono text-[0.6875rem] text-(--ui-text-tertiary) cursor-pointer hover:text-(--ui-accent)',
        onClick: () => togglePane(),
        children: '\u25a4 -',
      }),
    })
  }
  const total = tracked.reduce((s, p) => s + p.total, 0)
  const done = tracked.reduce((s, p) => s + p.done, 0)
  const pctVal = pct(done, total)

  const label = jsxs('div', {
    className: 'flex w-[240px] flex-col gap-0.5',
    children: [
      jsx('div', {
        className: 'font-mono text-[0.625rem]',
        children: `${tracked.length} tracked · ${done}/${total} · ${pctVal}%`,
      }),
      ...tracked.map((p) =>
        jsxs('div', {
          className: 'flex items-center gap-1.5 text-[0.625rem]',
          children: [
            jsx('span', {
              className: cn(
                'font-mono min-w-[0.75rem] text-center',
                p.scope === 'session' ? 'opacity-100' : 'opacity-60',
              ),
              children: p.scope === 'session' ? 'S' : 'G',
            }),
            jsx('span', {
              className: 'min-w-0 flex-1 truncate',
              children: p.name,
            }),
            jsx('span', {
              className: 'font-mono tabular-nums opacity-70',
              children: `${p.done}/${p.total} ${pct(p.done, p.total)}%`,
            }),
          ],
        }),
      ),
      jsx('div', {
        className: 'text-[0.5625rem] opacity-60',
        children: 'click to open or close the pane',
      }),
    ],
  })

  return jsx(Tip, {
    label,
    children: jsx('span', {
      className: 'flex items-center gap-1.5 px-1.5 font-mono text-[0.6875rem] text-(--ui-text-tertiary) cursor-pointer hover:text-(--ui-accent)',
      onClick: () => togglePane(),
      children: `\u25a4 ${pctVal}%`,
    }),
  })
}

/* ---------------- registration ---------------- */

export default {
  id: ID,
  name: 'Plan Tracker',
  register(ctx) {
    // ctx.rest is already scoped to /api/plugins/<id>; pass the route only.
    ctxRest = (path, opts) => ctx.rest(path, opts)
    getStorage = (key, fallback) => {
      try {
        return ctx.storage.get(key, fallback)
      } catch (e) {
        return fallback
      }
    }
    setStorage = (key, value) => {
      try {
        ctx.storage.set(key, value)
      } catch (e) { /* storage not available */ }
    }

    paneCtx = ctx
    paneOpen = getStorage('paneOpen', true) !== false
    if (paneOpen) openPane()

    ctx.register({
      id: 'plan-tracker-chip',
      area: STATUSBAR_AREAS.right,
      order: 44,
      render: () => jsx(Chip, {}),
    })
  },
}