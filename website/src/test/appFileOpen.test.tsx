/**
 * `useOpenFile` (app-sdk/openFile.ts) through its host (components/appFileOpen.ts):
 * what an app must hold before the chat's own open route is asked, and that a
 * served call takes that route (`api.revealPath(path, 'open')`) and no other.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { createPortal } from 'react-dom'

// jsdom marks every event it is handed as untrusted, and `isTrusted` cannot be
// set on one. `trust.next` stands in what the person's click carries.
const trust = vi.hoisted(() => ({ next: true }))
vi.mock('../components/trustedClick', () => ({ isTrustedClick: () => trust.next }))
const brandingEnv = vi.hoisted(() => ({ directLocal: true, desktopLauncher: true }))
vi.mock('../hooks/useBranding', () => ({
  useBranding: () => ({ botName: 'Test', avatar: '', directLocal: brandingEnv.directLocal, desktopLauncher: brandingEnv.desktopLauncher }),
}))
vi.mock('../hooks/useGatewayPlatform', () => ({ useGatewayPlatform: () => 'darwin' }))
vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, api: { ...actual.api, revealPath: vi.fn() } }
})
vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn().mockResolvedValue(true) }))

import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import { useOpenFile, type OpenFileResult } from '../app-sdk/openFile'
import { AppFileOpenRoot, readOpenFilePath } from '../components/appFileOpen'
import { CLICK_GRANT_MS } from '../linkPreview/linkPreviewFrames'
import { resolveSessionControls } from '../hooks/useSessionControls'

const revealPath = vi.mocked(api.revealPath)

beforeEach(() => {
  trust.next = true
  brandingEnv.directLocal = true
  brandingEnv.desktopLauncher = true
  revealPath.mockReset()
  revealPath.mockResolvedValue({ ok: true })
})
afterEach(() => { vi.restoreAllMocks() })

/** A stand-in app module: it draws a path and opens it on a click, through the
 *  SDK, inline or in a portal of its own (a dialog, a menu). */
function AppModule({ path, portal = false }: { path: string; portal?: boolean }) {
  const openFile = useOpenFile()
  const [result, setResult] = useState<OpenFileResult | null>(null)
  const button = <button type="button" onClick={async () => setResult(await openFile(path))}>{path}</button>
  return (
    <>
      {portal ? createPortal(button, document.body) : button}
      <output data-testid="result">{result ? JSON.stringify(result) : ''}</output>
    </>
  )
}

/** The host's half, as `AppHost` and `SessionControlHost` mount it. */
function Host({ granted, path, portal }: { granted: boolean; path: string; portal?: boolean }) {
  return (
    <>
      <AppFileOpenRoot granted={granted}><AppModule path={path} portal={portal} /></AppFileOpenRoot>
      <span data-testid="outside">elsewhere</span>
    </>
  )
}

/** Clicks the app's button, a real click unless `real` is false. */
async function clickOpen(opts: { real?: boolean } = {}) {
  trust.next = opts.real !== false
  await act(async () => { fireEvent.click(screen.getByRole('button')) })
  const text = screen.getByTestId('result').textContent || ''
  return text ? JSON.parse(text) as OpenFileResult : null
}

