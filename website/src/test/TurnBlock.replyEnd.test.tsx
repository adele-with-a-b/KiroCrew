import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import TurnBlock from '../pages/chat/TurnBlock'
import { findContinuedReplies, groupDisplayItems } from '../pages/chat/groupDisplayItems'
import type { DisplayItem, TurnItem } from '../pages/chat/types'
import type { ChatMessage } from '../types'

/**
 * A turn the harness sent back: the agent answered and ended its turn, a Stop
 * hook blocked the stop (that feedback leaves no row in the transcript), and
 * the agent kept working and wrote a correction. The answer is then no longer
 * the turn's last message, so the conclusion rule alone folded it out of view.
 * `meta.reply_end` (set by the backend on a segment whose model message ended
 * on its text) keeps it open; a progress note never carries it and still folds.
 * The correction is the turn's last message, so the conclusion rule keeps it.
 */
const PROGRESS = 'Let me look at how the scheduler reads its config before I answer that question.'
const ANSWER = 'The job runs at 09:00 local time because the scheduler reads `timezone: local` from config.'
const CORRECTION = 'Correction: the hook flagged that config.json overrides it; the job actually runs at 09:00 UTC.'

function sentBackTurn(answerMeta: Record<string, unknown> | undefined): Extract<DisplayItem, { kind: 'turn' }> {
  const grouped = groupDisplayItems([
    { role: 'user', content: 'When does the backup job run?', ts: '0' },
    { role: 'assistant', content: PROGRESS, ts: '1' },
    { role: 'tool', content: '🔧 Running: read scheduler.py', ts: '2' },
    { role: 'assistant', content: ANSWER, ts: '3', ...(answerMeta ? { meta: answerMeta } : {}) },
    { role: 'tool', content: '🔧 Running: read config.json', ts: '4' },
    { role: 'assistant', content: CORRECTION, ts: '5', meta: { turn_stats: { elapsed_ms: 900 } } },
  ] as ChatMessage[])
  const turn = grouped.turns.find((d): d is Extract<DisplayItem, { kind: 'turn' }> => d.kind === 'turn')
  expect(turn).toBeDefined()
  return turn!
}

const renderItem = (it: TurnItem) => (
  <div data-content>{it.kind === 'single' ? it.msg.content : 'group'}</div>
)

/** Whether the row holding `text` is folded: either not rendered at all or
 *  inside a collapsed section. */
function isFolded(root: HTMLElement, text: string): boolean {
  const el = Array.from(root.querySelectorAll('[data-content]')).find(n => n.textContent === text)
  return el === undefined || el.closest('[data-collapsed="true"]') !== null
}

describe('TurnBlock — a reply the model ended on stays open (meta.reply_end)', () => {
  it('keeps the answer and the correction open and folds the progress note', () => {
    const { container } = render(
      <TurnBlock turn={sentBackTurn({ reply_end: true })} renderItem={renderItem} collapseAll />,
    )
    expect(isFolded(container, ANSWER)).toBe(false)
    expect(isFolded(container, CORRECTION)).toBe(false)
    expect(isFolded(container, PROGRESS)).toBe(true)
  })

  it('folds the same answer without the flag (the shape that hid it)', () => {
    const { container } = render(
      <TurnBlock turn={sentBackTurn(undefined)} renderItem={renderItem} collapseAll />,
    )
    expect(isFolded(container, ANSWER)).toBe(true)
    expect(isFolded(container, CORRECTION)).toBe(false)
  })
})

/** The row text in DOM order, with each step line as `NOTE`. */
function order(root: HTMLElement): string[] {
  return Array.from(root.querySelectorAll('[data-content], [data-reply-continued]')).map(n =>
    n.hasAttribute('data-reply-continued') ? 'NOTE' : (n.textContent ?? ''))
}

function turnOf(msgs: ChatMessage[]): Extract<DisplayItem, { kind: 'turn' }> {
  const turn = groupDisplayItems(msgs).turns.find((d): d is Extract<DisplayItem, { kind: 'turn' }> => d.kind === 'turn')
  expect(turn).toBeDefined()
  return turn!
}

