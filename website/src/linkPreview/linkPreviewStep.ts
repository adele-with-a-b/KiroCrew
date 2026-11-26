/**
 * A pinned link-preview card's `step` to another of its provider's labels in the
 * same chat (`linkPreviewProtocol.ts`), on the host's side: bring the label into
 * view, hand back where it is so the card can be shown there, and speak the
 * app's line.
 *
 * Nothing here scrolls, pins or announces on its own: the message is reached by
 * the card's own `jump` (`sendAppNav`, `from: 'view'`, so paging older history,
 * the highlight and the Back pill are the jump's), the label inside it is
 * centred the way find-in-chat centres its hit (`pollRowSettled` with
 * `scrollRangeToCenter`, nested under the jump's row scroll), the card is shown
 * again by `FrameHost`, and the line is said in the turn stepper's live region
 * (`sayStepLine`). A label already wholly in view is not scrolled to: the card
 * moves and the chat stays still.
 *
 * @module linkPreview/linkPreviewStep
 */
import { sayStepLine } from '../pages/chat/turnStep'
import { attachUserScrollIntent, pollRowSettled, scrollRangeToCenter } from '../utils/searchScroll'
import { sendAppNav } from './linkPreviewNav'
import type { AnchorRect, MessageRef, PreviewLink, StepRefusal } from './linkPreviewProtocol'
import { resolveTextPreview } from './textPatterns'

/** The session key prefix a chat message's preview scope carries (`LinkPreviewLayer`). */
const SESSION_PREFIX = 'dashboard:'

/** One step: provider `key` asks for its label `text` in message `target` of the chat in `slot`. */
export interface StepAsk {
  key: string
  slot: string
  target: MessageRef
  text: string
  line?: string
}

export interface StepHooks {
  /** The label is on screen at `rect`: the first call shows the card there with
   *  `link`; later ones, while the scroll settles, move it with the label. */
  land(rect: AnchorRect, link: PreviewLink): void
  /** How the step ended, once: `true` after the first `land`, else why not. */
  done(result: true | StepRefusal): void
}

/**
 * The label span `ask` names, or null while it is not mounted: a
 * `[data-link-preview-text]` whose whole text is `ask.text` and whose owner is
 * the asking provider, in the preview scope of that chat and message.
 *
 * shortcut: a label written twice in one message resolves to its first copy; a
 * step names a label by its text, and no provider has needed the second one.
 */
export function findStepLabel(ask: StepAsk): HTMLElement | null {
  const session = SESSION_PREFIX + ask.slot
  for (const scope of document.querySelectorAll<HTMLElement>('[data-link-preview-scope]')) {
    if (scope.getAttribute('data-link-preview-session') !== session) continue
    if (scope.getAttribute('data-link-preview-ts') !== ask.target.ts) continue
    const mid = scope.getAttribute('data-link-preview-mid')
    if (ask.target.id && mid && mid !== ask.target.id) continue
    for (const span of scope.querySelectorAll<HTMLElement>('[data-link-preview-text]')) {
      if (span.closest('[data-link-preview-scope]') !== scope) continue
      const text = span.textContent ?? ''
      if (text !== ask.text || resolveTextPreview(text)?.owner !== ask.key) continue
      if (span.getClientRects().length === 0) continue
      return span
    }
  }
  return null
}

const rectOf = (el: HTMLElement): AnchorRect => {
  const r = el.getBoundingClientRect()
  return { left: r.left, top: r.top, width: r.width, height: r.height }
}

/** Whether `el` is wholly inside the box of every scroller it is in (the same
 *  scrollers `scrollRangeToCenter` moves) and the window. */
function wholeInView(el: HTMLElement): boolean {
  const r = el.getBoundingClientRect()
  if (r.top < 0 || r.bottom > window.innerHeight) return false
  for (let p = el.parentElement; p; p = p.parentElement) {
    if (p.scrollHeight <= p.clientHeight) continue
    const overflowY = getComputedStyle(p).overflowY
    if (overflowY !== 'auto' && overflowY !== 'scroll') continue
    const box = p.getBoundingClientRect()
    if (r.top < box.top || r.bottom > box.bottom) return false
  }
  return true
}

/**
 * Run one step; returns its cancel (a newer step, or the card going), after
 * which nothing more is called.
 */
export function runStep(ask: StepAsk, hooks: StepHooks): () => void {
  let over = false
  let landed = false
  let stopPoll: () => void = () => {}
  let detachUser: () => void = () => {}
  const end = (result: true | StepRefusal) => {
    if (over) return
    over = true
    stopPoll()
    detachUser()
    hooks.done(result)
  }
  const link: PreviewLink = { text: ask.text, sessionKey: SESSION_PREFIX + ask.slot, message: ask.target }

  const converge = () => {
    let span: HTMLElement | null = null
    stopPoll = pollRowSettled({
      measure: () => {
        span = findStepLabel(ask)
        return span ? span.getBoundingClientRect().top : null
      },
      step: () => {
        if (!span) return
        const range = document.createRange()
        range.selectNodeContents(span)
        scrollRangeToCenter(range)
        hooks.land(rectOf(span), link)
        if (!landed) {
          landed = true
          if (ask.line) sayStepLine(ask.line)
          hooks.done(true)
        }
      },
      onEnd: () => {
        detachUser()
        if (!landed) end('not_found')
        over = true
      },
    })
    // The person's own scroll ends it: never fight their wheel or keys.
    detachUser = attachUserScrollIntent(typeof window !== 'undefined' ? window : undefined, () => {
      if (landed) { over = true; stopPoll(); detachUser() } else end('replaced')
    })
  }

  // A label already in view: the card moves to it and nothing scrolls (no
  // highlight, no Back spot), so stepping through the labels on screen keeps
  // the chat still.
  const seen = findStepLabel(ask)
  if (seen && wholeInView(seen)) {
    landed = true
    hooks.land(rectOf(seen), link)
    if (ask.line) sayStepLine(ask.line)
    end(true)
  } else {
    const r = sendAppNav({
      kind: 'jump',
      slot: ask.slot,
      target: ask.target,
      from: 'view',
      onDone: (o) => {
        if (over) return
        if (o === true) converge()
        else end(o)
      },
    })
    if (r !== true && !over) end('not_on_screen')
  }
  return () => {
    if (over) return
    over = true
    stopPoll()
    detachUser()
  }
}
