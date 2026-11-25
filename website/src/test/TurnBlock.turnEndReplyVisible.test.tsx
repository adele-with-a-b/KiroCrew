import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import TurnBlock from '../pages/chat/TurnBlock'
import { applyRunningState, groupDisplayItems } from '../pages/chat/groupDisplayItems'
import type { DisplayItem, TurnItem } from '../pages/chat/types'
import type { ChatMessage } from '../types'

/**
 * A turn's last reply is the answer the person reads, so the collapse-all fold
 * never hides it under "Worked through N steps", whatever follows it in the
 * same display block. The runner stamps `meta.turn_stats` on that reply however
 * the turn ended (`_attach_turn_stats`), live after the `chat_done` refresh and
 * in the saved row alike.
 *
 * These orders put more text after it in one block, which makes the later text
 * the block's conclusion:
 *  - a turn the agent started on its own (claude: a background command exits
 *    and Claude Code answers the task notification) writes no opener row, so
 *    its rows join the block of the turn before it;
 *  - a finished workflow appends its result (an assistant-role envelope) after
 *    the reply of the turn that launched it, before the follow-up turn opens;
 *  - the slot-detail rebuild serves a cron row without its `injectKind`, so in
 *    a live tab it opens no block.
 */
const Q = 'Open the release PR and tell me when CI is green.'
const STEP_NOTE = 'Pushing the branch first, then I will open the pull request against main.'
const ANSWER = 'PR 64 is open and CI is green. Should I merge it now, or wait for the review?'
const LATER = 'The background build I started earlier also finished cleanly; nothing else to do here.'
const STATS = { turn_stats: { elapsed_ms: 4200 } }

let ts = 0
const row = (role: string, content: string, meta?: Record<string, unknown>): ChatMessage =>
  ({ role, content, ts: String(++ts), ...(meta ? { meta } : {}) }) as ChatMessage
const tool = (name: string) => row('tool', `🔧 Running: ${name}`)

const WORKFLOW_RESULT =
  '[Workflow completion event]\nWorkflow `release-check` (wf_abc123) → **completed**\n\nResult: all three checks passed on the release branch.'

/** The display block that holds `text`, folded by collapse-all. */
function blockWith(msgs: ChatMessage[], text: string, running = false): Extract<DisplayItem, { kind: 'turn' }> {
  const items = applyRunningState(groupDisplayItems(msgs), running)
  const turn = items.find((d): d is Extract<DisplayItem, { kind: 'turn' }> =>
    d.kind === 'turn' && d.items.some(it => it.kind === 'single' && it.msg.content === text))
  expect(turn).toBeDefined()
  return turn!
}

const renderItem = (it: TurnItem) => <div data-content>{it.kind === 'single' ? it.msg.content : 'group'}</div>

function folded(root: HTMLElement, text: string): boolean {
  const el = Array.from(root.querySelectorAll('[data-content]')).find(n => n.textContent === text)
  return el === undefined || el.closest('[data-collapsed="true"]') !== null
}

/** The person's turn, ended on its answer. `answerMeta` is what the backend stamped. */
const askedTurn = (answerMeta: Record<string, unknown> = STATS): ChatMessage[] => [
  row('user', Q),
  row('assistant', STEP_NOTE),
  tool('git push'),
  tool('gh pr create'),
  row('assistant', ANSWER, answerMeta),
]

/** A turn the agent started on its own: no opener row, a tool row, its own reply. */
const agentStartedTurn = (): ChatMessage[] => [
  tool('read background output'),
  row('assistant', LATER, { origin: { kind: 'agent' }, ...STATS }),
]

