/** "Not now" under a reply that asks the person something (an options line or a
 *  question label): one press sets that ask aside, so the chat leaves Needs you
 *  until its agent asks something new; "Bring back" undoes it. The gateway owns
 *  the rule (`reply_ask` on the slot row, `dashboard/needs_you_aside.py`): the
 *  press sends the agent nothing and writes no message, and the new row reaches
 *  every open window through the slots frame. Approvals and question cards have
 *  their own buttons and never carry a `reply_ask`, so no line shows for them. */
import { useState } from 'react'
import { motion } from 'framer-motion'
import { useMutation } from '@tanstack/react-query'
import { Moon, Undo2, X } from 'lucide-react'
import { api } from '../../api/client'
import { apiErrorCode } from '../../api/apiError'
import { useAppSelector } from '../../store'
import { Btn } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { useReducedMotion } from '../../hooks/useReducedMotion'
import { i18nT } from '../../i18n/t'
import type { ReplyAsk } from '../../types'

/** The slot's reply ask when it sits on `rowId`, else null. */
function useReplyAskOn(slotKey: string, rowId: string): ReplyAsk | null {
  return useAppSelector(st => {
    const ask = st.dashboard.slots.find(s => s.key === slotKey)?.reply_ask
    return ask && ask.row_id === rowId ? ask : null
  })
}

export default function NotNowLine({ slotKey, rowId }: { slotKey: string; rowId: string }) {
  const ask = useReplyAskOn(slotKey, rowId)
  const reduceMotion = useReducedMotion()
  const [error, setError] = useState<{ message: string; stale: boolean } | null>(null)
  const press = useMutation({
    mutationFn: (setAside: boolean) => api.setReplyAskAside(slotKey, rowId, setAside),
    onMutate: () => setError(null),
    // A 409 means a newer reply replaced this ask before the press: the slots
    // frame already carries the new one, which draws its own line.
    onError: (err: unknown) => {
      const stale = apiErrorCode(err) === 'not_waiting'
      setError({ message: i18nT(stale ? 'pages.chatPage.not_now_refused' : 'pages.chatPage.not_now_failed'), stale })
    },
  })
  if (!ask) return null
  const aside = ask.set_aside
  return (
    <motion.div layout={reduceMotion ? false : 'position'} className="flex flex-col gap-1 mt-2" data-testid="not-now-line">
      <p className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-[11px] leading-4 text-muted m-0">
        {aside && (
          <>
            <Moon size={11} className="shrink-0 opacity-80" aria-hidden />
            <span className="font-bold">{i18nT('pages.chatPage.not_now')}</span>
            <span>{`· ${i18nT('pages.chatPage.not_now_set_aside')}`}</span>
          </>
        )}
        {/* One button for both states, so focus stays on it after the press. */}
        <Btn
          className={`${aside ? 'order-last' : ''} gap-1 border-transparent hover:border-transparent px-0.5 py-0 text-[11px] leading-4 font-medium focus-ring-accent`}
          busy={press.isPending}
          onClick={() => press.mutate(!aside)}
          data-testid={aside ? 'not-now-bring-back' : 'not-now'}
        >
          {aside ? <Undo2 size={11} aria-hidden /> : <X size={11} aria-hidden />}
          {i18nT(aside ? 'pages.chatPage.not_now_bring_back' : 'pages.chatPage.not_now')}
        </Btn>
        {!aside && <span>{i18nT('pages.chatPage.not_now_note')}</span>}
      </p>
      <ErrorNotice variant="inline" message={error?.message} impact={error?.stale ? 'minor' : 'partial'} />
    </motion.div>
  )
}
