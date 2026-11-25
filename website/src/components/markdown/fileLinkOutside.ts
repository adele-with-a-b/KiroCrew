import { canOpenFileCopy, canOpenFileInEditor, EDITOR_OPENABLE_EXTS, editorAcceptsPath, openFileCopy, openFileInEditor } from '../../lib/electron'
import { fetchFileRead } from '../../utils/fileReadQuery'
import { chatSlotParam, fileHtmlUrl, isAbsolutePath } from '../../utils/fileReadUrl'

/**
 * Where a click on a file link in rendered text (a path chip, a Markdown link
 * to a file, an Open file chip) opens it OUTSIDE the side panel, in the
 * default app for its type on the machine the reader sits at.
 * `dashboard.file_links_open_default_app` (on by default) makes that the plain
 * click; Alt+click (Option+click) takes the other one, and a type with no
 * route below keeps the side panel.
 *
 *   - A web page (`.html`, `.htm`). In the desktop app it opens as a COPY in
 *     the default browser, read through `/api/file-html` (the side panel's
 *     own whole-page route) and written by the shell inside a `sandbox=""`
 *     frame of its own (`electron/open-copy.js`), so it shows what the side
 *     panel shows, full size. The original is never handed to the browser,
 *     which would run its scripts. In a browser tab or on a phone there is no
 *     default app: the page opens in a new tab on `/api/file-html`, whose
 *     `CSP: sandbox` header sandboxes it there too; where the tab is refused
 *     (a desktop window that allows no popup), the side panel opens instead.
 *   - A text or image file of a type the shell opens (`EDITOR_OPENABLE_EXTS`):
 *     the REAL file through `dashboard:open-file` when the desktop app's Crew
 *     is on this machine; otherwise, in the desktop app, a copy read through
 *     `/api/file-read` (text) or `/api/file-raw` (images). Edits to a copy do
 *     not reach the original. A text read the gateway cut short, or decoded
 *     with replacement characters, is not copied.
 *   - Anything else (a PDF, a CSV, an SVG, a script, an unknown type), a link
 *     with a line number, and every file in a browser tab: the side panel.
 *
 * Every read a route makes names the chat the link is in (`slot`), as the
 * side panel's own read does, so the gateway refuses a chat that runs
 * remotely; a host that cannot say which chat (`slot` absent) gets no route
 * that reads, and the side panel opens.
 */

/** The shell-owned `dashboard.file_links_open_default_app`, registered by
 *  `useConfigFileLinkTarget`; read at click time because `MarkdownRenderer`
 *  renders outside any query provider in places. Off until the config says it
 *  is on, so a click before the config arrives, or after it failed to, opens
 *  the side panel. */
let outsideByDefault = false

export function setFileLinksOpenOutside(on: boolean): void {
  outsideByDefault = on
}

export function fileLinksOpenOutside(): boolean {
  return outsideByDefault
}

export type OutsideRoute = 'page-copy' | 'page-tab' | 'file' | 'file-copy'

const PAGE_EXTS = new Set(['.html', '.htm'])
const IMAGE_EXTS = new Set(['.png', '.jpg', '.jpeg', '.gif', '.webp'])

function fileName(path: string): string {
  return path.split(/[\\/]/).pop() || ''
}

function extOf(path: string): string {
  const base = fileName(path)
  const dot = base.lastIndexOf('.')
  return dot > 0 ? base.slice(dot).toLowerCase() : ''
}

/** How this window opens `path` outside the side panel, or null to keep it
 *  there. `slot` is the chat the link is in; without one no route that reads
 *  through the gateway is offered. */
export function outsideRoute(path: string, slot?: string): OutsideRoute | null {
  const ext = extOf(path)
  if (PAGE_EXTS.has(ext)) {
    if (!isAbsolutePath(path) || !slot) return null
    return canOpenFileCopy() ? 'page-copy' : 'page-tab'
  }
  if (!EDITOR_OPENABLE_EXTS.has(ext)) return null
  if (canOpenFileInEditor() && editorAcceptsPath(path)) return 'file'
  if (canOpenFileCopy() && isAbsolutePath(path) && slot) return 'file-copy'
  return null
}

/** The bytes a copy is made of, read through the side panel's own routes for
 *  the chat `slot`, or null when the gateway refused or handed over less than
 *  the file as written (cut short, or decoded with replacement; a read with
 *  its credentials redacted is copied as redacted). */
async function copyBytes(path: string, route: 'page-copy' | 'file-copy', slot: string): Promise<Uint8Array | null> {
  if (route === 'file-copy' && !IMAGE_EXTS.has(extOf(path))) {
    const read = await fetchFileRead(path, undefined, slot)
    if (!read.ok || read.binary || read.truncated || read.lossy) return null
    return new TextEncoder().encode(read.text)
  }
  const url = route === 'page-copy'
    ? fileHtmlUrl(path, String(Date.now()))
    : '/api/file-raw?path=' + encodeURIComponent(path)
  if (!url) return null
  const res = await fetch(url + chatSlotParam(slot))
  return res.ok ? new Uint8Array(await res.arrayBuffer()) : null
}

/** Files whose open is still under way: a second click on one while it is
 *  joins the first, so a click gives one result, never a notice now and the
 *  file later as well. */
const inFlight = new Set<string>()

/**
 * Open `path` by `route` for the chat `slot`. `onFail` runs when it could not
 * be opened there, so the caller can open it in the side panel instead. A new
 * tab is opened before anything is awaited, inside the click that asked for
 * it; `window.open` answers null where the tab is refused (a blocked popup, a
 * desktop window that denies them), which is a failure like any other.
 */
export async function openOutside(path: string, route: OutsideRoute, onFail: () => void, slot?: string): Promise<void> {
  if (route === 'page-tab') {
    const url = slot ? fileHtmlUrl(path, String(Date.now())) : null
    // No `noopener` feature: it makes `window.open` answer null even when the
    // tab opened. The opener is cut by hand instead; the page there runs no
    // script that could use it anyway (`CSP: sandbox`).
    const tab = url ? window.open(url + chatSlotParam(slot), '_blank') : null
    if (tab) tab.opener = null
    else onFail()
    return
  }
  if (inFlight.has(path)) return
  inFlight.add(path)
  try {
    if (route === 'file') {
      if (!(await openFileInEditor(path)).ok) onFail()
      return
    }
    const bytes = slot ? await copyBytes(path, route, slot) : null
    if (!bytes || !(await openFileCopy(fileName(path), bytes)).ok) onFail()
  } catch {
    onFail()
  } finally {
    inFlight.delete(path)
  }
}
