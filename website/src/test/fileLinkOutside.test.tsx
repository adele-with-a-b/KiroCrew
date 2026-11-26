/**
 * A click on a chat file link opens it in the default app for its type, or in
 * the side panel (`dashboard.file_links_open_default_app`, on by default), on
 * each surface the dashboard runs in: the desktop app with its Crew on this
 * machine (`fileOpenAPI.open` + `openCopy`), the desktop app on a Crew on
 * another machine (`openCopy` only), and a plain browser tab (no bridge). The
 * desktop bridge is faked: nothing is launched.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import OpenFileChips from '../components/OpenFileChips'
import { fileLinksOpenOutside, outsideRoute, setFileLinksOpenOutside } from '../components/markdown/fileLinkOutside'
import { useConfigFileLinkTarget } from '../hooks/useConfigFileLinkTarget'
import { __resetPathKindCache } from '../hooks/usePathKind'
import { stubPathKinds } from './pathKindStub'

type Bridge = { open?: ReturnType<typeof vi.fn>; openCopy?: ReturnType<typeof vi.fn> }

const realFetch = globalThis.fetch
const SLOT = 'chat-1'
const FAILED = "Couldn't open it in the default app, so it opened in the side panel."

function surface(kind: 'local-app' | 'remote-app' | 'browser', answer = { ok: true }): Bridge {
  const bridge: Bridge = {}
  if (kind === 'local-app') bridge.open = vi.fn().mockResolvedValue(answer)
  if (kind !== 'browser') bridge.openCopy = vi.fn().mockResolvedValue(answer)
  if (kind === 'browser') delete (window as { fileOpenAPI?: unknown }).fileOpenAPI
  else (window as { fileOpenAPI?: unknown }).fileOpenAPI = bridge
  return bridge
}

/** The kind probe answers `file` for every path; `other` answers the rest. */
function stubFiles(other?: (url: string) => Response | Promise<Response>) {
  return stubPathKinds(() => 'file', url => Promise.resolve(other ? other(String(url)) : new Response('', { status: 404 })))
}

/** The reads a click made, the kind probe left out. */
const readsOf = (fetchMock: ReturnType<typeof vi.fn>) => fetchMock.mock.calls.map(c => String(c[0])).filter(u => !u.startsWith('/api/file-kinds'))

/** `null` leaves the host without that prop (an `undefined` would take the default). */
async function chipFor(content: string, onFileOpen: ReturnType<typeof vi.fn> | null = vi.fn(), slotKey: string | null = SLOT) {
  const view = render(<MarkdownRenderer content={content} onFileOpen={onFileOpen ?? undefined} slotKey={slotKey ?? undefined} />)
  const chip = await waitFor(() => {
    const c = view.container.querySelector('code[data-path-kind="file"]')
    expect(c).not.toBeNull()
    return c as HTMLElement
  })
  return { ...view, chip, onFileOpen }
}

/** A `window.open` that opened its tab. */
const openedTab = () => vi.spyOn(window, 'open').mockReturnValue({ opener: window } as unknown as Window)

beforeEach(() => {
  __resetPathKindCache()
  setFileLinksOpenOutside(true)
})

afterEach(() => {
  globalThis.fetch = realFetch
  delete (window as { fileOpenAPI?: unknown }).fileOpenAPI
  setFileLinksOpenOutside(false)
  vi.restoreAllMocks()
})

