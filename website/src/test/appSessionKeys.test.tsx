/**
 * App keys (`contributes.sessionControls[].keys`): the resolver (Crew's keys
 * win, the earlier app keeps a shared default, the person's choice wins over a
 * default), the key in the chat (opens the box on its chip with the key's `arg`,
 * the same key again closes, another key or another open keeps it open), and the
 * Settings recorder (a held key is refused and named; a free one is stored).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor, within } from '@testing-library/react'
import { QueryClient } from '@tanstack/react-query'

import { renderHookWithProviders, renderWithProviders } from './helpers'
import { ShortcutsPanel } from '../pages/settings/ShortcutsPanel'
import { api } from '../api/client'
import { sendAppNav } from '../linkPreview/linkPreviewNav'
import { useComposerSessionControls, useSessionControlChips } from '../pages/chat/page/sessionControls'
import {
  INSERT_ACT_MS,
  SessionControlRequestScope,
  clearSessionControlRequest,
  insertActClock,
  peekSessionControlRequestForTest,
  useInsertIntoComposer,
  type ComposerInsertResult,
} from '../app-sdk/sessionControlRequest'
import { useAppKeyRecorder } from '../hooks/useAppSessionKeys'
import { SHORTCUTS_ENABLED_KEY } from '../hooks/useKeyboardShortcuts'
import {
  APP_SESSION_KEYS_KEY,
  crewKeyOwner,
  loadAppKeyOverrides,
  matchAppKeyEvent,
  parseDeclaredKeys,
  parseKeyDefault,
  resolveAppKeys,
  setAppKeyBinding,
  type CrewKeys,
} from '../lib/appSessionKeys'
import { resolveShortcuts } from '../lib/shortcutRegistry'
import type { ResolvedSessionControl } from '../hooks/useSessionControls'

vi.mock('../api/client', () => ({
  api: { listApps: vi.fn(), chatFolders: vi.fn(), appSessionStatus: vi.fn(), kirocrewConfig: vi.fn() },
}))
// jsdom marks every event it is handed as untrusted; a test stands in the person's key.
const trust = vi.hoisted(() => ({ next: true }))
vi.mock('../components/trustedClick', async (orig) => ({
  ...(await orig<typeof import('../components/trustedClick')>()),
  isTrustedEvent: () => trust.next,
}))
const mockApps = api.listApps as unknown as ReturnType<typeof vi.fn>
const clock = { t: 1_000 }

const KEYS = [
  { id: 'questions', label: 'Open Questions', default: 'Alt+Shift+Q', arg: { view: 'questions' } },
  { id: 'references', label: 'Open References', default: 'Alt+Shift+L', arg: { view: 'references' } },
]
const refsApp = {
  name: 'refs-app',
  enabled: true,
  installedAt: '2026-11-01T00:00:00Z',
  manifest: {
    displayName: 'Ref Links',
    contributes: { sessionControls: [{ id: 'questions', entryPoint: 'dist/q.mjs', label: 'Questions', icon: 'Tag', keys: KEYS }] },
  },
}
const KEY = 'refs-app:questions'

const crew = (): CrewKeys => ({ shortcuts: resolveShortcuts({}, 'other'), panels: {}, search: { mode: 'mod-k' } })

const control = (over: Partial<ResolvedSessionControl>): ResolvedSessionControl => ({
  key: 'a:c', appName: 'a', appDisplayName: 'A', appVersion: '1', id: 'c', entryPoint: 'x.mjs', label: 'C', icon: '',
  allowedApi: [], allowedEvents: [], statusPath: '', processBacked: false, width: 340, openFiles: false,
  keys: [], installedAt: '', ...over,
})

/** The chip as the composer draws it, laid out (a hidden chat page draws no rects). */
function drawChip(key = KEY) {
  const chip = document.createElement('button')
  chip.setAttribute('data-session-control-key', key)
  chip.getClientRects = () => [{ width: 20, height: 20 }] as unknown as DOMRectList
  document.body.appendChild(chip)
  return chip
}

