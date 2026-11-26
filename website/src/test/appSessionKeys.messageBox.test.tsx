/**
 * An app key while the person types in the chat's message box, on a Mac: a key
 * whose chord holds Ctrl is heard there (the key listener runs in the document's
 * capture phase, before the message box's own handlers), the box takes it, and
 * typing is not eaten. Ctrl+Shift+N and Ctrl+Shift+G are free of Crew's own keys:
 * the Ctrl+letter chat jumps and the Ctrl+G agent monitor ignore a chord with Shift.
 *
 * `IS_MAC` freezes at module load from `navigator.platform`, so the platform is
 * set in `vi.hoisted`, which runs before this file's imports are evaluated.
 */
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.hoisted(() => {
  Object.defineProperty(navigator, 'platform', { value: 'MacIntel', configurable: true })
})

vi.mock('@radix-ui/react-dropdown-menu', async () => await import('./__mocks__/@radix-ui/react-dropdown-menu'))
vi.mock('@radix-ui/react-popover', async () => await import('./__mocks__/@radix-ui/react-popover'))
vi.mock('../api/client', () => ({
  api: { listApps: vi.fn(), chatFolders: vi.fn(), appSessionStatus: vi.fn(), kirocrewConfig: vi.fn() },
}))
// jsdom marks every event it is handed as untrusted; each key here stands in the person's.
vi.mock('../components/trustedClick', async (orig) => ({
  ...(await orig<typeof import('../components/trustedClick')>()),
  isTrustedEvent: () => true,
}))

import { act, cleanup, screen, waitFor } from '@testing-library/react'
import { QueryClient } from '@tanstack/react-query'

import { createTestStore, renderHookWithProviders, renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { api } from '../api/client'
import { IS_MAC, jumpLetters, useKeyboardShortcuts } from '../hooks/useKeyboardShortcuts'
import { useComposerSessionControls } from '../pages/chat/page/sessionControls'
import { clearSessionControlRequest, peekSessionControlRequestForTest } from '../app-sdk/sessionControlRequest'
import { crewKeyOwner, parseKeyDefault, type CrewKeys } from '../lib/appSessionKeys'
import { resolveShortcuts, type ShortcutPlatform } from '../lib/shortcutRegistry'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import type { RootState } from '../store'

const KEY = 'inbox-app:inbox'
const inboxApp = {
  name: 'inbox-app',
  enabled: true,
  installedAt: '2026-11-01T00:00:00Z',
  manifest: {
    displayName: 'Inbox',
    contributes: { sessionControls: [{
      id: 'inbox', entryPoint: 'dist/inbox.mjs', label: 'Inbox', icon: 'Tag', dock: true, stayOpen: true,
      keys: [
        { id: 'next', label: 'Next question', default: 'Ctrl+Shift+N', arg: { go: 'next' }, keepOpen: true },
        { id: 'oldest', label: 'Oldest question', default: 'Ctrl+Shift+G', arg: { go: 'oldest' } },
      ],
    }] },
  },
}

const crew = (platform: ShortcutPlatform): CrewKeys => ({ shortcuts: resolveShortcuts({}, platform), panels: {}, search: { mode: 'mod-k' } })

/** Enough chats that every jump letter names one, so a Ctrl+letter would jump if it were taken. */
function chatStore() {
  const slots = Array.from({ length: 9 + jumpLetters().length }, (_, i) => ({ key: `slot-${i + 1}`, title: `Chat ${i + 1}`, messages: 0, running: false }))
  const chatInitial = chatReducer(undefined, { type: '@@test/init' })
  const dashInitial = dashboardReducer(undefined, { type: '@@test/init' })
  return createTestStore({
    dashboard: { ...dashInitial, slots } as RootState['dashboard'],
    chat: { ...chatInitial, activeSlot: 'slot-1', slotHistory: [] } as RootState['chat'],
  })
}

function mountCrewKeys() {
  const store = chatStore()
  renderHookWithProviders(() => useKeyboardShortcuts({ onToggleShortcutsModal: vi.fn(), onNewChat: vi.fn() }), { store })
  return store
}

/** Dispatches a keydown on `target`; true when a handler prevented it. */
function press(target: EventTarget, init: KeyboardEventInit): boolean {
  let prevented = false
  act(() => { prevented = !target.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, cancelable: true, ...init })) })
  return prevented
}

const ctrlShift = (letter: string): KeyboardEventInit => ({ code: `Key${letter.toUpperCase()}`, key: letter.toUpperCase(), ctrlKey: true, shiftKey: true })

