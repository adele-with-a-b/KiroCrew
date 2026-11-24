/**
 * An app opening its OWN session control (the chip's popover under the message
 * box) with a payload: the chat page's half (`useComposerSessionControls`), the
 * reader hook (`useSessionControlRequest`), a status target's `sessionControl`,
 * the go's hand-off, the named refusals, and the popover staying mounted when a
 * link click re-asks.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react'
import { QueryClient } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import type { ReactNode } from 'react'

import { renderHookWithProviders } from './helpers'
import { api } from '../api/client'
import { onLinkPreviewNav, sendAppNav, setClickOpenProbe, type AppOpenTarget, type LinkPreviewNav } from '../linkPreview/linkPreviewNav'
import { CLICK_GRANT_MS } from '../linkPreview/linkPreviewFrames'
import { useComposerSessionControls } from '../pages/chat/page/sessionControls'
import {
  SessionControlRequestScope,
  clearSessionControlRequest,
  peekSessionControlRequestForTest,
  putSessionControlRequest,
  useSessionControlJump,
  useSessionControlRequest,
} from '../app-sdk/sessionControlRequest'
import { normalizeStatusTarget, setActiveStatusGo, getActiveStatusGo } from '../lib/statusGo'
import { useStatusLinks } from '../pages/chat/useStatusLinks'
import SessionControlHost from '../components/SessionControlHost'
import type { ResolvedSessionControl } from '../hooks/useSessionControls'

vi.mock('../api/client', () => ({
  api: { listApps: vi.fn(), chatFolders: vi.fn(), appSessionStatus: vi.fn(), kirocrewConfig: vi.fn() },
}))

const mockApps = api.listApps as unknown as ReturnType<typeof vi.fn>

const refsApp = {
  name: 'refs-app',
  enabled: true,
  manifest: {
    displayName: 'Refs',
    contributes: { sessionControls: [{ id: 'questions', entryPoint: 'dist/questions.mjs', label: 'Questions', icon: 'Tag', width: 396 }] },
  },
}
const KEY = 'refs-app:questions'
const CONTROL: AppOpenTarget = { kind: 'sessionControl', id: 'questions' }

/** The chip as the composer draws it. */
function drawChip(key = KEY) {
  const chip = document.createElement('button')
  chip.setAttribute('data-session-control-key', key)
  document.body.appendChild(chip)
  return chip
}

const open = (slot: string, appName = 'refs-app', target: AppOpenTarget = CONTROL, payload: unknown = { label: 'Q4a' }) => {
  let r: ReturnType<typeof sendAppNav> = 'not_on_screen'
  act(() => { r = sendAppNav({ kind: 'open', slot, appName, target, payload, via: 'link' }) })
  return r
}

async function mountComposer(activeSlot = 'chat-1') {
  const hook = renderHookWithProviders(() =>
    useComposerSessionControls({ activeSlot, currentSlot: undefined, queryClient: new QueryClient() }),
  )
  await waitFor(() => expect(hook.result.current.sessionControls).toHaveLength(1))
  return hook
}

