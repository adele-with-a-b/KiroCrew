import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import type { PreviewFrame } from '../linkPreviewFrames'
import type { ToFrame } from '../linkPreviewProtocol'
import { AnsweringMode } from './answeringMode'
import { setAnswersPage, type AnswersSend } from './answersPage'
import AnsweringBar from './AnsweringBar'

// Script-made events stand in for the person's own presses here, except where a
// test says the press is not theirs.
const trust = vi.hoisted(() => ({ all: true }))
vi.mock('./personPress', () => ({ isPersonPress: () => trust.all }))

const KEY = 'cards-app:cards'
let mode: AnsweringMode
let stop: () => void
let clearPage: () => void
let sends: AnswersSend[]
let told: ToFrame[]
let frame: PreviewFrame

const setup = { place: 'Q4a · 2 of 3', hint: '⇥ take the offer', keys: [{ key: 'Tab', label: 'go' }], buttons: [{ id: 'go', label: 'go', hint: '⇥' }, { id: 'later', label: 'later' }], keepAt: 1 }

function enter() {
  act(() => {
    mode.onFrameMessage(frame, { type: 'answering', op: 'enter', invocation: 1, setup }, { slot: 'chat-7', messages: true }, () => {})
  })
}
function hand(text: string, count: number) {
  act(() => {
    mode.onFrameMessage(frame, { type: 'answering', op: 'hand', invocation: 1, text, count, rev: mode.state?.lineRev ?? 0 }, null, () => {})
  })
}
const line = () => screen.getByTestId('answering-line') as HTMLInputElement
const press = async (el: Element, init: KeyboardEventInit) => { await act(async () => { fireEvent.keyDown(el, init) }) }

beforeEach(() => {
  trust.all = true
  sends = []
  told = []
  const el = document.createElement('iframe')
  el.getBoundingClientRect = () => ({ left: 100, top: 50, width: 600, height: 300, right: 700, bottom: 350, x: 100, y: 50, toJSON() {} })
  document.body.appendChild(el)
  frame = { key: KEY, appName: 'cards-app', el } as unknown as PreviewFrame
  mode = new AnsweringMode({ granted: () => true, appTitle: () => 'Answer Cards', post: (_f, msg) => told.push(msg) })
  stop = mode.start()
  clearPage = setAnswersPage({ slot: 'chat-7', chatTitle: 'garden-log', send: async (req) => { sends.push(req); return { kind: 'sent' } } })
  render(<AnsweringBar mode={mode} />)
})
afterEach(() => {
  cleanup()
  stop()
  clearPage()
  document.body.replaceChildren()
})

