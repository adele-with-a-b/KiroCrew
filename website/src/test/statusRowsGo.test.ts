/**
 * Status items whose card lists rows that go somewhere: the app's untrusted
 * row / go / target answer, the `?status=` card request, the go in flight and
 * its way back, and the go key.
 */
import { afterEach, describe, it, expect, vi } from 'vitest'
import { act } from '@testing-library/react'

import {
  MAX_STATUS_ROWS,
  MAX_TARGET_ARG_BYTES,
  firePrimaryStatusGo,
  getActiveStatusGo,
  normalizeStatusGo,
  normalizeStatusRows,
  normalizeStatusTarget,
  parseStatusCardParam,
  setActiveStatusGo,
  setPrimaryStatusGo,
} from '../shell/topbar/statusGo'
import { STATUS_CARD_WAIT_MS, requestStatusCard, takeStatusCardRequest } from '../shell/topbar/statusCardRequest'
import { STATUS_GO_CHORD, chordMatchesEvent, shortcutEntry } from '../lib/shortcutRegistry'
import { renderHookWithProviders } from './helpers'
import { useStatusLinks } from '../pages/chat/useStatusLinks'

const target = { sid: 'chat-12-1795201321', panelTab: 'questions', arg: { view: 'waiting', label: 'Q4a' } }

afterEach(() => {
  // In act: a hook from the test may still be subscribed to the go store.
  act(() => { setActiveStatusGo(null) })
  setPrimaryStatusGo(null)
  takeStatusCardRequest(['x:y'], Number.MAX_SAFE_INTEGER)
  vi.useRealTimers()
})

describe('normalizeStatusTarget', () => {
  it('keeps a well-formed target', () => {
    expect(normalizeStatusTarget(target)).toEqual(target)
  })

  it('refuses a missing or malformed sid', () => {
    for (const sid of [undefined, '', 'a b', 'x'.repeat(129), '../etc', '-lead', 42]) {
      expect(normalizeStatusTarget({ ...target, sid })).toBeNull()
    }
  })

  it('opens the chat alone for a malformed tab id, dropping its arg', () => {
    expect(normalizeStatusTarget({ ...target, panelTab: 'Questions!' })).toEqual({ sid: target.sid })
  })

  it('drops an oversized arg but still opens the tab', () => {
    const big = { blob: 'x'.repeat(MAX_TARGET_ARG_BYTES) }
    expect(normalizeStatusTarget({ ...target, arg: big })).toEqual({ sid: target.sid, panelTab: 'questions' })
  })

  it('drops an arg that is not JSON, and keeps a copy rather than the app object', () => {
    const cyclic: Record<string, unknown> = {}
    cyclic.self = cyclic
    expect(normalizeStatusTarget({ ...target, arg: cyclic })?.arg).toBeUndefined()
    const arg = { view: 'waiting' }
    const kept = normalizeStatusTarget({ ...target, arg })?.arg
    expect(kept).toEqual(arg)
    expect(kept).not.toBe(arg)
  })
})

describe('normalizeStatusRows / normalizeStatusGo', () => {
  it('reads a row', () => {
    expect(normalizeStatusRows([{ lead: '1h 12m', text: 'Blick', detail: 'Q3a Ship it?', tone: 'danger', target }]))
      .toEqual([{ lead: '1h 12m', text: 'Blick', detail: 'Q3a Ship it?', tone: 'danger', target }])
  })

  it('caps the rows and maps an unknown tone to muted', () => {
    const rows = Array.from({ length: MAX_STATUS_ROWS + 5 }, (_, i) => ({ text: `c${i}`, tone: 'pink', target }))
    const out = normalizeStatusRows(rows)
    expect(out).toHaveLength(MAX_STATUS_ROWS)
    expect(out.every(r => r.tone === 'muted')).toBe(true)
  })

  it('draws a row with a bad target as text, and skips an empty row', () => {
    const out = normalizeStatusRows([{ text: 'Blick', target: { sid: '' } }, { lead: '4m' }, 'junk', null])
    expect(out).toEqual([{ lead: '', text: 'Blick', detail: '', tone: 'muted' }])
  })

  it('strips control and bidi characters and caps by code point', () => {
    const [row] = normalizeStatusRows([{ text: 'Bl\u202Eick\u0007', detail: '😀'.repeat(200), target }])
    expect(row.text).toBe('Blick')
    expect(Array.from(row.detail)).toHaveLength(160)
  })

  it('drops a go with a bad target and keeps an empty label for the host default', () => {
    expect(normalizeStatusGo({ label: 'Go', target: { sid: 'bad sid' } })).toBeNull()
    expect(normalizeStatusGo({ target })).toEqual({ label: '', target })
    expect(normalizeStatusRows(undefined)).toEqual([])
  })
})

