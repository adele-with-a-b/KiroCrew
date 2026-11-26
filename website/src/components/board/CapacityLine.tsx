/** The seats each computer holds, in two shapes over one count. A seat is a worker
 *  chat on an open story whose chat is not closed (`computerLoads`, grouped by the
 *  story's `runs_on`): a bound worker holds its story's seat until the story closes.
 *  `CapacityLine` is the all-projects page's tiles, one per computer: the count, and
 *  this computer's room, the memory `resource_status` reads (`GET /api/crew-board/capacity`).
 *  `SeatsStrip` is the right bar's line under the goal: the same counts from the same
 *  reads (every Conductor-mode project's board under the right bar's own key and poll
 *  rule, and the capacity read), so the two never disagree.
 *  Counts only, never "N of M": no computer refuses a worker chat past a count. The
 *  goal's concurrency cap bounds one project's seats (the run line's "slots busy"),
 *  the gateway's live-chat ceiling counts every chat, not worker chats, and the
 *  execution cap counts subagents. A linked Crew's room is not known here, which its
 *  tile says. No test line: Crew keeps no queue of checks to count. Colour only where
 *  it hurts: memory tight, critical or under kernel pressure is red. */
import { useQueries, useQuery } from '@tanstack/react-query'
import { Monitor } from 'lucide-react'
import { api } from '../../api/client'
import { CREW_BOARD_POLL_MS, crewBoardQueryKey, type WorkBoardResponse } from '../../api/crewBoard'
import { CREW_BOARD_CAPACITY_POLL_MS, crewBoardCapacityQueryKey, type CrewBoardCapacity } from '../../api/crewBoardCapacity'
import { computerLoads, projectRoots, type ComputerLoad, type ProjectSlotRow } from '../../lib/allProjectsBoard'
import { pollGapMs, timedPoll, usePollRule } from '../../lib/pollRule'
import { useAppSelector } from '../../store'
import { PART_SEP } from './BoardRow'
import { fmtNumber } from '../../i18n/format'
import { i18nT } from '../../i18n/t'

/** This computer's room as one word, whether it hurts, and whether memory was read. */
function roomWord(capacity: CrewBoardCapacity): { word: string; hurts: boolean; measured: boolean } {
  if (capacity.pressure_held) return { word: i18nT('pages.allProjectsBoard.room_pressure'), hurts: true, measured: true }
  if (capacity.posture === 'critical') return { word: i18nT('pages.allProjectsBoard.room_critical'), hurts: true, measured: true }
  if (capacity.posture === 'tight') return { word: i18nT('pages.allProjectsBoard.room_tight'), hurts: true, measured: true }
  if (capacity.posture === 'ample') return { word: i18nT('pages.allProjectsBoard.room_ample'), hurts: false, measured: true }
  return { word: i18nT('pages.allProjectsBoard.room_unknown'), hurts: false, measured: false }
}

/** This computer's room in words, and whether it hurts. */
export function roomLine(capacity: CrewBoardCapacity | undefined): { text: string; hurts: boolean } {
  if (!capacity) return { text: '', hurts: false }
  const { word, hurts, measured } = roomWord(capacity)
  const free = !measured || capacity.available_gb == null ? '' : i18nT('pages.allProjectsBoard.free_gb', { gb: fmtNumber(capacity.available_gb, { maximumFractionDigits: 1 }) })
  return { text: [free, word].filter(Boolean).join(' · '), hurts }
}

function roomOf(load: ComputerLoad, here: { text: string; hurts: boolean }): { text: string; hurts: boolean } {
  return load.here ? here : { text: i18nT('pages.allProjectsBoard.room_linked'), hurts: false }
}

