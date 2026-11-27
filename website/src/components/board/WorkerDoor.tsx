/** A story's way to its worker chat (`workerDoor`): one button that opens the chat,
 *  or reopens a closed one from its saved transcript, or, with no way from here, the
 *  plain line that says why. A Board row draws it small under its lines; the story
 *  view draws it under who the worker is. */
import { MessageSquare } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import type { WorkerDoor } from '../../pages/chat/command-center/openCrewWorker'
import { Btn } from '../ui'

const LABEL = {
  open: 'components.crewMode.board_story_open_chat',
  reopen: 'components.crewMode.board_story_reopen_chat',
} as const

export default function WorkerDoorAction({ door, small = false }: { door: WorkerDoor; small?: boolean }) {
  if (door.go && door.action) {
    return (
      <Btn type="button" onClick={door.go} data-testid="board-worker-door" data-action={door.action}
        className={small ? 'h-6 px-2 py-0 text-[11px] font-medium' : 'mt-1.5 text-[12px] font-medium'}>
        <MessageSquare size={12} aria-hidden="true" />{i18nT(LABEL[door.action])}
      </Btn>
    )
  }
  if (!door.why) return null
  return <p className={`${small ? 'text-[11px]' : 'mt-1 text-[12px]'} text-muted [overflow-wrap:anywhere]`} data-testid="board-worker-door-none">{door.why}</p>
}
