import { safeGetItem } from '../utils/safeStorage'
import { savePrefExplicitly } from './uiPrefs'
import {
  chordMatchesEvent,
  eventKeyToken,
  isValidChord,
  normalizeChord,
  SHORTCUT_REGISTRY,
  type Chord,
  type ResolvedShortcut,
  type ShortcutPlatform,
} from './shortcutRegistry'
import {
  PANEL_TOGGLE_IDS,
  resolvePanelToggleBinding,
  type PanelToggleId,
  type PanelToggleOverrides,
} from './panelToggleShortcuts'
import { formatChordKeys, type QuickSearchConfig } from './quickSearchShortcut'
import type { ResolvedSessionControl } from '../hooks/useSessionControls'

/**
 * Keys an app declares for its session control (`contributes.sessionControls[].keys`):
 * each opens the control's box in the chat on screen, with the key's `arg`.
 *
 * The person changes them in Settings → Shortcuts → Apps. Storage is the panel
 * toggles' pattern ({@link ./panelToggleShortcuts}): OVERRIDES only, keyed
 * `<app>:<control>:<key>`, an explicit `null` is a key cleared to none, and an
 * absent key falls back to the app's `default`. Chords, matching and key caps are
 * the registry's own ({@link ./shortcutRegistry}), so there is one chord grammar.
 *
 * Crew's own keys always win: a key one of them holds is off, never shared, and
 * between two apps the one installed first keeps it ({@link resolveAppKeys}).
 */

/** localStorage key holding the JSON {@link AppKeyOverrides}. */
export const APP_SESSION_KEYS_KEY = 'mc-app-session-keys'

/** Window event dispatched after a key changes, so live readers refresh. */
export const APP_SESSION_KEYS_EVENT = 'mc-app-session-keys-changed'

/** Keys one control may declare; mirrors `MAX_SESSION_CONTROL_KEYS` in manifest_contributes.py. */
export const MAX_SESSION_CONTROL_KEYS = 4

/** One key as the manifest declares it, read defensively. */
export interface DeclaredSessionKey {
  id: string
  label: string
  /** The parsed `default`, or null when there is none or it does not parse. */
  default: Chord | null
  /** The key's `arg`, handed to the box as the request payload; null for none. */
  arg: unknown
}

/** User overrides by {@link appKeyId}: a chord, or null for a key cleared to none. */
export type AppKeyOverrides = Record<string, Chord | null>

/** The id a key is stored and shown under: `<app>:<control>:<key>`. */
export function appKeyId(controlKey: string, keyId: string): string {
  return `${controlKey}:${keyId}`
}

const MODIFIERS = new Set(['mod', 'ctrl', 'alt', 'shift'])
// Enter and the arrows, as `KeyboardEvent.key` names them (key names, not copy).
const NAMED_RE = /^(?:Enter|Arrow(?:Up|Down|Left|Right))$/
const PUNCT = new Set([',', '.', '/', '\\', '[', ']', '`', '-', '=', ';', "'"])
const KEY_ID_RE = /^[a-z0-9]+(?:-[a-z0-9]+)*$/

/** A key the grammar takes: a letter, digit, US punctuation, Enter or an arrow. */
function isGrammarKey(key: string): boolean {
  return (key.length === 1 && (/^[A-Za-z0-9]$/.test(key) || PUNCT.has(key))) || NAMED_RE.test(key)
}

/**
 * A manifest `default` (`"Alt+Shift+Q"`, `"Mod+Shift+L"`) as a chord, or null.
 * The same grammar `session_control_key_default_error` refuses at install:
 * Mod/Ctrl/Alt/Shift once each, then a letter, digit, US punctuation, Enter or
 * an arrow, holding Mod, Ctrl or Alt.
 */
export function parseKeyDefault(raw: unknown): Chord | null {
  if (typeof raw !== 'string' || raw === '') return null
  const parts = raw.split('+')
  const key = parts[parts.length - 1]
  const mods = parts.slice(0, -1).map(p => p.toLowerCase())
  if (mods.some(m => !MODIFIERS.has(m)) || new Set(mods).size !== mods.length) return null
  if (!isGrammarKey(key)) return null
  const chord: Chord = { key }
  for (const m of mods) chord[m as 'mod' | 'ctrl' | 'alt' | 'shift'] = true
  return isValidChord(chord) ? normalizeChord(chord) : null
}

/** A control's declared `keys`, read the way the server checks them; a bad entry is skipped. */
export function parseDeclaredKeys(raw: unknown): DeclaredSessionKey[] {
  if (!Array.isArray(raw)) return []
  const out: DeclaredSessionKey[] = []
  const seen = new Set<string>()
  for (const k of raw.slice(0, MAX_SESSION_CONTROL_KEYS)) {
    if (!k || typeof k !== 'object') continue
    const { id, label, default: def, arg } = k as Record<string, unknown>
    if (typeof id !== 'string' || !KEY_ID_RE.test(id) || seen.has(id)) continue
    if (typeof label !== 'string' || !label.trim()) continue
    seen.add(id)
    out.push({ id, label, default: parseKeyDefault(def), arg: arg === undefined ? null : arg })
  }
  return out
}