describe('useOpenFile', () => {
  it('opens an approved app\'s path on a real click, through the chat\'s open route', async () => {
    render(<Host granted path="/Users/me/Scratch/look.html" />)
    expect(await clickOpen()).toEqual({ ok: true })
    expect(revealPath).toHaveBeenCalledTimes(1)
    expect(revealPath).toHaveBeenCalledWith('/Users/me/Scratch/look.html', 'open')
  })

  it('refuses an app whose owner has not approved permissions.openFiles', async () => {
    render(<Host granted={false} path="/Users/me/Scratch/look.html" />)
    expect(await clickOpen()).toEqual({ ok: false, reason: 'not_granted' })
    expect(revealPath).not.toHaveBeenCalled()
  })

  it('refuses a call with no real click behind it', async () => {
    render(<Host granted path="/Users/me/Scratch/look.html" />)
    expect(await clickOpen({ real: false })).toEqual({ ok: false, reason: 'no_click' })
    expect(revealPath).not.toHaveBeenCalled()
  })

  it('refuses a click outside the app\'s own surface', async () => {
    render(<Host granted path="/Users/me/Scratch/look.html" />)
    fireEvent.click(screen.getByTestId('outside'))
    expect(await clickOpen({ real: false })).toEqual({ ok: false, reason: 'no_click' })
    expect(revealPath).not.toHaveBeenCalled()
  })

  it('takes a click in the app\'s own portal, which React carries to the root', async () => {
    render(<Host granted portal path="/Users/me/Scratch/look.html" />)
    expect(await clickOpen()).toEqual({ ok: true })
    expect(revealPath).toHaveBeenCalledWith('/Users/me/Scratch/look.html', 'open')
  })

  it('opens one file per click', async () => {
    render(<Host granted path="/Users/me/Scratch/look.html" />)
    expect(await clickOpen()).toEqual({ ok: true })
    expect(await clickOpen({ real: false })).toEqual({ ok: false, reason: 'no_click' })
    expect(revealPath).toHaveBeenCalledTimes(1)
  })

  it('refuses a click older than the grant window', async () => {
    const now = vi.spyOn(performance, 'now')
    now.mockReturnValue(1_000)
    render(<Host granted path="/Users/me/Scratch/look.html" />)
    fireEvent.click(screen.getByTestId('result'))
    now.mockReturnValue(1_000 + CLICK_GRANT_MS + 1)
    expect(await clickOpen({ real: false })).toEqual({ ok: false, reason: 'no_click' })
    expect(revealPath).not.toHaveBeenCalled()
  })

  it.each([
    'look.html', './look.html', '~/look.html', 'https://example.com/look.html', 'javascript:alert(1)',
    'file://otherhost/etc/hosts', '/Users/me/../etc/hosts',
    // The URL parser would make these absolute or collapse their `..`.
    'file:look.html', 'file:/Users/me/look.html', 'file:///Users/me/../etc/hosts',
    'file:///Users/me/%2e%2e/etc/hosts', 'file:///Users/me/.%2E/etc/hosts', 'file:///Users/me\\..\\etc/hosts',
    // The parser drops a tab or a newline, joining these into a `..`.
    'file:///Users/me/.\t./etc/hosts', 'file:///Users/me/%2\te%2e/etc/hosts', 'file:///Users/me/.\n./etc/hosts',
  ])(
    'refuses %s, which is not an absolute local path', async (path) => {
      render(<Host granted path={path} />)
      expect(await clickOpen()).toEqual({ ok: false, reason: 'not_absolute' })
      expect(revealPath).not.toHaveBeenCalled()
    })

  it('reports a path the gateway refuses as sensitive as blocked, in the host\'s words', async () => {
    revealPath.mockRejectedValue(new ApiError(403, 'access denied'))
    vi.spyOn(console, 'error').mockImplementation(() => {})
    render(<Host granted path="/Users/me/.kiro/crew/.env" />)
    const result = await clickOpen()
    expect(result).toMatchObject({ ok: false, reason: 'blocked' })
    expect(result && !result.ok && result.message).toBeTruthy()
    expect(revealPath).toHaveBeenCalledWith('/Users/me/.kiro/crew/.env', 'open')
  })

  it('asks nothing of the route from a browser that is not on the gateway\'s machine', async () => {
    brandingEnv.directLocal = false
    render(<Host granted path="/Users/me/Scratch/look.html" />)
    expect(await clickOpen()).toEqual({ ok: false, reason: 'unavailable' })
    expect(revealPath).not.toHaveBeenCalled()
  })

  it('resolves not_granted outside a host that serves it', async () => {
    render(<AppModule path="/Users/me/Scratch/look.html" />)
    expect(await clickOpen({ real: false })).toEqual({ ok: false, reason: 'not_granted' })
  })
})

describe('a session control\'s grant', () => {
  const app = (openFilesGrant?: { approved?: boolean }) => ({
    name: 'paths', enabled: true, openFilesGrant,
    manifest: { contributes: { sessionControls: [{ id: 'c', entryPoint: 'c.mjs' }] } },
  })

  it('is the row\'s approved openFilesGrant, and nothing else', () => {
    expect(resolveSessionControls([app({ approved: true })])[0].openFiles).toBe(true)
    expect(resolveSessionControls([app({ approved: false })])[0].openFiles).toBe(false)
    expect(resolveSessionControls([app()])[0].openFiles).toBe(false)
  })
})

describe('readOpenFilePath', () => {
  it('reads a local file:// URL as its decoded path', () => {
    expect(readOpenFilePath('file:///Users/me/My%20Mocks/look.html')).toBe('/Users/me/My Mocks/look.html')
    expect(readOpenFilePath('file://localhost/tmp/a.txt')).toBe('/tmp/a.txt')
  })

  it('refuses a file:// URL with a query or fragment, and control characters', () => {
    expect(readOpenFilePath('file:///tmp/a.txt?x=1')).toBeNull()
    expect(readOpenFilePath('file:///tmp/a.txt#top')).toBeNull()
    expect(readOpenFilePath('/tmp/a\n.txt')).toBeNull()
  })
})