beforeEach(() => {
  vi.clearAllMocks()
  mockApps.mockResolvedValue([refsApp])
  ;(api.chatFolders as unknown as ReturnType<typeof vi.fn>).mockResolvedValue([])
  ;(api.appSessionStatus as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({ state: 'none' })
})
afterEach(() => {
  cleanup()
  document.body.replaceChildren()
  clearSessionControlRequest('dashboard:chat-1', KEY)
  setActiveStatusGo(null)
  vi.useRealTimers()
})

describe('the chat page opens an app\'s own session control', () => {
  it('opens it in the chat on screen, anchored on its chip, with the payload and where it came from', async () => {
    const { result } = await mountComposer()
    drawChip()
    expect(open('chat-1')).toBe(true)
    expect(result.current.openSessionControl).toEqual({ key: KEY, slot: 'chat-1' })
    expect(result.current.sessionControlRect).not.toBeNull()
    expect(peekSessionControlRequestForTest('dashboard:chat-1', KEY)).toMatchObject({ payload: { label: 'Q4a' }, via: 'link' })
    expect(result.current.sessionControls[0].width).toBe(396)
  })

  it('a second open of the same control keeps the popover (same state) and hands it the new payload', async () => {
    const { result } = await mountComposer()
    drawChip()
    open('chat-1', 'refs-app', CONTROL, { label: 'Q4a' })
    const first = result.current.openSessionControl
    const seq = peekSessionControlRequestForTest('dashboard:chat-1', KEY)?.seq ?? 0
    open('chat-1', 'refs-app', CONTROL, { label: 'Q5b' })
    expect(result.current.openSessionControl).toBe(first)
    expect(peekSessionControlRequestForTest('dashboard:chat-1', KEY)).toMatchObject({ payload: { label: 'Q5b' } })
    expect(peekSessionControlRequestForTest('dashboard:chat-1', KEY)!.seq).toBeGreaterThan(seq)
  })

  it('refuses with a named reason: another app or an undeclared control is not_declared; no chip, or another chat, not_on_screen', async () => {
    const { result } = await mountComposer()
    expect(open('chat-1')).toBe('not_on_screen')
    drawChip()
    expect(open('chat-1', 'other-app')).toBe('not_declared')
    expect(open('chat-1', 'refs-app', { kind: 'sessionControl', id: 'nope' })).toBe('not_declared')
    expect(open('chat-9')).toBe('not_on_screen')
    expect(result.current.openSessionControl).toBeNull()
    expect(peekSessionControlRequestForTest('dashboard:chat-1', KEY)).toBeNull()
  })

  it('closing the popover drops the request, so the chip opened by hand reads none', async () => {
    const { result } = await mountComposer()
    drawChip()
    open('chat-1')
    act(() => { result.current.setOpenSessionControl(null) })
    expect(peekSessionControlRequestForTest('dashboard:chat-1', KEY)).toBeNull()
  })
})

describe('useSessionControlRequest', () => {
  function Body() {
    const req = useSessionControlRequest()
    return <output>{req ? `${req.via}:${JSON.stringify(req.payload)}` : 'none'}</output>
  }
  const mountBody = (sessionKey: string, control: string) =>
    render(<SessionControlRequestScope sessionKey={sessionKey} control={control}><Body /></SessionControlRequestScope>)

  it('reads a request sent before it mounted, only its own, and none outside a control', () => {
    putSessionControlRequest('dashboard:chat-1', KEY, { label: 'Q4a' }, 'status')
    expect(mountBody('dashboard:chat-1', KEY).container.textContent).toBe('status:{"label":"Q4a"}')
    expect(mountBody('dashboard:chat-2', KEY).container.textContent).toBe('none')
    expect(mountBody('dashboard:chat-1', 'refs-app:other').container.textContent).toBe('none')
    expect(render(<Body />).container.textContent).toBe('none')
    act(() => clearSessionControlRequest('dashboard:chat-1', KEY))
    expect(screen.getAllByRole('status')[0].textContent).toBe('none')
  })
})

describe('useSessionControlJump', () => {
  const scoped = (sessionKey: string) => ({ children }: { children: ReactNode }) =>
    <SessionControlRequestScope sessionKey={sessionKey} control={KEY}>{children}</SessionControlRequestScope>
  /** A chat page that takes jumps for `slot` and answers each with `answer`. */
  function chatPage(slot: string, answer: Parameters<NonNullable<Extract<LinkPreviewNav, { kind: 'jump' }>['onDone']>>[0]) {
    const seen: LinkPreviewNav[] = []
    const off = onLinkPreviewNav((req) => {
      if (req.kind !== 'jump' || req.slot !== slot) return false
      seen.push(req)
      req.onDone?.(answer)
      return true
    })
    return { seen, off }
  }

  it('is null outside a session control', () => {
    expect(renderHook(() => useSessionControlJump()).result.current).toBeNull()
  })

  it("jumps the control's own chat, from the view on screen, and answers with how the jump ended", async () => {
    const page = chatPage('chat-1', true)
    try {
      const jump = renderHook(() => useSessionControlJump(), { wrapper: scoped('dashboard:chat-1') }).result.current!
      await expect(jump({ ts: 't4', id: 'm4' })).resolves.toEqual({ ok: true })
      expect(page.seen).toEqual([expect.objectContaining({ kind: 'jump', slot: 'chat-1', target: { ts: 't4', id: 'm4' }, from: 'view' })])
    } finally { page.off() }
    const gone = chatPage('chat-1', 'not_found')
    try {
      const jump = renderHook(() => useSessionControlJump(), { wrapper: scoped('dashboard:chat-1') }).result.current!
      await expect(jump({ ts: 'other-chat' })).resolves.toEqual({ ok: false, reason: 'not_found' })
    } finally { gone.off() }
  })

  it('refuses at once, sending nothing: a bad target is not_found, a chat not on screen not_on_screen', async () => {
    const page = chatPage('chat-1', true)
    try {
      const jump = renderHook(() => useSessionControlJump(), { wrapper: scoped('dashboard:chat-1') }).result.current!
      for (const bad of [{ ts: '' }, { ts: 7 }, { ts: 'x'.repeat(129) }, { id: 'm4' }, null]) {
        await expect(jump(bad as never)).resolves.toEqual({ ok: false, reason: 'not_found' })
      }
      expect(page.seen).toEqual([])
      // As the card's jump reads it: an id that is not a usable string is left out.
      for (const id of [null, '', 9]) {
        await expect(jump({ ts: 't4', id } as never)).resolves.toEqual({ ok: true })
      }
      expect(page.seen.map(r => r.kind === 'jump' && r.target)).toEqual([{ ts: 't4' }, { ts: 't4' }, { ts: 't4' }])
      page.seen.length = 0
      const elsewhere = renderHook(() => useSessionControlJump(), { wrapper: scoped('dashboard:chat-2') }).result.current!
      await expect(elsewhere({ ts: 't4' })).resolves.toEqual({ ok: false, reason: 'not_on_screen' })
      const notDashboard = renderHook(() => useSessionControlJump(), { wrapper: scoped('slack:C1') }).result.current!
      await expect(notDashboard({ ts: 't4' })).resolves.toEqual({ ok: false, reason: 'not_on_screen' })
    } finally { page.off() }
  })
})

describe('a status target can name a session control', () => {
  it('keeps sessionControl with its arg; a target naming both keeps panelTab; a bad id opens the chat alone', () => {
    expect(normalizeStatusTarget({ sid: 'chat-4', sessionControl: 'questions', arg: { label: 'Q4a' } }))
      .toEqual({ sid: 'chat-4', sessionControl: 'questions', arg: { label: 'Q4a' } })
    expect(normalizeStatusTarget({ sid: 'chat-4', panelTab: 'questions', sessionControl: 'questions', arg: 1 }))
      .toEqual({ sid: 'chat-4', panelTab: 'questions', arg: 1 })
    expect(normalizeStatusTarget({ sid: 'chat-4', sessionControl: '../x', arg: 1 })).toEqual({ sid: 'chat-4' })
  })

  it('a landed go hands its control over once, and a refusal is named once the hand-off gives up', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const opener = vi.fn().mockReturnValueOnce('not_declared').mockReturnValue(true)
    setActiveStatusGo({ to: 'chat-4', from: 'chat-1', appName: 'refs-app', pending: { target: CONTROL, arg: { label: 'Q4a' } } })
    const first = renderHook(() => useStatusLinks({ statusParam: null, activeSlot: 'chat-4', open: opener }))
    await act(async () => { await vi.advanceTimersByTimeAsync(250) })
    expect(opener).toHaveBeenCalledWith('chat-4', 'refs-app', CONTROL, { label: 'Q4a' })
    expect(opener).toHaveBeenCalledTimes(2)
    expect(getActiveStatusGo()?.pending).toBeNull()
    first.unmount()

    const refuse = vi.fn().mockReturnValue('not_declared')
    setActiveStatusGo({ to: 'chat-4', from: 'chat-1', appName: 'refs-app', pending: { target: CONTROL, arg: null } })
    renderHook(() => useStatusLinks({ statusParam: null, activeSlot: 'chat-4', open: refuse }))
    await act(async () => { await vi.advanceTimersByTimeAsync(5_400) })
    expect(getActiveStatusGo()?.pending).toBeNull()
    expect(warn).toHaveBeenCalledWith(expect.stringContaining('not_declared'))
    warn.mockRestore()
  })
})

