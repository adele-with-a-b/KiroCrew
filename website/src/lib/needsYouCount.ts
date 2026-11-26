/** The browser tab's attention number: how many of the person's chats wait on
 *  them (`needsYouOf`), never how many are unread. Read by the shell's tab title. */
import { createSelector } from '@reduxjs/toolkit'
import type { RootState } from '../store'
import { selectPersonSlots } from '../store/personSlots'
import { crewWaitingApprovals } from '../pages/chat/command-center/model'
import { needsYouOf } from './needsYou'

/** Over the person's own list (`selectPersonSlots`), crew approvals read from
 *  every live row, crew members included. */
export const selectChatsWaitingOnYou = createSelector(
  [(state: RootState) => state.dashboard.slots, selectPersonSlots],
  (all, person) => {
    const crewApprovals = crewWaitingApprovals(all)
    let n = 0
    for (const s of person) if (needsYouOf(s, crewApprovals[s.key] ?? 0)) n += 1
    return n
  },
)