const press = (code: string, target: EventTarget = document.body, extra: KeyboardEventInit = {}) => {
  const e = new KeyboardEvent('keydown', { code, key: code.slice(3).toLowerCase(), altKey: true, shiftKey: true, bubbles: true, cancelable: true, ...extra })
  act(() => { target.dispatchEvent(e) })
  return e
}

async function mountComposer() {
  const hook = renderHookWithProviders(() =>
    useComposerSessionControls({ activeSlot: 'chat-1', currentSlot: undefined, queryClient: new QueryClient() }),
  )
  await waitFor(() => expect(hook.result.current.appKeys).toHaveLength(2))
  return hook
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  mockApps.mockResolvedValue([refsApp])
  ;(api.chatFolders as unknown as ReturnType<typeof vi.fn>).mockResolvedValue([])
  ;(api.appSessionStatus as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({ state: 'none' })
  trust.next = true
  clock.t = 1_000
  insertActClock.now = () => clock.t
})
afterEach(() => {
  cleanup()
  document.body.replaceChildren()
  clearSessionControlRequest('dashboard:chat-1', KEY)
  insertActClock.now = () => performance.now()
})

describe('reading and resolving app keys', () => {
  it('parses the manifest grammar and refuses what install refuses', () => {
    expect(parseKeyDefault('Alt+Shift+Q')).toEqual({ key: 'q', alt: true, shift: true })
    expect(parseKeyDefault('Mod+/')).toEqual({ key: '/', mod: true })
    expect(parseKeyDefault('Alt+ArrowUp')).toEqual({ key: 'ArrowUp', alt: true })
    expect(parseKeyDefault('Mod+Enter')).toEqual({ key: 'Enter', mod: true })
    for (const bad of ['Q', 'Shift+Q', 'Alt+Alt+Q', 'Cmd+Q', 'Alt+Escape', 7, '']) expect(parseKeyDefault(bad)).toBeNull()
    expect(parseDeclaredKeys([{ id: 'a', label: 'A' }, { id: 'a', label: 'B' }, { id: 'Bad', label: 'C' }, { id: 'b' }, 'x'])
      .map(k => k.id)).toEqual(['a'])
  })

  it("Crew's own keys hold every chord a registry entry, a jump family, a panel toggle or the search key answers to", () => {
    expect(crewKeyOwner({ key: 'r', alt: true, shift: true }, 'other', crew())).toEqual({ kind: 'shortcut', id: 'reject-approval' })
    expect(crewKeyOwner({ key: 'j', alt: true }, 'other', crew())).toEqual({ kind: 'shortcut', id: 'chat-letters' })
    expect(crewKeyOwner({ key: '3', alt: true }, 'mac', crew())).toEqual({ kind: 'shortcut', id: 'chat-3', n: 3 })
    expect(crewKeyOwner({ key: 'b', mod: true }, 'other', crew())).toEqual({ kind: 'panel', id: 'session-panel' })
    expect(crewKeyOwner({ key: 'k', mod: true }, 'other', crew())).toEqual({ kind: 'search' })
    expect(crewKeyOwner({ key: 'q', alt: true, shift: true }, 'other', crew())).toBeNull()
  })

  it('find, the message box Enter, raw paste and the editing keys are Crew’s too, though no registry entry names them', () => {
    for (const [chord, platform] of [
      [{ key: 'f', mod: true }, 'mac'],
      [{ key: 'Enter', mod: true }, 'other'],
      [{ key: 'Enter', ctrl: true }, 'mac'],
      [{ key: 'v', mod: true, shift: true }, 'other'],
      [{ key: 'c', mod: true }, 'mac'],
      [{ key: 'z', mod: true, shift: true }, 'mac'],
    ] as const) expect(crewKeyOwner(chord, platform, crew())).toEqual({ kind: 'editing' })
  })

  it('AltGr typing (Windows reports Ctrl+Alt) never matches an app key', () => {
    const [key] = resolveAppKeys([control({ keys: parseDeclaredKeys([{ id: 'k', label: 'K', default: 'Ctrl+Alt+Q' }]) })], {}, 'other', crew())
    const ev = (altGraph: boolean) => ({ code: 'KeyQ', key: altGraph ? '@' : 'q', ctrlKey: true, altKey: true, metaKey: false, shiftKey: false, getModifierState: (m: string) => altGraph && m === 'AltGraph' })
    expect(matchAppKeyEvent(ev(false), [key], 'other')?.id).toBe('a:c:k')
    expect(matchAppKeyEvent(ev(true), [key], 'other')).toBeNull()
  })

  it("a default Crew holds is off and named; between two apps the one installed first keeps it; the person's choice wins", () => {
    const early = control({ key: 'notes:box', installedAt: '2026-01-01', keys: parseDeclaredKeys([{ id: 'open', label: 'Open', default: 'Alt+Shift+Q' }]) })
    const late = control({ key: 'refs:box', installedAt: '2026-06-01', keys: parseDeclaredKeys([
      { id: 'q', label: 'Q', default: 'Alt+Shift+Q' },
      { id: 'r', label: 'R', default: 'Alt+Shift+R' },
      { id: 'l', label: 'L', default: 'Alt+Shift+L' },
    ]) })
    const [open, q, r, l] = resolveAppKeys([late, early], { 'refs:box:l': null }, 'other', crew())
    expect(open.chord).toEqual({ key: 'q', alt: true, shift: true })
    expect(q.chord).toBeNull()
    expect(q.off && 'app' in q.off && q.off.app.id).toBe('notes:box:open')
    expect(r.chord).toBeNull()
    expect(r.off).toEqual({ crew: { kind: 'shortcut', id: 'reject-approval' } })
    expect(l).toMatchObject({ chord: null, off: null })
    const [, q2] = resolveAppKeys([late, early], { 'refs:box:q': { key: 'y', alt: true, shift: true } }, 'other', crew())
    expect(q2).toMatchObject({ chord: { key: 'y', alt: true, shift: true }, off: null })
  })
})

