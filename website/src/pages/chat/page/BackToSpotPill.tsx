/**
 * The Back pill after a link-preview card's or a session control's "jump to
 * it": it floats where the jump-to-bottom pill does, says which reply the jump
 * left from, and a click scrolls back there (`transcriptJumps.ts` keeps the
 * spot). A press on it leaves an open session-control popover open
 * (`data-session-control-keep`): going back is a scroll, not a dismissal.
 */
import { motion, useReducedMotion } from 'framer-motion'
import { ArrowDown, ArrowUp } from 'lucide-react'
import { fmtTime } from '../../../i18n/format'
import { i18nT } from '../../../i18n/t'
import type { BackSpot } from './transcriptJumps'

export default function BackToSpotPill({ spot, short, onBack }: {
  spot: BackSpot
  /** Phone: the pill names only the time. */
  short: boolean
  onBack: () => void
}) {
  const still = useReducedMotion()
  const time = fmtTime(spot.from.ts)
  const Arrow = spot.down ? ArrowDown : ArrowUp
  return (
    <div className="absolute -top-12 inset-x-0 z-10 pointer-events-none flex justify-center">
      <motion.button
        type="button"
        initial={still ? false : { opacity: 0, y: 6 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.16 }}
        onClick={onBack}
        className="pointer-events-auto flex items-center gap-1.5 rounded-full bg-accent text-accent-fg px-3.5 py-[7px] text-[13px] font-semibold shadow-lg cursor-pointer active:scale-95"
        data-testid="back-to-spot"
        data-session-control-keep=""
      >
        <Arrow size={14} strokeWidth={2.5} aria-hidden="true" />
        <span>{i18nT(short ? 'pages.chat.backToSpot.label_short' : 'pages.chat.backToSpot.label', { time })}</span>
      </motion.button>
    </div>
  )
}