describe('AnsweringBar', () => {
  it('draws Crew’s line, the exact words with the count and the chat, and the buttons with keep, next', () => {
    enter()
    expect(document.activeElement).toBe(line())
    expect(screen.getByTestId('answering-nothing').textContent).toBe('Nothing kept yet')
    hand('Q1a yes, after lunch. Q6a skip.', 2)
    expect(screen.getByTestId('answering-preview').textContent).toContain('2 answers to garden-log')
    expect(screen.getByTestId('answering-preview-text').textContent).toBe('Q1a yes, after lunch. Q6a skip.')
    const row = screen.getAllByRole('button').filter((b) => b.closest('[data-testid="answering-bar"]') && b.dataset.testid !== 'answering-preview-text')
    expect(row.map((b) => b.textContent)).toEqual(['go⇥', 'keep, next↵', 'later'])
    expect(screen.getByTestId('answering-mode-line').textContent).toContain('Q4a · 2 of 3')
  })

  it('while Shift is held keep, next reads send prompt; Shift+click on it sends', async () => {
    enter()
    hand('Q1a go', 1)
    await press(document.body, { key: 'Shift', shiftKey: true })
    expect(screen.getByTestId('answering-keep').textContent).toContain('send prompt')
    await act(async () => { fireEvent.click(screen.getByTestId('answering-keep'), { shiftKey: true }) })
    expect(sends).toEqual([{ text: 'Q1a go', count: 1, app: 'Answer Cards' }])
  })

  it('Shift+Enter in the line sends exactly what the whole-message view shows, once', async () => {
    const text = 'Q1a yes, after lunch.\nQ6a skip — «ok» 🌱  two  spaces'
    enter()
    hand(text, 2)
    fireEvent.click(screen.getByTestId('answering-preview-text'))
    const shown = screen.getByTestId('answering-preview-full').textContent
    const el = line()
    await press(el, { key: 'Enter', shiftKey: true })
    await press(el, { key: 'Enter', shiftKey: true })
    await press(document.body, { key: 'Enter', shiftKey: true })
    expect(sends).toEqual([{ text, count: 2, app: 'Answer Cards' }])
    expect(sends[0].text).toBe(shown)
    expect(screen.queryByTestId('answering-bar')).toBeNull()
  })

  it('a held key repeating, or a press that is not the person’s, sends nothing', async () => {
    enter()
    hand('Q1a go', 1)
    await press(line(), { key: 'Enter', shiftKey: true, repeat: true })
    trust.all = false
    await press(line(), { key: 'Enter', shiftKey: true })
    await act(async () => { fireEvent.click(screen.getByTestId('answering-keep'), { shiftKey: true }) })
    await press(document.body, { key: 'Enter', shiftKey: true })
    expect(sends).toEqual([])
    expect(mode.state?.status).toBe('on')
  })

  it('with nothing kept, Shift+Enter sends nothing and the line says so', async () => {
    enter()
    await press(line(), { key: 'Enter', shiftKey: true })
    expect(sends).toEqual([])
    expect(screen.getByTestId('answering-nothing').className).toContain('text-warn')
  })

  it('Enter keeps the line’s words and empties it; a declared key goes to the card', async () => {
    enter()
    fireEvent.change(line(), { target: { value: 'yes, after lunch' } })
    await press(line(), { key: 'Enter' })
    await press(line(), { key: 'Tab', code: 'Tab' })
    expect(line().value).toBe('')
    expect(told.map((m) => (m.type === 'answering' ? m.event : m))).toEqual([
      { kind: 'draft', text: 'yes, after lunch', rev: 1 },
      { kind: 'kept', text: 'yes, after lunch', rev: 2 },
      { kind: 'key', key: 'Tab' },
    ])
    expect(sends).toEqual([])
  })

  it('Enter, a declared key, keep, next and a card button are each noted as the person’s press for the card, once', async () => {
    enter()
    expect(mode.takePress(frame)).toBe(false)
    const presses: (() => Promise<void> | void)[] = [
      () => press(line(), { key: 'Enter' }),
      () => press(line(), { key: 'Tab', code: 'Tab' }),
      () => press(document.body, { key: 'Tab', code: 'Tab' }),
      () => { fireEvent.click(screen.getByTestId('answering-keep')) },
      () => { fireEvent.click(screen.getAllByTestId('answering-button')[1]) },
    ]
    for (const p of presses) {
      await p()
      expect(mode.takePress(frame)).toBe(true)
      expect(mode.takePress(frame)).toBe(false)
    }
    // Typing, Shift, and opening the whole message are not presses for the card.
    fireEvent.change(line(), { target: { value: 'yes' } })
    await press(document.body, { key: 'Shift', shiftKey: true })
    expect(mode.takePress(frame)).toBe(false)
  })

  it('a key or click that is not the person’s is told to the card but noted as no press', async () => {
    enter()
    trust.all = false
    await press(line(), { key: 'Enter' })
    await press(line(), { key: 'Tab', code: 'Tab' })
    fireEvent.click(screen.getByTestId('answering-keep'))
    fireEvent.click(screen.getAllByTestId('answering-button')[1])
    expect(told.map((m) => (m.type === 'answering' ? m.event.kind : m.type))).toEqual(['kept', 'key'])
    expect(mode.takePress(frame)).toBe(false)
  })

  it('back at a question they answered, Crew’s line holds the words they kept there, to edit', async () => {
    const at = (question: string) => act(() => {
      mode.onFrameMessage(frame, { type: 'answering', op: 'update', invocation: 1, setup: { question } }, null, () => {})
    })
    enter()
    at('q1')
    fireEvent.change(line(), { target: { value: 'yes, after lunch' } })
    await press(line(), { key: 'Enter' })
    at('q2')
    expect(line().value).toBe('')
    at('q1')
    expect(line().value).toBe('yes, after lunch')
    expect(document.activeElement).toBe(line())
    fireEvent.change(line(), { target: { value: 'yes, after lunch, at two' } })
    await press(line(), { key: 'Enter' })
    expect(told.at(-1)).toEqual({ type: 'answering', event: { kind: 'kept', text: 'yes, after lunch, at two', rev: mode.state?.lineRev } })
  })

  it('Escape leaves the mode and sends nothing', async () => {
    enter()
    hand('Q1a go', 1)
    await press(line(), { key: 'Escape' })
    expect(screen.queryByTestId('answering-bar')).toBeNull()
    expect(sends).toEqual([])
    expect(told.at(-1)).toEqual({ type: 'answering', event: { kind: 'ended', why: 'escaped' } })
  })

  it('the message box keeps its own keys: Shift+Enter there neither sends nor is taken', async () => {
    const box = document.createElement('textarea')
    box.setAttribute('data-composer-input', '')
    box.value = 'remind me about the seeds'
    document.body.appendChild(box)
    enter()
    hand('Q1a go', 1)
    box.focus()
    const ev = new KeyboardEvent('keydown', { key: 'Enter', shiftKey: true, bubbles: true, cancelable: true })
    await act(async () => { box.dispatchEvent(ev) })
    expect(ev.defaultPrevented).toBe(false)
    expect(sends).toEqual([])
    expect(box.value).toBe('remind me about the seeds')
    // On the page itself (no text field) the mode holds Shift+Enter.
    box.blur()
    await press(document.body, { key: 'Enter', shiftKey: true })
    expect(sends).toEqual([{ text: 'Q1a go', count: 1, app: 'Answer Cards' }])
    expect(box.value).toBe('remind me about the seeds')
  })
})
