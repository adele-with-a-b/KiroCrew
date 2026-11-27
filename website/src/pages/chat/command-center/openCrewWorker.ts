/** Opening one crew member from a Board row, the one way every Board does it: a chat
 *  on another crew in the crew window over the pane, a live one in the main pane, a
 *  closed one reopened from its saved transcript first. The right bar and the
 *  all-projects page share it, so a row opens the same worker on both. */
import { useCallback, useState } from 'react'
import type { WorkBoardItem } from '../../../api/crewBoard'
import type { CrewWorker } from '../../../types'
import { crewWorkerCrewChat } from '../../../lib/crewWorkers'
import { i18nT } from '../../../i18n/t'
import { useAppDispatch, useAppStore } from '../../../store'
import { closeActivityPanel } from '../../../store/chatSlice'
import { useIsMobile } from '../../../hooks/useIsMobile'
import { openCrewWindow } from '../crew-window/crewWindowStore'
import { openCrewSession, reopenCrewWorker } from './CrewWorkerList'

/** A remote row opens only when it names its chat on the crew. */
export function canOpenCrewWorker(w: CrewWorker): boolean {
  return !w.remote_crew || !!crewWorkerCrewChat(w)
}

/** `open(w)`, and whether the last reopen of a closed member failed. `then` runs once
 *  the member is on its way to the screen: the all-projects page goes to the chat. */
export function useOpenCrewWorker(then?: () => void): {
  open: (w: CrewWorker) => void
  reopenFailed: boolean
  setReopenFailed: (failed: boolean) => void
} {
  const dispatch = useAppDispatch()
  const store = useAppStore()
  const isMobile = useIsMobile()
  const [reopenFailed, setReopenFailed] = useState(false)
  const open = useCallback((w: CrewWorker) => {
    setReopenFailed(false)
    const crewChat = crewWorkerCrewChat(w)
    if (crewChat) {
      // A chat on another crew opens in the crew window over the pane, the one
      // viewer for a crew's chat; on a phone the panel's sheet steps aside for it.
      openCrewWindow(crewChat)
      if (isMobile) dispatch(closeActivityPanel())
      then?.()
      return
    }
    if (w.status !== 'closed') {
      openCrewSession(dispatch, w.key, !isMobile)
      then?.()
      return
    }
    void reopenCrewWorker(dispatch, store.getState, w.key, w.title, !isMobile).then(ok => {
      if (ok) then?.()
      else setReopenFailed(true)
    })
  }, [dispatch, store, isMobile, then])
  return { open, reopenFailed, setReopenFailed }
}

/** The way to a story's worker, for its Board row and its story view. */
export interface WorkerDoor {
  /** Its chat's title, else a plain "worker chat". */
  who: string
  /** This machine, or the linked crew's name; with no crew-list row, the board's `runs_on`. */
  where: string
  /** What its button does: `open` a live chat (a chat on a linked crew in the crew
   *  window), `reopen` a closed chat on this machine from its saved transcript. */
  action?: 'open' | 'reopen'
  go?: () => void
  /** Why there is no button, in plain words. */
  why?: string
}

/** A story's door to its worker, or null when no worker was ever bound to it. It
 *  reads the crew-list row bound to the item (`ledger_item`) and opens through `open`
 *  (`useOpenCrewWorker`). A chat closed on a linked crew names no chat there (the
 *  crew window shows only a chat its crew still has), so the door says so. */
export function workerDoor(item: WorkBoardItem, workers: readonly CrewWorker[], open: (w: CrewWorker) => void): WorkerDoor | null {
  const w = workers.find(x => x.ledger_item === item.item_id)
  if (!w && !item.has_worker) return null
  const who = w?.title || i18nT('components.crewMode.board_worker_chat')
  const where = w ? w.remote_crew || i18nT('sessionEnvironments.picker.thisMachine') : item.runs_on || ''
  if (w && canOpenCrewWorker(w)) return { who, where, action: !w.remote_crew && w.status === 'closed' ? 'reopen' : 'open', go: () => open(w) }
  const closed = w ? w.status === 'closed' : item.alive === 'closed'
  return { who, where, why: closed && where ? i18nT('components.crewMode.board_chat_closed_on', { where }) : i18nT('components.crewMode.board_chat_not_here') }
}

/** A story row's click: open the worker its ledger item is bound to, when the crew
 *  list names one that can be opened (`workerDoor`); none otherwise. */
export function itemOpener(workers: readonly CrewWorker[], open: (w: CrewWorker) => void): (item: WorkBoardItem) => (() => void) | undefined {
  return item => workerDoor(item, workers, open)?.go
}
