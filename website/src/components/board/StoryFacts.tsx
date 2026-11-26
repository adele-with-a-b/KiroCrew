/** A story's quiet facts, line three of its Board row: its worker and the machine it
 *  runs on, its pull request and what GitHub last said of it, and how many of its
 *  sub-tasks are merged. Only what Crew has read: every number is a count, and the
 *  time of the GitHub read rides the PR's tooltip. The machine is the board read's
 *  `runs_on`, never a name written here. */
import { Bot, GitPullRequest } from 'lucide-react'
import type { CrewPrFacts, WorkBoardItem } from '../../api/crewBoard'
import { prFactTags } from '../crew/prFactTags'
import { i18nT } from '../../i18n/t'
import { joinParts } from './BoardRow'

export function storyHasFacts(item: WorkBoardItem, worker: string | undefined, subtasks: { done: number; total: number }): boolean {
  return !!worker || !!item.runs_on || item.pr != null || subtasks.total > 0
}

export default function StoryFacts({ item, worker, facts, subtasks }: {
  item: WorkBoardItem
  /** The bound worker's name, from the crew list. */
  worker?: string
  facts?: CrewPrFacts
  subtasks: { done: number; total: number }
}) {
  const who = joinParts([worker ?? '', item.runs_on ?? ''])
  const tags = prFactTags(facts)
  const said = tags.filter(t => !t.read)
  const read = tags.find(t => t.read)
  return (
    <>
      {who && (
        <span className="inline-flex items-center gap-1 min-w-0" data-testid="board-fact-worker" title={item.runs_on ? i18nT('components.crewMode.plan_runs_on', { name: item.runs_on }) : undefined}>
          <Bot size={12} aria-hidden="true" className="shrink-0" /><span className="truncate">{who}</span>
        </span>
      )}
      {item.pr != null && (
        <span className="inline-flex items-center gap-1 tabular-nums" data-testid="board-fact-pr" title={[facts?.label, read?.text].filter(Boolean).join(' · ') || undefined}>
          <GitPullRequest size={12} aria-hidden="true" className="shrink-0" />
          {joinParts([i18nT('components.crewMode.plan_pr', { n: item.pr }), ...said.map(t => t.text)])}
        </span>
      )}
      {subtasks.total > 0 && (
        <span className="tabular-nums" data-testid="board-fact-subtasks">{i18nT('components.crewMode.plan_tasks_toggle', { done: subtasks.done, total: subtasks.total })}</span>
      )}
    </>
  )
}
