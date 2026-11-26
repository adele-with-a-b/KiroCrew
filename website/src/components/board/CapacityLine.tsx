/** The seats each computer holds, in two shapes over one count. A seat is a worker
 *  chat on an open story whose chat is not closed (`computerLoads`, grouped by the
 *  story's `runs_on`): a bound worker holds its story's seat until the story closes.
 *  `CapacityLine` is the all-projects page's tiles, one per computer: the count, and
 *  this computer's room, the memory `resource_status` reads (`GET /api/crew-board/capacity`).
 *  `seatParts` is the right bar's run line's share of it: the same counts from the same
 *  reads (every Conductor-mode project's board under the right bar's own key and poll
 *  rule, and the capacity read), so the two never disagree, each computer with its
 *  room in words (a linked Crew's as the patrol last read it).
 *  Counts only, never "N of M": no computer refuses a worker chat past a count. The
 *  goal's concurrency cap bounds one project's seats, the gateway's live-chat ceiling
 *  counts every chat, not worker chats, and the execution cap counts subagents. The
 *  tiles say a linked Crew's room is not reported. No test line: Crew keeps no queue
 *  of checks to count. Colour only where it hurts: memory tight, critical or under
 *  kernel pressure is red. */
import { useQueries, useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import { CREW_BOARD_POLL_MS, crewBoardQueryKey, type WorkBoardResponse } from '../../api/crewBoard'
import { CREW_BOARD_CAPACITY_POLL_MS, crewBoardCapacityQueryKey, type CrewBoardCapacity } from '../../api/crewBoardCapacity'
import { computerLoads, projectRoots, type ComputerLoad, type ProjectSlotRow } from '../../lib/allProjectsBoard'
import { pollGapMs, timedPoll, usePollRule } from '../../lib/pollRule'
import { useAppSelector } from '../../store'
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
          <li key={load.name} className="min-w-[10rem] crew-board-card rounded-lg border border-border px-3 py-1.5" data-testid="board-capacity-computer" data-here={load.here || undefined}>
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

/** One computer's part of the run line: its worker chats and its room in words. */
export interface SeatPart { name: string; here: boolean; text: string; hurts: boolean }

/** A linked Crew's room as the patrol last read it, in words; `''` with no reading
 *  (or an `unknown` one): then its count stands alone. */
function crewRoomWord(room: string | undefined): string {
  if (room === '') return i18nT('pages.allProjectsBoard.room_ample')
  if (room === 'memory') return i18nT('pages.allProjectsBoard.room_short_memory')
  if (room === 'disk') return i18nT('pages.allProjectsBoard.room_short_disk')
  if (room === 'cpu') return i18nT('pages.allProjectsBoard.room_short_cpu')
  if (room === 'unreachable') return i18nT('pages.allProjectsBoard.room_unreachable')
  return ''
}

/** Each computer's part of the run line, this computer first: "this computer 14,
 *  has room", "demo-crew 3, has room". This computer's room word is always said,
 *  since "has room" answers "is the machine the bottleneck" too, and is red when it
 *  hurts; an unmeasured one, or a crew with no fresh reading, shows its count alone. */
export function seatParts(loads: readonly ComputerLoad[], capacity: CrewBoardCapacity | undefined): SeatPart[] {
  if (!capacity) return []
  const here = roomWord(capacity)
  return loads.map(load => {
    const room = load.here
      ? (here.measured ? here.word : '')
      : crewRoomWord(capacity.crews?.find(c => c.name === load.name)?.room)
    const count = fmtNumber(load.chats)
    const text = load.here
      ? (room ? i18nT('pages.allProjectsBoard.seats_here_room', { count, room }) : i18nT('pages.allProjectsBoard.seats_here_count', { count }))
      : (room ? i18nT('pages.allProjectsBoard.seats_on_room', { name: load.name, count, room }) : i18nT('pages.allProjectsBoard.seats_on_count', { name: load.name, count }))
    return { name: load.name, here: load.here, text, hurts: load.here && here.hurts }
  })
}

/** The run line's computer parts, from every Conductor-mode project's board and the
 *  capacity read, the all-projects page's own reads; empty until both name this computer. */
export function useSeatParts(active: boolean): SeatPart[] {
  const { loads, capacity } = useComputerLoads(active)
  return seatParts(loads, capacity)
}