describe('an app key in the chat on screen', () => {
  it('opens the box on its chip with the arg, a second key of the box keeps it open, the same key again closes it', async () => {
    const { result } = await mountComposer()
    drawChip()
    const e = press('KeyQ')
    expect(e.defaultPrevented).toBe(true)
    expect(result.current.openSessionControl).toEqual({ key: KEY, slot: 'chat-1' })
    expect(peekSessionControlRequestForTest('dashboard:chat-1', KEY)).toMatchObject({ payload: { view: 'questions' }, via: 'key' })
    const opened = result.current.openSessionControl
    press('KeyL')
    expect(result.current.openSessionControl).toBe(opened)
    expect(peekSessionControlRequestForTest('dashboard:chat-1', KEY)).toMatchObject({ payload: { view: 'references' }, via: 'key' })
    press('KeyL')
    expect(result.current.openSessionControl).toBeNull()
    expect(peekSessionControlRequestForTest('dashboard:chat-1', KEY)).toBeNull()
  })

  it("takes only the person's own key: a dispatched chord opens nothing, hands nothing over and starts no insert window", async () => {
    const { result } = await mountComposer()
    drawChip()
    // The box's insert, for the chat and control the key is for.
    let insert: ((t: string) => ComposerInsertResult) | null = null
    function Probe() {
      insert = useInsertIntoComposer()
      return null
    }
    render(<SessionControlRequestScope sessionKey="dashboard:chat-1" control={KEY} insert={() => undefined}><Probe /></SessionControlRequestScope>)
    clock.t += INSERT_ACT_MS + 1
    trust.next = false
    expect(press('KeyQ').defaultPrevented).toBe(false)
    expect(result.current.openSessionControl).toBeNull()
    expect(peekSessionControlRequestForTest('dashboard:chat-1', KEY)).toBeNull()
    expect(insert!('Q4a')).toEqual({ ok: false, code: 'no_gesture' })
    trust.next = true
    expect(press('KeyQ').defaultPrevented).toBe(true)
    expect(result.current.openSessionControl).toEqual({ key: KEY, slot: 'chat-1' })
    expect(insert!('Q4a')).toEqual({ ok: true })
    // A scripted press of the key that opened it does not close it either.
    trust.next = false
    press('KeyQ')
    expect(result.current.openSessionControl).toEqual({ key: KEY, slot: 'chat-1' })
  })

  it('the key that closes a docked box from inside it hands focus back to the chip', async () => {
    const { result } = await mountComposer()
    const chip = drawChip()
    press('KeyQ')
    // The box as the host draws a docked one, with focus in the app's own field.
    const box = document.createElement('div')
    box.setAttribute('data-session-control-dock', KEY)
    const field = document.createElement('textarea')
    box.appendChild(field)
    document.body.appendChild(box)
    field.focus()
    press('KeyQ', field)
    expect(result.current.openSessionControl).toBeNull()
    expect(document.activeElement).toBe(chip)
  })

  it('a box opened another way stays open on its key, with the new arg', async () => {
    const { result } = await mountComposer()
    drawChip()
    press('KeyQ')
    act(() => { sendAppNav({ kind: 'open', slot: 'chat-1', appName: 'refs-app', target: { kind: 'sessionControl', id: 'questions' }, payload: { label: 'Q4a' }, via: 'link' }) })
    press('KeyQ')
    expect(result.current.openSessionControl).toEqual({ key: KEY, slot: 'chat-1' })
    expect(peekSessionControlRequestForTest('dashboard:chat-1', KEY)).toMatchObject({ payload: { view: 'questions' }, via: 'key' })
  })

  it('runs before a box that stops the keys typed in it, and passes on with no chip, in a terminal, or with shortcuts off', async () => {
    const { result } = await mountComposer()
    // No chip drawn: the key does nothing and is not claimed.
    expect(press('KeyQ').defaultPrevented).toBe(false)
    const chip = drawChip()
    const term = document.createElement('div')
    term.className = 'xterm'
    document.body.appendChild(term)
    expect(press('KeyQ', term).defaultPrevented).toBe(false)
    localStorage.setItem(SHORTCUTS_ENABLED_KEY, '0')
    expect(press('KeyQ').defaultPrevented).toBe(false)
    localStorage.removeItem(SHORTCUTS_ENABLED_KEY)
    expect(result.current.openSessionControl).toBeNull()
    // A field inside the popover, whose host stops every key but Escape.
    const box = document.createElement('div')
    box.addEventListener('keydown', ev => ev.stopPropagation())
    const field = document.createElement('textarea')
    box.appendChild(field)
    document.body.appendChild(box)
    press('KeyQ', field)
    expect(result.current.openSessionControl).toEqual({ key: KEY, slot: 'chat-1' })
    chip.remove()
  })

  it("a held key's repeats are claimed (nothing typed) but act once; under a modal dialog the key passes on", async () => {
    const { result } = await mountComposer()
    drawChip()
    press('KeyQ')
    const opened = result.current.openSessionControl
    const seq = peekSessionControlRequestForTest('dashboard:chat-1', KEY)?.seq
    for (let i = 0; i < 3; i++) expect(press('KeyQ', document.body, { repeat: true }).defaultPrevented).toBe(true)
    expect(result.current.openSessionControl).toBe(opened)
    expect(peekSessionControlRequestForTest('dashboard:chat-1', KEY)?.seq).toBe(seq)
    const sheet = document.createElement('div')
    sheet.setAttribute('aria-modal', 'true')
    document.body.appendChild(sheet)
    expect(press('KeyQ').defaultPrevented).toBe(false)
    expect(result.current.openSessionControl).toBe(opened)
    sheet.remove()
    press('KeyQ')
    expect(result.current.openSessionControl).toBeNull()
  })

  it('a key changed in Settings applies at once; a cleared one does nothing', async () => {
    const { result } = await mountComposer()
    drawChip()
    act(() => { setAppKeyBinding(`${KEY}:questions`, { key: 'y', alt: true, shift: true }) })
    expect(press('KeyQ').defaultPrevented).toBe(false)
    press('KeyY')
    expect(result.current.openSessionControl).toEqual({ key: KEY, slot: 'chat-1' })
    act(() => { setAppKeyBinding(`${KEY}:references`, null) })
    expect(press('KeyL').defaultPrevented).toBe(false)
  })

  it("the chip's hover names its first live key", async () => {
    const { result } = await mountComposer()
    const chips = renderHook(() => useSessionControlChips({
      sessionControls: result.current.sessionControls, openSessionControl: null, activeSlot: 'chat-1',
      sessionControlStatuses: {}, appKeys: result.current.appKeys,
    })).result.current
    expect(chips[0].shortcut?.aria).toBe('Alt+Shift+Q')
  })
})

