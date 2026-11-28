/**
 * The chat page's side of a card's answering mode (`linkPreview/answering/`):
 * which chat is on screen, and the mode's one send, through this composer's own
 * `send` like a reply card's Start (`composeSend`), so the message is the
 * person's row with its receipts and the composer's busy decision (steer, or
 * queue behind the turn).
 *
 * The send is isolated: the person's draft, staged files, quote and pastes are
 * never read, sent or cleared. Its row carries the mode's `answers` fold
 * (`count` and the app's name), which the gateway keeps only on a person's own
 * send (`dashboard/chat_api/answers_meta.py`). Where nothing can go out (offline,
 * a held crew composer, a force stop, a split view, another chat) it is refused
 * and nothing is put in the draft.
 *
 * @module pages/chat/page/useAnswersPage
 */
import { useEffect, useRef } from 'react'
import { setAnswersPage, type AnswersSendOutcome } from '../../../linkPreview/answering/answersPage'

type Send = (optionText?: string, targetSlot?: string, steerNow?: boolean | 'auto', isolated?: boolean, sendNow?: boolean, onUndelivered?: (kind: 'refused' | 'indeterminate', reason?: string) => void, rowMeta?: Record<string, unknown>) => Promise<boolean>

export interface AnswersPageInputs {
  /** The chat this composer serves, when it is the one on screen; null otherwise (a split view). */
  slot: string | null
  chatTitle: string
  /** Whether a send can go out now (connected, not held, not stopping), for this chat. */
  canSend(slot: string): boolean
  /** The composer's own busy decision for this chat (`busySteerFlag`). */
  steerFlag(slot: string): boolean | 'auto' | undefined
  send: Send
}

export function useAnswersPage(inputs: AnswersPageInputs): void {
  const ref = useRef(inputs)
  ref.current = inputs
  const { slot } = inputs
  useEffect(() => {
    if (!slot) return undefined
    return setAnswersPage({
      slot,
      // Read when the mode starts, so a rename does not end it.
      get chatTitle() { return ref.current.chatTitle },
      send: async (req) => {
        const cur = ref.current
        if (cur.slot !== slot || !cur.canSend(slot)) return { kind: 'refused' }
        const undelivered: { outcome: AnswersSendOutcome | null } = { outcome: null }
        const ok = await cur.send(
          req.text, slot, cur.steerFlag(slot), true, false,
          (kind, reason) => { undelivered.outcome = kind === 'refused' ? { kind: 'refused', reason } : { kind: 'unconfirmed' } },
          { answers: { count: req.count, app: req.app } },
        )
        return ok ? { kind: 'sent' } : undelivered.outcome ?? { kind: 'refused' }
      },
    })
  }, [slot])
}
