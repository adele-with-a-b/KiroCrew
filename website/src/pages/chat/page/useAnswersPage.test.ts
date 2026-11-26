import { describe, it, expect, afterEach, vi } from 'vitest'
import { cleanup, renderHook } from '@testing-library/react'
import { answersPage } from '../../../linkPreview/answering/answersPage'
import { readAnswersFold, answersFoldLine } from '../../../linkPreview/answering/answersFold'
import { useAnswersPage, type AnswersPageInputs } from './useAnswersPage'

afterEach(() => cleanup())

function mount(over: Partial<AnswersPageInputs> = {}) {
  const send = vi.fn<AnswersPageInputs['send']>(async () => true)
  const inputs: AnswersPageInputs = { slot: 'chat-7', chatTitle: 'garden-log', canSend: () => true, steerFlag: () => 'auto', send, ...over }
  const hook = renderHook((p: AnswersPageInputs) => useAnswersPage(p), { initialProps: inputs })
  return { send: (over.send as typeof send | undefined) ?? send, hook, inputs }
}

describe('useAnswersPage', () => {
  it('sends the text as an isolated send of the chat on screen, so the draft is never read, sent or cleared', async () => {
    const { send } = mount()
    expect(answersPage()?.slot).toBe('chat-7')
    expect(await answersPage()?.send({ text: 'Q1a go\nQ6a skip', count: 2, app: 'Answer Cards' })).toEqual({ kind: 'sent' })
    expect(send).toHaveBeenCalledTimes(1)
    const [text, slot, steer, isolated, sendNow, , rowMeta] = send.mock.calls[0]
    expect({ text, slot, steer, isolated, sendNow, rowMeta }).toEqual({
      text: 'Q1a go\nQ6a skip', slot: 'chat-7', steer: 'auto', isolated: true, sendNow: false,
      rowMeta: { answers: { count: 2, app: 'Answer Cards' } },
    })
  })

  it('refuses where nothing can go out, and says when the answer was lost', async () => {
    const { send } = mount({ canSend: () => false })
    expect(await answersPage()?.send({ text: 'x', count: 1, app: 'A' })).toEqual({ kind: 'refused' })
    expect(send).not.toHaveBeenCalled()
    cleanup()
    mount({ send: vi.fn(async (...args: Parameters<AnswersPageInputs['send']>) => { args[5]?.('indeterminate'); return false }) })
    expect(await answersPage()?.send({ text: 'x', count: 1, app: 'A' })).toEqual({ kind: 'unconfirmed' })
  })

  it('a split view or no chat leaves no page; a rename keeps the same page', () => {
    const { hook, inputs } = mount()
    const first = answersPage()
    hook.rerender({ ...inputs, chatTitle: 'renamed' })
    expect(answersPage()).toBe(first)
    expect(answersPage()?.chatTitle).toBe('renamed')
    hook.rerender({ ...inputs, slot: null })
    expect(answersPage()).toBeNull()
  })
})

describe('the "N answers sent" fold', () => {
  it('reads only its one shape and writes Crew’s own line', () => {
    expect(readAnswersFold({ answers: { count: 2, app: 'Answer Cards' } })).toEqual({ count: 2, app: 'Answer Cards' })
    for (const bad of [{}, { answers: 'x' }, { answers: { count: 0, app: 'A' } }, { answers: { count: 2.5, app: 'A' } }, { answers: { count: 2, app: '' } }, null]) {
      expect(readAnswersFold(bad)).toBeNull()
    }
    expect(answersFoldLine({ count: 2, app: 'Answer Cards' })).toBe('✓ 2 answers sent · Answer Cards')
    expect(answersFoldLine({ count: 1, app: 'Answer Cards' })).toBe('✓ 1 answer sent · Answer Cards')
  })
})
