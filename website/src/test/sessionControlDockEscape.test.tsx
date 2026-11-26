/**
 * One Escape, one owner, with an app's docked box open. The composer has two
 * Escape claimants that yield to any other Escape owner (`escapeOwners.ts`):
 * stopping the running turn (`stopKey.ts`) and discarding a dictation
 * (`voice.ts`). A docked box that will itself answer an Escape must be visible
 * to that probe, and one that stays open must let an Escape meant for the
 * composer pass. These drive the real claimants beside the real host.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { cleanup, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { RefObject } from 'react'
import SessionControlHost from '../components/SessionControlHost'
import type { SessionControlContext } from '../components/SessionControlHost'
import type { ResolvedSessionControl } from '../hooks/useSessionControls'
import { useComposerStopKey } from '../components/chat-input/stopKey'
import { useDictationControls } from '../components/chat-input/voice'
import { anotherEscapeOwnerIsOpen } from '../components/chat-input/escapeOwners'

vi.mock('../app-sdk', () => ({
  AppApiProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}))
vi.mock('../hooks/useIsMobile', async (orig) => ({ ...(await orig<typeof import('../hooks/useIsMobile')>()), useIsMobile: () => false }))

const base: ResolvedSessionControl = {
  key: 'test-app:scope', appName: 'test-app', appDisplayName: 'Test App', appVersion: '0.1.0', id: 'scope',
  entryPoint: 'dist/session-control.mjs', label: 'Scope', icon: 'Tag', allowedApi: [], allowedEvents: [],
  statusPath: '', processBacked: false, width: 340, openFiles: false, keys: [], installedAt: '',
}
const session: SessionControlContext = { sessionKey: 'dashboard:chat-2', folderId: '', folderName: '', cwd: '' }
const notOpen: RefObject<boolean> = { current: false }

/** The message box, with the composer's real Escape-to-stop gate. */
function MessageBox({ onStop, voiceRecording = false }: { onStop: () => void; voiceRecording?: boolean }) {
  const { stopTurnOnEscape } = useComposerStopKey({
    isRunning: true, onStop, anyPickerOpenRef: notOpen, voiceRecording, voiceDrainCancellable: false,
    hasApproval: false, approvalNudgeActive: false,
  })
  return <textarea aria-label="Message" onKeyDown={e => { if (e.key === 'Escape') stopTurnOnEscape(e, { native: e.nativeEvent, composing: false }) }} />
}

/** A recording dictation, with the composer's real Escape-to-discard listener. */
function recording() {
  const onVoiceCancel = vi.fn()
  renderHook(() => useDictationControls({
    composerControl: () => null, value: '', anyPickerOpenRef: notOpen,
    voiceCaretRef: { current: null } as never, voicePendingCaretRef: { current: null } as never,
    voiceDictationPanel: true, voiceRecording: true, voiceDrainCancellable: false, voiceError: null as never,
    voiceSampleRef: { current: null } as never, voiceTranscribing: false, onVoiceCancel, onVoiceToggle: vi.fn(),
  }))
  return onVoiceCancel
}

function mount(control: Partial<ResolvedSessionControl>, opts: { voiceRecording?: boolean } = {}) {
  const slot = document.createElement('div')
  document.body.appendChild(slot)
  const onClose = vi.fn()
  const onStop = vi.fn()
  render(
    <>
      <SessionControlHost control={{ ...base, ...control }} session={session} anchorRect={null} onClose={onClose} dockSlot={slot} />
      <MessageBox onStop={onStop} voiceRecording={opts.voiceRecording} />
    </>,
    { wrapper: MemoryRouter },
  )
  return { onClose, onStop, box: screen.getByRole('region', { name: 'Scope — Test App' }), message: screen.getByRole('textbox', { name: 'Message' }) }
}

/** A field inside the app's module, focused, as the person typing in the box. */
async function focusInBox(box: HTMLElement): Promise<HTMLInputElement> {
  // The module's own content, not the loading fallback, which React replaces.
  await waitFor(() => expect(box.textContent).toMatch(/Could not load Scope/))
  const field = document.createElement('input')
  box.querySelector('.p-3')!.appendChild(field)
  field.focus()
  return field
}

afterEach(() => {
  cleanup()
  document.body.replaceChildren()
})

describe('a docked box that does not stay open owns Escape while open, as a popover does', () => {
  it('one Escape in the message box during a reply closes the box and leaves the turn running', () => {
    const { onClose, onStop, message } = mount({ dock: true })
    expect(anotherEscapeOwnerIsOpen()).toBe(true)
    message.focus()
    fireEvent.keyDown(message, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(onStop).not.toHaveBeenCalled()
  })

  it('one Escape during a dictation closes the box and keeps the dictation', () => {
    const onVoiceCancel = recording()
    const { onClose, message } = mount({ dock: true }, { voiceRecording: true })
    message.focus()
    fireEvent.keyDown(message, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(onVoiceCancel).not.toHaveBeenCalled()
  })
})

describe('a docked box that stays open owns Escape only while focus is in it', () => {
  it('Escape inside the box during a dictation closes the box and keeps the dictation', async () => {
    const onVoiceCancel = recording()
    const { onClose, box } = mount({ dock: true, stayOpen: true }, { voiceRecording: true })
    expect(anotherEscapeOwnerIsOpen()).toBe(false)
    const field = await focusInBox(box)
    await waitFor(() => expect(anotherEscapeOwnerIsOpen()).toBe(true))
    fireEvent.keyDown(field, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(onVoiceCancel).not.toHaveBeenCalled()
  })

  it('Escape in the message box is the composer\'s: it stops the reply, or discards the dictation, and the box stays', async () => {
    const { onClose, onStop, box, message } = mount({ dock: true, stayOpen: true })
    await focusInBox(box)
    message.focus()
    await waitFor(() => expect(anotherEscapeOwnerIsOpen()).toBe(false))
    fireEvent.keyDown(message, { key: 'Escape' })
    expect(onStop).toHaveBeenCalledTimes(1)
    expect(onClose).not.toHaveBeenCalled()
    cleanup()
    document.body.replaceChildren()

    const onVoiceCancel = recording()
    const again = mount({ dock: true, stayOpen: true }, { voiceRecording: true })
    again.message.focus()
    fireEvent.keyDown(again.message, { key: 'Escape' })
    expect(onVoiceCancel).toHaveBeenCalledTimes(1)
    expect(again.onClose).not.toHaveBeenCalled()
  })
})