describe('outsideRoute', () => {
  it('opens a web page as a copy in the app and in a new tab in a browser', () => {
    surface('local-app')
    expect(outsideRoute('/ws/mock.html', SLOT)).toBe('page-copy')
    expect(outsideRoute('/ws/OLD.HTM', SLOT)).toBe('page-copy')
    surface('remote-app')
    expect(outsideRoute('/ws/mock.html', SLOT)).toBe('page-copy')
    surface('browser')
    expect(outsideRoute('/ws/mock.html', SLOT)).toBe('page-tab')
    expect(outsideRoute('mock.html', SLOT)).toBeNull()
  })

  it('opens text and images as the real file locally and as a copy remotely', () => {
    surface('local-app')
    expect(outsideRoute('/ws/notes.md', SLOT)).toBe('file')
    expect(outsideRoute('/ws/shot.png', SLOT)).toBe('file')
    // The shell cannot expand `~`, so a home path goes as a copy.
    expect(outsideRoute('~/notes.md', SLOT)).toBe('file-copy')
    surface('remote-app')
    expect(outsideRoute('/ws/notes.md', SLOT)).toBe('file-copy')
    expect(outsideRoute('/ws/shot.png', SLOT)).toBe('file-copy')
    surface('browser')
    expect(outsideRoute('/ws/notes.md', SLOT)).toBeNull()
  })

  it('offers no route that reads through the gateway without the chat it is for', () => {
    surface('local-app')
    expect(outsideRoute('/ws/mock.html')).toBeNull()
    expect(outsideRoute('~/notes.md')).toBeNull()
    // The real file is opened by path, with no gateway read.
    expect(outsideRoute('/ws/notes.md')).toBe('file')
    surface('remote-app')
    expect(outsideRoute('/ws/notes.md')).toBeNull()
    surface('browser')
    expect(outsideRoute('/ws/mock.html')).toBeNull()
  })

  it('keeps every type the shell bars, and unknown ones, in the side panel', () => {
    for (const kind of ['local-app', 'remote-app', 'browser'] as const) {
      surface(kind)
      for (const p of ['/ws/report.pdf', '/ws/data.csv', '/ws/art.svg', '/ws/run.sh', '/ws/app.command', '/ws/main.py', '/ws/README']) {
        expect(outsideRoute(p, SLOT), `${kind} ${p}`).toBeNull()
      }
    }
  })
})

