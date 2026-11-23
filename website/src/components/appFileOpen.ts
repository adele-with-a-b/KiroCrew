/**
 * The host side of `useOpenFile` (`app-sdk/openFile.ts`): the opener an app's
 * surface is handed. `AppHost` and `SessionControlHost` both mount
 * `AppFileOpenRoot` around the app's own module and nothing else.
 *
 * It opens nothing itself. A served call goes to `revealOrOpen(path, 'open')`,
 * the one route the chat's own "Open with default app" row takes, so the path
 * rules are `POST /api/reveal`'s (`is_sensitive_path`, a regular file, a
 * direct-local browser), and `useCanOpenFile` is the same gate that row reads.
 * What this module adds is what an app must hold before that route is asked:
 * the owner's approval of `permissions.openFiles`, an absolute path, and a click.
 *
 * The click grant is the one a link-preview card's `openPanelTab` spends
 * (`linkPreview/linkPreviewFrames.ts`): a real click (`isTrusted`) anywhere in the
 * app's own React tree is good for one open, for `CLICK_GRANT_MS` after it. It is
 * not tied to the path the app then opens. The root records it with
 * `onClickCapture`, so it is recorded before the app's own click handler runs,
 * and React carries it through the app's portals (a `Modal`, a menu) as it does
 * every event. Host chrome stays outside the root: a host's retry button or
 * loading notice never grants.
 *
 * @module components/appFileOpen
 */
import { createElement, useCallback, useRef, type MouseEvent as ReactMouseEvent, type ReactNode } from 'react'
import { CLICK_GRANT_MS } from '../linkPreview/linkPreviewFrames'
import { AppFileOpenScope, type OpenFile, type OpenFileResult } from '../app-sdk/openFile'
import { revealOrOpen, useCanOpenFile } from './FilePathMenu'
import { isTrustedClick } from './trustedClick'

/** Longest path an app may hand over, in UTF-16 units. */
export const MAX_OPEN_PATH_CHARS = 4096

/** A URL scheme at the start of a string (`https:`, `javascript:`, `C:`). */
const SCHEME = /^[a-z][a-z0-9+.-]*:/i
/** C0 controls and DEL: never part of a path a person clicked. */
const CONTROL = /[\u0000-\u001f\u007f]/
/** A `..` segment in a URL as written, before the parser collapses it: `\` is a
 *  separator in a `file:` URL, and `%2e` is a dot. */
const URL_DOT_DOT = /(^|[/\\])(\.|%2e){2}([/\\?#]|$)/i

/**
 * The absolute local path `input` names, or null.
 *
 * Takes an absolute POSIX path, or a `file://` URL of one on this machine (no
 * host, or `localhost`), percent-decoded. Refuses a relative path (`file:` with
 * no `//` too), `~`, any other scheme, a `..` segment in any spelling, control
 * characters and anything over `MAX_OPEN_PATH_CHARS`. Shape only: which absolute
 * paths are never opened is the gateway's sensitive-path owner's to say, on the
 * route.
 */
export function readOpenFilePath(input: unknown): string | null {
  if (typeof input !== 'string' || !input || input.length > MAX_OPEN_PATH_CHARS) return null
  // Before any parsing: the URL parser drops a tab or a newline, which would join a
  // `.<TAB>.` into a `..` it then collapses.
  if (CONTROL.test(input)) return null
  let path = input
  if (/^file:/i.test(input)) {
    // The URL parser makes `file:look.html` absolute and drops `..`, so the URL is
    // read as written first.
    if (!/^file:\/\//i.test(input) || URL_DOT_DOT.test(input.slice('file://'.length))) return null
    let url: URL
    try {
      url = new URL(input)
    } catch {
      return null
    }
    if (url.host && url.hostname !== 'localhost') return null
    if (url.search || url.hash) return null
    try {
      path = decodeURIComponent(url.pathname)
    } catch {
      return null
    }
  } else if (SCHEME.test(input)) {
    return null
  }
  // Again on the decoded path: `%0a` decodes to a newline.
  if (!path.startsWith('/') || CONTROL.test(path)) return null
  if (path.split('/').includes('..')) return null
  return path
}

/**
 * A one-use click grant: `take()` is true once per click `record` saw, within
 * `CLICK_GRANT_MS` of it. `record` is the root's `onClickCapture`.
 */
export function useClickGrant(): { record: (e: ReactMouseEvent) => void; take: () => boolean } {
  const at = useRef<number | null>(null)
  const record = useCallback((e: ReactMouseEvent) => {
    if (isTrustedClick(e)) at.current = performance.now()
  }, [])
  const take = useCallback(() => {
    const when = at.current
    at.current = null
    if (when === null) return false
    const age = performance.now() - when
    return age >= 0 && age <= CLICK_GRANT_MS
  }, [])
  return { record, take }
}

/**
 * The opener for one app's surface. `granted` is the app row's
 * `openFilesGrant.approved`; `takeClick` the surface's click grant.
 */
export function useAppFileOpener({ granted, takeClick }: {
  granted: boolean
  takeClick: () => boolean
}): OpenFile {
  const canOpen = useCanOpenFile('file')
  return useCallback(async (input: string): Promise<OpenFileResult> => {
    if (!granted) return { ok: false, reason: 'not_granted' }
    const path = readOpenFilePath(input)
    if (!path) return { ok: false, reason: 'not_absolute' }
    if (!takeClick()) return { ok: false, reason: 'no_click' }
    if (!canOpen) return { ok: false, reason: 'unavailable' }
    let message: string | undefined
    const outcome = await revealOrOpen(path, 'open', { onError: (m) => { message = m } })
    if (outcome === 'done') return { ok: true }
    const reason = outcome === 'copied' ? 'unavailable' : outcome
    return message ? { ok: false, reason, message } : { ok: false, reason }
  }, [granted, takeClick, canOpen])
}

/**
 * Host only: wraps the app's own module, and nothing of the host's, so its clicks
 * grant and its `useOpenFile` is served. `display: contents`, so the app lays out
 * as it would without it.
 */
export function AppFileOpenRoot({ granted, children }: { granted: boolean; children: ReactNode }) {
  const { record, take } = useClickGrant()
  const open = useAppFileOpener({ granted, takeClick: take })
  return createElement(
    'div',
    { 'data-app-file-open-root': '', style: { display: 'contents' }, onClickCapture: record },
    createElement(AppFileOpenScope, { open, children }),
  )
}