describe('Settings → Shortcuts → Apps', () => {
  it('lists each app key with its keys, names the holder of a default that is off, and Clear stores the clearing', async () => {
    const clash = { id: 'reject', label: 'Reject', default: 'Alt+Shift+R' }
    mockApps.mockResolvedValue([{ ...refsApp, manifest: { ...refsApp.manifest, contributes: { sessionControls: [
      { ...refsApp.manifest.contributes.sessionControls[0], keys: [...KEYS, clash] },
    ] } } }])
    renderWithProviders(<ShortcutsPanel />)
    await waitFor(() => expect(screen.getAllByTestId('app-key-row')).toHaveLength(3))
    const [questions, , reject] = screen.getAllByTestId('app-key-row')
    expect(within(questions).getByText('Ref Links · Open Questions')).toBeTruthy()
    expect(within(reject).getByText(/so it is off/)).toBeTruthy()
    expect(within(reject).queryByRole('button', { name: /clear/i })).toBeNull()
    fireEvent.click(within(questions).getByRole('button', { name: /clear/i }))
    expect(loadAppKeyOverrides()).toEqual({ [`${KEY}:questions`]: null })
    await waitFor(() => expect(within(screen.getAllByTestId('app-key-row')[0]).queryByRole('button', { name: /clear/i })).toBeNull())
  })
})

