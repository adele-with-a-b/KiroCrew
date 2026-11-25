import { useCallback, useEffect, useMemo, useRef, useState, type Dispatch, type MutableRefObject, type SetStateAction } from 'react'

import { deriveFollowUpOptions } from '../../../app-sdk/protocol'
import type { FollowUpDerivation } from '../../../app-sdk/protocol/options'
import type { ComposerDraftStore } from '../../../chat-core/composer/draftStore'
import type { AppReplyChipsOffer } from '../../../hooks/useSessionControls'
import type { pendingQuestionFor } from '../../../store/chatSlice'
import { appendFollowUpOption, removeFollowUpOption, selectSingleFollowUpOption, type OwnedSuffix } from '../../../lib/followUpToggle'
import type { ChatMessage } from '../../../types'
import { isReasoningRole } from '../groupDisplayItems'
import { isInterceptedSlashCommand } from '../ChatInput'

/** A row that shows a running turn moved past the chips' offer: tool work or reasoning (#15926). */
export const isTurnActivityRow = (m: ChatMessage): boolean => m.role === 'tool' || isReasoningRole(m)

/**
 * The chips for this chat: an app's strings when one supplies any, else the
 * newest reply's `[OPTIONS:]` line exactly as `deriveFollowUpOptions` reads it.
 *
 * An app's strings replace the reply's line and do not read the transcript at
 * all: the app tracks the question, so a later wake row, note, queued or user
 * row does not close it; only the app's next answer does. They are steady
 * through a running turn (streaming text, tool steps): the chats an app serves
 * this way run turns most of the time, and a bar that came and went at each
 * step would drop the picks each time. They hide under a question card, like
 * the reply's own chips, and once one of them was sent (`suppressed`), until
 * the app answers again. This is no backward scan over old turns (the
 * newest-reply-only rule for `[OPTIONS:]`): it is the app's answer about the
 * chat now.
 */
export function deriveChips(
  messages: ChatMessage[],
  isStreaming: boolean,
  questionPending: boolean,
  turnRunning: boolean,
  app: AppReplyChipsOffer | null,
  suppressed = false,
): FollowUpDerivation {
  if (app) {
    if (questionPending || suppressed) return deriveFollowUpOptions([], true)
    return { followUpOptions: app.options, followUpSourceKey: `app:${app.key}`, followUpMulti: app.multi }
  }
  return deriveFollowUpOptions(messages, isStreaming, questionPending, turnRunning ? isTurnActivityRow : undefined)
}

/**
 * Whether a send carries a chip from the app list on offer: its text is one of
 * the strings (a double-click or quick send), or it sends the box (`box`, its
 * text) with one of the strings standing whole in it, however it got there (a
 * pick, Tab on the hint, a draft kept across a switch). A send of other text
 * (`Not now` against a `No` chip), an emptied box and a slash command are not.
 */
export function isAppChipSend(app: AppReplyChipsOffer, text: string | undefined, box: string): boolean {
  if (text) return app.options.includes(text.trim())
  if (!box || isInterceptedSlashCommand(box)) return false
  return app.options.some(o => holdsWhole(box, o))
}

const WORD_CHAR = /[\p{L}\p{N}_]/u
/** `s` appears in `box` with no letter or digit run on either side of it. */
function holdsWhole(box: string, s: string): boolean {
  for (let i = box.indexOf(s); i !== -1; i = box.indexOf(s, i + 1)) {
    const before = i > 0 ? box[i - 1] : ''
    const after = box[i + s.length] ?? ''
    if (!(before && WORD_CHAR.test(before)) && !(after && WORD_CHAR.test(after))) return true
  }
  return false
}

/** Settles a noted send: `false` (it never went out) takes the note back. */
export type FollowUpSendSettle = (sent: boolean) => void
const SETTLE_NOTHING: FollowUpSendSettle = () => {}

/** Settles a steer's note: with the send it handed off to, or (a real steer,
 *  which reports nothing) with whether there was a connection to steer over. */
export function settleSteer(result: Promise<boolean> | void, settle: FollowUpSendSettle, connected: boolean): void {
  if (result) void result.then(settle, () => settle(false))
  else settle(connected)
}

