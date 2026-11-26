/**
 * App Kit level 18: a docked session control puts text in its chat's message box
 * (`useInsertIntoComposer`). The composer's one app-text write (`useComposerActions`'
 * `put`, which the "+" menu's composer actions use too) joins it at the caret beside
 * the draft and focuses the box; the box's gate (`useDockedBoxInsert`) answers only
 * while the box is open docked; the SDK hook takes a call only within
 * `INSERT_ACT_MS` of the person's last act on the control; and the app reads back
 * `{ ok }` or `{ ok, code }`, never the draft.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// jsdom marks every event it is handed as untrusted; a test stands in a real one.
const trust = vi.hoisted(() => ({ next: true }))
vi.mock('../components/trustedClick', () => ({ isTrustedClick: () => trust.next, isTrustedKey: () => trust.next, isTrustedEvent: () => trust.next }))
vi.mock('@radix-ui/react-dropdown-menu', async () => await import('./__mocks__/@radix-ui/react-dropdown-menu'))
vi.mock('@radix-ui/react-popover', async () => await import('./__mocks__/@radix-ui/react-popover'))
vi.mock('../api/client', () => ({ api: {} }))

import { Suspense } from 'react'
import { createPortal } from 'react-dom'
import { act, cleanup, fireEvent, render, renderHook, screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { SessionControlChips } from '../components/chat-input/ContextShelf'
import { useComposerActions, type AppTextInsert } from '../components/chat-input/composerActions'
import type { ComposerControl } from '../components/composerControl'
import { useDockedBoxInsert } from '../components/sessionControlInsert'
import {
  INSERT_ACT_MS,
  SessionControlRequestScope,
  clearSessionControlRequest,
  insertActClock,
  noteSessionControlAct,
  putSessionControlRequest,
  useInsertIntoComposer,
  type ComposerInsertResult,
  type ScopeComposerInsert,
} from '../app-sdk/sessionControlRequest'

const clock = { t: 1_000 }
const SESSION = 'dashboard:chat-2'
const CONTROL = 'refs-app:inbox'

type BoxOpts = { caret?: { start: number; end: number } | null; locked?: boolean; paused?: boolean; slot?: string; clamp?: (t: string, at: number) => number }

/** The composer's live box: its draft, caret, focus, and the hook's insert. */
function composer(initial: string, opts: BoxOpts = {}) {
  const state = { value: initial, selection: null as [number, number, boolean] | null, writes: [] as string[] }
  const control: ComposerControl = {
    focus: vi.fn(),
    getRootElement: () => null,
    getSelection: () => null,
    setSelection: (start, end, o) => { state.selection = [start, end ?? start, !!o?.focus] },
  }
  const insertRef: { current: AppTextInsert | null } = { current: null }
  const caretRef = { current: opts.caret ?? null }
  const onChange = vi.fn((v: string) => { state.value = v; state.writes.push(v) })
  const hook = renderHook(({ value }) => useComposerActions({
    items: [], value, onChange, pasteBlocks: [], composerControl: () => control, scopeKey: opts.slot ?? 'chat-2',
    locked: opts.locked, anchor: () => null, insertRef, caretRef, clampOffset: opts.clamp, insertPaused: opts.paused,
  }), { initialProps: { value: initial } })
  return { state, onChange, insertRef, caretRef, hook }
}

beforeEach(() => {
  // The write places the caret on the next frame; run frames at once.
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { cb(0); return 0 })
  // Forward, never back: an act an earlier case noted is long past here.
  clock.t += 10 * INSERT_ACT_MS
  insertActClock.now = () => clock.t
  trust.next = true
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  insertActClock.now = () => performance.now()
})

