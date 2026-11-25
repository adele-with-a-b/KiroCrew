import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import TurnBlock from '../pages/chat/TurnBlock'
import { groupDisplayItems } from '../pages/chat/groupDisplayItems'
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
