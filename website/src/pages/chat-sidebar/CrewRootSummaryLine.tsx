/** A conductor mode root's status line in the Sessions list (`SessionRow`). */
import { useEffect, useState, type ReactNode } from 'react'
import { shallowEqual } from 'react-redux'
import { useAppSelector } from '../../store'
import type { CrewWorkFacts } from '../../types'
import { crewNeedsYou, crewWorkCounts, longestStalledWork, stalledWorkKeys, useCrewWorkClock } from '../../lib/crewWork'
import { crewRootOf } from '../../lib/crewMode'
import { summarizeCrewWorkers, crewRowCounts } from '../../lib/crewWorkers'
import { i18nT } from '../../i18n/t'
import ErrorNotice from '../../components/ErrorNotice'
import { useCrewWorkerSummary } from '../chat/command-center/CrewWorkerList'
import { CrewRowAttention, crewRowTooltip } from './CrewRowCounts'
import { CrewStateWord, type CrewRowOwnState } from './CrewRowState'

/** A conductor mode root's status line: "N needs you", "N stalled" (plus who
 *  stalled longest), "N running", or "Done" -- only what applies. With no crew
 *  and nothing owed it shows *fallback*, the row's usual line. */
export function CrewRootSummaryLine({ root, fallback, own = null, work, rootOwes = 0, dot = null, lineCls }: {
  root: string; fallback: ReactNode; own?: CrewRowOwnState | null; /** The row's unread dot, kept in front of the status. */ dot?: ReactNode
  /** The root's ledger in brief (`crew_work`): what waits on a person, what stalled. */
  work?: CrewWorkFacts | null
  /** The root itself holds an approval or a question for the person. */
  rootOwes?: number
  /** The row's status-line classes, so this line sits on the row's type scale. */
  lineCls: string
}) {
  // The server's groups (the worker list API), so the row, the panel and the
  // breadcrumb agree. Only what needs attention is drawn (CrewRowAttention); the
  // full counts live in the tooltip. A failed poll says so in the status line.
  const { lastWorkers, failed, paused } = useCrewWorkerSummary(root)
  const { groups, done, total, remoteRunning } = summarizeCrewWorkers(lastWorkers ?? [])
  const rowCounts = crewRowCounts(groups)
  const running = rowCounts.find(c => c.bucket === 'working')?.count ?? 0
  // The ledger's own stall rule (the Board's Milestones read the same one).
  useCrewWorkClock(work ? [work] : [])
  const stall = useAppSelector(st => {
    const now = Date.now()
    const counts = crewWorkCounts(work, st.dashboard.slots, now)
    // A running conductor turn is picking its stalls up: none are shown meanwhile.
    const handling = !!st.dashboard.slots.find(x => x.key === root)?.running
    // The crew members whose live row waits on a tool approval, as one string so
    // the selector compares by value.
    const live = st.dashboard.slots.filter(x => x.pending_approval && crewRootOf(x) === root).map(x => x.key).join(' ')
    return { person: counts?.person ?? 0, stalled: handling ? 0 : counts?.stalled ?? 0, keys: handling ? '' : stalledWorkKeys(work, st.dashboard.slots, now), live }
  }, shallowEqual)
  // A stalled line's duration reads the clock: step it once a minute while shown.
  const [, setMinute] = useState(0)
  useEffect(() => {
    if (stall.stalled <= 0) return
    const id = setInterval(() => setMinute(n => n + 1), 60_000)
    return () => clearInterval(id)
  }, [stall.stalled])
  const longest = longestStalledWork(stall.keys, worker => {
    const at = lastWorkers?.find(w => w.key === worker)?.last_activity
    return at ? Date.parse(at) : NaN
  })
  const longestSlot = useAppSelector(st => (longest?.worker ? st.dashboard.slots.find(x => x.key === longest.worker)?.title ?? '' : ''))
  const needsYou = crewNeedsYou(lastWorkers, stall.live ? stall.live.split(' ') : [], stall.person, work?.person_workers) + rootOwes
  const member = longest ? lastWorkers?.find(w => w.key === longest.worker) : undefined
  const stalledForMs = longest ? Date.now() - longest.since : 0
  const stalledName = member?.title || member?.agent || longestSlot || i18nT('commandCenter.worker_untitled')
  const title = crewRowTooltip(total, rowCounts, stall.stalled, needsYou, remoteRunning)
  // Finished only when the main chat is settled too, and, with a ledger, the gateway's run verdict says done.
  const allDone = (work?.run ? work.run.settled === 'done' : total > 0 && done === total) && !own
  // An interrupted turn or a stalled goal loop of the main chat is said first.
  const ownFirst = !paused && own && (own.kind === 'paused' || own.kind === 'stalled') ? own : null
  // Nothing needs attention and the crew is not finished (or paused): the row's usual line.
  if (!failed && !paused && !ownFirst && !allDone && needsYou === 0 && stall.stalled === 0 && running === 0) return <>{fallback}</>
  return (
    <div className={lineCls} data-testid="crew-root-summary">{dot}
      {/* The row's own state outranks a failed crew read: it is said first. */}
      {failed && own && <CrewStateWord kind={own.kind} title={own.title} />}
      {!failed && ownFirst && <CrewStateWord kind={ownFirst.kind} title={ownFirst.title} />}
      {failed ? (
        <ErrorNotice variant="inline" askAgent className="min-w-0" messageClassName="truncate" message={i18nT('commandCenter.workers_error')} />
      ) : (
        <CrewRowAttention needsYou={needsYou} stalled={stall.stalled} running={running} done={allDone} paused={paused} remoteRunning={remoteRunning}
          stalledName={stall.stalled > 0 ? stalledName : undefined} stalledForMs={stall.stalled > 0 ? stalledForMs : undefined} title={title} />
      )}
    </div>
  )
}