describe('TurnBlock — a turn-ending reply never folds under collapse-all', () => {
  it('keeps the answer open when a turn the agent started follows it (claude background task)', () => {
    const msgs = [...askedTurn(), ...agentStartedTurn()]
    const { container } = render(<TurnBlock turn={blockWith(msgs, ANSWER)} renderItem={renderItem} collapseAll />)
    expect(folded(container, ANSWER)).toBe(false)
    expect(folded(container, LATER)).toBe(false)
    expect(folded(container, STEP_NOTE)).toBe(true)
  })

  it('keeps it open after a reload, when the rows come back from history', () => {
    const msgs = [...askedTurn(), ...agentStartedTurn()]
    // A reload rebuilds the same rows from disk: fresh objects, same meta.
    const reloaded = msgs.map(m => ({ ...m, meta: m.meta ? { ...m.meta } : undefined }) as ChatMessage)
    const { container } = render(<TurnBlock turn={blockWith(reloaded, ANSWER)} renderItem={renderItem} collapseAll />)
    expect(folded(container, ANSWER)).toBe(false)
  })

  it('keeps it open live, across the agent-started turn settling', () => {
    const msgs = [...askedTurn(), ...agentStartedTurn()]
    const { container, rerender } = render(
      <TurnBlock turn={blockWith(msgs, ANSWER, true)} renderItem={renderItem} collapseAll />)
    expect(folded(container, ANSWER)).toBe(false)
    rerender(<TurnBlock turn={blockWith(msgs, ANSWER, false)} renderItem={renderItem} collapseAll />)
    expect(folded(container, ANSWER)).toBe(false)
  })

  it('keeps the answer open when a workflow result lands after it', () => {
    const msgs = [
      ...askedTurn(),
      row('assistant', WORKFLOW_RESULT, { kind: 'workflow_result' }),
      row('inject', 'The workflow finished; answer the request that started it.', { injectKind: 'workflow' }),
      tool('workflow_result'),
      row('assistant', LATER, STATS),
    ]
    const { container } = render(<TurnBlock turn={blockWith(msgs, ANSWER)} renderItem={renderItem} collapseAll />)
    expect(folded(container, ANSWER)).toBe(false)
    expect(folded(container, WORKFLOW_RESULT)).toBe(false)
    expect(folded(container, STEP_NOTE)).toBe(true)
  })

  it('keeps the answer open when a cron row arrives without its kind (the live slot refresh)', () => {
    // The slot-detail rebuild replaces a cron row's meta with its JSON cls, so
    // the live tab gets no injectKind and the row opens no block.
    const msgs = [
      ...askedTurn(),
      row('inject', '[Cron notification from "digest"] ping', { cronLabel: 'digest' }),
      row('assistant', LATER, STATS),
    ]
    const { container } = render(<TurnBlock turn={blockWith(msgs, ANSWER)} renderItem={renderItem} collapseAll />)
    expect(folded(container, ANSWER)).toBe(false)
  })

  it('keeps every answer open across several turns in one block', () => {
    const third = 'A second background command finished too; its log shows no warnings at all.'
    const msgs = [
      ...askedTurn(), ...agentStartedTurn(),
      tool('read second output'), row('assistant', third, { origin: { kind: 'agent' }, ...STATS }),
    ]
    const { container } = render(<TurnBlock turn={blockWith(msgs, ANSWER)} renderItem={renderItem} collapseAll />)
    expect(folded(container, ANSWER)).toBe(false)
    expect(folded(container, LATER)).toBe(false)
    expect(folded(container, third)).toBe(false)
  })

  it('keeps the visible answer open when the stamped row is only a steering note', () => {
    // The runner stamps the turn's last reply; a reply that is only a steering
    // note draws nothing, and the footer moves up to the answer above it.
    const msgs = [
      row('user', Q), row('assistant', STEP_NOTE), tool('git push'),
      row('assistant', ANSWER),
      row('assistant', '[STEERING steer-a1b2: also check the docs]', STATS),
      ...agentStartedTurn(),
    ]
    const { container } = render(<TurnBlock turn={blockWith(msgs, ANSWER)} renderItem={renderItem} collapseAll />)
    expect(folded(container, ANSWER)).toBe(false)
    expect(folded(container, STEP_NOTE)).toBe(true)
  })

  it('keeps the answer open when the workflow follow-up turn recovers before its first text', () => {
    const msgs = [
      ...askedTurn(),
      row('assistant', WORKFLOW_RESULT, { kind: 'workflow_result' }),
      row('inject', 'The workflow finished; answer the request that started it.', { injectKind: 'workflow' }),
      tool('workflow_result'),
      row('error', '⟳ Tool appeared stalled — recovering…', { kind: 'transient_retry' }),
      row('inject', '[Interrupted turn — automatic recovery]\nContinue from where it stopped.', { injectKind: 'recovery' }),
      row('assistant', LATER, STATS),
    ]
    const { container } = render(<TurnBlock turn={blockWith(msgs, ANSWER)} renderItem={renderItem} collapseAll />)
    expect(folded(container, ANSWER)).toBe(false)
  })

  it('keeps the answer open when a later turn a kindless inject started recovers before its first text', () => {
    // A cron row the slot-detail rebuild served without its kind opens no
    // block, so the later turn's rows, its recovery included, share this one.
    const msgs = [
      ...askedTurn(),
      row('inject', '[Cron notification from "digest"] ping', { cronLabel: 'digest' }),
      tool('read digest'),
      row('error', '⟳ Tool appeared stalled — recovering…', { kind: 'transient_retry' }),
      row('inject', '[Interrupted turn — automatic recovery]\nContinue from where it stopped.', { injectKind: 'recovery' }),
      row('assistant', LATER, STATS),
    ]
    const { container } = render(<TurnBlock turn={blockWith(msgs, ANSWER)} renderItem={renderItem} collapseAll />)
    expect(folded(container, ANSWER)).toBe(false)
    expect(folded(container, LATER)).toBe(false)
  })

  it('still folds stamped narration a user_replay continued, as a recovery would', () => {
    const msgs = [
      row('user', Q),
      row('assistant', STEP_NOTE, STATS),
      tool('git push'),
      row('inject', Q, { injectKind: 'user_replay' }),
      row('assistant', ANSWER, STATS),
    ]
    const { container } = render(<TurnBlock turn={blockWith(msgs, ANSWER)} renderItem={renderItem} collapseAll />)
    expect(folded(container, STEP_NOTE)).toBe(true)
    expect(folded(container, ANSWER)).toBe(false)
  })

  it('still folds stamped narration when a recovery resumed its turn', () => {
    const msgs = [
      row('user', Q),
      row('assistant', STEP_NOTE, STATS),
      tool('git push'),
      row('error', '⟳ Tool appeared stalled — recovering…', { kind: 'transient_retry' }),
      row('inject', '[Interrupted turn — automatic recovery]\nContinue from where it stopped.', { injectKind: 'recovery' }),
      row('assistant', ANSWER, STATS),
    ]
    const { container } = render(<TurnBlock turn={blockWith(msgs, ANSWER)} renderItem={renderItem} collapseAll />)
    expect(folded(container, STEP_NOTE)).toBe(true)
    expect(folded(container, ANSWER)).toBe(false)
  })

  it('still folds the progress note of the same turn, and every tool row', () => {
    const msgs = [...askedTurn(), ...agentStartedTurn()]
    const { container } = render(<TurnBlock turn={blockWith(msgs, ANSWER)} renderItem={renderItem} collapseAll />)
    expect(folded(container, STEP_NOTE)).toBe(true)
    expect(folded(container, '🔧 Running: git push')).toBe(true)
    expect(folded(container, '🔧 Running: read background output')).toBe(true)
  })

  it('leaves an interim fan-out region folded whole: the synthesis restates it', () => {
    const msgs = [
      row('user', Q),
      tool('spawn_run'),
      row('assistant', ANSWER, STATS),
      row('inject', 'Synthesize the results.', { injectKind: 'synthesis' }),
      row('assistant', LATER, STATS),
    ]
    const { container } = render(<TurnBlock turn={blockWith(msgs, ANSWER)} renderItem={renderItem} collapseAll />)
    expect(folded(container, ANSWER)).toBe(true)
  })
})