describe('a click on a path chip', () => {
  it('opens a local markdown file in its default app; Alt+click opens the side panel', async () => {
    const bridge = surface('local-app')
    stubFiles()
    const { chip, onFileOpen } = await chipFor('See `/ws/notes.md`.')
    fireEvent.click(chip)
    await waitFor(() => expect(bridge.open).toHaveBeenCalledWith('/ws/notes.md'))
    expect(onFileOpen).not.toHaveBeenCalled()
    fireEvent.click(chip, { altKey: true })
    expect(onFileOpen).toHaveBeenCalledWith('/ws/notes.md')
    expect(bridge.open).toHaveBeenCalledTimes(1)
    expect(chip.getAttribute('title')).toContain('Alt/Option+click to open in the side panel')
  })

  it('with the setting off opens the side panel; Alt+click opens the default app', async () => {
    setFileLinksOpenOutside(false)
    const bridge = surface('local-app')
    stubFiles()
    const { chip, onFileOpen } = await chipFor('See `/ws/notes.md`.')
    fireEvent.click(chip)
    expect(onFileOpen).toHaveBeenCalledWith('/ws/notes.md')
    expect(bridge.open).not.toHaveBeenCalled()
    fireEvent.click(chip, { altKey: true })
    await waitFor(() => expect(bridge.open).toHaveBeenCalledWith('/ws/notes.md'))
    expect(chip.getAttribute('title')).toContain('Alt/Option+click to open in the default app')
  })

  it('opens a web page as a copy of the side panel\'s whole-page read, never by path', async () => {
    const bridge = surface('local-app')
    const page = '<!doctype html><style>h1{color:red}</style><h1 id="C">C</h1>'
    const fetchMock = stubFiles(url => (url.startsWith('/api/file-html?path=%2Fws%2Fmock.html&v=') ? new Response(page) : new Response('', { status: 404 })))
    const { chip, onFileOpen } = await chipFor('See `/ws/mock.html`.')
    expect(chip.getAttribute('title')).toContain('Opens a copy: edits to it do not save back')
    fireEvent.click(chip)
    await waitFor(() => expect(bridge.openCopy).toHaveBeenCalledTimes(1))
    const [name, bytes] = bridge.openCopy!.mock.calls[0] as [string, Uint8Array]
    expect(name).toBe('mock.html')
    expect(new TextDecoder().decode(bytes)).toBe(page)
    expect(readsOf(fetchMock).every(u => u.includes(`slot=${SLOT}`))).toBe(true)
    expect(bridge.open).not.toHaveBeenCalled()
    expect(onFileOpen).not.toHaveBeenCalled()
  })

  it('on a Crew on another machine copies a markdown file read through /api/file-read', async () => {
    const bridge = surface('remote-app')
    stubFiles(url => (url.startsWith('/api/file-read?path=%2Fws%2Fnotes.md') ? new Response('# Plan\n') : new Response('', { status: 404 })))
    const { chip, onFileOpen } = await chipFor('See `/ws/notes.md`.')
    fireEvent.click(chip)
    await waitFor(() => expect(bridge.openCopy).toHaveBeenCalledTimes(1))
    const [name, bytes] = bridge.openCopy!.mock.calls[0] as [string, Uint8Array]
    expect(name).toBe('notes.md')
    expect(new TextDecoder().decode(bytes)).toBe('# Plan\n')
    expect(onFileOpen).not.toHaveBeenCalled()
  })

  it('names the chat in every copy read, so the gateway can refuse a chat that runs remotely', async () => {
    const bridge = surface('remote-app')
    const fetchMock = stubFiles(url => (url.includes('slot=chat-7') ? new Response('x') : new Response('', { status: 409 })))
    for (const p of ['/ws/notes.md', '/ws/mock.html', '/ws/shot.png']) {
      const view = await chipFor('See `' + p + '`.', vi.fn(), 'chat-7')
      fireEvent.click(view.chip)
      await waitFor(() => expect(readsOf(fetchMock).length).toBeGreaterThan(0))
      view.unmount()
      fetchMock.mockClear()
    }
    await waitFor(() => expect(bridge.openCopy).toHaveBeenCalledTimes(3))
  })

  it('does not copy when the gateway refuses the chat, or when no chat is named: the side panel opens', async () => {
    const bridge = surface('remote-app')
    const fetchMock = stubFiles(url => (url.startsWith('/api/file-html?') ? new Response('', { status: 409 }) : new Response('', { status: 404 })))
    const refused = await chipFor('See `/ws/mock.html`.', vi.fn(), 'remote-chat')
    fireEvent.click(refused.chip)
    expect(await screen.findByText(FAILED)).toBeTruthy()
    refused.unmount()
    fetchMock.mockClear()
    const noSlot = await chipFor('See `/ws/notes.md`.', vi.fn(), null)
    fireEvent.click(noSlot.chip)
    expect(noSlot.onFileOpen).toHaveBeenCalledWith('/ws/notes.md')
    expect(readsOf(fetchMock)).toEqual([])
    expect(bridge.openCopy).not.toHaveBeenCalled()
  })

  it('does not copy a read the gateway cut short or decoded with replacement: the side panel opens and says why', async () => {
    const bridge = surface('remote-app')
    for (const header of ['X-Truncated', 'X-Lossy-Decode']) {
      stubFiles(url => (url.startsWith('/api/file-read?') ? new Response('# part', { headers: { [header]: 'true' } }) : new Response('', { status: 404 })))
      const view = await chipFor('See `/ws/big.md`.')
      fireEvent.click(view.chip)
      await waitFor(() => expect(view.onFileOpen).toHaveBeenCalledWith('/ws/big.md'))
      expect(await screen.findByText(FAILED)).toBeTruthy()
      view.unmount()
    }
    expect(bridge.openCopy).not.toHaveBeenCalled()
  })

  it('falls back to the side panel when the default app refuses', async () => {
    const bridge = surface('local-app', { ok: false })
    stubFiles()
    const { chip, onFileOpen } = await chipFor('See `/ws/notes.md`.')
    fireEvent.click(chip)
    await waitFor(() => expect(onFileOpen).toHaveBeenCalledWith('/ws/notes.md'))
    expect(bridge.open).toHaveBeenCalledTimes(1)
  })

  it('gives one result per click: a second click while a copy is on its way joins it, and an old notice goes', async () => {
    const bridge = surface('remote-app')
    let release: () => void = () => {}
    const gate = new Promise<void>(resolve => { release = resolve })
    stubFiles(url => (url.startsWith('/api/file-html?') ? gate.then(() => new Response('<p>x</p>')) : new Response('', { status: 404 })))
    const { chip, onFileOpen } = await chipFor('See `/ws/mock.html`.')
    fireEvent.click(chip)
    fireEvent.click(chip)
    release()
    await waitFor(() => expect(bridge.openCopy).toHaveBeenCalledTimes(1))
    expect(onFileOpen).not.toHaveBeenCalled()
    expect(screen.queryByText(FAILED)).toBeNull()
    // A failed click's notice is gone as soon as the next click starts.
    bridge.openCopy!.mockResolvedValueOnce({ ok: false })
    fireEvent.click(chip)
    expect(await screen.findByText(FAILED)).toBeTruthy()
    fireEvent.click(chip)
    expect(screen.queryByText(FAILED)).toBeNull()
    await waitFor(() => expect(bridge.openCopy).toHaveBeenCalledTimes(3))
  })

  it('opens a web page in a new tab in a plain browser, and other files in the side panel', async () => {
    surface('browser')
    stubFiles()
    const open = openedTab()
    const page = await chipFor('See `/ws/mock.html`.')
    fireEvent.click(page.chip)
    expect(open).toHaveBeenCalledWith(expect.stringMatching(new RegExp(`^/api/file-html\\?path=%2Fws%2Fmock\\.html&v=\\d+&slot=${SLOT}$`)), '_blank')
    expect((open.mock.results[0].value as Window).opener).toBeNull()
    expect(page.onFileOpen).not.toHaveBeenCalled()
    page.unmount()
    const md = await chipFor('See `/ws/notes.md`.')
    fireEvent.click(md.chip)
    expect(md.onFileOpen).toHaveBeenCalledWith('/ws/notes.md')
    expect(open).toHaveBeenCalledTimes(1)
  })

  it('opens the side panel where the new tab is refused (a window that allows no popup)', async () => {
    surface('browser')
    stubFiles()
    vi.spyOn(window, 'open').mockReturnValue(null)
    const { chip, onFileOpen } = await chipFor('See `/ws/mock.html`.')
    fireEvent.click(chip)
    expect(onFileOpen).toHaveBeenCalledWith('/ws/mock.html')
    expect(await screen.findByText(FAILED)).toBeTruthy()
  })

  it('keeps PDF, CSV and SVG files, and a link to a line, in the side panel', async () => {
    const bridge = surface('local-app')
    // No file is literally named `notes.md:12`, so the suffix reads as a line.
    stubPathKinds(p => (p.includes(':') ? null : 'file'))
    for (const p of ['/ws/report.pdf', '/ws/data.csv', '/ws/art.svg']) {
      const view = await chipFor('See `' + p + '`.')
      fireEvent.click(view.chip)
      expect(view.onFileOpen).toHaveBeenCalledWith(p)
      view.unmount()
    }
    const line = await chipFor('See `/ws/notes.md:12`.')
    fireEvent.click(line.chip)
    expect(line.onFileOpen).toHaveBeenCalledWith('/ws/notes.md', { line: 12 })
    expect(bridge.open).not.toHaveBeenCalled()
    expect(bridge.openCopy).not.toHaveBeenCalled()
  })

  it('keeps what a click did on a surface with no side panel: the file manager, never the default app', async () => {
    const bridge = surface('local-app')
    const fetchMock = stubFiles(() => new Response(JSON.stringify({ ok: true })))
    const { chip } = await chipFor('See `/ws/mock.html` and `/ws/notes.md`.', null)
    fireEvent.click(chip)
    await waitFor(() => expect(readsOf(fetchMock).some(u => u.startsWith('/api/reveal'))).toBe(true))
    expect(chip.getAttribute('title')).not.toContain('Alt/Option')
    expect(bridge.open).not.toHaveBeenCalled()
    expect(bridge.openCopy).not.toHaveBeenCalled()
  })

  it('the menu row "Open in side panel" opens the side panel whatever a click does', async () => {
    const bridge = surface('local-app')
    stubFiles()
    const { chip, onFileOpen } = await chipFor('See `/ws/notes.md`.')
    fireEvent.contextMenu(chip)
    const row = await waitFor(() => {
      const r = Array.from(document.querySelectorAll('[role="menuitem"]')).find(el => el.textContent?.includes('Open in side panel'))
      expect(r).toBeDefined()
      return r as HTMLElement
    })
    fireEvent.click(row)
    expect(onFileOpen).toHaveBeenCalledWith('/ws/notes.md')
    expect(bridge.open).not.toHaveBeenCalled()
  })
})

