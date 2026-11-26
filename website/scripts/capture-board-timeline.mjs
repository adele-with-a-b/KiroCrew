/**
 * Screenshot harness for the Board's Timeline view (the right bar's third
 * position): the lanes over the last hours, the Conductor's own lane with its
 * longest quiet stretch, the window chips and the legend.
 *
 * Runs the REAL built SPA (website/dist) gateway-free behind the shared
 * stubDashboardApi fixtures, opens a conductor chat on the Board tab, picks
 * Timeline, and answers `GET /api/crew-board` and `GET /api/crew-board/timeline`.
 *
 * With a directory argument, both routes answer with the BYTES A REAL GATEWAY
 * RETURNED: `board.json` and `timeline-6.json` / `timeline-12.json` /
 * `timeline-24.json`, as saved from a running Crew. Without one, the built-in
 * fixture below stands in, so the frames can be regenerated with no gateway.
 *
 * Usage (in website/, after `npm run build`):
 *   node scripts/capture-board-timeline.mjs [outDir] [prefix] [fromDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync, existsSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = resolve(process.argv[2] || '../temp-screenshots/board-timeline')
const PREFIX = process.argv[3] || 'after'
const FROM = process.argv[4] ? resolve(process.argv[4]) : ''
mkdirSync(OUT, { recursive: true })

const ROOT = 'chat-root'
const NOW = Date.now()
const HOUR = 3_600_000
const at = (h) => new Date(NOW - h * HOUR).toISOString()

const CATALOG = {
  agents: [
    { name: 'kirocrew', selection_kind: 'template', kiro_agent: '', workspace: 'default', memory_store: 'default', source: 'builtin' },
    { name: 'kirocrew-conductor', selection_kind: 'template', kiro_agent: '', workspace: 'default', memory_store: 'default', source: 'builtin' },
  ],
  default_agent: 'kirocrew',
  crew_mode_agent: 'kirocrew-conductor',
  conductor_agents: ['kirocrew-conductor'],
}

const slots = [
  { key: ROOT, title: 'Board views', messages: 2, running: false, agent: 'kirocrew-conductor', agent_kind: 'template', crew_mode_root: true, mode: '', origin: 'user', memory_mode: 'persistent', created: at(13), last_ts: at(0.02), folder_id: '', workspace: 'default' },
]

const item = (id, title, over) => ({
  schema: 1, item_id: id, title, acceptance: { kind: 'human_approval' }, state: 'open', verdict: null,
  decision: '', round: 1, fails: 0, status: 'progress', reason: null, summary: '', artifacts: {}, pr: null,
  last_report_at: at(1), created_at: at(12), closed_at: null, decided_at: null, orphaned: false, stale: false,
  acceptance_concrete: true, report_undelivered: null, outstanding: false, terminal: false, alive: 'idle',
  events: [], awaits_person: false, has_worker: true, in_backlog: false, ...over,
})

/** The stand-in board: one story per move, so every lane colour is in the frame. */
const BOARD = {
  conductor: { schema: 1, slot_key: ROOT, goal: 'Ship the new board views.', round: 1, depth: 0, parent_item: null, created_at: at(13) },
  conductor_alive: 'idle',
  take_over_available: false,
  items: [
    item('it_gate', 'Port the gate', { status: 'done', summary: 'Gate ported, PR is up.', pr: 118, last_report_at: at(0.67) }),
    item('it_rows', 'Fix the rows', { status: 'question', reason: 'needs_human', awaits_person: true, outstanding: true, summary: 'Should an empty section stay on the board or go?', last_report_at: at(2) }),
    item('it_menu', 'Draft the menu', { alive: 'running', summary: 'All twelve drafted, checking the counts.', last_report_at: at(1.1) }),
    item('it_copy', 'Trim the copy', { status: 'done', verdict: 'pending', summary: 'Copy trimmed, PR is up.', pr: 119, last_report_at: at(4.2) }),
    item('it_list', 'Join the lists', { status: null, has_worker: false, last_report_at: null, created_at: at(5.9) }),
  ],
}

const line = (kind, h, status = null) => ({ ts: at(h), kind, status })

/**
 * The stand-in window: the same lines the lanes above were drawn from.
 *
 * Only sequences a real log can hold. The store coalesces CONSECUTIVE `progress`
 * reports and keeps the newest (`work_ledger_store._coalesce_progress`), so a run
 * with no status change in it leaves exactly one progress line, however long it ran.
 */
const EVENTS = {
  it_gate: [line('bind', 12.6), line('report', 8.5, 'progress'), line('report', 0.67, 'done')],
  it_rows: [line('bind', 11.4), line('report', 8, 'progress'), line('report', 2, 'question')],
  it_menu: [line('create', 10.2), line('bind', 9.6), line('report', 1.1, 'progress')],
  it_copy: [line('create', 9.1), line('bind', 8.4), line('report', 7, 'blocked'), line('decision', 6.6), line('report', 4.2, 'done'), { ...line('verdict', 4.1), verdict: 'pending' }],
  it_list: [line('create', 5.9)],
  it_files: [line('report', 11.5, 'done'), { ...line('verdict', 10.2), verdict: 'pass' }, line('close', 10.1)],
}

