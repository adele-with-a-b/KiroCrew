import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import type { ReactNode } from 'react'

import type { ChatMessage } from '../types'
import { resolveGatewayNotice, restartReceiptDetails } from '../pages/chat/gatewayNotice'
import RestartReceiptNotice from '../pages/chat/RestartReceiptNotice'
import { defaultMessageRenderers, mergeRenderers, resolveRenderer } from '../app-sdk/messageRenderers'
import { createTranscriptRenderers } from '../pages/chat/transcriptRenderers'
import TurnBlock from '../pages/chat/TurnBlock'
import { groupDisplayItems, applyRunningState } from '../pages/chat/groupDisplayItems'
import type { TurnItem } from '../pages/chat/types'
import en from '../i18n/locales/en.json'
import '../i18n/all'

/**
 * The restart receipt of a chat a chat keeper kept running
 * (`dashboard/restart_receipt.py`): one line worded from its token, and a closed
 * fold with only what the keeper measured.
 */

const copy = en.pages.chat.gatewayNotice

const row = (args?: Record<string, unknown>): ChatMessage =>
  ({
    role: 'notice',
    content: 'fallback prose the dashboard never shows',
    cls: 'msg msg-info',
    meta: { kind: 'restart_receipt', notice: 'restart_kept_running', ...(args ? { notice_args: args } : {}) },
  }) as ChatMessage

afterEach(cleanup)

describe('restart receipt notice', () => {
  it('is matched on its token only', () => {
    expect(restartReceiptDetails(row())).not.toBeNull()
    expect(restartReceiptDetails({ ...row(), meta: { notice: 'turn_interrupted_restart' } } as ChatMessage)).toBeNull()
    expect(restartReceiptDetails({ ...row(), role: 'error' })).toBeNull()
  })

  it.each([
    ['main transcript registry', mergeRenderers(createTranscriptRenderers({ slot: 's1' }))],
    ['app-sdk default registry', defaultMessageRenderers],
  ])('%s draws it with its own renderer, not the plain notice card', (_name, registry) => {
    expect(resolveRenderer(row(), registry)?.id).toBe('restart_receipt')
  })

  it('reads its line from the catalog, not the row English', () => {
    expect(resolveGatewayNotice(row(), [], 0)?.text).toBe(copy.restart_kept_running)
  })

  it('says nothing about waits when no approval waited', () => {
    const lines = restartReceiptDetails(row({ down_secs: 42 })) ?? []
    expect(lines).toHaveLength(2)
    expect(lines[0]).toBe(copy.restart_receipt_kept)
    expect(lines[1]).toContain('42')
    expect(lines.join(' ')).not.toMatch(/approval/)
  })

  it('reports the measured waits', () => {
    const lines = restartReceiptDetails(row({ down_secs: 63, waits: 2, wait_secs: 41 })) ?? []
    expect(lines).toHaveLength(3)
    expect(lines[2]).toMatch(/^2 tool calls waited up to .*41.* for approval\.$/)
    const one = restartReceiptDetails(row({ down_secs: 9, waits: 1, wait_secs: 8 })) ?? []
    expect(one[2]).toMatch(/^1 tool call waited .*8.* for approval\.$/)
  })

  it('drops a number it cannot trust', () => {
    expect(restartReceiptDetails(row({ down_secs: -1, waits: 3 }))).toEqual([copy.restart_receipt_kept])
    expect(restartReceiptDetails(row({ down_secs: 'x', waits: 2, wait_secs: null }))).toEqual([copy.restart_receipt_kept])
  })

  it('draws one line with a closed fold of the details', () => {
    render(<RestartReceiptNotice content="kept" details={['first', 'second']} />)
    expect(screen.getByTestId('notice-card-text').textContent).toContain('kept')
    const fold = screen.getByTestId('restart-receipt-details') as HTMLDetailsElement
    expect(fold.open).toBe(false)
    expect(fold.querySelector('summary')?.textContent).toBe(copy.restart_receipt_details)
    expect([...fold.querySelectorAll('li')].map(li => li.textContent)).toEqual(['first', 'second'])
  })
})

describe('a collapsed turn the restart receipt lands in', () => {
  const renderTurnItem = (it: TurnItem): ReactNode => (
    it.kind === 'single'
      ? <div key={`m${it.msg.ts}`} data-testid={`row-${it.msg.role}`}>{it.msg.content}</div>
      : <div key={`g${it.startIdx}`} data-testid="row-group" />
  )
  const tool = (ts: string): ChatMessage => ({ role: 'tool', content: '🔧 Running: Bash', ts })

  it('keeps the receipt visible while the steps around it fold', () => {
    const messages: ChatMessage[] = [
      { role: 'user', content: 'Run the build.', ts: 'u' },
      tool('1'),
      { ...row({ down_secs: 31 }), ts: '2' },
      tool('3'),
      { role: 'assistant', content: 'DONE', ts: '9' },
    ]
    const turn = applyRunningState(groupDisplayItems(messages), false).find(i => i.kind === 'turn')
    expect(turn).toBeTruthy()
    const { container } = render(<TurnBlock turn={turn as never} renderItem={renderTurnItem} collapseAll disclosureKey="t" />)
    expect(screen.getByTestId('row-notice').closest('[data-collapsed="true"]')).toBeNull()
    expect(container.querySelectorAll('[data-collapsed="true"]')).toHaveLength(2)
  })
})
