import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { renderWithProviders } from './helpers'
import { server } from './mocks/server'
import { AgentBackendTab } from '../src/pages/developer/AgentBackendTab'

vi.mock('../src/pages/developer/KiroSignInCard', () => ({ KiroSignInCard: () => null }))

const LABEL = 'Guard Claude Code Chats'
const FAILED = 'Could not save the Guard Claude Code Chats setting. Try again.'

let agent: { acp_backend: string; claude_guards?: boolean }
let writes: unknown[]

beforeEach(() => {
  agent = { acp_backend: 'claude' }
  writes = []
  server.use(
    http.get('/api/config/kirocrew', () => HttpResponse.json({ agent })),
    http.get('/api/config/schema', () => HttpResponse.json({ entries: [
      { path: 'agent.acp_backend', type: 'enum', enumValues: ['kas', 'kiro', 'claude'] },
      { path: 'agent.claude_guards', type: 'boolean', defaultValue: false },
    ] })),
    http.get('/api/acp-backends', () => HttpResponse.json({ backends: [] })),
    http.patch('/api/config/kirocrew', async ({ request }) => {
      const body = await request.json() as { path: string; value: boolean }
      writes.push(body)
      if (body.path !== 'agent.claude_guards') return new HttpResponse(null, { status: 400 })
      agent = { ...agent, claude_guards: body.value }
      return HttpResponse.json({ ok: true })
    }),
  )
})

const toggle = () => screen.getByRole('switch', { name: LABEL })
const open = (route = '/settings/agent') => renderWithProviders(<AgentBackendTab />, { route })

async function ready() {
  await screen.findByRole('switch', { name: LABEL })
  await waitFor(() => expect(toggle()).toBeEnabled())
}

describe('Guard Claude Code Chats setting', () => {
  it('explains what it changes and what it leaves, without saving when the info tip opens', async () => {
    const user = userEvent.setup()
    open()
    await ready()
    const row = toggle().closest('[data-setting-key]') as HTMLElement
    expect(row).toHaveAttribute('data-setting-key', 'agent.claude_guards')
    expect(within(row).getByText(
      'Claude Code chats ask Kiro Crew before they use a tool, even when your own Claude Code settings let them run tools without asking. Changes apply when a chat starts; an open chat keeps its current mode.',
    )).toBeVisible()
    await user.click(within(row).getByRole('button'))
    const tip = await screen.findByRole('tooltip')
    expect(tip).toHaveTextContent('With Normal you approve each call that asks; with YOLO it runs unless a Kiro Crew rule blocks it.')
    expect(tip).toHaveTextContent('A tool your own Claude Code settings already allow still runs without asking')
    expect(writes).toEqual([])
  })

  it('defaults off, saves both values, and reads the saved value after reopening', async () => {
    const user = userEvent.setup()
    const view = open()
    await ready()
    expect(toggle()).toHaveAttribute('aria-checked', 'false')
    await user.click(toggle())
    await waitFor(() => expect(toggle()).toHaveAttribute('aria-checked', 'true'))
    expect(writes).toEqual([{ path: 'agent.claude_guards', value: true }])
    expect(agent.acp_backend).toBe('claude')

    view.unmount()
    open()
    await ready()
    expect(toggle()).toHaveAttribute('aria-checked', 'true')
    await user.click(toggle())
    await waitFor(() => expect(toggle()).toHaveAttribute('aria-checked', 'false'))
    expect(writes).toEqual([
      { path: 'agent.claude_guards', value: true },
      { path: 'agent.claude_guards', value: false },
    ])
  })

  it('keeps the saved value and offers a retry after a rejected save', async () => {
    const user = userEvent.setup()
    server.use(http.patch('/api/config/kirocrew', () => HttpResponse.json({ error: 'refused' }, { status: 403 }), { once: true }))
    open()
    await ready()
    await user.click(toggle())
    expect(await screen.findByText(FAILED)).toBeInTheDocument()
    expect(toggle()).toHaveAttribute('aria-checked', 'false')
    await user.click(toggle())
    await waitFor(() => expect(toggle()).toHaveAttribute('aria-checked', 'true'))
    expect(screen.queryByText(FAILED)).toBeNull()
  })

  it.each([
    'key%3Aagent.claude_guards',
    'agent.guard-claude-code-chats',
  ])('shows only in Claude Code detail and opens it from the %s highlight without switching harness', async highlight => {
    agent = { acp_backend: 'kas' }
    const user = userEvent.setup()
    open(`/settings/agent?highlight=${highlight}`)
    await ready()
    expect(screen.getByRole('radio', { name: 'Claude Code' })).toBeChecked()
    await user.click(screen.getByRole('radio', { name: 'Kiro Agent' }))
    expect(screen.queryByRole('switch', { name: LABEL })).toBeNull()
    expect(writes).toEqual([])
    expect(agent.acp_backend).toBe('kas')
  })
})
