/**
 * Capture harness for "Needs you" (capture/sidebar-needs-you.html): the Sessions
 * panel over fifteen made-up chats in five folders, four waiting, in dark and
 * light: the filter off and on, Harbor Conductor answered with the filter on and
 * off, and one more waiting chat in no folder with the filter off and on.
 *
 * Usage:
 *   node capture/shoot-sidebar-needs-you.mjs <outDir> [port]
 *
 * Environment:
 *   CHROMIUM_PATH  — override the Chromium executable (default: Playwright's own)
 */
import { createServer } from 'vite'
import { chromium } from 'playwright-core'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

// vite.config.ts reads ./package.json, so run from the website folder.
process.chdir(fileURLToPath(new URL('..', import.meta.url)))
// The page needs no gateway, and the dev proxy must never reach a running one:
// point it at the discard port, where nothing listens, unless the caller names one.
process.env.KIROCREW_PORT ||= '9'

const OUT = process.argv[2]
if (!OUT) throw new Error('usage: shoot-sidebar-needs-you.mjs <outDir> [port]')
const PORT = Number(process.argv[3] || 5291)
mkdirSync(OUT, { recursive: true })

const server = await createServer({
  configFile: 'vite.config.ts',
  server: { port: PORT, strictPort: true, host: '127.0.0.1' },
})
await server.listen()
console.log(`vite up on ${PORT}`)

/** Five made-up folders, answered over the real `/api/chat/folders` read. */
const FOLDERS = [
  { id: 'f-harbor', name: 'Harbor', color: '#3b82f6', collapsed: false, order: 0 },
  { id: 'f-lumen', name: 'Lumen', color: '#8b5cf6', collapsed: false, order: 1 },
  { id: 'f-quill', name: 'Quill', color: '#14b8a6', collapsed: true, order: 2 },
  { id: 'f-pebble', name: 'Pebble', color: '#ec4899', collapsed: false, order: 3 },
  { id: 'f-sundial', name: 'Sundial', color: '#22c55e', collapsed: true, order: 4 },
]

const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined, headless: true })

async function shoot(theme, filter, name, { menu = false, answered = 0, unfiled = 0 } = {}) {
  // Desktop width (a phone width draws the phone layout); the frame is cropped to the panel.
  const ctx = await browser.newContext({ viewport: { width: 1100, height: 2000 }, deviceScaleFactor: 2, reducedMotion: 'reduce' })
  const page = await ctx.newPage()
  // No gateway: every API read answers empty, the way the sidebar tests mock it,
  // so the frame shows the rows and not a sign-in notice.
  // The gateway's paths only: Vite serves the client's own /src/api/ modules too.
  await page.route(url => url.pathname.startsWith('/api/'), route => route.fulfill({
    status: 200, contentType: 'application/json',
    body: new URL(route.request().url()).pathname === '/api/chat/folders' ? JSON.stringify(FOLDERS) : '[]',
  }))
  await page.goto(`http://127.0.0.1:${PORT}/capture/sidebar-needs-you.html?theme=${theme}&filter=${filter}&answered=${answered}&unfiled=${unfiled}`)
  await page.waitForSelector('[data-capture-ready]', { timeout: 60000 })
  await page.waitForSelector('[data-session-row]', { timeout: 60000 })
  // The folders arrive on their own read; with the filter off the frame waits for the last one.
  if (!filter) await page.waitForSelector('[data-folder-row="f-sundial"]', { timeout: 60000 })
  await page.waitForTimeout(800)
  if (menu) {
    await page.click('button[aria-label="Sort and filter sessions"]')
    await page.waitForSelector('[data-testid="filter-needs-you"]', { timeout: 10000 })
    await page.waitForTimeout(300)
  }
  const box = menu
    // The menu's filter section, as the design draws it.
    ? await page.evaluate(() => {
      const m = document.querySelector('[role="menu"]').getBoundingClientRect()
      const sep = document.querySelector('[role="menu"] [role="separator"]')
      const bottom = sep ? sep.getBoundingClientRect().top : m.top + 360
      return { x: m.x, y: m.y, width: m.width, height: bottom - m.y }
    })
    : await page.evaluate(() => {
      const tab = document.querySelector('[data-capture-tab]').getBoundingClientRect()
      const lane = document.querySelector('[data-testid="tree-view-lane"]')
      // The Sessions card itself (`LIST_SHELL_CLS` carries the `sidebar` hook).
      const side = lane.closest('.sidebar').getBoundingClientRect()
      // The frame ends where the lane's last row or note ends.
      const marks = lane.querySelectorAll('[data-session-row], [data-folder-row], [data-testid^="folder-waits-more-"], [data-testid="needs-you-hidden-note"]')
      const last = marks.length ? marks[marks.length - 1].getBoundingClientRect().bottom : side.bottom
      return { x: 0, y: tab.y, width: side.right, height: Math.min(side.bottom, last + 16) - tab.y }
    })
  await page.screenshot({ path: join(OUT, `${name}.png`), clip: box })
  console.log(`  shot ${name}.png`)
  await ctx.close()
}

for (const theme of ['dark', 'light']) {
  await shoot(theme, 0, `${theme}-filter-off`)
  await shoot(theme, 1, `${theme}-filter-on`)
  await shoot(theme, 1, `${theme}-answered-filter-on`, { answered: 1 })
  await shoot(theme, 0, `${theme}-answered-filter-off`, { answered: 1 })
  await shoot(theme, 0, `${theme}-unfiled-filter-off`, { unfiled: 1 })
  await shoot(theme, 1, `${theme}-unfiled-filter-on`, { unfiled: 1 })
}
await shoot('dark', 1, 'dark-menu', { menu: true })

await browser.close()
await server.close()
console.log(`\nFrames written to ${OUT}`)
