/**
 * The answering mode's messages between a link-preview card and the dashboard.
 *
 * Frame to dashboard, `answering { op, invocation, ..., request? }` (read by
 * `readAnsweringMessage`, which drops anything else):
 *
 * - `enter { place?, hint?, keys?, buttons?, keepAt?, question? }` put the card's chat
 *   into the answering mode. Only a pinned card of a provider the owner approved for
 *   `answering` (and `sessionKey`), right after the person's click or key in it.
 * - `update { place?, hint?, buttons?, keepAt?, question? }` change what the mode
 *   shows, and which question (the card's own key, never text) the card is at.
 * - `hand { text, count, rev }` the message the answers so far make, and how many
 *   answers it holds, built from the answer line's text number `rev`.
 * - `leave` end the mode; nothing is sent.
 *
 * Dashboard to frame: `answering { event }` (an `AnsweringEvent`), and
 * `answered { request, ok, reason? }` for an `enter`, `update` or `hand`.
 *
 * No message here sends anything: a frame can hand text over and start or end the
 * mode, never press. The send is the person's Shift+Enter, or a Shift+click on
 * Crew's own button, received by the dashboard's page (`AnsweringBar.tsx`).
 *
 * @module linkPreview/answering/answeringProtocol
 */
import { displaySafe } from '../../appFrame/frameProtocol'

/** Most keys a mode may declare, buttons it may ask Crew to draw, and answers one send counts. */
export const MAX_ANSWERING_KEYS = 8
export const MAX_ANSWERING_BUTTONS = 4
export const MAX_ANSWERS = 999
/** Longest place text (the mode line's "Q4a · 2 of 3"), answer line hint, and label. */
export const MAX_PLACE_CHARS = 40
export const MAX_HINT_CHARS = 120
export const MAX_LABEL_CHARS = 32
/** Longest question key (`AnsweringSetup.question`), as a button's id. */
export const MAX_QUESTION_KEY_CHARS = 64
/** Largest answering message, as JSON text: a handed message is up to 32,000 characters. */
export const MAX_ANSWERING_MESSAGE_BYTES = 40_000

/** A key the mode hands the card: Tab, an arrow up or down (each with Shift or
 *  not), or a letter or digit with Ctrl or Alt (and Shift or not). Enter,
 *  Shift+Enter and Escape are Crew's, and Meta is never declared. Written
 *  `Ctrl+Alt+Shift+Key` in that order, the key in upper case when a letter. */
export const DECLARED_KEY_RE = /^(?:(?:Shift\+)?(?:Tab|ArrowUp|ArrowDown)|(?:Ctrl\+Alt\+|Ctrl\+|Alt\+)(?:Shift\+)?[A-Z0-9])$/

/** The modifier prefixes of a key name, in `DECLARED_KEY_RE`'s order (Ctrl, Alt, Shift). */
const MODIFIER_NAMES = ['Ctrl+', 'Alt+', 'Shift+'] as const

export interface DeclaredKey { key: string; label: string }
export interface DeclaredButton { id: string; label: string; hint?: string }

export interface AnsweringSetup {
  place: string
  hint: string
  keys: DeclaredKey[]
  buttons: DeclaredButton[]
  /** Where Crew's own "keep, next" button sits among `buttons`, or null for none. */
  keepAt: number | null
  /** The card's own key for the question it is at; '' or absent for none. A key
   *  only: never shown, never sent, never put in Crew's line. Crew keeps the
   *  words the person kept in its line under it (`AnsweringMode.showKept`). */
  question?: string
}

export type AnsweringRefusal =
  | 'not_shown' | 'not_pinned' | 'no_click' | 'not_approved' | 'not_on_screen'
  | 'not_granted' | 'busy' | 'not_on' | 'not_literal' | 'too_large' | 'bad_count' | 'stale'

export type EndReason = 'sent' | 'escaped' | 'left' | 'chat' | 'closed'

export type AnsweringEvent =
  | { kind: 'kept'; text: string; rev: number }
  | { kind: 'draft'; text: string; rev: number }
  | { kind: 'key'; key: string }
  | { kind: 'button'; id: string }
  | { kind: 'ended'; why: EndReason }

export type AnsweringMessage =
  | { type: 'answering'; op: 'enter'; invocation: number; setup: AnsweringSetup; request?: number }
  | { type: 'answering'; op: 'update'; invocation: number; setup: Partial<AnsweringSetup>; request?: number }
  | { type: 'answering'; op: 'hand'; invocation: number; text: unknown; count: unknown; rev: number; request?: number }
  | { type: 'answering'; op: 'leave'; invocation: number }

