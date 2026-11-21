/**
 * The untested-version notice's "Don't show again for this version": the
 * dismissal is the gateway's (one read, one POST), so a release dismissed
 * anywhere hides every copy of its notice, a different release keeps showing,
 * and a viewer the gateway refuses gets the notice without the link.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, renderHook, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

import { ApiError } from '../../api/apiError'
import type { ChatMessage } from '../../types'

const versionNoticeDismissals = vi.fn()
const dismissVersionNotice = vi.fn()
vi.mock('../../api/client', async importOriginal => ({
  ...(await importOriginal<typeof import('../../api/client')>()),
  api: {
    versionNoticeDismissals: (...a: unknown[]) => versionNoticeDismissals(...a),
    dismissVersionNotice: (...a: unknown[]) => dismissVersionNotice(...a),
  },
}))

const { default: UntestedVersionNotice, isUntestedVersionNotice, untestedVersionSubject, useDismissedVersionNotice, VERSION_NOTICE_DISMISSALS_QUERY_KEY } =
  await import('./UntestedVersionNotice')
const { createTranscriptRenderers } = await import('./transcriptRenderers')

const CONTENT = "⚠️ claude 0.109.0 is outside the tested range 0.94.x. If something breaks, run 'npm i -g @agentclientprotocol/claude-agent-acp@0.94.0', then start a new chat."

const row = (version = '0.109.0', harness = 'claude'): ChatMessage => ({
  role: 'notice',
  content: CONTENT,
  cls: 'msg msg-info',
  meta: { kind: 'untested_version_notice', harness, version },
})

let qc: QueryClient
const wrap = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>

function draw(m: ChatMessage = row()) {
  const subject = untestedVersionSubject(m)
  if (!subject) throw new Error('not the notice')
  return render(<UntestedVersionNotice content={m.content} subject={subject} askAgent={false} />, { wrapper: wrap })
}

beforeEach(() => {
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  versionNoticeDismissals.mockReset()
  dismissVersionNotice.mockReset()
})
afterEach(() => qc.clear())

describe('untestedVersionSubject', () => {
  it('reads the harness and release off the tagged row only', () => {
    expect(untestedVersionSubject(row())).toEqual({ harness: 'claude', version: '0.109.0' })
    expect(isUntestedVersionNotice({ ...row(), meta: undefined })).toBe(false)
    expect(isUntestedVersionNotice({ ...row(), role: 'assistant' })).toBe(false)
    expect(isUntestedVersionNotice({ ...row(), meta: { kind: 'untested_version_notice', harness: 'claude' } })).toBe(false)
  })

  it('is the row the dashboard transcript claims for it', () => {
    const claimed = createTranscriptRenderers({}).find(r => r.roles.includes('notice') && (r.match ? r.match(row()) : true))
    expect(claimed?.id).toBe('untested_version_notice')
  })
})

describe('UntestedVersionNotice', () => {
  it('draws nothing until the dismissals are read, then the notice with its link', async () => {
    let answer: (v: unknown) => void = () => {}
    versionNoticeDismissals.mockReturnValue(new Promise(r => { answer = r }))
    draw()
    // A dismissed release must not flash on a page load while the read runs.
    expect(screen.queryByTestId('notice-card-text')).toBeNull()
    await act(async () => { answer({ dismissed: {} }) })
    expect((await screen.findByTestId('notice-card-text')).textContent).toContain('claude 0.109.0 is outside the tested range')
    const link = screen.getByTestId('untested-version-notice-dismiss')
    expect(link.textContent).toBe("Don't show again for this version")
    expect(link.getAttribute('aria-label')).toBe("Don't show this notice again for claude 0.109.0")
  })

  it('dismisses this harness at this release on the gateway, and hides', async () => {
    versionNoticeDismissals.mockResolvedValue({ dismissed: {} })
    dismissVersionNotice.mockResolvedValue({ dismissed: { claude: ['0.109.0'] } })
    draw()
    await userEvent.click(await screen.findByTestId('untested-version-notice-dismiss'))
    expect(dismissVersionNotice).toHaveBeenCalledWith('claude', '0.109.0')
    await waitFor(() => expect(screen.queryByTestId('notice-card-text')).toBeNull())
  })

  it('draws nothing for a release the gateway says is dismissed', async () => {
    versionNoticeDismissals.mockResolvedValue({ dismissed: { claude: ['0.100.0', '0.109.0'] } })
    draw()
    await waitFor(() => expect(screen.queryByTestId('notice-card-text')).toBeNull())
  })

  it('keeps showing a different release, and the same release of another harness', async () => {
    versionNoticeDismissals.mockResolvedValue({ dismissed: { claude: ['0.109.0'], codex: ['0.110.0'] } })
    draw(row('0.110.0'))
    expect(await screen.findByTestId('untested-version-notice-dismiss')).toBeTruthy()
    expect(screen.getByTestId('notice-card-text').textContent).toContain('claude 0.109.0 is outside the tested range')
  })

  it('says so and keeps the notice when the dismissal is not saved', async () => {
    versionNoticeDismissals.mockResolvedValue({ dismissed: {} })
    dismissVersionNotice.mockRejectedValue(new ApiError(500, 'API 500', '{"code":"dismissal_not_saved"}'))
    draw()
    await userEvent.click(await screen.findByTestId('untested-version-notice-dismiss'))
    expect((await screen.findByTestId('untested-version-notice-error')).textContent).toContain("Couldn't save the dismissal. Try again.")
    expect(screen.getByTestId('notice-card-text')).toBeTruthy()
  })

  it('shows the notice when the dismissals cannot be read', async () => {
    // A deadline, which the read never retries.
    versionNoticeDismissals.mockRejectedValue(Object.assign(new Error('read timed out'), { name: 'TimeoutError' }))
    draw()
    expect((await screen.findByTestId('notice-card-text')).textContent).toContain('outside the tested range')
    expect(screen.getByTestId('untested-version-notice-dismiss')).toBeTruthy()
  })

  it('shows a viewer the gateway refuses the notice with no link', async () => {
    versionNoticeDismissals.mockRejectedValue(new ApiError(403, 'API 403', '{"code":"owner_only"}'))
    draw()
    expect(await screen.findByTestId('notice-card-text')).toBeTruthy()
    expect(qc.getQueryState(VERSION_NOTICE_DISMISSALS_QUERY_KEY)?.status).toBe('error')
    expect(screen.queryByTestId('untested-version-notice-dismiss')).toBeNull()
  })

  it('hides every copy when one is dismissed', async () => {
    versionNoticeDismissals.mockResolvedValue({ dismissed: {} })
    dismissVersionNotice.mockResolvedValue({ dismissed: { claude: ['0.109.0'] } })
    const subject = { harness: 'claude', version: '0.109.0' }
    render(
      <>
        <UntestedVersionNotice content={CONTENT} subject={subject} askAgent={false} />
        <UntestedVersionNotice content={CONTENT} subject={subject} askAgent={false} />
      </>,
      { wrapper: wrap },
    )
    const links = await screen.findAllByTestId('untested-version-notice-dismiss')
    expect(links).toHaveLength(2)
    await userEvent.click(links[0])
    await waitFor(() => expect(screen.queryAllByTestId('notice-card-text')).toHaveLength(0))
    expect(versionNoticeDismissals).toHaveBeenCalledTimes(1)
  })
})

describe('useDismissedVersionNotice', () => {
  it('hides nothing with no query client, and fetches nothing', () => {
    const { result } = renderHook(() => useDismissedVersionNotice(true))
    expect(result.current(row())).toBe(false)
    expect(versionNoticeDismissals).not.toHaveBeenCalled()
  })

  it('follows the cached read while watching, and only then', () => {
    const watching = renderHook(() => useDismissedVersionNotice(true), { wrapper: wrap })
    const idle = renderHook(() => useDismissedVersionNotice(false), { wrapper: wrap })
    expect(watching.result.current(row())).toBe(false)
    act(() => { qc.setQueryData(VERSION_NOTICE_DISMISSALS_QUERY_KEY, { dismissed: { claude: ['0.109.0'] } }) })
    expect(watching.result.current(row())).toBe(true)
    expect(watching.result.current(row('0.110.0'))).toBe(false)
    expect(idle.result.current(row())).toBe(false)
    expect(versionNoticeDismissals).not.toHaveBeenCalled()
    // Unmounted here, so the afterEach's cache clear re-renders nothing.
    watching.unmount()
    idle.unmount()
  })
})