interface FollowUpChipsOptions {
  messages: ChatMessage[]
  isStreaming: boolean
  /** An app's reply-chip strings for this chat (`pickAppReplyChips`), or null. */
  appReplyChips?: AppReplyChipsOffer | null
  /** The slot's turn is running: a reply's chips hide once a tool or thinking row follows them. */
  turnRunning: boolean
  /** A pending question card suppresses the chips: only the card can answer it. */
  pendingQuestion: ReturnType<typeof pendingQuestionFor>
  activeSlot: string | null
  inputRef: MutableRefObject<string>
  setInput: ComposerDraftStore['set']
  prefillEdited: boolean
  setPrefillEdited: Dispatch<SetStateAction<boolean>>
}

/**
 * The follow-up option chips under the newest reply: which options apply,
 * which chips the user picked, and ownership of the text they appended
 * (#7616) -- which any direct edit of
 * the composer invalidates, so the composer's change handlers live here too.
 */
export function useFollowUpChips({
  messages,
  isStreaming,
  appReplyChips,
  turnRunning,
  pendingQuestion,
  activeSlot,
  inputRef,
  setInput,
  prefillEdited,
  setPrefillEdited,
}: FollowUpChipsOptions) {
  // Follow-up options derived from the last assistant message in the current chat,
  // or an app's strings for it (`deriveChips`).
  // Swapping chats (activeSlot change) → messages change → memo recomputes fresh.
  // A pending question card suppresses them: both would offer the same choices in
  // the same band, and only the card can answer the blocked tool call.
  // A chip from an app list was sent: in which chat, from which control, when.
  // That list stays off in that chat until its app answers a request made after
  // the send (`askedAt`: the turn-end re-read, `app_status_changed`, a switch
  // back in), so one answer cannot be sent twice while the turn it started runs,
  // and an answer already in flight at the send does not bring it back.
  const [sentAgainst, setSentAgainst] = useState<{ slot: string | null; key: string; sentAt: number } | null>(null)
  const app = appReplyChips ?? null
  const suppressed = !!app && !!sentAgainst && sentAgainst.slot === activeSlot && sentAgainst.key === app.key
    && app.askedAt <= sentAgainst.sentAt
  const { followUpOptions, followUpSourceKey, followUpMulti } = useMemo(
    () => deriveChips(messages, isStreaming, !!pendingQuestion, turnRunning, app, suppressed),
    [messages, isStreaming, pendingQuestion, turnRunning, app, suppressed],
  )
  // Visual-only highlight state; text in the input is the source of truth for
  // what gets sent. Cleared whenever the options list changes (new assistant
  // message) or the active chat switches — both signal a fresh turn.
  const [followUpPicked, setFollowUpPicked] = useState<Set<string>>(() => new Set())
  // Read by the option handler instead of the state: two clicks landing before a
  // re-render would both see the same set and both take the append branch.
  const followUpPickedRef = useRef(followUpPicked); followUpPickedRef.current = followUpPicked
  // Ownership of the appended suffix, not content-matching (#7616). See
  // lib/followUpToggle (shared with ChatPane): the chips own a recorded
  // (base, options) span, options kept as an ARRAY so a comma-bearing label is
  // one element. Advanced SYNCHRONOUSLY in the click handler, never in a
  // render-time state updater, so StrictMode's double-invocation cannot rebase
  // it on stale state (the #7616 F2 defect).
  const followUpInsertedRef = useRef<OwnedSuffix | null>(null)
  // Any DIRECT user edit of the composer invalidates chip ownership (#7616) —
  // the recorded span describes a chip-produced draft, so once the user types
  // it no longer maps to the live text (even an edit-then-restore). Chip
  // append/remove set the ref themselves and call setInput directly, bypassing
  // this handler, so they are unaffected.
  const clearFollowUpOwnership = useCallback(() => { followUpInsertedRef.current = null }, [])
  // Stable identities for the composer's change handlers. An inline arrow is a
  // new `onChange` on every page render, which re-keys the Composer root's
  // context and re-renders every atom under it.
  const composerRootChange = useCallback((v: string) => { clearFollowUpOwnership(); setInput(v) }, [clearFollowUpOwnership, setInput])
  const prefillEditedRef = useRef(prefillEdited); prefillEditedRef.current = prefillEdited
  const composerUserEdit = useCallback((v: string) => {
    clearFollowUpOwnership(); setInput(v)
    if (!prefillEditedRef.current) setPrefillEdited(true)
  }, [clearFollowUpOwnership, setInput, setPrefillEdited])
  const followUpOptionsKey = followUpOptions.join('\x00')
  // A single-select offer is keyed on its source row too: a newer reply can repeat
  // the same labels, and a single-select pick must not replace text the user picked
  // from the older offer. Multi-select rows keep the label-only reset.
  const singleSourceKey = followUpMulti ? null : followUpSourceKey
  // An app list keeps its picks while it is hidden and back (a question card
  // came and went): it resets on a new list, a switch, or its own send below.
  const appPickScope = app ? `${app.key}\x01${app.options.join('\x00')}` : null
  const pickScope = appPickScope ?? followUpOptionsKey
  const pickSource = appPickScope ? null : singleSourceKey
  const pickMulti = app ? app.multi : followUpMulti
  useEffect(() => { setFollowUpPicked(new Set()); followUpInsertedRef.current = null }, [pickScope, pickSource, pickMulti, activeSlot])
  // Called by every send the composer makes (the box, a chip's send, a steer,
  // voice auto-submit) just before it, with the text it sends when that is not
  // the box's draft. The list goes off at once, so a second click during the
  // send cannot repeat it; the send then settles the note, and one that never
  // went out (offline, held, refused) takes it back with the picks.
  const latest = useRef({ app, suppressed, activeSlot }); latest.current = { app, suppressed, activeSlot }
  const noteFollowUpSend = useCallback((text?: string): FollowUpSendSettle => {
    const { app: offer, suppressed: off, activeSlot: slot } = latest.current
    const picked = followUpPickedRef.current
    if (!offer || off || !isAppChipSend(offer, text, text ? '' : inputRef.current.trim())) return SETTLE_NOTHING
    const note = { slot, key: offer.key, sentAt: Date.now() }
    const owned = followUpInsertedRef.current
    setSentAgainst(note)
    followUpPickedRef.current = new Set()
    setFollowUpPicked(followUpPickedRef.current)
    followUpInsertedRef.current = null
    return sent => {
      if (sent) return
      setSentAgainst(s => (s === note ? null : s))
      // The picks come back only if nothing was picked since and the chat is the same.
      if (followUpPickedRef.current.size === 0 && latest.current.activeSlot === slot) {
        followUpPickedRef.current = picked
        setFollowUpPicked(picked)
        followUpInsertedRef.current = owned
      }
    }
  }, [inputRef])
  const toggleFollowUpOption = (o: string) => {
    // Regular options: toggle. Click unpicked → append + mark; click
    // picked → try to remove text + unmark (if the user edited the
    // text so it no longer matches, leave text alone — the chip
    // still un-highlights for consistency).
    if (followUpPickedRef.current.has(o)) {
      const next = new Set(followUpPickedRef.current); next.delete(o)
      followUpPickedRef.current = next
      // Synchronous transform on the live draft + ownership refs
      // (#7616): advance both refs and set the value in the click
      // handler, never in a render-time updater, so StrictMode's
      // double-invocation cannot rebase ownership on stale state.
      const r = removeFollowUpOption(inputRef.current, followUpInsertedRef.current, o)
      followUpInsertedRef.current = r.owned
      inputRef.current = r.value
      setInput(r.value)
      setFollowUpPicked(next)
    } else {
      // `[OPTION:]` is single-select: the new pick replaces the previous one.
      const next = followUpMulti ? new Set(followUpPickedRef.current) : new Set<string>(); next.add(o)
      followUpPickedRef.current = next
      const r = followUpMulti
        ? appendFollowUpOption(inputRef.current, followUpInsertedRef.current, o)
        : selectSingleFollowUpOption(inputRef.current, followUpInsertedRef.current, o)
      followUpInsertedRef.current = r.owned
      inputRef.current = r.value
      setInput(r.value)
      setFollowUpPicked(next)
    }
  }
  return {
    followUpOptions, followUpSourceKey, followUpMulti,
    followUpPicked, followUpPickedRef, toggleFollowUpOption, noteFollowUpSend,
    composerRootChange, composerUserEdit,
  }
}
