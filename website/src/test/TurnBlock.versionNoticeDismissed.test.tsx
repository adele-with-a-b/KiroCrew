/**
 * A notice whose release was dismissed on the gateway draws nothing, so a
 * collapsed turn neither folds nor counts it, like the dismissed hooks notice
 * (TurnBlock.specHooksNoticeDismissed.test.tsx). The count follows the shared
 * dismissals read, including a dismissal that lands while the turn is on screen,
 * and a turn with no query client counts the notice like any other.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import TurnBlock from '../pages/chat/TurnBlock'
import { groupDisplayItems, applyRunningState } from '../pages/chat/groupDisplayItems'
import { VERSION_NOTICE_DISMISSALS_QUERY_KEY } from '../pages/chat/UntestedVersionNotice'
import type { TurnItem } from '../pages/chat/types'
import type { ChatMessage } from '../types'

const renderTurnItem = (it: TurnItem): ReactNode => (
  it.kind === 'single'
    ? <div key={`m${it.msg.ts}`} data-testid={`row-${it.msg.role}`}>{it.msg.content}</div>
    : <div key={`g${it.startIdx}`} data-testid="row-group" />
)

let seq = 0
const tool = (): ChatMessage => ({ role: 'tool', content: '🔧 Running: fs_read', ts: `${++seq}` })
const versionNotice = (): ChatMessage => ({
  role: 'notice',
  content: '⚠️ claude 0.109.0 is outside the tested range 0.94.x.',
  cls: 'msg msg-info',
  ts: `${++seq}`,
  meta: { kind: 'untested_version_notice', harness: 'claude', version: '0.109.0' },
})

function turnOf(): never {
  seq = 0
  const messages: ChatMessage[] = [
    { role: 'user', content: 'summarize the README', ts: `${++seq}` },
    versionNotice(), tool(),
    { role: 'assistant', content: 'The README explains how to install the gateway and start it.', ts: `${++seq}` },
  ]
  const turn = applyRunningState(groupDisplayItems(messages), false).find(i => i.kind === 'turn')
  expect(turn).toBeTruthy()
  return turn as never
}

const toggleText = () => screen.getByRole('button').textContent ?? ''

let qc: QueryClient
beforeEach(() => { qc = new QueryClient({ defaultOptions: { queries: { retry: false } } }) })
const drawTurn = () => render(
  <QueryClientProvider client={qc}>
    <TurnBlock turn={turnOf()} renderItem={renderTurnItem} collapseAll disclosureKey="t" />
  </QueryClientProvider>,
)

describe('a collapsed turn holding the untested-version notice', () => {
  it('counts the notice as a step while its release is not dismissed', () => {
    qc.setQueryData(VERSION_NOTICE_DISMISSALS_QUERY_KEY, { dismissed: { claude: ['0.100.0'] } })
    drawTurn()
    expect(toggleText()).toMatch(/\b2\b/)
  })

  it('leaves a dismissed release out of the fold and the count', () => {
    qc.setQueryData(VERSION_NOTICE_DISMISSALS_QUERY_KEY, { dismissed: { claude: ['0.109.0'] } })
    drawTurn()
    expect(toggleText()).toMatch(/\b1\b/)
    expect(toggleText()).not.toMatch(/\b2\b/)
  })

  it('recounts when the release is dismissed while the turn is on screen', () => {
    drawTurn()
    expect(toggleText()).toMatch(/\b2\b/)
    act(() => { qc.setQueryData(VERSION_NOTICE_DISMISSALS_QUERY_KEY, { dismissed: { claude: ['0.109.0'] } }) })
    expect(toggleText()).toMatch(/\b1\b/)
  })

  it('counts it with no query client at all', () => {
    render(<TurnBlock turn={turnOf()} renderItem={renderTurnItem} collapseAll disclosureKey="t" />)
    expect(toggleText()).toMatch(/\b2\b/)
  })
})
