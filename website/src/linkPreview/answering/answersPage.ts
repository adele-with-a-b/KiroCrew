/**
 * The chat page's side of the answering mode: which chat is on screen, its name,
 * and the one way the mode's text is sent, through the page's own composer send
 * (`useAnswersPage`), so the message is the person's row with its receipts,
 * steer and queue handling like anything they type.
 *
 * Set by the chat page, read by the mode (`answeringMode.ts`). A frame reaches
 * neither: this is a module of the dashboard's own page.
 *
 * @module linkPreview/answering/answersPage
 */

/** How one send went: it went out (`sent`), nothing went out (`refused`, with
 *  the reason when the server gave one), or it may have gone out (`unconfirmed`:
 *  the answer was lost, so sending again could send it twice). */
export type AnswersSendOutcome = { kind: 'sent' } | { kind: 'refused'; reason?: string } | { kind: 'unconfirmed' }

export interface AnswersSend {
  /** The text, byte for byte as the mode showed it. */
  text: string
  /** How many answers it holds, and the app's name, for the row's fold. */
  count: number
  app: string
}

export interface AnswersPage {
  /** The chat on screen (its local slot key). */
  slot: string
  /** Its name, as the chat list shows it. */
  chatTitle: string
  /** Send `req` to `slot` as the person's message. */
  send(req: AnswersSend): Promise<AnswersSendOutcome>
}

let current: AnswersPage | null = null
const listeners = new Set<() => void>()

/** The chat page on screen, or null when none is (another page, a split view). */
export function answersPage(): AnswersPage | null {
  return current
}

/** Set the page on screen; returns the clear for when it goes. Listeners hear each change. */
export function setAnswersPage(page: AnswersPage | null): () => void {
  current = page
  for (const l of [...listeners]) l()
  return () => {
    if (current !== page) return
    current = null
    for (const l of [...listeners]) l()
  }
}

export function onAnswersPageChange(listener: () => void): () => void {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}