export default function CapacityLine({ loads, capacity }: { loads: readonly ComputerLoad[]; capacity: CrewBoardCapacity | undefined }) {
  if (loads.length === 0) return null
  const here = roomLine(capacity)
  return (
    <ul className="flex flex-wrap gap-2" aria-label={i18nT('pages.allProjectsBoard.capacity_label')} data-testid="board-capacity">
      {loads.map(load => {
        const room = roomOf(load, here)
        return (
          <li key={load.name} className="min-w-[10rem] rounded-lg border border-border bg-card text-card-fg px-3 py-1.5" data-testid="board-capacity-computer" data-here={load.here || undefined}>
            <div className="text-[11px] text-muted truncate" data-testid="board-capacity-name">{load.here ? i18nT('pages.allProjectsBoard.seats_here') : i18nT('pages.allProjectsBoard.seats_on', { name: load.name })}</div>
            <div className="flex items-baseline gap-2 flex-wrap">
              <span className="text-[15px] font-semibold text-text-strong tabular-nums" data-testid="board-capacity-chats">{fmtNumber(load.chats)}</span>
              {room.text && <span className={`text-[12px] ${room.hurts ? 'crew-tone-danger' : 'text-muted'}`} data-testid="board-capacity-room" data-hurts={room.hurts || undefined}>{room.text}</span>}
            </div>
          </li>
        )
      })}
    </ul>
  )
}

/** Every Conductor-mode project's board and the capacity read, as the all-projects
 *  page reads them (same keys, same poll rule, one cache entry each), folded into the
 *  seats per computer. Empty until the capacity read names this computer. */
export function useComputerLoads(active: boolean): { loads: ComputerLoad[]; capacity: CrewBoardCapacity | undefined } {
  const rows = useAppSelector(s => s.dashboard.slots) as readonly ProjectSlotRow[]
  const roots = projectRoots(rows).map(r => r.key)
  const boards = useQueries({
    queries: roots.map(root => ({
      queryKey: crewBoardQueryKey(root),
      queryFn: () => timedPoll(`crew-board:${root}`, () => api.crewBoard(root)) as Promise<WorkBoardResponse>,
      enabled: active,
      refetchInterval: () => (active ? pollGapMs(`crew-board:${root}`, CREW_BOARD_POLL_MS) : false),
      retry: false,
    })),
  })
  const capacityInterval = usePollRule('crew-board-capacity', CREW_BOARD_CAPACITY_POLL_MS, active, crewBoardCapacityQueryKey)
  const capacity = useQuery({ queryKey: crewBoardCapacityQueryKey, queryFn: () => timedPoll('crew-board-capacity', () => api.crewBoardCapacity()), enabled: active, refetchInterval: capacityInterval, retry: false })
  const loads = capacity.data ? computerLoads(boards.map(b => b.data), capacity.data.name) : []
  return { loads, capacity: capacity.data }
}

/** The right bar's seats line under the goal: "Seats  This computer 3  linked-crew 1",
 *  this computer's room word said in red beside its count only when it hurts. */
export function SeatsStrip({ active }: { active: boolean }) {
  const { loads, capacity } = useComputerLoads(active)
  if (loads.length === 0 || !capacity) return null
  // The word alone: the free memory is the tiles' to say, and beside a count it reads as one number.
  const room = roomWord(capacity)
  return (
    <div className="flex items-center gap-2 rounded-lg border border-border bg-card text-card-fg px-2.5 py-1.5 text-[12px]" data-testid="board-seats-strip">
      <Monitor size={14} aria-hidden="true" className="shrink-0 text-muted" />
      <span className="text-muted">{i18nT('pages.allProjectsBoard.seats_label')}</span>
      <ul className="flex flex-wrap items-baseline gap-x-3.5 gap-y-0.5 min-w-0" aria-label={i18nT('pages.allProjectsBoard.capacity_label')}>
        {loads.map(load => {
          const hurts = load.here && room.hurts
          return (
            <li key={load.name} className="text-muted [overflow-wrap:anywhere]" data-testid="board-seats-computer" data-here={load.here || undefined}>
              {load.here ? i18nT('pages.allProjectsBoard.this_computer') : load.name}{' '}
              <b className={`font-medium tabular-nums ${hurts ? 'crew-tone-danger' : 'text-text'}`} data-testid="board-seats-count">{fmtNumber(load.chats)}</b>
              {hurts && <span className="crew-tone-danger" data-testid="board-seats-room">{`${PART_SEP} ${room.word}`}</span>}
            </li>
          )
        })}
      </ul>
    </div>
  )
}