/** A short shown string: cleaned, trimmed, and dropped (to '') when over `max`. */
function shortText(raw: unknown, max: number): string {
  if (typeof raw !== 'string') return ''
  const s = displaySafe(raw).replace(/\s+/g, ' ').trim()
  return s.length <= max ? s : ''
}

function readKeys(raw: unknown): DeclaredKey[] {
  const out: DeclaredKey[] = []
  for (const k of Array.isArray(raw) ? raw.slice(0, MAX_ANSWERING_KEYS) : []) {
    if (!k || typeof k !== 'object') continue
    const q = k as Record<string, unknown>
    if (typeof q.key !== 'string' || !DECLARED_KEY_RE.test(q.key) || out.some(d => d.key === q.key)) continue
    out.push({ key: q.key, label: shortText(q.label, MAX_LABEL_CHARS) })
  }
  return out
}

function readButtons(raw: unknown): DeclaredButton[] {
  const out: DeclaredButton[] = []
  for (const b of Array.isArray(raw) ? raw.slice(0, MAX_ANSWERING_BUTTONS) : []) {
    if (!b || typeof b !== 'object') continue
    const q = b as Record<string, unknown>
    const id = typeof q.id === 'string' && q.id && q.id.length <= 64 ? q.id : ''
    const label = shortText(q.label, MAX_LABEL_CHARS)
    if (!id || !label || out.some(d => d.id === id)) continue
    const hint = shortText(q.hint, 12)
    out.push(hint ? { id, label, hint } : { id, label })
  }
  return out
}

function readKeepAt(raw: unknown, buttons: number): number | null | undefined {
  if (raw === null) return null
  if (typeof raw === 'number' && Number.isInteger(raw)) return Math.max(0, Math.min(buttons, raw))
  return undefined
}

/** The setup fields `m` carries; with `whole`, every field with its default. */
function readSetup(m: Record<string, unknown>, whole: boolean): Partial<AnsweringSetup> {
  const out: Partial<AnsweringSetup> = {}
  if (whole || 'place' in m) out.place = shortText(m.place, MAX_PLACE_CHARS)
  if (whole || 'hint' in m) out.hint = shortText(m.hint, MAX_HINT_CHARS)
  if (whole) out.keys = readKeys(m.keys)
  if (whole || 'buttons' in m) out.buttons = readButtons(m.buttons)
  const keepAt = readKeepAt(m.keepAt, (out.buttons ?? []).length || MAX_ANSWERING_BUTTONS)
  if (keepAt !== undefined) out.keepAt = keepAt
  else if (whole) out.keepAt = 0
  // A key, as a button's id is: anything else names no question.
  if ('question' in m) out.question = typeof m.question === 'string' && m.question.length <= MAX_QUESTION_KEY_CHARS ? m.question : ''
  return out
}

/** The answering message in a tagged frame message `m`, or null. */
export function readAnsweringMessage(m: Record<string, unknown>, invocation: number): AnsweringMessage | null {
  const request = typeof m.request === 'number' && Number.isSafeInteger(m.request) && m.request >= 0 ? { request: m.request } : {}
  switch (m.op) {
    case 'enter':
      return { type: 'answering', op: 'enter', invocation, setup: readSetup(m, true) as AnsweringSetup, ...request }
    case 'update':
      return { type: 'answering', op: 'update', invocation, setup: readSetup(m, false), ...request }
    case 'hand': {
      const rev = typeof m.rev === 'number' && Number.isSafeInteger(m.rev) && m.rev >= 0 ? m.rev : -1
      return { type: 'answering', op: 'hand', invocation, text: m.text, count: m.count, rev, ...request }
    }
    case 'leave':
      return { type: 'answering', op: 'leave', invocation }
    default:
      return null
  }
}

/** The canonical name of a key press (`DECLARED_KEY_RE`'s form), or null when it
 *  is not one a mode may declare. */
export function keyName(e: { key: string; code: string; ctrlKey: boolean; altKey: boolean; shiftKey: boolean; metaKey: boolean }): string | null {
  if (e.metaKey) return null
  // A letter or digit by its place on the keyboard: Alt on a Mac types another
  // character (Alt+O is `ø`), and Shift changes the case.
  const physical = /^(?:Key([A-Z])|Digit([0-9]))$/.exec(e.code)
  const base = physical ? physical[1] ?? physical[2] : e.key
  const held = [e.ctrlKey, e.altKey, e.shiftKey]
  const name = MODIFIER_NAMES.filter((_m, i) => held[i]).join('') + base
  return DECLARED_KEY_RE.test(name) ? name : null
}
