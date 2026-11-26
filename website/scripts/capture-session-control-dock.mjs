/**
 * Screenshot harness for an app's docked session control box (`dock`,
 * `stayOpen`): the REAL built SPA (website/dist) over a stubbed dashboard API,
 * with the docked example app (docs/app-kit/examples/docked-control-app)
 * installed and its module served from that folder. Scenes:
 *   wide        -- 1440x900: the box docked above the message box, a note typed,
 *                  then a click in the chat: the box stays.
 *   short-stack -- 1200x700 with a task bar and three queued messages: is the
 *                  whole box on screen inside the status stack's 50svh cap?
 *   phone       -- 390x844: the same control is the popover on its chip.
 *   cross       -- open docked with a note, narrow to 600 px (popover, note
 *                  kept), widen again (docked, note kept).
 * Each scene asserts what it shows and fails loudly otherwise. Nothing in CI
 * runs this file.
 *
 * Usage: node scripts/capture-session-control-dock.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/session-control-dock'
const SLOT = 'chat-docked-box'
const PROJECT = '/home/user/workspace/notes'
const APP = 'docked-notes-example'
const CHIP = `[data-session-control-key="${APP}:notes"]`
const MODULE = readFileSync(fileURLToPath(new URL('../../docs/app-kit/examples/docked-control-app/ui/dist/session-control.mjs', import.meta.url)), 'utf8')

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT, title: 'Plan the release notes', running: true, last_message: 'Drafting the notes.',
  messages: 12, agent: 'kirocrew', memory_mode: 'persistent', project: PROJECT, folder_id: '',
  modified: Math.floor(Date.now() / 1000), source_links: [], source_links_total: 0,
}]
const PARA = 'The release notes group the changes by what a person sees first: the chat, the composer, then Settings. Each line names the change in plain words and links the pull request that made it.'
const detail = {
  running: true, has_more: false, total: 12, queue: [], project: PROJECT,
  messages: Array.from({ length: 12 }, (_, i) => i % 2 === 0
    ? { role: 'user', ts: Date.now() / 1000 - (12 - i) * 60, content: `Question ${i / 2 + 1}: how should this part of the notes read?` }
    : { role: 'assistant', ts: Date.now() / 1000 - (12 - i) * 60, content: `${PARA}\n\n- Chat: replies stream as before\n- Composer: a docked box can stay open\n- Settings: no change` }),
}
const app = {
  name: APP, version: '0.1.0', displayName: 'Docked Notes Example', enabled: true, installedAt: '2026-11-25T00:00:00Z',
  manifest: {
    name: APP, version: '0.1.0', displayName: 'Docked Notes Example',
    contributes: { sessionControls: [{ id: 'notes', entryPoint: 'dist/session-control.mjs', label: 'Notes', icon: 'Star', dock: true, stayOpen: true }] },
    permissions: {},
  },
}
const FRAMES = {
  todo: { type: 'todo_update', data: { slot: SLOT, todo: { description: 'Release notes', current: 'Write the composer section', completed: 1, total: 4, tasks: [
    { id: 't1', text: 'Collect the merged pull requests', completed: true },
    { id: 't2', text: 'Write the composer section', completed: false },
    { id: 't3', text: 'Write the Settings section', completed: false },
    { id: 't4', text: 'Read it through once', completed: false },
  ] } } },
  q1: { type: 'queue_push', data: { slot: SLOT, content: 'Then add a line about the phone layout.', ts: new Date().toISOString(), queue_id: 'q-1' } },
  q2: { type: 'queue_push', data: { slot: SLOT, content: 'And keep each line under twenty words.', ts: new Date().toISOString(), queue_id: 'q-2' } },
  q3: { type: 'queue_push', data: { slot: SLOT, content: 'Last, list what is not in this release.', ts: new Date().toISOString(), queue_id: 'q-3' } },
}

const extra = async (path, route) => {
  if (path === '/api/apps') { await json(route, [app]); return true }
  if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
  return false
}

async function open(browser, viewport, frames = []) {
  const context = await browser.newContext({ viewport, deviceScaleFactor: 2 })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { slots, theme: 'dark', extra, localStorageEntries: { 'mc-active-slot': SLOT } })
  await page.route(`**/apps/${APP}/ui/**`, route => route.fulfill({ status: 200, contentType: 'text/javascript', body: MODULE }))
  await page.routeWebSocket(/\/api\/ws/, ws => { frames.forEach((k, i) => setTimeout(() => ws.send(JSON.stringify(FRAMES[k])), 1500 + i * 300)) })
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await page.locator(CHIP).waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(1500 + frames.length * 300 + 800)
  return { context, page }
}

const check = (label, ok, why) => { if (!ok) throw new Error(`${label}: ${why}`); console.log('ok', label) }
// After the box's slide-up has run, so a frame never catches it half-faded.
const shot = async (page, name) => { await page.waitForTimeout(600); await page.screenshot({ path: `${OUT}/${name}.png` }); console.log('wrote', `${OUT}/${name}.png`) }
const box = page => page.getByRole('region', { name: 'Notes — Docked Notes Example' })
const note = page => page.getByRole('textbox', { name: 'Notes for this chat' })

let base = ''

async function main() {
  const { srv, base: b } = await serveDist()
  base = b
  const browser = await chromium.launch()

  // -- wide: docked, stays open on a click in the chat --------------------------
  {
    const { context, page } = await open(browser, { width: 1440, height: 900 })
    await page.locator(CHIP).click()
    await box(page).waitFor({ state: 'visible' })
    await note(page).fill('Lead with the composer change; it is the one people will notice.')
    check('wide/in the status stack', await page.locator('[data-testid="composer-status-stack"]').locator('[data-session-control-dock]').count() === 1, 'the box is not in the status stack')
    check('wide/no popover', await page.getByRole('dialog').count() === 0, 'a popover is drawn too')
    await shot(page, 'wide-docked')
    await page.locator('.chat-container').click({ position: { x: 400, y: 200 } })
    await page.waitForTimeout(400)
    check('wide/stays open on a click in the chat', await box(page).isVisible(), 'the box closed')
    await context.close()
  }

  // -- short-stack: 700 px tall, a task bar and three queued rows ----------------
  {
    const { context, page } = await open(browser, { width: 1200, height: 700 }, ['todo', 'q1', 'q2', 'q3'])
    await page.locator(CHIP).click()
    await box(page).waitFor({ state: 'visible' })
    await page.waitForTimeout(600)
    const m = await page.evaluate(() => {
      const stack = document.querySelector('[data-testid="composer-status-stack"]')
      const dock = document.querySelector('[data-session-control-dock]')
      const s = stack.getBoundingClientRect()
      const d = dock.getBoundingClientRect()
      return {
        stackScroll: stack.scrollHeight - stack.clientHeight,
        boxTop: Math.round(d.top - s.top), boxBottom: Math.round(s.bottom - d.bottom),
        boxH: Math.round(d.height), stackH: Math.round(s.height), viewH: innerHeight,
        todo: !!document.querySelector('[data-testid="todo-pill"]'), queued: document.querySelectorAll('[data-testid="queue-card"]').length,
      }
    })
    console.log('short-stack', JSON.stringify(m))
    check('short-stack/task bar and queue raised', m.todo && m.queued >= 1, `todo ${m.todo}, queue cards ${m.queued}`)
    check('short-stack/box wholly inside the stack', m.boxTop >= 0 && m.boxBottom >= 0, `box sticks out of the stack (top ${m.boxTop}, bottom ${m.boxBottom})`)
    check('short-stack/stack does not scroll the box out', m.stackScroll <= 1, `the stack scrolls by ${m.stackScroll}px`)
    await shot(page, 'short-stack-700')
    // The same window with the module taller than the box's cap: the box is at
    // its full height, the hardest case for the stack's 50svh cap.
    await page.evaluate(() => { document.querySelector('[data-session-control-dock] textarea').rows = 40 })
    await page.waitForTimeout(600)
    const t = await page.evaluate(() => {
      const stack = document.querySelector('[data-testid="composer-status-stack"]')
      const dock = document.querySelector('[data-session-control-dock]')
      const s = stack.getBoundingClientRect()
      const d = dock.getBoundingClientRect()
      return { stackScroll: stack.scrollHeight - stack.clientHeight, boxTop: Math.round(d.top - s.top), boxBottom: Math.round(s.bottom - d.bottom), boxH: Math.round(d.height), stackH: Math.round(s.height) }
    })
    console.log('short-stack-tall', JSON.stringify(t))
    await shot(page, 'short-stack-700-tall')
    check('short-stack-tall/box wholly inside the stack', t.boxTop >= 0 && t.boxBottom >= 0, `box sticks out of the stack (top ${t.boxTop}, bottom ${t.boxBottom})`)
    check('short-stack-tall/stack does not scroll the box out', t.stackScroll <= 1, `the stack scrolls by ${t.stackScroll}px`)
    await context.close()
  }

  // -- phone: the popover ----------------------------------------------------------
  {
    const { context, page } = await open(browser, { width: 390, height: 844 })
    await page.locator(CHIP).click()
    const pop = page.getByRole('dialog', { name: 'Notes — Docked Notes Example' })
    await pop.waitFor({ state: 'visible' })
    check('phone/no docked box', await page.locator('[data-session-control-dock]').count() === 0, 'docked on a phone')
    await note(page).fill('On the phone this is the popover.')
    await shot(page, 'phone-popover')
    await page.locator('.chat-container').click({ position: { x: 100, y: 120 } })
    await page.waitForTimeout(400)
    check('phone/closes on an outside press', await pop.count() === 0, 'the popover stayed open')
    await context.close()
  }

  // -- cross: wide -> narrow -> wide with the box open -----------------------------
  {
    const { context, page } = await open(browser, { width: 1200, height: 800 })
    await page.locator(CHIP).click()
    await box(page).waitFor({ state: 'visible' })
    const text = 'Typed while docked: still here after the window narrows.'
    await note(page).fill(text)
    await page.setViewportSize({ width: 600, height: 800 })
    const pop = page.getByRole('dialog', { name: 'Notes — Docked Notes Example' })
    await pop.waitFor({ state: 'visible' })
    check('cross/narrow is the popover', await page.locator('[data-session-control-dock]').count() === 0, 'still docked')
    check('cross/narrow keeps the note', await note(page).inputValue() === text, 'the note was lost')
    await shot(page, 'cross-narrow')
    await page.setViewportSize({ width: 1200, height: 800 })
    await box(page).waitFor({ state: 'visible' })
    check('cross/wide again keeps the note', await note(page).inputValue() === text, 'the note was lost')
    check('cross/no popover left', await page.getByRole('dialog').count() === 0, 'a popover is left')
    await shot(page, 'cross-wide-again')
    await context.close()
  }

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
