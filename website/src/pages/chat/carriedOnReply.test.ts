import { describe, expect, it } from 'vitest'

import type { ChatMessage } from '../../types'
import { groupDisplayItems } from './groupDisplayItems'
import type { DisplayItem, TurnItem } from './types'

const row = (role: string, content: string, ts: string, meta?: Record<string, unknown>) =>
  ({ role, content, ts, cls: '', ...(meta ? { meta } : {}) }) as ChatMessage

/** Every message the grouping draws, in order. */
const drawn = (items: DisplayItem[]): ChatMessage[] =>
  items.flatMap((it) => {
    const of = (t: TurnItem): ChatMessage[] => (t.kind === 'single' ? [t.msg] : t.msgs)
    return it.kind === 'turn' ? it.items.flatMap(of) : of(it)
  })

const user = row('user', 'Compare the film stocks for me.', 't0')
const cut = row('assistant', 'For landscapes and nature,', 't1', { carries_on: true, mid: 'm-cut' })
const notice = row('notice', 'claude is outside the tested range', 't2')
const rest = row('assistant', ' though, it quickly displaced Kodachrome.', 't3', {
  origin: { kind: 'agent' },
  mid: 'm-rest',
  footer: 'stats',
})

describe('a reply a restart cut while its chat keeper kept the turn running', () => {
  it('draws the saved rest in the cut reply bubble, after a reload', () => {
    const shown = drawn(groupDisplayItems([user, cut, notice, rest]).turns)
    const replies = shown.filter((m) => m.role === 'assistant')
    expect(replies).toHaveLength(1)
    expect(replies[0].content).toBe('For landscapes and nature, though, it quickly displaced Kodachrome.')
    expect(replies[0].ts).toBe('t1')
    expect(replies[0].meta?.mid).toBe('m-cut')
    expect(replies[0].meta?.footer).toBe('stats')
    expect(shown).toContain(notice)
  })

  it('grows the same bubble while the rest streams', () => {
    const streaming = { role: 'streaming', content: ' though,', cls: 'msg msg-a', meta: { clientTs: 'msg-live' } } as ChatMessage
    const shown = drawn(groupDisplayItems([user, cut, streaming]).turns)
    const bubbles = shown.filter((m) => m.role === 'assistant' || m.role === 'streaming')
    expect(bubbles).toHaveLength(1)
    expect(bubbles[0].role).toBe('streaming')
    expect(bubbles[0].content).toBe('For landscapes and nature, though,')
    expect(bubbles[0].ts).toBe('t1')
    expect(bubbles[0].meta?.clientTs).toBeUndefined()
  })

  it('hands back the same joined object for the same two rows', () => {
    const a = drawn(groupDisplayItems([user, cut, rest]).turns).find((m) => m.role === 'assistant')
    const b = drawn(groupDisplayItems([user, cut, rest]).turns).find((m) => m.role === 'assistant')
    expect(a?.content).toBe('For landscapes and nature, though, it quickly displaced Kodachrome.')
    expect(a).toBe(b)
  })
})

describe('replies that were not cut keep their own bubbles', () => {
  it('an agent-started turn after a reply that ended', () => {
    const ended = row('assistant', 'Done.', 't1')
    const shown = drawn(groupDisplayItems([user, ended, rest]).turns)
    expect(shown.filter((m) => m.role === 'assistant').map((m) => m.content)).toEqual([
      'Done.',
      ' though, it quickly displaced Kodachrome.',
    ])
  })

  it('a cut reply whose next row is the user’s next message', () => {
    const next = row('user', 'Go on.', 't2')
    const answer = row('assistant', 'Sure.', 't3')
    const shown = drawn(groupDisplayItems([user, cut, next, answer]).turns)
    expect(shown.filter((m) => m.role === 'assistant').map((m) => m.content)).toEqual([
      'For landscapes and nature,',
      'Sure.',
    ])
  })

  it('a cut reply whose turn went on with a tool call', () => {
    const tool = row('tool', 'ran', 't2')
    const shown = drawn(groupDisplayItems([user, cut, tool, rest]).turns)
    expect(shown.filter((m) => m.role === 'assistant')).toHaveLength(2)
  })
})

describe('the joined bubble keeps what the server decided for each part', () => {
  const BLOCK = { url: 'https://collect.example.com/?q=x', domain: 'collect.example.com', rule: 'exfil_query_length', query_chars: 262, withheld: null }
  const CRED = { id: 'r1', kind: 'aws_secret' }

  it("carries the cut part's blocked links and removed credentials", () => {
    const cutWith = row('assistant', 'See https://collect.example.com/?q=x and', 't1', {
      carries_on: true,
      link_blocks: [BLOCK],
      redactions: [CRED],
    })
    const restWith = row('assistant', ' more.', 't3', { origin: { kind: 'agent' }, redactions: [{ id: 'r2' }] })
    const reply = drawn(groupDisplayItems([user, cutWith, restWith]).turns).find((m) => m.role === 'assistant')
    expect(reply?.meta?.link_blocks).toEqual([BLOCK])
    expect(reply?.meta?.redactions).toEqual([CRED, { id: 'r2' }])
  })

  it("sits at the rest's index, so the latest reply keeps Regenerate and fork takes it whole", () => {
    const items = groupDisplayItems([user, cut, notice, rest]).turns.flatMap((it) =>
      it.kind === 'turn' ? it.items : [it],
    )
    const reply = items.find((t) => t.kind === 'single' && t.msg.role === 'assistant')
    expect(reply?.kind === 'single' && reply.idx).toBe(3)
  })
})