describe('TurnBlock — a step line shows where a reply ended and the turn went on', () => {
  it.each([true, false])('sits right under the answer, before the text after it (collapseAll=%s)', collapseAll => {
    const { container } = render(
      <TurnBlock turn={sentBackTurn({ reply_end: true })} renderItem={renderItem} collapseAll={collapseAll} />,
    )
    const rows = order(container)
    expect(rows.indexOf('NOTE')).toBe(rows.indexOf(ANSWER) + 1)
    expect(rows.indexOf(CORRECTION)).toBeGreaterThan(rows.indexOf('NOTE'))
    expect(rows.filter(r => r === 'NOTE')).toHaveLength(1)
    expect(container.querySelector('[data-reply-continued]')?.textContent).toBe('The reply continued after it ended')
  })

  it('shows between two replies with no step between them (the Stop hook shape)', () => {
    const { container } = render(
      <TurnBlock
        turn={turnOf([
          { role: 'user', content: 'When does the backup job run?', ts: '0' },
          { role: 'assistant', content: ANSWER, ts: '1', meta: { reply_end: true } },
          { role: 'assistant', content: CORRECTION, ts: '2', meta: { turn_stats: { elapsed_ms: 900 } } },
        ] as ChatMessage[])}
        renderItem={renderItem}
        collapseAll
      />,
    )
    expect(order(container)).toEqual([ANSWER, 'NOTE', CORRECTION])
  })

  it('does not show without the mark', () => {
    const { container } = render(
      <TurnBlock turn={sentBackTurn(undefined)} renderItem={renderItem} collapseAll />,
    )
    expect(container.querySelector('[data-reply-continued]')).toBeNull()
  })

  it('does not show when no text follows the marked reply', () => {
    const { container } = render(
      <TurnBlock
        turn={turnOf([
          { role: 'user', content: 'When does the backup job run?', ts: '0' },
          { role: 'assistant', content: ANSWER, ts: '1', meta: { reply_end: true } },
          { role: 'tool', content: '🔧 Running: read config.json', ts: '2' },
        ] as ChatMessage[])}
        renderItem={renderItem}
        collapseAll
      />,
    )
    expect(container.querySelector('[data-reply-continued]')).toBeNull()
  })

  it('shows under the reply while the text after it still streams', () => {
    const { container } = render(
      <TurnBlock
        turn={{
          ...turnOf([
            { role: 'user', content: 'When does the backup job run?', ts: '0' },
            { role: 'assistant', content: ANSWER, ts: '1', meta: { reply_end: true } },
            { role: 'streaming', content: CORRECTION, ts: '2' },
          ] as ChatMessage[]),
          complete: false,
        }}
        renderItem={renderItem}
        collapseAll
      />,
    )
    expect(order(container)).toEqual([ANSWER, 'NOTE', CORRECTION])
  })
})

describe('findContinuedReplies — what counts as the reply going on', () => {
  const rows = (msgs: ChatMessage[]): TurnItem[] => msgs.map(msg => ({ kind: 'single', msg }) as TurnItem)
  const marked = { role: 'assistant', content: ANSWER, ts: '1', meta: { reply_end: true } } as ChatMessage

  it('counts a saved or a streaming reply after the marked one', () => {
    expect([...findContinuedReplies(rows([marked, { role: 'assistant', content: CORRECTION, ts: '2' } as ChatMessage]))]).toEqual([0])
    expect([...findContinuedReplies(rows([marked, { role: 'streaming', content: CORRECTION, ts: '2' } as ChatMessage]))]).toEqual([0])
  })

  it('does not count an envelope another writer appended', () => {
    const envelope = { role: 'assistant', content: 'Workflow finished: 3 of 3 steps.', ts: '2', meta: { kind: 'workflow_result' } } as ChatMessage
    expect(findContinuedReplies(rows([marked, envelope])).size).toBe(0)
  })

  it('leaves a marked reply nothing followed as a loose row', () => {
    const grouped = groupDisplayItems([
      { role: 'user', content: 'When does the backup job run?', ts: '0' },
      marked,
    ] as ChatMessage[])
    expect(grouped.turns.some(d => d.kind === 'turn')).toBe(false)
  })
})
