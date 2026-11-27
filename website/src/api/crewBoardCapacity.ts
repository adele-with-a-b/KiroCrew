/** `GET /api/crew-board/capacity` (`dashboard/handlers/crew_board_capacity.py`):
 *  this computer's room for the all-projects Board's capacity line. Numbers and
 *  status words only. `name` is what this gateway's boards put in a local story's
 *  `runs_on`, so the page can tell a story running here from one on a linked Crew. */
export interface CrewBoardCapacity {
  name: string
  /** Free memory in GB; null when it was not measured. */
  available_gb: number | null
  /** `resource_status`'s posture: ample, tight, critical or unknown. */
  posture: 'ample' | 'tight' | 'critical' | 'unknown' | string
  tight_gb: number
  critical_gb: number
  /** The macOS kernel reports memory pressure. */
  pressure_held: boolean
  /** Each linked Crew's room as the patrol last read it, by its stories' `runs_on`
   *  name: `''` has room, else `memory`, `disk`, `cpu`, `unknown` or `unreachable`.
   *  A crew no patrol read lately is absent; an older gateway sends no list. */
  crews?: { name: string; room: string }[]
}

export const crewBoardCapacityQueryKey = ['crew-board-capacity'] as const

/** The probe moves on the scale of seconds; the boards poll every 10 s, so this does too. */
export const CREW_BOARD_CAPACITY_POLL_MS = 10_000