describe('?status=<app>:<item>', () => {
  it('parses an item key and refuses anything else', () => {
    expect(parseStatusCardParam('ref-links:waiting')).toBe('ref-links:waiting')
    for (const v of [null, '', 'ref-links', ':waiting', 'a:b:c', 'A B:c', 'x'.repeat(200) + ':y']) {
      expect(parseStatusCardParam(v)).toBeNull()
    }
  })

  it('is taken once, by the bar that holds the item, while fresh', () => {
    requestStatusCard('ref-links:waiting', 1000)
    expect(takeStatusCardRequest(['other:item'], 1000)).toBeNull()
    expect(takeStatusCardRequest(['ref-links:waiting'], 1000 + STATUS_CARD_WAIT_MS)).toBe('ref-links:waiting')
    expect(takeStatusCardRequest(['ref-links:waiting'], 1000 + STATUS_CARD_WAIT_MS)).toBeNull()
  })

  it('opens nothing once the wait is over', () => {
    requestStatusCard('ref-links:waiting', 1000)
    expect(takeStatusCardRequest(['ref-links:waiting'], 1001 + STATUS_CARD_WAIT_MS)).toBeNull()
  })

  it('the chat page hands the URL parameter to the bar', () => {
    renderHookWithProviders(() => useStatusLinks({ statusParam: 'ref-links:waiting', activeSlot: null, open: () => 'not_on_screen' }))
    expect(takeStatusCardRequest(['ref-links:waiting'])).toBe('ref-links:waiting')
  })
})

describe('a go landing on its chat', () => {
  const go = { to: target.sid, from: 'chat-1-1', appName: 'ref-links', pending: { target: { kind: 'panelTab' as const, id: 'questions' }, arg: target.arg } }

  it('hands its tab request over once, then keeps the way back', () => {
    const open = vi.fn(() => true)
    setActiveStatusGo(go)
    renderHookWithProviders(() => useStatusLinks({ statusParam: null, activeSlot: target.sid, open }))
    expect(open).toHaveBeenCalledTimes(1)
    expect(open).toHaveBeenCalledWith(target.sid, 'ref-links', { kind: 'panelTab', id: 'questions' }, target.arg)
    expect(getActiveStatusGo()).toEqual({ ...go, pending: null })
  })

  it('retries while the panel cannot take it yet, and gives up', () => {
    vi.useFakeTimers()
    const open = vi.fn(() => 'not_on_screen' as const)
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    setActiveStatusGo(go)
    renderHookWithProviders(() => useStatusLinks({ statusParam: null, activeSlot: target.sid, open }))
    act(() => { vi.advanceTimersByTime(1000) })
    expect(open.mock.calls.length).toBeGreaterThan(1)
    act(() => { vi.advanceTimersByTime(5000) })
    expect(getActiveStatusGo()?.pending).toBeNull()
    const calls = open.mock.calls.length
    act(() => { vi.advanceTimersByTime(1000) })
    expect(open.mock.calls.length).toBe(calls)
    expect(warn).toHaveBeenCalledTimes(1)
    warn.mockRestore()
  })

  it('ends when another chat is on screen', () => {
    setActiveStatusGo(go)
    renderHookWithProviders(() => useStatusLinks({ statusParam: null, activeSlot: 'chat-9-9', open: () => true }))
    expect(getActiveStatusGo()).toBeNull()
  })
})

describe('the go key', () => {
  it('is one constant, the registry default on every platform', () => {
    const e = shortcutEntry('status-go')
    expect(STATUS_GO_CHORD).toEqual({ key: 'g', alt: true, shift: true })
    expect(e?.dispatch).toBe('registry')
    expect(e?.defaults.mac).toBe(STATUS_GO_CHORD)
    expect(e?.defaults.other).toBe(STATUS_GO_CHORD)
  })

  it('matches by position, whatever character the layout or Option types', () => {
    const ev = { code: 'KeyG', key: '˝', metaKey: false, ctrlKey: false, altKey: true, shiftKey: true }
    expect(chordMatchesEvent(ev, STATUS_GO_CHORD, 'mac')).toBe(true)
    expect(chordMatchesEvent({ ...ev, key: 'g', code: 'KeyH' }, STATUS_GO_CHORD, 'mac')).toBe(false)
    expect(chordMatchesEvent({ ...ev, shiftKey: false }, STATUS_GO_CHORD, 'other')).toBe(false)
  })

  it('fires the registered primary go, and says when there is none', () => {
    expect(firePrimaryStatusGo()).toBe(false)
    const fn = vi.fn()
    setPrimaryStatusGo(fn)
    expect(firePrimaryStatusGo()).toBe(true)
    expect(fn).toHaveBeenCalledTimes(1)
  })

  it("clears only its own registration, so one bar unmounting keeps another's go", () => {
    const first = vi.fn()
    const second = vi.fn()
    const undoFirst = setPrimaryStatusGo(first)
    const undoSecond = setPrimaryStatusGo(second)
    undoFirst()
    expect(firePrimaryStatusGo()).toBe(true)
    expect(second).toHaveBeenCalledTimes(1)
    undoSecond()
    expect(firePrimaryStatusGo()).toBe(false)
  })
})