describe("the composer's insert at the caret", () => {
  it('joins at the caret, keeps the draft, and focuses the box with the caret after the text', () => {
    const c = composer('hello world', { caret: { start: 5, end: 5 } })
    expect(c.insertRef.current!('chat-2', 'Q4a')).toBeUndefined()
    expect(c.onChange).toHaveBeenCalledWith('hello Q4a world')
    expect(c.state.selection).toEqual([9, 9, true])
    expect(c.caretRef.current).toEqual({ start: 9, end: 9 })
  })

  it('goes at the end when the person placed no caret, with one space before it', () => {
    const c = composer('thanks')
    expect(c.insertRef.current!('chat-2', 'Q4a agreed')).toBeUndefined()
    expect(c.onChange).toHaveBeenCalledWith('thanks Q4a agreed')
    expect(c.state.selection).toEqual([17, 17, true])
  })

  it('puts no space into an empty box or after a newline, and keeps a selection, going after it', () => {
    expect(composer('').insertRef.current!('chat-2', 'Q4a') ?? 'ok').toBe('ok')
    const afterNewline = composer('notes\n')
    afterNewline.insertRef.current!('chat-2', 'Q4a')
    expect(afterNewline.onChange).toHaveBeenCalledWith('notes\nQ4a')
    const selected = composer('pick this one', { caret: { start: 5, end: 9 } })
    selected.insertRef.current!('chat-2', 'Q4a')
    expect(selected.onChange).toHaveBeenCalledWith('pick this Q4a one')
  })

  it("keeps the text's trailing space, but adds no second one before a space already there", () => {
    const atEnd = composer('hello')
    atEnd.insertRef.current!('chat-2', 'Q3a ')
    expect(atEnd.onChange).toHaveBeenCalledWith('hello Q3a ')
    const mid = composer('hello there', { caret: { start: 5, end: 5 } })
    mid.insertRef.current!('chat-2', 'Q3a ')
    expect(mid.onChange).toHaveBeenCalledWith('hello Q3a there')
    expect(mid.state.selection).toEqual([9, 9, true])
  })

  it("clamps the caret out of a token with the host's clamp", () => {
    const c = composer('see @a.ts now', { caret: { start: 6, end: 6 }, clamp: () => 9 })
    c.insertRef.current!('chat-2', 'Q4a')
    expect(c.onChange).toHaveBeenCalledWith('see @a.ts Q4a now')
  })

  it.each([
    ['inside a $skill', 'run $deploy', 6, 'run $deploy Q3a'],
    ['in front of a mention', '@src/a.ts', 0, '@src/a.ts Q3a'],
    ['inside a mention', 'see @src/a.ts now', 8, 'see @src/a.ts Q3a now'],
    ['inside a command at a line start', 'notes\n/clear', 8, 'notes\n/clear Q3a'],
  ])("never splits or sticks to the person's own token: a caret %s moves to its end", (_name, draft, at, expected) => {
    const c = composer(draft, { caret: { start: at, end: at } })
    expect(c.insertRef.current!('chat-2', 'Q3a')).toBeUndefined()
    expect(c.onChange).toHaveBeenCalledWith(expected)
  })

  it('reads the draft as it is at the call, not as it was', () => {
    const c = composer('a')
    c.hook.rerender({ value: 'a typed later' })
    c.insertRef.current!('chat-2', 'Q4a')
    expect(c.onChange).toHaveBeenCalledWith('a typed later Q4a')
  })

  it('lands two calls in one handler both, in order, the second after the first', () => {
    const c = composer('hello')
    expect(c.insertRef.current!('chat-2', 'Q4a')).toBeUndefined()
    expect(c.insertRef.current!('chat-2', 'Q4b')).toBeUndefined()
    expect(c.state.writes).toEqual(['hello Q4a', 'hello Q4a Q4b'])
    expect(c.state.selection).toEqual([13, 13, true])
  })

  it.each<[string, BoxOpts, string | null]>([
    ['another chat in the box', {}, 'chat-9'],
    ['a typing lock', { locked: true }, 'chat-2'],
    ['a box that takes no text now (terminal command, disabled, optimizer)', { paused: true }, 'chat-2'],
  ])('answers stale to %s and leaves the box alone', (_name, opts, slot) => {
    const c = composer('draft', opts)
    expect(c.insertRef.current!(slot, 'Q4a')).toBe('stale')
    expect(c.onChange).not.toHaveBeenCalled()
    expect(c.state.selection).toBeNull()
  })

  it('refuses text that joins the draft into a skill, as not_literal', () => {
    const c = composer('please deploy', { caret: { start: 7, end: 7 } })
    expect(c.insertRef.current!('chat-2', 'run $')).toBe('not_literal')
    expect(c.onChange).not.toHaveBeenCalled()
  })

  it('takes 32,000 characters exactly', () => {
    const c = composer('')
    expect(c.insertRef.current!('chat-2', 'x'.repeat(32_000))).toBeUndefined()
  })

  it('lets go of the ref when the composer unmounts', () => {
    const c = composer('')
    c.hook.unmount()
    expect(c.insertRef.current).toBeNull()
  })
})