describe('SessionControlHost: an open request wins over the outside click that asked for it', () => {
  const control: ResolvedSessionControl = {
    key: KEY, appName: 'refs-app', appDisplayName: 'Refs', appVersion: '1', id: 'questions',
    entryPoint: 'dist/questions.mjs', label: 'Questions', icon: 'Tag', allowedApi: [], allowedEvents: [],
    statusPath: '', processBacked: false, width: 396,
  }
  const rect = { left: 10, top: 600, width: 40, height: 28, right: 50, bottom: 628, x: 10, y: 600, toJSON: () => ({}) } as DOMRect
  const session = { sessionKey: 'dashboard:chat-1', cwd: '' }
  function message() {
    const scope = document.createElement('div')
    scope.setAttribute('data-link-preview-scope', '')
    const label = document.createElement('span')
    label.setAttribute('data-link-preview-text', '')
    label.textContent = '[Q4a]'
    scope.appendChild(label)
    document.body.appendChild(scope)
    return label
  }
  const host = (onClose: () => void) =>
    render(<MemoryRouter><SessionControlHost control={control} session={session} anchorRect={rect} onClose={onClose} /></MemoryRouter>)
  /** A whole press: the button goes down and comes back up. */
  const press = (el: Element) => { fireEvent.mouseDown(el); fireEvent.mouseUp(el) }

  it('a press on a preview label holds the close; a new open in the bound keeps it mounted, none closes it', () => {
    vi.useFakeTimers()
    const onClose = vi.fn()
    host(onClose)
    const dialog = screen.getByRole('dialog')
    const label = message()
    press(label)
    act(() => { putSessionControlRequest('dashboard:chat-1', KEY, { label: 'Q5b' }, 'link') })
    act(() => { vi.advanceTimersByTime(CLICK_GRANT_MS + 1) })
    expect(onClose).not.toHaveBeenCalled()
    expect(screen.getByRole('dialog')).toBe(dialog)
    press(label)
    act(() => { vi.advanceTimersByTime(CLICK_GRANT_MS + 1) })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('a press on the Back pill after a jump leaves the popover open', () => {
    const onClose = vi.fn()
    host(onClose)
    const pill = document.createElement('button')
    pill.setAttribute('data-session-control-keep', '')
    pill.appendChild(document.createElement('span'))
    document.body.appendChild(pill)
    fireEvent.mouseDown(pill.firstChild!)
    expect(onClose).not.toHaveBeenCalled()
  })

  it('any other outside press still closes at once, a press on a chat link too', () => {
    const onClose = vi.fn()
    host(onClose)
    fireEvent.mouseDown(document.body)
    expect(onClose).toHaveBeenCalledTimes(1)
    const scope = document.createElement('div')
    scope.setAttribute('data-link-preview-scope', '')
    const link = document.createElement('a')
    link.href = 'https://example.com/'
    scope.appendChild(link)
    document.body.appendChild(scope)
    fireEvent.mouseDown(link)
    expect(onClose).toHaveBeenCalledTimes(2)
  })

  it('a press back in the popover ends the hold, so it does not close later', () => {
    vi.useFakeTimers()
    const onClose = vi.fn()
    host(onClose)
    press(message())
    press(screen.getByRole('dialog'))
    act(() => { vi.advanceTimersByTime(CLICK_GRANT_MS * 3) })
    expect(onClose).not.toHaveBeenCalled()
  })

  it('the hold lasts while a click may still open (a card whose frame is loading), and closes soon after it no longer may', () => {
    vi.useFakeTimers()
    // The worst-case bound while a frame loads: its ready timeout, then the grant.
    let end = performance.now() + 13_000
    const unprobe = setClickOpenProbe(() => end)
    try {
      const onClose = vi.fn()
      host(onClose)
      press(message())
      act(() => { vi.advanceTimersByTime(CLICK_GRANT_MS + 1) })
      expect(onClose).not.toHaveBeenCalled()
      act(() => { vi.advanceTimersByTime(1_500) })
      expect(onClose).not.toHaveBeenCalled()
      // The frame was ready and its card opened nothing: no open is possible any more.
      end = 0
      act(() => { vi.advanceTimersByTime(300) })
      expect(onClose).toHaveBeenCalledTimes(1)
    } finally {
      unprobe()
    }
  })

  it('a press held past the grant keeps the popover until the button comes up (the click shows the card)', () => {
    vi.useFakeTimers()
    const onClose = vi.fn()
    host(onClose)
    const label = message()
    fireEvent.mouseDown(label)
    act(() => { vi.advanceTimersByTime(CLICK_GRANT_MS + 2_000) })
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.mouseUp(label)
    act(() => { putSessionControlRequest('dashboard:chat-1', KEY, { label: 'Q5b' }, 'link') })
    act(() => { vi.advanceTimersByTime(CLICK_GRANT_MS) })
    expect(onClose).not.toHaveBeenCalled()
    press(label)
    act(() => { vi.advanceTimersByTime(CLICK_GRANT_MS + 300) })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('draws the width the app asked for on a desktop window, the default on a phone', () => {
    const width = window.innerWidth
    try {
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1280 })
      host(() => {})
      expect(screen.getByRole('dialog').style.width).toBe('396px')
      cleanup()
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: 600 })
      host(() => {})
      expect(screen.getByRole('dialog').style.width).toBe('340px')
    } finally {
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: width })
    }
  })
})