describe('TurnBlock — the cases that already kept the answer open', () => {
  it.each([
    ['a work-ledger wake (nudge row)', row('nudge', '[work-ledger wake] item=it_1 status=done')],
    ['a peer session send (user row)', row('user', '[sent by session peer-1 via session_send]\n\nStatus?', { relay: { sender: 'peer-1' } })],
    ['a steer typed mid-turn', row('user', 'Also check the docs.', { steer: true })],
    ['a cron delivery', row('inject', '[Cron notification from "job"] ping', { injectKind: 'cron' })],
  ])('the next turn opened by %s gets its own block', (_label, opener) => {
    const msgs = [...askedTurn(), opener, tool('next step'), row('assistant', LATER, STATS)]
    const { container } = render(<TurnBlock turn={blockWith(msgs, ANSWER)} renderItem={renderItem} collapseAll />)
    expect(folded(container, ANSWER)).toBe(false)
  })

  it('a steer typed mid-turn: the answer after it closes its own block', () => {
    const msgs = [
      row('user', Q), row('assistant', STEP_NOTE), tool('git push'),
      row('user', 'Also check the docs.', { steer: true }), tool('read docs'),
      row('assistant', ANSWER, STATS),
    ]
    const { container } = render(<TurnBlock turn={blockWith(msgs, ANSWER)} renderItem={renderItem} collapseAll />)
    expect(folded(container, ANSWER)).toBe(false)
  })

  it('a reply a restart cut, carried on by the adopted turn, draws as one open bubble', () => {
    const msgs = [
      row('user', Q), row('assistant', STEP_NOTE), tool('git push'),
      row('assistant', 'PR 64 is open and CI is', { carries_on: true, ...STATS }),
      row('assistant', ' green. Should I merge it now, or wait for the review?', { origin: { kind: 'agent' }, ...STATS }),
    ]
    const turn = blockWith(msgs, STEP_NOTE)
    const { container } = render(<TurnBlock turn={turn} renderItem={renderItem} collapseAll />)
    const bubble = Array.from(container.querySelectorAll('[data-content]')).find(n => n.textContent?.includes('PR 64 is open'))
    expect(bubble?.closest('[data-collapsed="true"]') ?? null).toBeNull()
  })

  it('a claude reply stamped reply_end stays open with a turn after it', () => {
    const msgs = [...askedTurn({ ...STATS, reply_end: true }), ...agentStartedTurn()]
    const { container } = render(<TurnBlock turn={blockWith(msgs, ANSWER)} renderItem={renderItem} collapseAll />)
    expect(folded(container, ANSWER)).toBe(false)
  })
})
