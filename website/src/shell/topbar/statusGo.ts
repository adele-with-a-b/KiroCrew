/**
 * Where an app's status item can send the operator: the rows of its card and
 * its one primary "go" (`contributes.statusItems`, the route's `rows[]` and
 * `go`).
 *
 * A target names a chat (`sid`) and, optionally, a side-panel tab the SAME app
 * declares (`panelTab`) with a JSON value for it (`arg`). Going there switches
 * to that chat the way any "go to chat" button does, then hands the tab its
 * value through the panel-tab request the link-preview cards already use, so
 * the tab reads it with `usePanelTabRequest()` like any other open. The chat it
 * left is kept, so the chat header can offer the way back.
 *
 * Every field comes from a third-party app's route, so it is untrusted: this
 * module is the one place it is checked. Nothing here knows any app.
 *
 * @module shell/topbar/statusGo
 */
import { useCallback, useSyncExternalStore } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'

import { isCrewLinkKey } from '../../lib/crewDeepLink'
import { useGuardedLeave } from '../../components/NavigationLeaveGuard'
import { useAppDispatch, useAppSelector, useAppStore } from '../../store'
import { api } from '../../api/client'
import { resumeFromHistory, setSwitchSlotGone, switchSlot } from '../../store/chatSlice'
import { isChatRoute } from '../nav/routeActive'
import { sessionDisplayTitle } from '../../utils/sessionDisplayTitle'

/** Most rows a card draws. The card is a glance, not a page: an app with more
 *  says so in its heading and keeps the rest on its own tab. */
export const MAX_STATUS_ROWS = 10
/** Grapheme caps per row field: the age, the chat name, the question. */
export const MAX_ROW_LEAD = 12
export const MAX_ROW_TEXT = 48
export const MAX_ROW_DETAIL = 160
/** The card's heading and the go button's label. */
export const MAX_STATUS_HEADING = 48
export const MAX_GO_LABEL = 24
/** Largest `arg` kept, as serialized JSON. A larger one is dropped and the chat still opens. */
export const MAX_TARGET_ARG_BYTES = 4096

const TAB_ID_RE = /^[a-z0-9][a-z0-9-]{0,63}$/

export type StatusTone = 'ok' | 'warn' | 'danger' | 'muted'
const TONES = new Set<string>(['ok', 'warn', 'danger', 'muted'])

/** A place a status item can send the operator. */
export interface StatusTarget {
  /** The chat's session key, as `/chat?sid=` takes it. */
  sid: string
  /** A side-panel tab id of the app that answered. */
  panelTab?: string
  /** What that tab is opened with. */
  arg?: unknown
}

export interface StatusRow {
  lead: string
  text: string
  detail: string
  tone: StatusTone
  /** Absent when the app's target failed the check: the row is drawn, not clickable. */
  target?: StatusTarget
}

export interface StatusGo {
  /** Empty = the host's own "Go" label. */
  label: string
  target: StatusTarget
}

/** Strip what could reorder or hide text (the Unicode C category and the
 *  line/paragraph separators), keeping the zero-width joiner and non-joiner
 *  that Bengali, Hindi and emoji sequences need. */
const STRIP = /(?![\u200c\u200d])[\p{C}\p{Zl}\p{Zp}]/gu
const graphemes = typeof Intl !== 'undefined' && 'Segmenter' in Intl
  ? new Intl.Segmenter(undefined, { granularity: 'grapheme' })
  : null

/**
 * The one cleaner for every string a status route answers: strip as above,
 * trim, then cap at `max` grapheme clusters so a character never splits.
 * `Infinity` strips without a cap.
 */
export function cleanStatusText(raw: unknown, max: number): string {
  if (typeof raw !== 'string') return ''
  const text = raw.replace(STRIP, '').trim()
  if (!Number.isFinite(max)) return text
  const parts = graphemes ? Array.from(graphemes.segment(text), g => g.segment) : Array.from(text)
  return parts.slice(0, max).join('')
}

export function normalizeStatusTone(raw: unknown): StatusTone {
  return typeof raw === 'string' && TONES.has(raw) ? (raw as StatusTone) : 'muted'
}

