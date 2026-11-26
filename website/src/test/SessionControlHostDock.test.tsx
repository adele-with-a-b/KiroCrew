/**
 * A session control that declares `dock` (and `stayOpen`): its box is drawn in
 * the composer dock under a host header, and with `stayOpen` an outside press or
 * an Escape meant for the chat leaves it open. A control that declares neither is
 * the popover `SessionControlHost.test.tsx` covers.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'

import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import SessionControlHost from '../components/SessionControlHost'
import type { SessionControlContext } from '../components/SessionControlHost'
import type { ResolvedSessionControl } from '../hooks/useSessionControls'

vi.mock('../app-sdk', () => ({
  AppApiProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}))
// The dashboard's wide or narrow form, as the rest of the page reads it.
const form = vi.hoisted(() => ({ narrow: false }))
vi.mock('../hooks/useIsMobile', async (orig) => ({ ...(await orig<typeof import('../hooks/useIsMobile')>()), useIsMobile: () => form.narrow }))

const base: ResolvedSessionControl = {
  key: 'test-app:scope',
  appName: 'test-app',
  appDisplayName: 'Test App',
  appVersion: '0.1.0',
  id: 'scope',
  entryPoint: 'dist/session-control.mjs',
  label: 'Scope',
  icon: 'Tag',
  allowedApi: [],
  allowedEvents: [],
  statusPath: '',
  processBacked: false,
  width: 340,
  openFiles: false,
  keys: [],
  installedAt: '',
}

const session: SessionControlContext = { sessionKey: 'dashboard:chat-2', folderId: '', folderName: '', cwd: '' }

const rect = { left: 100, top: 400, width: 40, height: 28, right: 140, bottom: 428, x: 100, y: 400, toJSON: () => ({}) } as DOMRect

function renderHost(over: Partial<ResolvedSessionControl>, anchorRect: DOMRect | null = null) {
  const onClose = vi.fn()
  const utils = render(
    <SessionControlHost control={{ ...base, ...over }} session={session} anchorRect={anchorRect} onClose={onClose} />,
    { wrapper: MemoryRouter },
  )
  return { onClose, ...utils }
}

/** Something outside the box: the chat, the message box. */
function outside(tag = 'div'): HTMLElement {
  const el = document.createElement(tag)
  document.body.appendChild(el)
  return el
}

afterEach(() => {
  cleanup()
  document.body.replaceChildren()
  form.narrow = false
})

