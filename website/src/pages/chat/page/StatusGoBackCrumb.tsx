/**
 * The way back after an app status item sent the operator to this chat: the
 * chat it left, named before this chat's title, the same plain-text crumb the
 * Crew Mode worker header uses. Nothing while no go is on screen.
 */
import { ChevronLeft } from 'lucide-react'

import { i18nT } from '../../../i18n/t'
import { useActiveStatusGo, useStatusGoBack } from '../../../lib/statusGo'

/** Reads only the go store, so a header that holds no go (and a test that
 *  renders it bare) needs no store or router context. */
export default function StatusGoBackCrumb() {
  const go = useActiveStatusGo()
  return go?.from ? <Crumb /> : null
}

function Crumb() {
  const way = useStatusGoBack()
  if (!way) return null
  return (
    <button
      type="button"
      onClick={way.back}
      title={way.title}
      aria-label={i18nT('pages.chat.statusGoBack.label', { title: way.title })}
      data-testid="status-go-back"
      className="pointer-events-auto inline-flex items-center gap-0.5 min-w-0 max-w-[40%] shrink rounded-sm bg-transparent border-none px-0 py-3 -my-3 md:py-0 md:my-0 text-[13px] text-muted hover:text-text hover:underline focus-visible:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent cursor-pointer"
    >
      <ChevronLeft size={14} aria-hidden="true" className="shrink-0" />
      <span className="truncate">{way.title}</span>
    </button>
  )
}