/** A JSON copy of `arg` when it is small enough to keep, else undefined. */
function keepArg(arg: unknown): unknown {
  if (arg === undefined) return undefined
  let json: string | undefined
  try {
    json = JSON.stringify(arg)
  } catch {
    return undefined
  }
  if (json === undefined || new TextEncoder().encode(json).length > MAX_TARGET_ARG_BYTES) return undefined
  return JSON.parse(json) as unknown
}

/** The checked target, or null when it names no chat. */
export function normalizeStatusTarget(raw: unknown): StatusTarget | null {
  if (!raw || typeof raw !== 'object') return null
  const r = raw as { sid?: unknown; panelTab?: unknown; arg?: unknown }
  if (!isCrewLinkKey(r.sid)) return null
  const out: StatusTarget = { sid: r.sid }
  // The tab must be one this app declares; that is checked where the
  // declarations are known (the chat page's panel-tab request). Here only its
  // shape: a bad id opens the chat alone.
  if (typeof r.panelTab === 'string' && TAB_ID_RE.test(r.panelTab)) {
    out.panelTab = r.panelTab
    const arg = keepArg(r.arg)
    if (arg !== undefined) out.arg = arg
  }
  return out
}

export function normalizeStatusRows(raw: unknown): StatusRow[] {
  if (!Array.isArray(raw)) return []
  const out: StatusRow[] = []
  for (const r of raw) {
    if (out.length >= MAX_STATUS_ROWS) break
    if (!r || typeof r !== 'object') continue
    const row = r as { lead?: unknown; text?: unknown; detail?: unknown; tone?: unknown; target?: unknown }
    const text = cleanStatusText(row.text, MAX_ROW_TEXT)
    const detail = cleanStatusText(row.detail, MAX_ROW_DETAIL)
    if (!text && !detail) continue
    const item: StatusRow = { lead: cleanStatusText(row.lead, MAX_ROW_LEAD), text, detail, tone: normalizeStatusTone(row.tone) }
    const target = normalizeStatusTarget(row.target)
    if (target) item.target = target
    out.push(item)
  }
  return out
}

export function normalizeStatusGo(raw: unknown): StatusGo | null {
  if (!raw || typeof raw !== 'object') return null
  const g = raw as { label?: unknown; target?: unknown }
  const target = normalizeStatusTarget(g.target)
  return target ? { label: cleanStatusText(g.label, MAX_GO_LABEL), target } : null
}

/**
 * `?status=<app>:<item>`: the status item whose card a chat URL opens, or null.
 * Same grammar as the item key (`${appName}:${item.id}`).
 */
export function parseStatusCardParam(raw: string | null | undefined): string | null {
  if (typeof raw !== 'string' || raw.length > 129) return null
  return /^[a-z0-9][a-z0-9_-]{0,63}:[a-z0-9][a-z0-9-]{0,63}$/.test(raw) ? raw : null
}

// ---- The go in flight, and the way back ------------------------------------

/** The latest go: where it went, where it left, and the tab request still to hand over. */
export interface ActiveStatusGo {
  to: string
  /** The chat the go left, or null when it left a page that is not a chat. */
  from: string | null
  appName: string
  /** Not yet handed to the landed chat's panel. Cleared once it was taken. */
  pending: { tab: string; arg: unknown } | null
  /** True while a closed chat is being reopened: another chat on screen is
   *  then not a walk-away yet. */
  opening?: boolean
}

let active: ActiveStatusGo | null = null
const listeners = new Set<() => void>()

function emit(): void {
  for (const fn of [...listeners]) fn()
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn)
  return () => { listeners.delete(fn) }
}

export function getActiveStatusGo(): ActiveStatusGo | null {
  return active
}

/** Replace (or clear) the go in flight. Exported for the chat page and tests. */
export function setActiveStatusGo(next: ActiveStatusGo | null): void {
  if (next === active) return
  active = next
  emit()
}

export function useActiveStatusGo(): ActiveStatusGo | null {
  return useSyncExternalStore(subscribe, getActiveStatusGo, getActiveStatusGo)
}

/**
 * `go(appName, target)`: switch to the target's chat (behind the unsaved-draft
 * guard every "go to chat" button uses) and queue its tab request. A target in
 * the chat already on screen keeps no way back: nothing was left.
 */