describe('a click on a Markdown link to a file', () => {
  it('opens a web page in a new tab in a browser; Alt+click opens the side panel', async () => {
    surface('browser')
    stubFiles()
    const open = openedTab()
    const onFileOpen = vi.fn()
    render(<MarkdownRenderer content={'See [the mock](/ws/mock.html).'} onFileOpen={onFileOpen} slotKey={SLOT} />)
    const link = await screen.findByRole('link', { name: 'the mock' })
    await waitFor(() => {
      fireEvent.click(link)
      expect(open).toHaveBeenCalledTimes(1)
    })
    expect(onFileOpen).not.toHaveBeenCalled()
    fireEvent.click(link, { altKey: true })
    expect(onFileOpen).toHaveBeenCalledWith('/ws/mock.html')
    expect(open).toHaveBeenCalledTimes(1)
  })
})

describe('an Open file chip under a reply', () => {
  const reply = 'Done.\n\n<!-- kirocrew:open-file path="/ws/a.md" -->\n<!-- kirocrew:open-file path="/ws/b.md" -->'
  const chips = () => Array.from(document.querySelectorAll('[data-testid="open-file-chips"] button[data-path]')) as HTMLElement[]
  /** The chips once the gateway has answered for every path, as `kind`. */
  const answered = (kind: string) => waitFor(() => {
    const all = chips()
    expect(all.length).toBeGreaterThan(0)
    for (const c of all) expect(c.getAttribute('data-path-kind')).toBe(kind)
    return all
  })

  it('opens like a path click once the gateway confirms the file, and tells a failure under the chip that was clicked', async () => {
    const bridge = surface('local-app', { ok: false })
    stubFiles()
    const onFileOpen = vi.fn()
    render(<OpenFileChips content={reply} onFileOpen={onFileOpen} slot={SLOT} />)
    const [a, b] = await answered('file')
    fireEvent.click(b)
    await waitFor(() => expect(onFileOpen).toHaveBeenCalledWith('/ws/b.md'))
    expect(bridge.open).toHaveBeenCalledWith('/ws/b.md')
    const notice = await screen.findByTestId('open-file-chip-error')
    expect(notice.previousElementSibling).toBe(b)
    // A click on another chip clears it.
    bridge.open!.mockResolvedValue({ ok: true })
    fireEvent.click(a)
    await waitFor(() => expect(screen.queryByTestId('open-file-chip-error')).toBeNull())
    // Alt/Option+click takes the side panel.
    fireEvent.click(a, { altKey: true })
    expect(onFileOpen).toHaveBeenLastCalledWith('/ws/a.md')
  })

  it('names its chat in a copy read, and without one opens the side panel', async () => {
    const bridge = surface('remote-app')
    const fetchMock = stubFiles(url => (url.includes(`slot=${SLOT}`) ? new Response('# a') : new Response('', { status: 409 })))
    const onFileOpen = vi.fn()
    const view = render(<OpenFileChips content={reply} onFileOpen={onFileOpen} slot={SLOT} />)
    fireEvent.click((await answered('file'))[0])
    await waitFor(() => expect(bridge.openCopy).toHaveBeenCalledTimes(1))
    expect(readsOf(fetchMock)).toEqual([expect.stringContaining(`slot=${SLOT}`)])
    view.unmount()
    fetchMock.mockClear()
    render(<OpenFileChips content={reply} onFileOpen={onFileOpen} />)
    fireEvent.click((await answered('file'))[0])
    expect(onFileOpen).toHaveBeenCalledWith('/ws/a.md')
    expect(readsOf(fetchMock)).toEqual([])
    expect(bridge.openCopy).toHaveBeenCalledTimes(1)
  })

  it('never hands a path the gateway refuses to the shell: the side panel\'s read decides', async () => {
    // Credential files with allow-listed extensions: the probe reads each as
    // missing, as it does for every path is_sensitive_path refuses.
    const secrets = ['/home/me/.config/gh/hosts.yml', '/home/me/.docker/config.json', '/srv/app/prod.env']
    const markers = 'Here.\n\n' + secrets.map(p => `<!-- kirocrew:open-file path="${p}" -->`).join('\n')
    for (const kind of ['local-app', 'remote-app'] as const) {
      const bridge = surface(kind)
      const fetchMock = stubPathKinds(() => null)
      const onFileOpen = vi.fn()
      const view = render(<OpenFileChips content={markers} onFileOpen={onFileOpen} slot={SLOT} />)
      const all = await answered('missing')
      for (const c of all) fireEvent.click(c)
      for (const c of all) fireEvent.click(c, { altKey: true })
      expect(onFileOpen.mock.calls.map(c => c[0])).toEqual([...secrets, ...secrets])
      expect(bridge.open ?? vi.fn()).not.toHaveBeenCalled()
      expect(bridge.openCopy).not.toHaveBeenCalled()
      expect(readsOf(fetchMock)).toEqual([])
      view.unmount()
    }
  })

  it('opens the side panel while the gateway has not answered yet', async () => {
    const bridge = surface('local-app')
    stubPathKinds(() => 'file', () => new Promise<Response>(() => {}))
    globalThis.fetch = vi.fn(() => new Promise<Response>(() => {})) as unknown as typeof fetch
    const onFileOpen = vi.fn()
    render(<OpenFileChips content={reply} onFileOpen={onFileOpen} slot={SLOT} />)
    const [a] = chips()
    expect(a.hasAttribute('data-path-kind')).toBe(false)
    fireEvent.click(a)
    expect(onFileOpen).toHaveBeenCalledWith('/ws/a.md')
    expect(bridge.open).not.toHaveBeenCalled()
  })
})