describe('SessionControlHost — dock', () => {
  it('draws in place, with no chip rect, as a region named for the control and its app', () => {
    const { container } = renderHost({ dock: true })
    const box = screen.getByRole('region', { name: 'Scope — Test App' })
    // In the dock it was rendered into, not portaled to the body as a popover is.
    expect(container.contains(box)).toBe(true)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('draws a host header naming the app, with a close button that closes it', () => {
    const { onClose } = renderHost({ dock: true })
    const box = screen.getByRole('region', { name: 'Scope — Test App' })
    expect(box.textContent).toContain('Scope — Test App')
    fireEvent.click(screen.getByRole('button', { name: 'Close Scope' }))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('hands focus back to its chip when closed from inside', () => {
    const chip = outside('button')
    chip.setAttribute('data-session-control-key', 'test-app:scope')
    const { onClose } = renderHost({ dock: true, stayOpen: true })
    const close = screen.getByRole('button', { name: 'Close Scope' })
    close.focus()
    fireEvent.keyDown(close, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(document.activeElement).toBe(chip)
  })

  it('without stayOpen, still closes on a press outside and on any Escape, as a popover does', () => {
    const { onClose } = renderHost({ dock: true })
    fireEvent.mouseDown(outside())
    expect(onClose).toHaveBeenCalledTimes(1)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(2)
  })

  it('with stayOpen, stays open on a press outside it', () => {
    const { onClose } = renderHost({ dock: true, stayOpen: true })
    fireEvent.mouseDown(outside())
    fireEvent.mouseDown(document.body)
    expect(onClose).not.toHaveBeenCalled()
    expect(screen.getByRole('region', { name: 'Scope — Test App' })).toBeTruthy()
  })

  it('with stayOpen, leaves an Escape meant for the message box alone and closes on one inside the box', () => {
    const { onClose } = renderHost({ dock: true, stayOpen: true })
    const composer = outside('textarea')
    fireEvent.keyDown(composer, { key: 'Escape' })
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.keyDown(screen.getByRole('region', { name: 'Scope — Test App' }), { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('mounts the control in the box: a bundle that fails to load says so inside it', async () => {
    renderHost({ dock: true, stayOpen: true })
    await waitFor(() =>
      expect(screen.getByRole('region', { name: 'Scope — Test App' }).textContent).toMatch(/Could not load Scope/),
    )
  })

  it('never keeps a popover open: stayOpen without dock still closes on a press outside', () => {
    const { onClose } = renderHost({ stayOpen: true }, rect)
    expect(screen.getByRole('dialog')).toBeTruthy()
    fireEvent.mouseDown(outside())
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})

describe('SessionControlHost — dock on a wide screen only', () => {
  /** The composer dock's slot, where a docked box is drawn. */
  function dockSlot(): HTMLElement {
    const slot = outside()
    slot.setAttribute('data-testid', 'dock-slot')
    return slot
  }
  const docks: Partial<ResolvedSessionControl> = { dock: true, stayOpen: true }
  const host = (slot: HTMLElement, onClose = vi.fn()) =>
    <SessionControlHost control={{ ...base, ...docks }} session={session} anchorRect={rect} onClose={onClose} dockSlot={slot} />

  it('draws a wide screen\'s box in the dock slot, and nothing else', () => {
    const slot = dockSlot()
    render(host(slot), { wrapper: MemoryRouter })
    expect(slot.contains(screen.getByRole('region', { name: 'Scope — Test App' }))).toBe(true)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it("on a phone is today's popover: nothing in the dock, and an outside press closes it", () => {
    form.narrow = true
    const slot = dockSlot()
    const onClose = vi.fn()
    render(host(slot, onClose), { wrapper: MemoryRouter })
    expect(screen.queryByRole('region')).toBeNull()
    // The dock slot stays empty, so the dock is no taller and the chat gets no padding for it.
    expect(slot.childElementCount).toBe(0)
    expect(screen.getByRole('dialog', { name: 'Scope — Test App' })).toBeTruthy()
    fireEvent.mouseDown(outside())
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('crossing the breakpoint moves the open box without remounting its module, and leaves the dock empty', async () => {
    const slot = dockSlot()
    const { rerender } = render(host(slot), { wrapper: MemoryRouter })
    const region = screen.getByRole('region', { name: 'Scope — Test App' })
    await waitFor(() => expect(region.textContent).toMatch(/Could not load Scope/))
    // A node the module drew: the same node after each move means no remount.
    const drawn = region.querySelector('.p-3') as HTMLElement
    expect(drawn).toBeTruthy()

    form.narrow = true
    rerender(host(slot))
    const dialog = screen.getByRole('dialog', { name: 'Scope — Test App' })
    expect(dialog.contains(drawn)).toBe(true)
    expect(slot.childElementCount).toBe(0)

    form.narrow = false
    rerender(host(slot))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.getByRole('region', { name: 'Scope — Test App' }).contains(drawn)).toBe(true)
  })

  it('once docked, narrowing before the chip was measured draws the popover on the chip as drawn, module kept', async () => {
    const slot = dockSlot()
    const chip = outside('button')
    chip.setAttribute('data-session-control-key', 'test-app:scope')
    chip.getBoundingClientRect = () => ({ ...rect, left: 120, top: 500 }) as DOMRect
    const unmeasured = () => (
      <SessionControlHost control={{ ...base, ...docks }} session={session} anchorRect={null} onClose={vi.fn()} dockSlot={slot} />
    )
    const { rerender } = render(unmeasured(), { wrapper: MemoryRouter })
    const region = screen.getByRole('region', { name: 'Scope — Test App' })
    // The module's own content, not the loading fallback, which React replaces.
    await waitFor(() => expect(region.textContent).toMatch(/Could not load Scope/))
    const drawn = region.querySelector('.p-3') as HTMLElement

    form.narrow = true
    rerender(unmeasured())
    const dialog = screen.getByRole('dialog', { name: 'Scope — Test App' })
    expect(dialog.contains(drawn)).toBe(true)
    expect(dialog.style.left).toBe('120px')
    // With no chip on screen there is nothing to anchor on, and nothing is drawn.
    chip.remove()
    rerender(unmeasured())
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.queryByRole('region')).toBeNull()
  })

  it('keeps focus in the module across the move', async () => {
    const slot = dockSlot()
    const { rerender } = render(host(slot), { wrapper: MemoryRouter })
    const region = screen.getByRole('region', { name: 'Scope — Test App' })
    // The module's own content, not the loading fallback, which React replaces.
    await waitFor(() => expect(region.textContent).toMatch(/Could not load Scope/))
    const field = document.createElement('input')
    region.querySelector('.p-3')!.appendChild(field)
    field.focus()
    form.narrow = true
    rerender(host(slot))
    expect(screen.getByRole('dialog').contains(field)).toBe(true)
    expect(document.activeElement).toBe(field)
  })
})
