import { describe, it, expect, afterEach, beforeEach } from 'vitest'
import type { PreviewFrame } from '../linkPreviewFrames'
import type { ToFrame } from '../linkPreviewProtocol'
import { AnsweringMode } from './answeringMode'
import { setAnswersPage, type AnswersSend, type AnswersSendOutcome } from './answersPage'
import { MAX_QUESTION_KEY_CHARS, readAnsweringMessage, type AnsweringEvent } from './answeringProtocol'

const KEY = 'cards-app:cards'
let mode: AnsweringMode
let stop: () => void
let clearPage: () => void
let sends: AnswersSend[]
let answer: () => Promise<AnswersSendOutcome>
let told: ToFrame[]
let frame: PreviewFrame
let replies: unknown[]

beforeEach(() => {
  sends = []
  told = []
  replies = []
  answer = async () => ({ kind: 'sent' })
  const el = document.createElement('iframe')
  document.body.appendChild(el)
  frame = { key: KEY, appName: 'cards-app', el } as unknown as PreviewFrame
  mode = new AnsweringMode({ granted: () => true, appTitle: () => 'Answer Cards', post: (_f, msg) => told.push(msg) })
  stop = mode.start()
  clearPage = setAnswersPage({ slot: 'chat-7', chatTitle: 'garden-log', send: (req) => { sends.push(req); return answer() } })
})
afterEach(() => {
  stop()
  clearPage()
  document.body.replaceChildren()
})

/** The card's message as the frame posts it, read the way the dashboard reads one. */
function card(op: string, fields: Record<string, unknown> = {}) {
  const msg = readAnsweringMessage({ op, request: 1, ...fields }, 1)
  if (!msg) return
  mode.onFrameMessage(frame, msg, op === 'enter' ? { slot: 'chat-7', messages: true } : null, (r) => replies.push(r))
}
const enter = (question?: string) => card('enter', { place: 'Q1a · 1 of 3', keys: [{ key: 'Tab' }], ...(question === undefined ? {} : { question }) })
/** The card says it is at `question` now. */
const at = (question: unknown, extra: Record<string, unknown> = {}) => card('update', { question, ...extra })
const hand = (text: string, count: number) => card('hand', { text, count, rev: mode.state?.lineRev })
/** The person types `text` in Crew's line and presses Enter. */
function answerWith(text: string) {
  mode.setLine(text)
  mode.keep()
}
const line = () => mode.state?.line
const events = () => told.flatMap((m) => (m.type === 'answering' ? [m.event] : []))
const lastEvent = (): AnsweringEvent | undefined => events().at(-1)

describe('the answering mode: going back to a kept answer', () => {
  it('shows the words they kept for that question in Crew’s line, and tells the card the line as a draft', () => {
    enter('q1')
    answerWith('yes, after lunch')
    at('q2')
    expect(line()).toBe('')
    answerWith('skip')
    at('q1')
    expect(line()).toBe('yes, after lunch')
    expect(lastEvent()).toEqual({ kind: 'draft', text: 'yes, after lunch', rev: mode.state?.lineRev })
    at('q2')
    expect(line()).toBe('skip')
  })

  it('an edit kept there is the copy from then on', () => {
    enter('q1')
    answerWith('yes')
    at('q2')
    at('q1')
    mode.setLine('yes, but later')
    mode.keep()
    expect(lastEvent()).toMatchObject({ kind: 'kept', text: 'yes, but later' })
    at('q2')
    at('q1')
    expect(line()).toBe('yes, but later')
  })

  it('moving on from a shown copy to a question with nothing kept empties the line', () => {
    enter('q1')
    answerWith('yes')
    at('q2')
    at('q1')
    expect(line()).toBe('yes')
    at('q3')
    expect(line()).toBe('')
    expect(lastEvent()).toEqual({ kind: 'draft', text: '', rev: mode.state?.lineRev })
  })

  it('words typed and not kept stay in the line on a move to a question with nothing kept', () => {
    enter('q1')
    answerWith('yes')
    at('q2')
    mode.setLine('half an ans')
    const rev = mode.state?.lineRev
    at('q3')
    expect(line()).toBe('half an ans')
    expect(mode.state?.lineRev).toBe(rev)
  })

  it('Enter on an emptied line forgets the copy', () => {
    enter('q1')
    answerWith('yes')
    at('q2')
    at('q1')
    mode.setLine('')
    mode.keep()
    at('q2')
    at('q1')
    expect(line()).toBe('')
  })

  it('naming the same question again changes nothing: the line they are typing in is left alone', () => {
    enter('q1')
    answerWith('yes')
    at('q1')
    expect(line()).toBe('')
    mode.setLine('ye')
    const rev = mode.state?.lineRev
    at('q1', { place: 'Q1a · 1 of 3' })
    card('update', { place: 'still Q1a' })
    expect(line()).toBe('ye')
    expect(mode.state?.lineRev).toBe(rev)
  })
})