describe('useConfigFileLinkTarget', () => {
  function wrapper(data?: unknown, fail = false) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    if (data !== undefined) qc.setQueryData(['dashboardConfig'], data)
    if (fail) globalThis.fetch = vi.fn(() => Promise.reject(new Error('down'))) as unknown as typeof fetch
    else if (data === undefined) globalThis.fetch = vi.fn(() => new Promise<Response>(() => {})) as unknown as typeof fetch
    return ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  }

  it('is on only once the config says so; off while it loads, after a failed read, and when the key is absent', async () => {
    renderHook(() => useConfigFileLinkTarget(), { wrapper: wrapper({ file_links_open_default_app: true }) })
    expect(fileLinksOpenOutside()).toBe(true)
    renderHook(() => useConfigFileLinkTarget(), { wrapper: wrapper({ file_links_open_default_app: false }) })
    expect(fileLinksOpenOutside()).toBe(false)
    setFileLinksOpenOutside(true)
    renderHook(() => useConfigFileLinkTarget(), { wrapper: wrapper({}) })
    expect(fileLinksOpenOutside()).toBe(false)
    setFileLinksOpenOutside(true)
    renderHook(() => useConfigFileLinkTarget(), { wrapper: wrapper() })
    expect(fileLinksOpenOutside()).toBe(false)
    setFileLinksOpenOutside(true)
    const failed = renderHook(() => useConfigFileLinkTarget(), { wrapper: wrapper(undefined, true) })
    failed.rerender()
    expect(fileLinksOpenOutside()).toBe(false)
  })
})
