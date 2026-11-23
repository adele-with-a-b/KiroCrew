/**
 * `useOpenFile` as the real hosts mount it: `AppHost` and `SessionControlHost`
 * serve it from the row's `openFilesGrant.approved`, and only a click in the app's
 * own module grants an open; the host's own chrome never does.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { useState, type ReactNode } from 'react'

const trust = vi.hoisted(() => ({ next: true }))
vi.mock('../components/trustedClick', () => ({ isTrustedClick: () => trust.next }))
vi.mock('../hooks/useBranding', () => ({
  useBranding: () => ({ botName: 'Test', avatar: '', directLocal: true, desktopLauncher: true }),
}))
vi.mock('../hooks/useGatewayPlatform', () => ({ useGatewayPlatform: () => 'darwin' }))
vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, api: { ...actual.api, revealPath: vi.fn() } }
})
vi.mock('../app-sdk', () => ({
  AppApiProvider: ({ children }: { children: ReactNode }) => <>{children}</>,
}))

const PATH = '/Users/me/Scratch/look.html'

/** The app's module: a path it opens on a click, through the SDK. */
function PathApp() {
  const openFile = useOpenFile()
  const [result, setResult] = useState('')
  return (
    <>
      <button type="button" onClick={async () => setResult(JSON.stringify(await openFile(PATH)))}>{PATH}</button>
      <output data-testid="result">{result}</output>
    </>
  )
}

vi.mock('../components/appBundleImport', () => ({
  importAppBundle: () => Promise.resolve({ default: PathApp }),
}))
vi.mock('/apps/paths/ui/dist/control.mjs', () => ({ default: PathApp }))

import { api } from '../api/client'
import { useOpenFile } from '../app-sdk/openFile'
import AppHost from '../components/AppHost'
import SessionControlHost from '../components/SessionControlHost'
import type { ResolvedSessionControl } from '../hooks/useSessionControls'
import { renderWithProviders } from './helpers'

const revealPath = vi.mocked(api.revealPath)

beforeEach(() => {
  trust.next = true
  revealPath.mockReset()
  revealPath.mockResolvedValue({ ok: true })
})

async function clickPath() {
  const button = await screen.findByRole('button', { name: PATH })
  await act(async () => { fireEvent.click(button) })
  return JSON.parse(screen.getByTestId('result').textContent || 'null')
}

describe('AppHost', () => {
  const app = (openFilesGrant?: { approved: boolean; staged?: boolean }) => ({
    name: 'paths', version: '1.0.0', enabled: true, openFilesGrant,
    manifest: { version: '1.0.0', ui: { entry: 'index.js' }, permissions: { api: [], events: [] } },
  })

  it('serves an approved app\'s open on a click in its module', async () => {
    renderWithProviders(<AppHost app={app({ approved: true, staged: false })} />)
    expect(await clickPath()).toEqual({ ok: true })
    expect(revealPath).toHaveBeenCalledWith(PATH, 'open')
  })

  it('reads approved, not staged: a held-back grant is not_granted', async () => {
    renderWithProviders(<AppHost app={app({ approved: false, staged: true })} />)
    expect(await clickPath()).toEqual({ ok: false, reason: 'not_granted' })
    expect(revealPath).not.toHaveBeenCalled()
  })

  it('wraps the app\'s module and not the host\'s page chrome', async () => {
    renderWithProviders(<AppHost app={app({ approved: true })} />)
    const button = await screen.findByRole('button', { name: PATH })
    const root = button.closest('[data-app-file-open-root]')
    expect(root).not.toBeNull()
    expect(root?.parentElement?.closest('[data-app-file-open-root]')).toBeNull()
  })
})

describe('SessionControlHost', () => {
  const control = (openFiles: boolean): ResolvedSessionControl => ({
    key: 'paths:c', appName: 'paths', appDisplayName: 'Paths', appVersion: '1.0.0', id: 'c',
    entryPoint: 'dist/control.mjs', label: 'Paths', icon: 'Tag', allowedApi: [], allowedEvents: [],
    statusPath: 'session-status', openFiles,
  } as ResolvedSessionControl)
  const session = { sessionKey: 'dashboard:chat-1', folderId: 'f', folderName: 'F', cwd: '/home/me' }
  const rect = { left: 100, top: 400, width: 40, height: 28, right: 140, bottom: 428, x: 100, y: 400, toJSON: () => ({}) } as DOMRect
  const mount = (openFiles: boolean) => render(
    <SessionControlHost control={control(openFiles)} session={session} anchorRect={rect} onClose={() => {}} />,
    { wrapper: MemoryRouter },
  )

  it('serves an approved control\'s open on a click in the control', async () => {
    mount(true)
    expect(await clickPath()).toEqual({ ok: true })
    expect(revealPath).toHaveBeenCalledWith(PATH, 'open')
  })

  it('refuses a control whose grant is not approved', async () => {
    mount(false)
    expect(await clickPath()).toEqual({ ok: false, reason: 'not_granted' })
  })

  it('takes no grant from a click on the popover around the control', async () => {
    mount(true)
    await screen.findByRole('button', { name: PATH })
    fireEvent.click(screen.getByRole('dialog'))
    trust.next = false
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: PATH })) })
    await waitFor(() => expect(screen.getByTestId('result').textContent).not.toBe(''))
    expect(JSON.parse(screen.getByTestId('result').textContent || 'null')).toEqual({ ok: false, reason: 'no_click' })
  })
})