export function useStatusGo(): (appName: string, target: StatusTarget) => void {
  const dispatch = useAppDispatch()
  const store = useAppStore()
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const leave = useGuardedLeave()
  return useCallback((appName: string, target: StatusTarget) => {
    leave(() => {
      const here = store.getState().chat.activeSlot
      const onChat = isChatRoute(pathname)
      // A second go inside the chat the last one landed in left nothing new:
      // it keeps that go's way back.
      const prev = getActiveStatusGo()
      const stay = onChat && here === target.sid && prev?.to === here && !prev.opening
      const from = onChat && here && here !== target.sid ? here : (stay ? prev.from : null)
      // Not an open tab (closed, or older than the list): reopen it the way the
      // command palette does, which may give the tab another key.
      const reopen = here !== target.sid && !store.getState().dashboard.slots.some(r => r.key === target.sid)
      const go: ActiveStatusGo = {
        to: target.sid,
        from,
        appName,
        pending: target.panelTab ? { tab: target.panelTab, arg: target.arg } : null,
        ...(reopen ? { opening: true } : {}),
      }
      setActiveStatusGo(go)
      if (reopen) {
        // Said where the operator is looking, as an open tab that 404s is.
        const fail = (kind: 'gone' | 'failed') => {
          if (getActiveStatusGo() !== go) return
          setActiveStatusGo(null)
          dispatch(setSwitchSlotGone({ name: '', kind }))
        }
        // Only a chat with a transcript is reopened: a resume of a key with
        // none starts a new, empty chat. The probe answers the transcript's
        // own key and title, which is what a resume takes.
        void api.sessionMeta(target.sid).then(row => {
          if (getActiveStatusGo() !== go) return
          if (!row) return fail('gone')
          return dispatch(resumeFromHistory({ key: row.key, title: row.title || '' })).unwrap().then(r => {
            if (getActiveStatusGo() !== go) return
            if (!r.ok) return fail('failed')
            setActiveStatusGo({ ...go, to: r.key || go.to, opening: false })
          })
        }).catch(() => fail('failed'))
      } else if (here !== target.sid) {
        dispatch(switchSlot({ key: target.sid, announceOnMissing: true, gesture: true }))
      }
      if (!onChat) navigate('/chat')
    })
  }, [dispatch, leave, navigate, pathname, store])
}

/** The way back from a go, while its chat is on screen. */
export interface StatusGoBack {
  /** The left chat's title, as its sidebar row reads. */
  title: string
  back: () => void
}

/**
 * The way back to the chat a go left, or null: no go, the go left no chat, or
 * the chat on screen is not the one it went to. Back switches to that chat and
 * ends the go. On a computer the browser's Back (or ⌘←) reaches the same chat,
 * because the switch pushed a history entry; a phone replaces it
 * (`shouldReplaceSessionUrl`), so there this is the way back.
 */
export function useStatusGoBack(): StatusGoBack | null {
  const go = useActiveStatusGo()
  const dispatch = useAppDispatch()
  const leave = useGuardedLeave()
  const activeSlot = useAppSelector(state => state.chat.activeSlot)
  const fromRow = useAppSelector(state => (go?.from ? state.dashboard.slots.find(r => r.key === go.from) : undefined))
  const from = go && go.from && go.to === activeSlot ? go.from : null
  const back = useCallback(() => {
    if (!from) return
    leave(() => {
      setActiveStatusGo(null)
      dispatch(switchSlot({ key: from, announceOnMissing: true, gesture: true }))
    })
  }, [dispatch, from, leave])
  if (!from) return null
  return { title: sessionDisplayTitle({ key: from, title: fromRow?.title ?? '' }), back }
}

// ---- The primary go, for the global key ------------------------------------

type PrimaryGo = () => void
let primary: PrimaryGo | null = null

/**
 * The status bar registers the go its first item with one offers (bar order),
 * or null. The key handler fires it. Returns the undo, which clears only its
 * own registration, so a bar unmounting never drops another bar's go.
 */
export function setPrimaryStatusGo(fn: PrimaryGo | null): () => void {
  primary = fn
  return () => { if (primary === fn) primary = null }
}

/** Fire the primary go; false when no item offers one. */
export function firePrimaryStatusGo(): boolean {
  if (!primary) return false
  primary()
  return true
}
