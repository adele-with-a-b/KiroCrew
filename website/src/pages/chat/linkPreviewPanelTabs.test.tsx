import { describe, it, expect, vi } from 'vitest'
import { act, render, renderHook } from '@testing-library/react'
import type { PanelTabDescriptor } from '../../hooks/panelTabRegistry'
import { sendLinkPreviewNav } from '../../linkPreview/linkPreviewNav'
import { PanelTabRequestScope, clearPanelTabRequests, usePanelTabRequest } from '../../app-sdk/panelTabRequest'
import { useLinkPreviewPanelTabs } from './linkPreviewPanelTabs'

const tab = (appName: string, tabId: string): PanelTabDescriptor => ({
  kind: `app:${appName}:${tabId}`, appName, tabId, title: tabId, menuLabel: tabId, icon: 'list', entry: 'dist/tab.mjs',
})
const QUESTIONS = tab('refs-app', 'questions')
const OTHER = tab('other-app', 'notes')

function mount(slot: string | null = 'chat-1') {
  const reveal = vi.fn()
  const hook = renderHook(() => useLinkPreviewPanelTabs({ current: slot }, [QUESTIONS, OTHER], reveal))
  return { reveal, hook }
}
const open = (slot: string, appName: string, tabId: string, payload: unknown = { label: '[R4]' }) => {
  let taken = false
  act(() => { taken = sendLinkPreviewNav({ kind: 'open', slot, appName, target: { kind: 'panelTab', id: tabId }, payload, via: 'link' }) })
  return taken
}

/** What an app's tab body reads, mounted for `slot` and `kind`. */
function Body() {
  const req = usePanelTabRequest()
  return <output>{req ? `${req.seq}:${JSON.stringify(req.payload)}` : 'none'}</output>
}
const body = (slot: string, kind: string) => render(<PanelTabRequestScope slot={slot} kind={kind}><Body /></PanelTabRequestScope>)

describe("a link-preview card opens its own app's side-panel tab", () => {
  it('reveals the tab in the chat on screen and hands that tab the payload', () => {
    const { reveal, hook } = mount()
    const here = body('chat-1', QUESTIONS.kind)
    expect(here.container.textContent).toBe('none')
    expect(open('chat-1', 'refs-app', 'questions')).toBe(true)
    expect(reveal).toHaveBeenCalledWith(QUESTIONS)
    const first = here.container.textContent ?? ''
    expect(first).toMatch(/^\d+:\{"label":"\[R4\]"\}$/)
    // The same payload again still reads as a new request.
    open('chat-1', 'refs-app', 'questions')
    expect(here.container.textContent).not.toBe(first)
    hook.unmount()
    here.unmount()
  })

  it("another app's tab, a tab the app does not declare, or another chat is refused", () => {
    const { reveal, hook } = mount()
    const other = body('chat-1', OTHER.kind)
    const elsewhere = body('chat-2', QUESTIONS.kind)
    expect(open('chat-1', 'refs-app', 'notes')).toBe(false)
    expect(open('chat-1', 'refs-app', 'missing')).toBe(false)
    expect(open('chat-2', 'refs-app', 'questions')).toBe(false)
    expect(reveal).not.toHaveBeenCalled()
    expect(other.container.textContent).toBe('none')
    expect(elsewhere.container.textContent).toBe('none')
    hook.unmount()
  })

  it('with no chat on screen nothing opens', () => {
    const { reveal, hook } = mount(null)
    expect(open('chat-1', 'refs-app', 'questions')).toBe(false)
    expect(reveal).not.toHaveBeenCalled()
    hook.unmount()
  })

  it('closing the tab forgets its request; a body mounted again before that reads the same one', () => {
    const { hook } = mount()
    open('chat-1', 'refs-app', 'questions', { n: 1 })
    const first = body('chat-1', QUESTIONS.kind)
    const read = first.container.textContent
    first.unmount()
    const again = body('chat-1', QUESTIONS.kind)
    expect(again.container.textContent).toBe(read)
    act(() => { clearPanelTabRequests('chat-1', QUESTIONS.kind) })
    expect(again.container.textContent).toBe('none')
    open('chat-1', 'refs-app', 'questions', { n: 2 })
    act(() => { clearPanelTabRequests('chat-1') })
    expect(again.container.textContent).toBe('none')
    hook.unmount()
  })

  it('outside a side-panel tab the hook reads nothing', () => {
    const { hook } = mount()
    open('chat-1', 'refs-app', 'questions')
    expect(render(<Body />).container.textContent).toBe('none')
    hook.unmount()
  })
})