describe('the Settings recorder', () => {
  it("refuses a key Crew or another app key holds and keeps listening; stores a free one; Escape keeps the old", async () => {
    const { result: composer } = await mountComposer()
    const { result } = renderHook(() => useAppKeyRecorder(composer.current.appKeys))
    const id = `${KEY}:references`
    act(() => { result.current.startRecording(id) })
    // A browser sends each held modifier's own keydown before the letter's.
    press('AltLeft', document.body, { key: 'Alt', shiftKey: false })
    press('ShiftLeft', document.body, { key: 'Shift' })
    press('MetaLeft', document.body, { key: 'Meta', altKey: false, shiftKey: false, metaKey: true })
    // A numpad key is outside the grammar, and would never fire from the numpad.
    press('Numpad5', document.body, { key: '5' })
    expect(result.current.recordingId).toBe(id)
    expect(result.current.refused).toBeNull()
    expect(loadAppKeyOverrides()).toEqual({})
    press('KeyR')
    expect(result.current.refused?.holder).toEqual({ crew: { kind: 'shortcut', id: 'reject-approval' } })
    expect(result.current.recordingId).toBe(id)
    press('KeyQ')
    expect(result.current.refused?.holder && 'app' in result.current.refused.holder && result.current.refused.holder.app.id).toBe(`${KEY}:questions`)
    expect(loadAppKeyOverrides()).toEqual({})
    press('KeyJ')
    expect(result.current.recordingId).toBeNull()
    expect(loadAppKeyOverrides()).toEqual({ [id]: { key: 'j', alt: true, shift: true } })
    act(() => { result.current.startRecording(id) })
    press('Escape', document.body, { key: 'Escape', altKey: false, shiftKey: false })
    expect(result.current.recordingId).toBeNull()
    expect(JSON.parse(localStorage.getItem(APP_SESSION_KEYS_KEY)!)).toEqual({ [id]: { key: 'j', alt: true, shift: true } })
  })
})
