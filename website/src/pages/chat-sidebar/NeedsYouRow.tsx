/** The parts a session row draws while its chat waits on the person
 *  (`needsYouOf`, lib/needsYou): the status line that names the oldest ask, the
 *  count of open asks with how long the oldest has waited, and a one-time ring
 *  when the wait has just begun. The row's own `session-needs-you` class paints
 *  the fill and the edge bar (sidebar-and-side-panel.css). */
import { useState } from 'react'
import { motion } from 'framer-motion'
import { useMinuteClock } from '../chat/command-center/ConductorModeCrewPanel'
import { Hand, ListChecks, MessageCircleQuestionMark, ShieldCheck, Tag } from 'lucide-react'
import type { NeedsYou, NeedsYouKind } from '../../types'
import { needsYouAge } from '../../lib/needsYou'
import { useReducedMotion } from '../../hooks/useReducedMotion'
import { ROW_META_CLS } from '../../components/listShell'
import { fmtNumber } from '../../i18n/format'
import { i18nT } from '../../i18n/t'

/** What the line says when the gateway sent no first words: the words the row
 *  already uses for that state, so the list keeps one vocabulary. A labelled
 *  question always comes with its words, so it falls back to "Needs you". */
const KIND_LABEL_KEY: Record<NeedsYouKind, string> = {
  approval: 'pages.chatSidebar.needs_approval',
  question: 'pages.chatSidebar.needs_your_answer',
  options: 'pages.chatSidebar.loop_waiting_on_you',
  labelled: 'pages.chatSidebar.needs_you_status',
}

/** A ring this young at render still pulses: the wait has just begun. */
const FRESH_WAIT_MS = 10_000

/** The glyph of one kind of ask, drawn in front of the "Needs you" word. */
export function NeedsYouKindMark({ kind, px }: { kind: NeedsYouKind | undefined; px: number }) {
  if (kind === 'approval') return <ShieldCheck size={px} className="shrink-0" aria-hidden />
  if (kind === 'question') return <MessageCircleQuestionMark size={px} className="shrink-0" aria-hidden />
  if (kind === 'options') return <ListChecks size={px} className="shrink-0" aria-hidden />
  if (kind === 'labelled') {
    return (
      <span className="inline-grid place-items-center w-3 h-3 rounded-[3px] bg-needs text-needs-fg shrink-0" aria-hidden>
        <Tag size={8} strokeWidth={3} />
      </span>
    )
  }
  return <Hand size={px} className="shrink-0" aria-hidden />
}

/** The row's status line while it waits: "Needs you · <first words>", or, with
 *  no words, the row's own words for that ask, as its approval line always read
 *  ("Needs approval · <last message>"); under it how many more asks wait behind
 *  the oldest one. A question card's last message is what the agent said BEFORE
 *  it asked, so a question's label stands alone. */
export function NeedsYouLine({ needs, lastMessage, lineCls, iconPx }: { needs: NeedsYou; lastMessage?: string; lineCls: string; iconPx: number }) {
  const kind = needs.kinds[0]
  const status = needs.first_words || !kind ? i18nT('pages.chatSidebar.needs_you_status') : i18nT(KIND_LABEL_KEY[kind])
  const words = needs.first_words || (kind === 'approval' || kind === 'options' ? lastMessage ?? '' : '')
  return (
    <>
      <div className={`${lineCls} text-needs`} title={words ? `${status} · ${words}` : status} data-testid="session-needs-you">
        <NeedsYouKindMark kind={kind} px={iconPx} />
        <span className="truncate min-w-0">
          <span className="font-bold whitespace-nowrap">{status}</span>
          {words ? <span className="text-text font-medium">{` · ${words}`}</span> : null}
        </span>
      </div>
      {needs.count > 1 && (
        <div className={`${lineCls} text-muted`} data-testid="session-needs-you-more">
          <span className="pl-4 truncate">{i18nT('pages.chatSidebar.needs_you_more', { count: needs.count - 1 })}</span>
        </div>
      )}
    </>
  )
}

/** The row's right edge while it waits: the open asks as a count, and how long
 *  the oldest one has waited. Both are read out by name. */
export function NeedsYouSide({ needs }: { needs: NeedsYou }) {
  const now = useMinuteClock()
  const age = needsYouAge(needs.oldest_ts, now)
  const open = i18nT('pages.chatSidebar.needs_you_open_count', { count: needs.count })
  const waited = age ? i18nT('pages.chatSidebar.needs_you_waited', { age }) : ''
  return (
    <div className="shrink-0 self-start pt-3 pl-2 flex flex-col items-end gap-[3px]" data-testid="session-needs-you-side">
      <span role="img" aria-label={open} title={open} data-testid="session-needs-you-count"
        className="min-w-[18px] h-[18px] px-[5px] rounded-full bg-needs text-needs-fg text-[11px] leading-[18px] font-extrabold text-center tabular-nums">
        {fmtNumber(needs.count)}
      </span>
      {age && (
        <span role="img" aria-label={waited} title={waited} data-testid="session-needs-you-age"
          className={`${ROW_META_CLS} font-mono text-needs`}>{age}</span>
      )}
    </div>
  )
}

/** One ring around a row whose wait began moments ago, so the eye catches it
 *  arrive. Never under reduced motion, and never for a wait already under way
 *  when the row is drawn (a reload, a row scrolled back into view). */
export function NeedsYouPulse({ needs }: { needs: NeedsYou }) {
  const reduceMotion = useReducedMotion()
  const [fresh] = useState(() => needs.oldest_ts > 0 && Date.now() - needs.oldest_ts * 1000 < FRESH_WAIT_MS)
  if (!fresh || reduceMotion) return null
  return (
    <motion.span aria-hidden="true" data-testid="session-needs-you-pulse"
      className="absolute inset-0 rounded-md border-2 border-needs pointer-events-none"
      initial={{ opacity: 0.9, scale: 1 }} animate={{ opacity: 0, scale: 1.03 }} transition={{ duration: 2.2, ease: 'easeOut' }} />
  )
}