/** The stored overrides; a malformed entry is dropped, never thrown. */
export function loadAppKeyOverrides(): AppKeyOverrides {
  const raw = safeGetItem(APP_SESSION_KEYS_KEY)
  if (!raw) return {}
  const out: AppKeyOverrides = {}
  try {
    const parsed = JSON.parse(raw) as Record<string, unknown> | null
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {}
    for (const [id, value] of Object.entries(parsed)) {
      if (value === null) out[id] = null
      else if (isValidChord(value as Partial<Chord>)) out[id] = normalizeChord(value as Chord)
    }
  } catch {
    return {}
  }
  return out
}

/** Store one key (null = none) and broadcast. A chord with no Mod/Ctrl/Alt is refused. */
export function setAppKeyBinding(id: string, chord: Chord | null): boolean {
  if (chord !== null && !isValidChord(chord)) return false
  const overrides = loadAppKeyOverrides()
  overrides[id] = chord === null ? null : normalizeChord(chord)
  const ok = savePrefExplicitly(APP_SESSION_KEYS_KEY, JSON.stringify(overrides))
  if (typeof window !== 'undefined') window.dispatchEvent(new Event(APP_SESSION_KEYS_EVENT))
  return ok
}

/** Whether two chords are the same keystroke on `platform` (off macOS ⌘ and ⌃ are one key). */
export function sameChord(a: Chord, b: Chord, platform: ShortcutPlatform): boolean {
  const k = (c: Chord) => (c.key.length === 1 ? c.key.toLowerCase() : c.key)
  if (k(a) !== k(b) || !!a.alt !== !!b.alt || !!a.shift !== !!b.shift) return false
  return platform === 'mac'
    ? !!a.mod === !!b.mod && !!a.ctrl === !!b.ctrl
    : (!!a.mod || !!a.ctrl) === (!!b.mod || !!b.ctrl)
}

/** Which of Crew's own keys holds a chord. */
export type CrewKeyOwner =
  | { kind: 'shortcut'; id: string; n?: number }
  | { kind: 'panel'; id: PanelToggleId }
  | { kind: 'search' }
  | { kind: 'editing' }

/**
 * Keys Crew's message box, find and the browser's editing take outside the
 * registry: find (`useMessageSearch`), the send / newline / steer Enter
 * (`chat-input/keyboard.ts`, ⌘ or Ctrl on a Mac), raw paste
 * (`composerPastePolicy`), and copy, cut, paste, undo, redo, select all.
 */
const EDITING_CHORDS: readonly Chord[] = [
  ...['a', 'c', 'f', 'v', 'x', 'y', 'z', 'Enter'].map(key => ({ key, mod: true })),
  { key: 'Enter', ctrl: true },
  { key: 'v', mod: true, shift: true },
  { key: 'v', mod: true, alt: true, shift: true },
  { key: 'z', mod: true, shift: true },
]

/** Crew's live keys: the registry resolved, the panel toggles, the search key. */
export interface CrewKeys {
  shortcuts: Readonly<Record<string, ResolvedShortcut>>
  panels: PanelToggleOverrides
  search: QuickSearchConfig
}

const SINGLE_LETTER = /^[a-z]$/

/**
 * The Crew key that holds `chord` on `platform`, or null when it is free.
 *
 * Every registry entry counts, the `code` families too: a jump family drawn as
 * `a…z` holds every letter with its modifiers, and Option/Alt with a bare letter
 * or digit is Crew's (the chat jumps, which a Mac moves between ⌥ and ⌃ in
 * Settings, and the Alt+letter panels).
 */
export function crewKeyOwner(chord: Chord, platform: ShortcutPlatform, crew: CrewKeys): CrewKeyOwner | null {
  const letter = SINGLE_LETTER.test(chord.key.toLowerCase())
  const digit = /^[1-9]$/.test(chord.key)
  for (const entry of SHORTCUT_REGISTRY) {
    const r = crew.shortcuts[entry.id]
    const chords = r ? [r.primary, ...r.aliases] : [entry.defaults[platform]]
    for (const c of chords) {
      if (!c) continue
      const hit = c.key === 'a…z'
        ? letter && sameChord({ ...c, key: chord.key }, chord, platform)
        : sameChord(c, chord, platform)
      if (hit) return { kind: 'shortcut', id: entry.id, ...(entry.n !== undefined ? { n: entry.n } : {}) }
    }
  }
  if ((letter || digit) && chord.alt && !chord.shift && !chord.mod && !chord.ctrl) {
    return digit ? { kind: 'shortcut', id: `chat-${chord.key}`, n: Number(chord.key) } : { kind: 'shortcut', id: 'chat-letters' }
  }
  for (const id of PANEL_TOGGLE_IDS) {
    const b = resolvePanelToggleBinding(id, crew.panels)
    if (b && sameChord(b, chord, platform)) return { kind: 'panel', id }
  }
  const search = crew.search.mode === 'mod-k' ? { key: 'k', mod: true } : crew.search.mode === 'custom' ? crew.search.custom : undefined
  if (search && sameChord(search, chord, platform)) return { kind: 'search' }
  if (EDITING_CHORDS.some(c => sameChord(c, chord, platform))) return { kind: 'editing' }
  return null
}

