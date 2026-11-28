/**
 * The answering mode: one pinned card at a time puts its chat into a mode where
 * Crew draws the answer line, the preview of the message, and the mode line, and
 * holds the keys the mode needs (`AnsweringBar.tsx`).
 *
 * Who does what:
 * - The card (an app frame) may `enter` the mode on the person's click or key in
 *   it, `update` what it shows, `hand` over the message its kept answers make and
 *   how many there are, and `leave`. It is told what the person did in Crew's
 *   line (`draft`, `kept`, a declared `key`, a `button`) and why the mode `ended`.
 * - Crew alone sends. `pressSend` is called only from the person's own press on
 *   Crew's page: Shift+Enter in Crew's line or on the page, or a Shift+click on
 *   Crew's button. It sends the text last handed over, exactly as the preview
 *   shows it, through the chat page's composer send (`answersPage.ts`), once.
 *   No frame message reaches `pressSend`: `onFrameMessage` never calls it.
 *
 * The relayed press: Enter, a declared key or a button the person presses on
 * Crew's page reaches the card as `kept`, `key` or `button`, and the card
 * answers by moving to another label (`card.step`, `card.jumpTo`). Focus is in
 * Crew's line then, never in the card, so the mode notes that it relayed a real
 * press (`isPersonPress`) and `takePress` lets that same card's next step or
 * jump use it up: one press, at most one move, within `RELAYED_PRESS_MS`.
 *
 * The mode ends, with nothing sent, on Escape, on the card's `leave`, when the
 * chat on screen changes, and when the card closes, unpins or goes. While a send
 * is in flight nothing changes the text and the mode ends once it is answered.
 *
 * @module linkPreview/answering/answeringMode
 */
import { readOfferedText } from '../../composerActions/literalInsert'
import type { PreviewFrame, ShowScope } from '../linkPreviewFrames'
import type { StepRefusal, ToFrame } from '../linkPreviewProtocol'
import { answersPage, onAnswersPageChange } from './answersPage'
import {
  MAX_ANSWERS,
  type AnsweringEvent,
  type AnsweringMessage,
  type AnsweringRefusal,
  type AnsweringSetup,
  type EndReason,
} from './answeringProtocol'
import { isPersonPress } from './personPress'

/** How long a press Crew relayed to a card lets that card move once (`takePress`).
 *  A card answers the event from what it already shows: a card's message round
 *  trip was measured at 7-34 ms (`CLICK_GRANT_MS` in `linkPreviewFrames.ts`), so
 *  this leaves room for a busy page, and stays far inside the browser's own
 *  few seconds of user activation that a click in the card gives.
 *  shortcut: a fixed bound; raise it if a card must read its server before it moves. */
export const RELAYED_PRESS_MS = 1_000

/** What the line under the preview says, besides the preview itself. */
export type ModeNote = 'nothing' | 'updating' | 'refused' | null

export interface Handed {
  /** Cleaned once on arrival (`readOfferedText`); shown and sent as it is. */
  text: string
  count: number
  rev: number
}

export interface ModeState {
  /** One per `enter`, so a late answer of an earlier mode is told apart. */
  token: number
  frame: PreviewFrame
  key: string
  appTitle: string
  slot: string
  chatTitle: string
  setup: AnsweringSetup
  line: string
  /** Numbers the line's text; a handed message counts only for the current one. */
  lineRev: number
  handed: Handed | null
  status: 'on' | 'sending'
  note: ModeNote
  /** The server's reason for a refused send, when it gave one. */
  refusal?: string
  /** Shift is held: Crew's keep button reads "send prompt". */
  shift: boolean
  /** The whole message is open under the preview line. */
  full: boolean
}

export interface ModeDeps {
  /** The owner approved this provider for `answering`. */
  granted(key: string): boolean
  /** The app's name as Crew lists it. */
  appTitle(key: string, appName: string): string
  post(frame: PreviewFrame, msg: ToFrame): void
  /** Say a line in the chat's live region. */
  say?(what: 'entered' | EndReason, count: number): void
  /** The page's clock in ms (`performance.now()`), for the relayed press. */
  now?(): number
}

let tokens = 0

export class AnsweringMode {
  private s: ModeState | null = null
  private readonly listeners = new Set<() => void>()
  /** An end asked for while a send was in flight, applied once it is answered. */
  private pendingEnd: EndReason | null = null
  /** When Crew last relayed the person's own press to the card of the mode that
   *  is on, until a step or jump uses it up (`takePress`); null with none. */
  private press: number | null = null
  /** The words the person kept in Crew's line, by the question key the card had
   *  named (`setup.question`) when they kept them. Written only from the line, in
   *  `keep`: a card names a key, never the words. Gone with the mode. */
  private readonly keptLines = new Map<string, string>()
  /** The words the person typed in Crew's line and had not kept when the card
   *  left a question, by that question's key ('' while the card names none).
   *  Written only from the line, in `showLine`. Gone with the mode. */
  private readonly drafts = new Map<string, string>()
  /** The card has named a question in this mode (`showLine`'s first naming). */
  private named = false

  constructor(private readonly deps: ModeDeps) {}