describe('a text refused on its own answers the same in every box state', () => {
  const REFUSED: [string, unknown, string][] = [
    ['over 32,000 characters', 'x'.repeat(32_001), 'too_large'],
    ['not a string', 42, 'not_allowed'],
    ['blank once cleaned', ' \u200b\n', 'not_allowed'],
    ['a command', '/clear', 'not_literal'],
    ['a mention', 'see @notes.txt', 'not_literal'],
  ]
  const STATES: [string, BoxOpts, string | null][] = [
    ['a box that would take text', {}, 'chat-2'],
    ['another chat', {}, 'chat-9'],
    ['a typing lock', { locked: true }, 'chat-2'],
    ['a terminal command in the box', { paused: true }, 'chat-2'],
  ]

  it.each(REFUSED)('in the composer: %s', (_name, text, code) => {
    for (const [, opts, slot] of STATES) {
      const c = composer('! rm -rf ~', opts)
      expect(c.insertRef.current!(slot, text)).toBe(code)
      expect(c.onChange).not.toHaveBeenCalled()
      cleanup()
    }
  })

  it.each(REFUSED)('through the SDK, act or no act, box open or gone: %s', (_name, text, code) => {
    const box = (insert: ScopeComposerInsert) => insertResultOf(insert, text)
    expect(box(() => undefined)).toEqual({ ok: false, code })
    expect(box(() => 'stale')).toEqual({ ok: false, code })
    expect(box(() => 'not_allowed')).toEqual({ ok: false, code })
    clock.t += INSERT_ACT_MS + 1
    expect(insertResultOf(() => undefined, text, false)).toEqual({ ok: false, code })
  })
})

describe("the box's gate", () => {
  const passed: unknown[] = []
  const insert: ScopeComposerInsert = (text) => { passed.push(text); return undefined }
  beforeEach(() => { passed.length = 0 })

  it('hands no insert to a control that may not insert', () => {
    const { result } = renderHook(() => useDockedBoxInsert({ composerInsert: false, docked: true, crewHidden: false, insertIntoComposer: insert }))
    expect(result.current).toBeUndefined()
  })

  it('passes the text on while the box is open docked', () => {
    const { result } = renderHook(() => useDockedBoxInsert({ composerInsert: true, docked: true, crewHidden: false, insertIntoComposer: insert }))
    expect(result.current!('Q4a')).toBeUndefined()
    expect(passed).toEqual(['Q4a'])
  })

  it('answers not_allowed while drawn as a popover or once the grant is withdrawn, stale behind a remote Crew and once closed', () => {
    type P = { docked: boolean; crewHidden: boolean; composerInsert: boolean }
    const { result, rerender, unmount } = renderHook((p: P) => useDockedBoxInsert({ ...p, insertIntoComposer: insert }), { initialProps: { docked: false, crewHidden: false, composerInsert: true } })
    const held = result.current!
    expect(held('Q4a')).toBe('not_allowed')
    rerender({ docked: true, crewHidden: true, composerInsert: true })
    expect(held('Q4a')).toBe('stale')
    rerender({ docked: true, crewHidden: false, composerInsert: true })
    expect(held('Q4a')).toBeUndefined()
    rerender({ docked: true, crewHidden: false, composerInsert: false })
    expect(result.current).toBeUndefined()
    expect(held('Q4a')).toBe('not_allowed')
    unmount()
    // A handle the module kept after the box closed.
    expect(held('Q4a')).toBe('stale')
    expect(passed).toEqual(['Q4a'])
  })
})

