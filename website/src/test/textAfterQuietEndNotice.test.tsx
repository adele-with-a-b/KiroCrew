import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent } from '@testing-library/react'
import type { ReactNode } from 'react'

import type { ChatMessage } from '../types'
import { defaultMessageRenderers, mergeRenderers, resolveRenderer, type MessageRenderContext } from '../app-sdk/messageRenderers'
import { createTranscriptRenderers } from '../pages/chat/transcriptRenderers'
import TurnBlock from '../pages/chat/TurnBlock'
import { groupDisplayItems, applyRunningState } from '../pages/chat/groupDisplayItems'
import type { TurnItem } from '../pages/chat/types'
import { TEXT_AFTER_QUIET_END_NOTICE } from '../hooks/unreadOnAttention'
import en from '../i18n/locales/en.json'
import '../i18n/all'

/**
 * The notice row that keeps text a turn wrote after it ended quietly
 * (`dashboard/chat_turn/stream_chunks.py`): one line, and the text in a closed
 * fold that one click opens.
 */

const answer = 'CI is green again, and the fix PR is merged.\n**Next:** nothing.'

const row = (over: Partial<ChatMessage> = {}): ChatMessage =>
  ({
    role: 'notice',
    content: answer,
    cls: 'msg msg-info',
    ts: '2',
    meta: { notice: TEXT_AFTER_QUIET_END_NOTICE },
    ...over,
  }) as ChatMessage

const ctx: MessageRenderContext = {
  index: 0,
  messages: [],
  running: false,
  key: 'k0',
  hideCardOwnedOAuth: false,
  autoDeniedIds: new Set<string>(),
  wrapper: (children) => children,
  row: (children) => children,
}

const registries = [
  ['main transcript registry', mergeRenderers(createTranscriptRenderers({ slot: 's1' }))],
  ['app-sdk default registry', defaultMessageRenderers],
] as const

afterEach(cleanup)

describe('text kept after a quiet end', () => {
  it.each(registries)('%s draws it with its own renderer, matched on its marker', (_name, registry) => {
    expect(resolveRenderer(row(), registry)?.id).toBe('text_after_quiet_end')
    expect(resolveRenderer(row({ meta: {} }), registry)?.id).not.toBe('text_after_quiet_end')
  })

  it.each(registries)('%s shows one line, and one click opens the text', (_name, registry) => {
    const m = row()
    render(<>{resolveRenderer(m, registry)?.render(m, ctx)}</>)
    const fold = screen.getByTestId('text-after-quiet-end') as HTMLDetailsElement
    const summary = fold.querySelector('summary') as HTMLElement
    expect(summary.textContent).toBe(en.pages.chat.noticeCard.text_after_quiet_end)
    expect(fold.open).toBe(false)
    fireEvent.click(summary)
    expect(fold.open).toBe(true)
    // The text as written, never rendered as markdown.
    expect(fold.textContent).toContain(answer)
    expect(fold.querySelector('strong')).toBeNull()
  })

  it('stays in view, folded, when a quiet turn collapses its steps', () => {
    const renderTurnItem = (it: TurnItem): ReactNode => (
      it.kind === 'single'
        ? <div key={`m${it.msg.ts}`} data-testid={`row-${it.msg.role}`}>{it.msg.content}</div>
        : <div key={`g${it.startIdx}`} data-testid="row-group" />
    )
    const messages: ChatMessage[] = [
      { role: 'nudge', content: 'patrol', ts: 'u' },
      { role: 'tool', content: '🔧 Running: gh pr view', ts: '1' },
      { role: 'tool', content: '🔧 nothing_to_do', ts: '1b' },
      row(),
    ]
    const turn = applyRunningState(groupDisplayItems(messages), false).find(i => i.kind === 'turn')
    expect(turn).toBeTruthy()
    render(<TurnBlock turn={turn as never} renderItem={renderTurnItem} collapseAll disclosureKey="t" />)
    expect(screen.getByTestId('row-notice').closest('[data-collapsed="true"]')).toBeNull()
  })
})