  /** Watch the chat on screen while the layer is mounted; the returned stop
   *  ends any mode on. */
  start(): () => void {
    // The chat on screen changed (or the chat page went): the mode was for that chat.
    const unwatch = onAnswersPageChange(() => {
      const s = this.s
      if (!s) return
      const page = answersPage()
      if (!page || page.slot !== s.slot) this.end('chat')
      else if (page.chatTitle !== s.chatTitle) this.patch({ chatTitle: page.chatTitle })
    })
    return () => {
      unwatch()
      this.end('closed')
    }
  }

  get state(): ModeState | null {
    return this.s
  }

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }

  getSnapshot = (): ModeState | null => this.s

  private emit(): void {
    for (const l of [...this.listeners]) l()
  }

  private patch(p: Partial<ModeState>): void {
    if (!this.s) return
    this.s = { ...this.s, ...p }
    this.emit()
  }

  private tell(s: ModeState, event: AnsweringEvent): void {
    if (s.frame.el.isConnected) this.deps.post(s.frame, { type: 'answering', event })
  }

  private now(): number {
    return this.deps.now ? this.deps.now() : performance.now()
  }

  /** Tell the card what the person pressed on Crew's page. `by` is that press's
   *  own event: a real one (`isPersonPress`) is noted for `takePress`. */
  private relay(s: ModeState, event: AnsweringEvent, by: Event | undefined): void {
    this.tell(s, event)
    if (by && isPersonPress(by)) this.press = this.now()
  }

  /**
   * Whether `frame`'s step or jump may count as the person's press: its own mode
   * is on and Crew relayed a real press to it no more than `RELAYED_PRESS_MS`
   * ago. Asking uses the press up, so one press moves the card at most once.
   * Only `relay` notes a press: no message from a frame does, and a frame whose
   * mode is not on is answered false with nothing changed.
   */
  takePress(frame: PreviewFrame): boolean {
    if (this.s?.frame !== frame) return false
    const at = this.press
    this.press = null
    if (at === null) return false
    const age = this.now() - at
    return age >= 0 && age <= RELAYED_PRESS_MS
  }

  /** A card's message. Starts, changes, fills or ends the mode; never sends. */
  onFrameMessage(frame: PreviewFrame, msg: AnsweringMessage, gate: (ShowScope & { slot: string }) | StepRefusal | null, reply: (r: true | AnsweringRefusal) => void): void {
    const s = this.s
    switch (msg.op) {
      case 'enter': {
        if (gate === null) { reply('not_shown'); return }
        if (typeof gate === 'string') { reply(gate === 'not_found' || gate === 'load_failed' || gate === 'replaced' ? 'not_shown' : gate); return }
        if (!this.deps.granted(frame.key)) { reply('not_granted'); return }
        // One mode at a time: another card's mode, or this card's own send in flight.
        if (s && (s.frame !== frame || s.status === 'sending')) { reply('busy'); return }
        const page = answersPage()
        if (!page || page.slot !== gate.slot) { reply('not_on_screen'); return }
        tokens += 1
        this.s = {
          token: tokens, frame, key: frame.key, appTitle: this.deps.appTitle(frame.key, frame.appName),
          slot: gate.slot, chatTitle: page.chatTitle, setup: msg.setup,
          line: '', lineRev: s ? s.lineRev + 1 : 0, handed: null, status: 'on', note: null, shift: false, full: false,
        }
        this.pendingEnd = null
        this.forget()
        this.named = Boolean(msg.setup.question)
        reply(true)
        this.emit()
        if (!s) this.deps.say?.('entered', 0)
        return
      }
      case 'update': {
        if (!s || s.frame !== frame) { reply('not_on'); return }
        this.patch({ setup: { ...s.setup, ...msg.setup } })
        reply(true)
        this.showLine(s.setup.question ?? '')
        return
      }
      case 'hand': {
        if (!s || s.frame !== frame) { reply('not_on'); return }
        // The text a press is sending stays what it was until the send is answered.
        if (s.status === 'sending') { reply('busy'); return }
        const count = msg.count
        if (typeof count !== 'number' || !Number.isInteger(count) || count < 0 || count > MAX_ANSWERS) { reply('bad_count'); return }
        if (msg.rev !== s.lineRev) { reply('stale'); return }
        if (count === 0) {
          this.patch({ handed: { text: '', count: 0, rev: msg.rev }, note: null, full: false })
          reply(true)
          return
        }
        const read = readOfferedText(msg.text)
        if (!read.ok) { reply(read.code === 'too_large' ? 'too_large' : 'not_literal'); return }
        this.patch({ handed: { text: read.text, count, rev: msg.rev }, note: null })
        reply(true)
        return
      }
      case 'leave':
        if (s && s.frame === frame) this.end('left')
        return
    }
  }

  /** The card that started the mode stopped standing where it was. */
  cardGone(frame: PreviewFrame): void {
    if (this.s?.frame === frame) this.end('closed')
  }

  // -- Crew's own line, buttons and keys. Called only from the person's events
  //    on Crew's page (`AnsweringBar.tsx`).

  /** The person changed the text in Crew's answer line (or `showLine` put their
   *  own words for a question back in it): a new line number, told to the card
   *  as a draft. */
  setLine(text: string): void {
    const s = this.s
    if (!s || s.status === 'sending') return
    const lineRev = s.lineRev + 1
    this.patch({ line: text, lineRev, note: null })
    this.tell(s, { kind: 'draft', text, rev: lineRev })
  }

  /** Plain Enter, or a plain click on "keep, next": the line's text is kept.
   *  `by`, here and in `key` and `button`, is the press's event (`relay`). */
  keep(by?: Event): void {
    const s = this.s
    if (!s || s.status === 'sending') return
    // Crew's own copy of what they kept for the question the card is at; an
    // emptied line kept forgets it. Kept words are no draft any more.
    const question = s.setup.question ?? ''
    if (question && s.line) this.keptLines.set(question, s.line)
    else this.keptLines.delete(question)
    this.drafts.delete(question)
    const lineRev = s.lineRev + 1
    this.patch({ line: '', lineRev, note: null })
    this.relay(s, { kind: 'kept', text: s.line, rev: lineRev }, by)
  }

  /**
   * The card named another question than `before` (`update`): Crew's line goes
   * with the question. What the line holds is first put by as the draft of the
   * question they are leaving (none when the line is empty or still the words
   * kept there, so an emptied draft falls back to the kept words). The line then
   * shows, for the new question, the draft they left there, else the words they
   * kept there, else nothing. Both are Crew's copies of what was typed in this
   * line; the card supplies the key and nothing else. A changed line is a new
   * line number, so a message handed for the older line is not sent until the
   * card hands again.
   * The first question a card names in a mode takes the line as it stands:
   * what they typed before any question had a name stays in sight, as that
   * question's line.
   */
  private showLine(before: string): void {
    const s = this.s
    if (!s) return
    const question = s.setup.question ?? ''
    const first = !this.named
    if (question) this.named = true
    if (first || question === before || s.status === 'sending') return
    if (s.line && s.line !== this.keptLines.get(before)) this.drafts.set(before, s.line)
    else this.drafts.delete(before)
    const text = this.drafts.get(question) ?? this.keptLines.get(question) ?? ''
    if (text !== s.line) this.setLine(text)
  }

  /** What a mode holds besides its state; none of it outlives the mode. */
  private forget(): void {
    this.press = null
    this.keptLines.clear()
    this.drafts.clear()
    this.named = false
  }

  /** A key the card declared, pressed in Crew's line or on the page. */
  key(name: string, by?: Event): boolean {
    const s = this.s
    if (!s || s.status === 'sending' || !s.setup.keys.some(k => k.key === name)) return false
    this.relay(s, { kind: 'key', key: name }, by)
    return true
  }

  /** A click on one of the card's buttons Crew draws. */
  button(id: string, by?: Event): void {
    const s = this.s
    if (!s || s.status === 'sending' || !s.setup.buttons.some(b => b.id === id)) return
    this.relay(s, { kind: 'button', id }, by)
  }

  setShift(held: boolean): void {
    if (this.s && this.s.shift !== held) this.patch({ shift: held })
  }

  toggleFull(): void {
    if (this.s) this.patch({ full: !this.s.full })
  }

  /** Escape: leaves the mode, nothing sent. True when the mode took the key. */
  escape(): boolean {
    const s = this.s
    if (!s) return false
    if (s.status !== 'sending') this.end('escaped')
    return true
  }

  /**
   * The person's Shift+Enter (or Shift+click on Crew's button): send the text
   * handed over, as it is shown, once, and end the mode. Nothing is sent with no
   * answer kept, while the card has not yet handed a message for the line as it
   * stands, or while a send is already in flight.
   */
  async pressSend(): Promise<void> {
    const s = this.s
    if (!s || s.status === 'sending') return
    const h = s.handed
    if (!h || h.count < 1 || !h.text) { this.patch({ note: 'nothing' }); return }
    if (h.rev !== s.lineRev) { this.patch({ note: 'updating' }); return }
    const page = answersPage()
    if (!page || page.slot !== s.slot) { this.end('chat'); return }
    this.pendingEnd = null
    this.patch({ status: 'sending', note: null, refusal: undefined })
    let outcome
    try {
      outcome = await page.send({ text: h.text, count: h.count, app: s.appTitle })
    } catch {
      outcome = { kind: 'unconfirmed' as const }
    }
    if (this.s?.token !== s.token) return
    if (outcome.kind === 'refused') {
      const pending = this.pendingEnd
      this.pendingEnd = null
      this.patch({ status: 'on', note: 'refused', refusal: outcome.reason })
      if (pending) this.end(pending)
      return
    }
    // Sent, or perhaps sent: either way never again from this press.
    this.patch({ status: 'on' })
    this.end('sent', h.count)
  }

  private end(why: EndReason, count = 0): void {
    const s = this.s
    if (!s) return
    if (s.status === 'sending') { this.pendingEnd = this.pendingEnd ?? why; return }
    this.s = null
    this.pendingEnd = null
    this.forget()
    this.tell(s, { kind: 'ended', why })
    this.emit()
    this.deps.say?.(why, count)
  }
}
