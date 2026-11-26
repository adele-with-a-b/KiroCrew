/** The all-projects Board's capacity line: one small block per computer, saying how
 *  many worker chats it holds and the room it has. Real counts only: the chats are
 *  the open stories whose worker chat is not closed (`computerLoads`), and this
 *  computer's room is the memory `resource_status` reads (`GET /api/crew-board/capacity`).
 *  Crew keeps no ceiling on worker chats per computer, so no "N of M" is drawn, and
 *  a linked Crew's room is not known here, which its block says. Colour only where
 *  it hurts: memory tight, critical or under kernel pressure is red. */
import type { CrewBoardCapacity } from '../../api/crewBoardCapacity'
import type { ComputerLoad } from '../../lib/allProjectsBoard'
import { fmtNumber } from '../../i18n/format'
import { i18nT } from '../../i18n/t'

/** This computer's room in words, and whether it hurts. */
export function roomLine(capacity: CrewBoardCapacity | undefined): { text: string; hurts: boolean } {
  if (!capacity) return { text: '', hurts: false }
  const free = capacity.available_gb == null ? '' : i18nT('pages.allProjectsBoard.free_gb', { gb: fmtNumber(capacity.available_gb, { maximumFractionDigits: 1 }) })
  if (capacity.pressure_held) return { text: [free, i18nT('pages.allProjectsBoard.room_pressure')].filter(Boolean).join(' · '), hurts: true }
  if (capacity.posture === 'critical') return { text: [free, i18nT('pages.allProjectsBoard.room_critical')].filter(Boolean).join(' · '), hurts: true }
  if (capacity.posture === 'tight') return { text: [free, i18nT('pages.allProjectsBoard.room_tight')].filter(Boolean).join(' · '), hurts: true }
  if (capacity.posture === 'ample') return { text: [free, i18nT('pages.allProjectsBoard.room_ample')].filter(Boolean).join(' · '), hurts: false }
  return { text: i18nT('pages.allProjectsBoard.room_unknown'), hurts: false }
}

export default function CapacityLine({ loads, capacity }: { loads: readonly ComputerLoad[]; capacity: CrewBoardCapacity | undefined }) {
  if (loads.length === 0) return null
  const here = roomLine(capacity)
  return (
    <ul className="flex flex-wrap gap-2" aria-label={i18nT('pages.allProjectsBoard.capacity_label')} data-testid="board-capacity">
      {loads.map(load => {
        const room = load.here ? here : { text: i18nT('pages.allProjectsBoard.room_linked'), hurts: false }
        return (
          <li key={load.name} className="min-w-[10rem] rounded-lg border border-border bg-bg-elevated px-3 py-1.5" data-testid="board-capacity-computer" data-here={load.here || undefined}>
            <div className="text-[11px] text-muted truncate" data-testid="board-capacity-name">{load.here ? i18nT('pages.allProjectsBoard.this_computer') : load.name}</div>
            <div className="flex items-baseline gap-2 flex-wrap">
              <span className="text-[15px] font-semibold text-text tabular-nums" data-testid="board-capacity-chats">{i18nT('pages.allProjectsBoard.worker_chats', { count: load.chats })}</span>
              {load.running > 0 && <span className="text-[12px] text-muted tabular-nums" data-testid="board-capacity-running">{i18nT('pages.allProjectsBoard.running', { count: load.running })}</span>}
            </div>
            {room.text && <div className={`text-[12px] ${room.hurts ? 'crew-tone-danger' : 'text-muted'}`} data-testid="board-capacity-room" data-hurts={room.hurts || undefined}>{room.text}</div>}
          </li>
        )
      })}
    </ul>
  )
}