const timeline = (hours) => ({
  read_at: new Date(NOW).toISOString(),
  since: at(hours),
  hours,
  events: Object.fromEntries(Object.entries(EVENTS).map(([id, lines]) => [
    id,
    // The route sends the window's lines plus the newest one before it.
    lines.filter(l => Date.parse(l.ts) >= NOW - hours * HOUR),
  ]).filter(([, lines]) => lines.length)),
})

/** A body a real gateway returned, when this run was given a directory. */
const real = (name) => {
  const path = join(FROM, name)
  if (!FROM) return null
  if (!existsSync(path)) throw new Error(`${path} is missing: run the capture against a Crew that answered it`)
  return JSON.parse(readFileSync(path, 'utf8'))
}

const BOARD_BODY = real('board.json') ?? BOARD
const WINDOWS = Object.fromEntries([6, 12, 24].map(h => [h, real(`timeline-${h}.json`) ?? timeline(h)]))
const CONDUCTOR = BOARD_BODY.conductor?.slot_key || ROOT

const extra = async (path, route) => {
  if (path === '/api/agents/catalog') { await json(route, CATALOG); return true }
  if (path === '/api/crew-board') { await json(route, BOARD_BODY); return true }
  if (path === '/api/crew-board/timeline') {
    const hours = Number(new URL(route.request().url()).searchParams.get('hours')) || 12
    await json(route, WINDOWS[hours] ?? WINDOWS[12])
    return true
  }
  if (path === `/api/chat/slots/${ROOT}/crew-workers`) {
    await json(route, { root: ROOT, workers: [
      { key: 'w-menu', title: 'menu-worker', agent: 'kirocrew-worker', status: 'working', group: 'working', last_activity: at(1.1), ledger_item: BOARD_BODY.items?.[2]?.item_id || 'it_menu', created_by: ROOT },
    ] })
    return true
  }
  if (path === `/api/chat/slots/${ROOT}`) {
    await json(route, { messages: [
      { role: 'user', content: 'Build the board views with a crew.', ts: at(13), meta: { mid: 'r-1' } },
      { role: 'assistant', content: 'Five stories are open.', ts: at(12.9), meta: { mid: 'r-2' } },
    ], has_more: false, total: 2 })
    return true
  }
  if (path === '/api/sessions') { await json(route, { sessions: [], has_more: false }); return true }
  if (path === '/api/chat/pins') { await json(route, { pins: [] }); return true }
  return false
}

/** One frame: the Board tab on Timeline, at `width`, in `theme`. */
async function frame(browser, base, { theme, width, height, name, hours }) {
  const context = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    slots, theme, extra,
    localStorageEntries: {
      'mc-theme-mode': theme, 'mc-active-slot-chat': ROOT, 'mc-lang': 'en',
      'mc-privacy-notice-v1': '1', 'mc-preview-crew-mode': '1',
      // The right bar at the width the Board is designed for.
      'mc-side-panel-width': '460',
    },
  })
  await page.goto(`${base}/chat?sid=${ROOT}`, { waitUntil: 'domcontentloaded' })
  await page.getByTestId('conductor-mode-view-timeline').click({ timeout: 20000 })
  await page.getByTestId('board-timeline-lanes').waitFor({ timeout: 20000 })
  if (hours && hours !== 12) await page.getByTestId(`board-timeline-window-${hours}`).click()
  // Wait for the read to land: a frame of the empty lanes proves nothing.
  await page.locator('[data-testid="board-timeline-story"] ~ div [data-span]').first().waitFor({ timeout: 20000 })
  await page.waitForTimeout(600)
  // The theme SEED is not the theme RENDERED; a pair of frames in one mode is no pair.
  const rendered = await page.evaluate(() => document.documentElement.getAttribute('data-theme') || '')
  if (!rendered.includes(theme)) throw new Error(`${name}: shell rendered theme ${rendered}`)
  const spans = await page.locator('[data-span]').evaluateAll(els => els.map(e => e.getAttribute('data-span')))
  const shown = await page.getByTestId('board-timeline').getAttribute('data-hours')
  console.log(`${name}: hours=${shown} spans=${spans.join(',')}`)
  const shot = page.getByTestId('conductor-mode-crew-panel')
  await shot.screenshot({ path: `${OUT}/${PREFIX}-${name}.png` })
  await context.close()
}

const { srv, base } = await serveDist()
const { LD_LIBRARY_PATH: _mise, ...env } = process.env
// Headless, said out loud: a capture must never open a window on the machine.
const browser = await chromium.launch({ env, headless: true })
try {
  console.log(`conductor ${CONDUCTOR}${FROM ? ` · bodies from ${FROM}` : ' · built-in fixture'}`)
  for (const theme of ['dark', 'light']) {
    await frame(browser, base, { theme, width: 1400, height: 1100, name: `bar-${theme}` })
    await frame(browser, base, { theme, width: 390, height: 900, name: `phone-${theme}` })
  }
  await frame(browser, base, { theme: 'dark', width: 1400, height: 1100, name: 'bar-dark-24h', hours: 24 })
} finally {
  await browser.close()
  srv.close()
}