describe('useInsertIntoComposer', () => {
  function Box({ seen, held }: { seen: ComposerInsertResult[]; held?: { fn: ((t: string) => ComposerInsertResult) | null } }) {
    const insertIntoComposer = useInsertIntoComposer()
    if (held) held.fn = insertIntoComposer
    // From a key handler, with no click.
    return <input aria-label="answer" onKeyDown={(e) => { if (e.key === 'Enter') seen.push(insertIntoComposer!('Q4a agreed')) }} />
  }

  it('is null outside a session control, and in a box whose control has no approved composerInsert', () => {
    expect(renderHook(() => useInsertIntoComposer()).result.current).toBeNull()
    const held: { fn: ((t: string) => ComposerInsertResult) | null } = { fn: () => ({ ok: true }) }
    render(<SessionControlRequestScope sessionKey={SESSION} control={CONTROL}><Box seen={[]} held={held} /></SessionControlRequestScope>)
    expect(held.fn).toBeNull()
  })

  it('works from a key handler, and the app gets back only ok or a code', () => {
    const seen: ComposerInsertResult[] = []
    const c = composer('my own words')
    const insert: ScopeComposerInsert = (text) => c.insertRef.current!('chat-2', text)
    render(<SessionControlRequestScope sessionKey={SESSION} control={CONTROL} insert={insert}><Box seen={seen} /></SessionControlRequestScope>)
    fireEvent.keyDown(screen.getByLabelText('answer'), { key: 'Enter' })
    expect(c.onChange).toHaveBeenCalledWith('my own words Q4a agreed')
    // Nothing of the draft, the caret or a send rides on the answer.
    expect(seen).toEqual([{ ok: true }])
    expect(Object.keys(seen[0])).toEqual(['ok'])
    expect(JSON.stringify(seen)).not.toContain('own words')
    const refused = insertResultOf(() => 'not_literal', 'x')
    expect(refused).toEqual({ ok: false, code: 'not_literal' })
    expect(Object.keys(refused).sort()).toEqual(['code', 'ok'])
  })

  it(`takes a call within ${INSERT_ACT_MS} ms of the person opening the box, and answers no_gesture after`, () => {
    const held: { fn: ((t: string) => ComposerInsertResult) | null } = { fn: null }
    const calls: unknown[] = []
    render(<SessionControlRequestScope sessionKey={SESSION} control={CONTROL} insert={(t) => { calls.push(t); return undefined }}><Box seen={[]} held={held} /></SessionControlRequestScope>)
    // The host's note for the person's click on the chip or press of its key.
    noteSessionControlAct(SESSION, CONTROL)
    clock.t += INSERT_ACT_MS
    expect(held.fn!('Q4a')).toEqual({ ok: true })
    clock.t += 1
    expect(held.fn!('Q4b')).toEqual({ ok: false, code: 'no_gesture' })
    expect(calls).toEqual(['Q4a'])
  })

  it('restarts the window on a trusted key or click in the box and on the host noting the person, never on an untrusted event or a request', () => {
    const held: { fn: ((t: string) => ComposerInsertResult) | null } = { fn: null }
    render(<SessionControlRequestScope sessionKey={SESSION} control={CONTROL} insert={() => undefined}><Box seen={[]} held={held} /></SessionControlRequestScope>)
    const late = () => { clock.t += INSERT_ACT_MS + 1 }
    trust.next = false
    fireEvent.keyDown(screen.getByLabelText('answer'), { key: 'a' })
    fireEvent.click(screen.getByLabelText('answer'))
    expect(held.fn!('x')).toEqual({ ok: false, code: 'no_gesture' })
    trust.next = true
    fireEvent.keyDown(screen.getByLabelText('answer'), { key: 'a' })
    expect(held.fn!('x')).toEqual({ ok: true })
    late()
    fireEvent.click(screen.getByLabelText('answer'))
    expect(held.fn!('x')).toEqual({ ok: true })
    late()
    // A request is no act on the box, whoever sent it: the host notes the person's
    // own key where it hears it (`useComposerSessionControls`).
    for (const via of ['link', 'status', 'control', 'key'] as const) {
      act(() => { putSessionControlRequest(SESSION, CONTROL, { view: 'questions' }, via) })
      expect(held.fn!('x')).toEqual({ ok: false, code: 'no_gesture' })
    }
    noteSessionControlAct(SESSION, CONTROL)
    expect(held.fn!('x')).toEqual({ ok: true })
    // The person acting on another chat's box opens no window here.
    late()
    noteSessionControlAct('dashboard:chat-9', CONTROL)
    expect(held.fn!('x')).toEqual({ ok: false, code: 'no_gesture' })
  })

  it('drops the act when the box closes, so a box the app opens again starts with no window', () => {
    const held: { fn: ((t: string) => ComposerInsertResult) | null } = { fn: null }
    render(<SessionControlRequestScope sessionKey={SESSION} control={CONTROL} insert={() => undefined}><Box seen={[]} held={held} /></SessionControlRequestScope>)
    noteSessionControlAct(SESSION, CONTROL)
    expect(held.fn!('x')).toEqual({ ok: true })
    clearSessionControlRequest(SESSION, CONTROL)
    act(() => { putSessionControlRequest(SESSION, CONTROL, null, 'link') })
    expect(held.fn!('x')).toEqual({ ok: false, code: 'no_gesture' })
  })

  it('opens no window when the box mounts, even for a module that suspends first and chooses when it mounts', async () => {
    const held: { fn: ((t: string) => ComposerInsertResult) | null } = { fn: null }
    render(<SessionControlRequestScope sessionKey={SESSION} control={CONTROL} insert={() => undefined}><Box seen={[]} held={held} /></SessionControlRequestScope>)
    expect(held.fn!('x')).toEqual({ ok: false, code: 'no_gesture' })
    cleanup()
    // The person opens the box; the module holds its first render past the window.
    let release = () => {}
    const loaded = { done: false }
    const ready = new Promise<void>((resolve) => { release = resolve }).then(() => { loaded.done = true })
    function Slow() {
      if (!loaded.done) throw ready
      return <Box seen={[]} held={held} />
    }
    held.fn = null
    noteSessionControlAct(SESSION, CONTROL)
    render(<Suspense fallback={null}><SessionControlRequestScope sessionKey={SESSION} control={CONTROL} insert={() => undefined}><Slow /></SessionControlRequestScope></Suspense>)
    expect(held.fn).toBeNull()
    clock.t += INSERT_ACT_MS + 1
    await act(async () => { release(); await ready })
    expect(held.fn!('x')).toEqual({ ok: false, code: 'no_gesture' })
  })

  it("counts a key or click on the box's own elements only, not on a portal its module draws elsewhere", () => {
    const held: { fn: ((t: string) => ComposerInsertResult) | null } = { fn: null }
    function Spread() {
      held.fn = useInsertIntoComposer()
      return <>{createPortal(<button type="button">elsewhere</button>, document.body)}<input aria-label="answer" /></>
    }
    render(<SessionControlRequestScope sessionKey={SESSION} control={CONTROL} insert={() => undefined}><Spread /></SessionControlRequestScope>)
    fireEvent.click(screen.getByText('elsewhere'))
    fireEvent.keyDown(screen.getByText('elsewhere'), { key: 'a' })
    expect(held.fn!('x')).toEqual({ ok: false, code: 'no_gesture' })
    fireEvent.click(screen.getByLabelText('answer'))
    expect(held.fn!('x')).toEqual({ ok: true })
  })
})

