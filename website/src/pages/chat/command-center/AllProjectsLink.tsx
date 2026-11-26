/** The right bar's "All projects" link: opens the all-projects Board (`/crew-board`).
 *  The Board's "Other projects" line (`OtherProjectsLine.tsx`) draws it at its right
 *  end, even when it has nothing to count, so the page is always one click away.
 *  Outside a router there is no page to go to, so nothing is drawn, as
 *  `AgentNoToolsNotice` does for its link. */
import { Link, useInRouterContext } from 'react-router-dom'
import { ArrowUpRight } from 'lucide-react'
import { i18nT } from '../../../i18n/t'

export default function AllProjectsLink() {
  if (!useInRouterContext()) return null
  return (
    <Link to="/crew-board" className="shrink-0 inline-flex items-center gap-0.5 text-[12px] font-medium conductor-mode-accent hover:underline" data-testid="board-all-projects-link">
      {i18nT('pages.allProjectsBoard.all_projects')}
      <ArrowUpRight size={12} aria-hidden="true" className="lucide-inline" />
    </Link>
  )
}
