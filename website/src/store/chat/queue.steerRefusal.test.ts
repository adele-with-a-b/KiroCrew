/**
 * A steer that fell back to the queue keeps its cause (`meta.steerRefusal`) on
 * the queued row through the store, on the live `queue_push` card and on cold
 * hydration of the queue, so the row's reason chip has it to show.
 */
import { describe, expect, it } from 'vitest'
import reducer, { appendQueuedMessage, hydrateSlotMessages } from '../chatSlice'

const slot = 'refusal-slot'
const ts = '2026-11-23T17:00:00Z'
const cold = () => reducer(undefined, { type: '@@INIT' })
const rows = (state: ReturnType<typeof cold>) => state.slotMessages[slot] ?? []

describe('queued steer refusal', () => {
  it('rides the queue_push card', () => {
    const state = reducer(cold(), appendQueuedMessage({ slot, content: 'rebase first', ts, queue_id: 'q1', meta: { steerRefusal: 'agent_turn' } }))
    expect(rows(state)[0].meta?.steerRefusal).toBe('agent_turn')
  })

  it('comes back from cold queue hydration', () => {
    const state = reducer(cold(), {
      type: hydrateSlotMessages.type,
      payload: { slot, messages: [], queue: [{ id: 'q2', content: 'rebase first', meta: { steerRefusal: 'permission_wait' } }], bounded: false },
    })
    expect(rows(state).find(m => m.role === 'queued')?.meta?.steerRefusal).toBe('permission_wait')
  })

  it('is absent on a row that was queued, not steered', () => {
    const state = reducer(cold(), appendQueuedMessage({ slot, content: 'next item', ts, queue_id: 'q3', meta: { steerRefusal: 7 } }))
    expect(rows(state)[0].meta?.steerRefusal).toBeUndefined()
  })
})