/** One app key, resolved against the person's choices and everyone else's keys. */
export interface ResolvedAppKey {
  /** {@link appKeyId}. */
  id: string
  control: ResolvedSessionControl
  key: DeclaredSessionKey
  /** The chord it answers to; null when it has none or is off. */
  chord: Chord | null
  /** The chord it would have, set only when it is off for a clash. */
  wanted: Chord | null
  /** Why it is off: Crew's key, or an app key installed earlier. */
  off: { crew: CrewKeyOwner } | { app: ResolvedAppKey } | null
}

/**
 * Every declared key, in the order Settings lists them: apps by install time
 * (then by control key), keys in manifest order. Each takes the person's chord,
 * else its default; one that Crew holds, or an earlier key already took, is off.
 */
export function resolveAppKeys(
  controls: readonly ResolvedSessionControl[],
  overrides: AppKeyOverrides,
  platform: ShortcutPlatform,
  crew: CrewKeys,
): ResolvedAppKey[] {
  const ordered = [...controls].sort((a, b) =>
    a.installedAt !== b.installedAt ? (a.installedAt < b.installedAt ? -1 : 1) : a.key < b.key ? -1 : a.key > b.key ? 1 : 0)
  const out: ResolvedAppKey[] = []
  for (const control of ordered) {
    for (const key of control.keys) {
      const id = appKeyId(control.key, key.id)
      const wanted = Object.prototype.hasOwnProperty.call(overrides, id) ? overrides[id] : key.default
      const row: ResolvedAppKey = { id, control, key, chord: wanted, wanted: null, off: null }
      if (wanted) {
        const crewOwner = crewKeyOwner(wanted, platform, crew)
        const earlier = crewOwner ? undefined : out.find(o => o.chord && sameChord(o.chord, wanted, platform))
        if (crewOwner || earlier) {
          row.chord = null
          row.wanted = wanted
          row.off = crewOwner ? { crew: crewOwner } : { app: earlier! }
        }
      }
      out.push(row)
    }
  }
  return out
}

type KeyEventLike = Pick<KeyboardEvent, 'code' | 'key' | 'metaKey' | 'ctrlKey' | 'altKey' | 'shiftKey'>
  & Partial<Pick<KeyboardEvent, 'getModifierState'>>

/**
 * AltGr, which Windows reports as Ctrl+Alt: a character being typed, never a
 * key. Off a Mac and with both flags only: Firefox on a Mac reports Option as
 * AltGraph, and an Option key must still fire.
 */
const isAltGraph = (e: KeyEventLike, platform: ShortcutPlatform) =>
  platform !== 'mac' && e.ctrlKey && e.altKey && e.getModifierState?.('AltGraph') === true

/** The live app key this keydown is, or null. */
export function matchAppKeyEvent(
  e: KeyEventLike,
  keys: readonly ResolvedAppKey[],
  platform: ShortcutPlatform,
): ResolvedAppKey | null {
  if (isAltGraph(e, platform)) return null
  for (const k of keys) if (k.chord && chordMatchesEvent(e, k.chord, platform)) return k
  return null
}

/** A chord as one short string: `⌥⇧Q` on a Mac, `Alt+Shift+Q` elsewhere. */
export function shownKeys(chord: Chord, platform: ShortcutPlatform): string {
  const mac = platform === 'mac'
  return formatChordKeys(chord, mac).join(mac ? '' : '+')
}

/** A chord in the `aria-keyshortcuts` form (`Alt+Shift+Q`, `Meta+K`). */
export function ariaKeys(chord: Chord, platform: ShortcutPlatform): string {
  const mods: string[] = []
  if (chord.ctrl || (chord.mod && platform !== 'mac')) mods.push('Control')
  if (chord.mod && platform === 'mac') mods.push('Meta')
  if (chord.alt) mods.push('Alt')
  if (chord.shift) mods.push('Shift')
  return [...mods, chord.key.length === 1 ? chord.key.toUpperCase() : chord.key].join('+')
}

/**
 * The chord a keydown would record, or null while it is not one yet: a bare
 * modifier (its own keydown comes before the letter's), AltGr, or a key outside
 * the manifest grammar (a numpad key, an ISO key). The token is the one
 * {@link matchAppKeyEvent} reads, so a recorded key fires from the key pressed.
 */
export function chordFromKeyEvent(e: KeyboardEvent, platform: ShortcutPlatform): Chord | null {
  const token = eventKeyToken(e)
  if (token === null || !isGrammarKey(token) || isAltGraph(e, platform)) return null
  const chord: Chord = { key: token }
  if (platform === 'mac' ? e.metaKey : e.ctrlKey) chord.mod = true
  if (platform === 'mac' && e.ctrlKey) chord.ctrl = true
  if (e.altKey) chord.alt = true
  if (e.shiftKey) chord.shift = true
  return chord
}