describe("the control's chip", () => {
  it('says whether a click on it was the person\'s own, so only that one opens the insert window', () => {
    const onSessionControlClick = vi.fn()
    renderWithProviders(<SessionControlChips sessionControls={[{ key: CONTROL, label: 'Inbox', icon: '' }]} shelfCompact={false} onSessionControlClick={onSessionControlClick} />)
    const chip = screen.getByRole('button', { name: 'Inbox' })
    trust.next = false
    fireEvent.click(chip)
    trust.next = true
    fireEvent.click(chip)
    expect(onSessionControlClick.mock.calls.map(c => [c[0], c[3]])).toEqual([[CONTROL, false], [CONTROL, true]])
  })
})

/** One call through the SDK hook with `insert` as the host's answer, `fresh`: right after the box opened. */
function insertResultOf(insert: ScopeComposerInsert, text: unknown, fresh = true): ComposerInsertResult {
  let fn: ((t: string) => ComposerInsertResult) | null = null
  function Probe() {
    fn = useInsertIntoComposer()
    return null
  }
  const { unmount } = render(<SessionControlRequestScope sessionKey="dashboard:chat-probe" control={CONTROL} insert={insert}><Probe /></SessionControlRequestScope>)
  noteSessionControlAct('dashboard:chat-probe', CONTROL)
  if (!fresh) clock.t += INSERT_ACT_MS + 1
  const out = (fn as unknown as (t: unknown) => ComposerInsertResult)(text)
  unmount()
  return out
}

describe('ChatInput', () => {
  const props = (over: Record<string, unknown> = {}) => ({ value: 'hello', onChange: vi.fn(), onSend: vi.fn(), connected: true, ...over })

  it('fills the ref with its insert, which writes through its own onChange', () => {
    const appInsertRef: { current: AppTextInsert | null } = { current: null }
    const p = props({ appInsertRef })
    renderWithProviders(<ChatInput {...p} />)
    let code: unknown = 'unset'
    act(() => { code = appInsertRef.current!(null, 'Q4a agreed') })
    expect(code).toBeUndefined()
    expect(p.onChange).toHaveBeenCalledWith('hello Q4a agreed')
    expect(p.onSend).not.toHaveBeenCalled()
  })

  it('answers stale while another tab holds its typing lock, and while disabled', () => {
    const locked: { current: AppTextInsert | null } = { current: null }
    renderWithProviders(<ChatInput {...props({ appInsertRef: locked, typingLock: { holderName: 'Alice' } })} />)
    expect(locked.current!(null, 'Q4a')).toBe('stale')
    cleanup()
    const disabled: { current: AppTextInsert | null } = { current: null }
    renderWithProviders(<ChatInput {...props({ appInsertRef: disabled, disabled: true })} />)
    expect(disabled.current!(null, 'Q4a')).toBe('stale')
  })
})