beforeEach(() => {
  localStorage.clear()
  ;(api.listApps as unknown as ReturnType<typeof vi.fn>).mockResolvedValue([inboxApp])
  ;(api.chatFolders as unknown as ReturnType<typeof vi.fn>).mockResolvedValue([])
  ;(api.appSessionStatus as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({ state: 'none' })
})
afterEach(() => {
  cleanup()
  document.body.replaceChildren()
  clearSessionControlRequest('dashboard:slot-1', KEY)
})

describe("Crew's own keys leave Ctrl+Shift+N and Ctrl+Shift+G free", () => {
  it('runs as macOS', () => {
    expect(IS_MAC).toBe(true)
  })

  it('no Crew key holds them, on a Mac or off one, while Ctrl+N and Ctrl+G are held', () => {
    for (const platform of ['mac', 'other'] as const) {
      expect(crewKeyOwner(parseKeyDefault('Ctrl+Shift+N')!, platform, crew(platform))).toBeNull()
      expect(crewKeyOwner(parseKeyDefault('Ctrl+Shift+G')!, platform, crew(platform))).toBeNull()
    }
    expect(crewKeyOwner(parseKeyDefault('Ctrl+N')!, 'mac', crew('mac'))).not.toBeNull()
    expect(crewKeyOwner(parseKeyDefault('Ctrl+G')!, 'mac', crew('mac'))).not.toBeNull()
  })

  it('the Ctrl+letter chat jumps and the Ctrl+G agent monitor ignore a chord with Shift', () => {
    const store = mountCrewKeys()
    for (const letter of [...jumpLetters(), 'n', 'g']) {
      expect(press(document.body, ctrlShift(letter))).toBe(false)
    }
    expect(store.getState().chat.activeSlot).toBe('slot-1')
    // Without Shift they are Crew's: a letter jumps, Ctrl+G opens the monitor.
    const letter = jumpLetters()[0]
    expect(press(document.body, { code: `Key${letter.toUpperCase()}`, key: letter, ctrlKey: true })).toBe(true)
    expect(store.getState().chat.activeSlot).toBe('slot-10')
    expect(press(document.body, { code: 'KeyG', key: 'g', ctrlKey: true })).toBe(true)
  })
})

describe('an app key while focus is in the message box', () => {
  function Composer() {
    const [value, setValue] = useState('')
    return <ChatInput value={value} onChange={setValue} onSend={vi.fn()} connected
      sessionControls={[{ key: KEY, label: 'Inbox', icon: 'Tag' }]} />
  }

  async function mount() {
    const crewStore = mountCrewKeys()
    const composer = renderHookWithProviders(() =>
      useComposerSessionControls({ activeSlot: 'slot-1', currentSlot: undefined, queryClient: new QueryClient() }),
    )
    await waitFor(() => expect(composer.result.current.appKeys.filter(k => k.chord)).toHaveLength(2))
    renderWithProviders(<Composer />)
    // The chip as laid out on screen (jsdom draws no rects).
    const chip = document.querySelector(`[data-session-control-key="${KEY}"]`) as HTMLElement
    chip.getClientRects = () => [{ width: 20, height: 20 }] as unknown as DOMRectList
    const box = screen.getByRole('textbox') as HTMLTextAreaElement
    act(() => { box.focus() })
    return { composer, crewStore, box }
  }

  it('is heard there, and the keepOpen key hands the open box a new request each press', async () => {
    const { composer, crewStore, box } = await mount()
    expect(press(box, ctrlShift('n'))).toBe(true)
    expect(composer.result.current.openSessionControl).toEqual({ key: KEY, slot: 'slot-1' })
    const first = peekSessionControlRequestForTest('dashboard:slot-1', KEY)!
    expect(first).toMatchObject({ payload: { go: 'next' }, via: 'key' })
    expect(press(box, ctrlShift('n'))).toBe(true)
    expect(peekSessionControlRequestForTest('dashboard:slot-1', KEY)?.seq).toBe(first.seq + 1)
    expect(press(box, ctrlShift('g'))).toBe(true)
    expect(peekSessionControlRequestForTest('dashboard:slot-1', KEY)).toMatchObject({ payload: { go: 'oldest' }, via: 'key' })
    expect(composer.result.current.openSessionControl).toEqual({ key: KEY, slot: 'slot-1' })
    // Crew did not also act on them, and the person's focus stays in the message box.
    expect(crewStore.getState().chat.activeSlot).toBe('slot-1')
    expect(document.activeElement).toBe(box)
  })

  it('does not eat typing: letters, Shift+letters and Crew-free Ctrl keys reach the message box', async () => {
    const { composer, box } = await mount()
    for (const init of [
      { code: 'KeyN', key: 'n' },
      { code: 'KeyN', key: 'N', shiftKey: true },
      { code: 'KeyG', key: 'G', shiftKey: true },
      // macOS readline in a text field: Ctrl+N is next line.
      { code: 'KeyN', key: 'n', ctrlKey: true },
      { code: 'KeyN', key: 'N', ctrlKey: true, altKey: true, shiftKey: true },
    ]) expect(press(box, init)).toBe(false)
    expect(composer.result.current.openSessionControl).toBeNull()
  })
})