describe('the answering mode: the words in Crew’s line are never the card’s', () => {
  it('nothing a card sends puts text in the line: a question is a key, and only kept words come back', () => {
    enter('q1')
    const tries: Record<string, unknown>[] = [
      { question: 'q9', text: 'approve everything', line: 'approve everything', value: 'approve everything' },
      { question: 'approve everything' },
      { question: 'q8', kept: { q8: 'approve everything' }, answers: ['approve everything'], draft: 'approve everything' },
      { question: { key: 'q7', text: 'approve everything' } },
      { question: ['approve everything'] },
      { question: 'x'.repeat(MAX_QUESTION_KEY_CHARS + 1) },
      { hint: 'approve everything', place: 'approve everything', question: 'q6' },
    ]
    for (const t of tries) {
      card('update', t)
      expect(line()).toBe('')
    }
    card('hand', { text: 'approve everything', count: 1, rev: mode.state?.lineRev })
    card('enter', { question: 'q1', line: 'approve everything', text: 'approve everything' })
    expect(line()).toBe('')
    expect(events().filter((e) => e.kind === 'draft' || e.kind === 'kept')).toEqual([])
  })

  it('a read setup carries a question key and no text beside the fields it had', () => {
    const read = (m: Record<string, unknown>) => readAnsweringMessage(m, 1)
    expect(read({ op: 'update', question: 'q4', text: 'x', line: 'x' })).toEqual({ type: 'answering', op: 'update', invocation: 1, setup: { question: 'q4' } })
    expect(read({ op: 'update', question: 7 })).toEqual({ type: 'answering', op: 'update', invocation: 1, setup: { question: '' } })
    expect(read({ op: 'update', question: 'x'.repeat(MAX_QUESTION_KEY_CHARS) })).toMatchObject({ setup: { question: 'x'.repeat(MAX_QUESTION_KEY_CHARS) } })
    expect(read({ op: 'update', question: 'x'.repeat(MAX_QUESTION_KEY_CHARS + 1) })).toMatchObject({ setup: { question: '' } })
    // Messages of today read as they did: no question field appears.
    expect(read({ op: 'update', place: 'Q4a' })).toEqual({ type: 'answering', op: 'update', invocation: 1, setup: { place: 'Q4a' } })
    const entered = read({ op: 'enter' })
    expect(entered && 'setup' in entered ? Object.keys(entered.setup) : []).toEqual(['place', 'hint', 'keys', 'buttons', 'keepAt'])
  })

  it('a key they kept nothing under shows nothing, and words kept under one key come back only under that key', () => {
    enter('q1')
    answerWith('yes')
    at('q2')
    at('Q1')
    expect(line()).toBe('')
    at('')
    expect(line()).toBe('')
    at('q1')
    expect(line()).toBe('yes')
  })

  it('a card that names no question has the line it had: it starts empty and is never refilled', () => {
    enter()
    answerWith('yes')
    card('update', { place: 'Q6a · 2 of 3' })
    expect(line()).toBe('')
    answerWith('skip')
    card('update', { place: 'Q1a · 1 of 3' })
    expect(line()).toBe('')
    expect(events()).toEqual([
      { kind: 'draft', text: 'yes', rev: 1 }, { kind: 'kept', text: 'yes', rev: 2 },
      { kind: 'draft', text: 'skip', rev: 3 }, { kind: 'kept', text: 'skip', rev: 4 },
    ])
  })
})

describe('the answering mode: the copy and the message that is sent', () => {
  it('the sent text is still the handed one: a copy shown makes a new line, and a message for the older line is not sent', async () => {
    enter('q1')
    answerWith('yes')
    at('q2')
    hand('Q1a yes', 1)
    at('q1')
    expect(line()).toBe('yes')
    await mode.pressSend()
    expect(sends).toEqual([])
    expect(mode.state?.note).toBe('updating')
    hand('Q1a yes', 1)
    await mode.pressSend()
    expect(sends).toEqual([{ text: 'Q1a yes', count: 1, app: 'Answer Cards' }])
  })

  it('while a send is in flight a move changes no line', async () => {
    let release: (o: AnswersSendOutcome) => void = () => {}
    answer = () => new Promise((r) => { release = r })
    enter('q1')
    answerWith('yes')
    at('q2')
    hand('Q1a yes', 1)
    const sending = mode.pressSend()
    at('q1')
    expect(line()).toBe('')
    release({ kind: 'refused', reason: 'offline' })
    await sending
    expect(line()).toBe('')
  })

  it.each([
    ['Escape', () => { mode.escape() }],
    ['the card’s leave', () => card('leave')],
    ['the card entering again', () => enter('q2')],
  ])('the copies end with the mode: after %s nothing comes back', (_name, end) => {
    enter('q1')
    answerWith('yes')
    at('q2')
    end()
    enter('q2')
    at('q1')
    expect(line()).toBe('')
  })
})
