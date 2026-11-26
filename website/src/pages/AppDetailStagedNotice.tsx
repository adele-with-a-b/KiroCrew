import { ShieldAlert } from 'lucide-react'
import type { api } from '../api/client'
import { Btn } from '../components/ui'
import { appDisplayName } from '../components/appstore/appManifest'
import { i18nT } from '../i18n/t'
import { fmtList } from '../i18n/format'
import { messageActionGrantLabel } from '../messageActions/messageActionRegistry'
import { selectionActionGrantLabel } from '../selectionActions/selectionActionRegistry'
import { actionGrant } from '../composerActions/composerActionRegistry'
import { StagedBotScopes } from './AppDetailBotGrants'
import { DesktopCapabilitiesStaged, OpenFilesStaged, SessionControlInsertsStaged, UserSessionReachStaged, WorkerAutoApproveStaged } from './AppDetailGrantBoxes'
import { SESSION_KEY_GRANT, type stagedGrantsOf } from './appDetailPageStagedGrants'
import type { AppInfo } from './appDetailPageTypes'

/** The enable route's grants consent: every held-back entry, by family. */
export type StagedGrantsConsent = NonNullable<Parameters<typeof api.enableApp>[2]>

const CODE = 'bg-bg-elevated border border-border px-1.5 py-0.5 rounded text-[11px] text-text'

/**
 * The App Detail page's "approve new permissions" notice: every grant an update
 * holds back, one row per family, and the button that approves them all. The page
 * owns the approval's effects (`onApprove`); this owns what is shown and what the
 * enable route is sent. Lives here because `AppDetailPage.tsx` is at the file cap.
 */
export default function AppDetailStagedNotice({ app, staged, stagedBots, busy, onApprove }: {
  app: AppInfo
  staged: ReturnType<typeof stagedGrantsOf>
  stagedBots: string[]
  busy: boolean
  /** Approve them: the grants consent, and the entries named for the confirmation. */
  onApprove: (grants: StagedGrantsConsent, entries: string) => void
}) {
  const {
    stagedApi, stagedEvents, stagedLinkHosts, stagedLinkPatterns, stagedFullUrl, stagedSessionKey,
    stagedMessageActions, declaredMessageActions, stagedOpenHosts, stagedOpenFullUrl,
    stagedComposerActions, stagedSessionControlInserts, stagedSelection, declaredSelectionActions, stagedWorkerAutoApprove,
    stagedUserSessionReach, stagedDesktopCapabilities, stagedFrameSources, stagedOpenFiles,
  } = staged
  const codes = (items: readonly string[], testId?: string, extra = '') => (
    <div className="flex flex-wrap gap-1 mt-1" data-testid={testId}>
      {items.map(v => <code key={v} className={`${CODE}${extra}`}>{v}</code>)}
    </div>
  )
  const approve = () => {
    const entries = fmtList([...stagedApi, ...stagedEvents, ...stagedBots, ...stagedLinkHosts, ...stagedLinkPatterns, ...(stagedFullUrl ? ['fullUrl'] : []), ...(stagedSessionKey ? ['sessionKey'] : []), ...stagedMessageActions.map(e => messageActionGrantLabel(e, declaredMessageActions)), ...stagedOpenHosts, ...(stagedOpenFullUrl ? ['fullUrl'] : []), ...stagedComposerActions, ...stagedSessionControlInserts, ...stagedSelection.map(e => selectionActionGrantLabel(e, declaredSelectionActions)), ...(stagedWorkerAutoApprove ? ['workerAutoApprove'] : []), ...stagedUserSessionReach, ...stagedDesktopCapabilities, ...stagedFrameSources, ...(stagedOpenFiles ? ['openFiles'] : [])], { style: 'narrow' })
    onApprove({
      api: stagedApi,
      events: stagedEvents,
      bots: stagedBots,
      linkPreviewHosts: stagedLinkHosts,
      linkPreviewTextPatterns: stagedLinkPatterns,
      linkPreviewFullUrl: stagedFullUrl ? ['fullUrl'] : [],
      linkPreviewSessionKey: stagedSessionKey ? [SESSION_KEY_GRANT] : [],
      messageActions: stagedMessageActions,
      linkOpenHosts: stagedOpenHosts,
      linkOpenFullUrl: stagedOpenFullUrl ? ['fullUrl'] : [],
      composerActions: stagedComposerActions.map(actionGrant),
      sessionControlInserts: stagedSessionControlInserts.map(actionGrant),
      selectionActions: stagedSelection,
      workerAutoApprove: stagedWorkerAutoApprove ? ['ownSlots'] : [],
      reachableByUserSessions: stagedUserSessionReach,
      desktopCapabilities: stagedDesktopCapabilities,
      frameSources: stagedFrameSources,
      openFiles: stagedOpenFiles ? ['open'] : [],
    }, entries)
  }
  return (
    <div
      role="status"
      className="mb-4 flex flex-wrap items-start gap-3 rounded-lg border border-warn/30 bg-warn-subtle p-3 animate-rise"
    >
      <ShieldAlert size={14} className="mt-[3px] shrink-0 text-warn" />
      <div className="text-text text-sm flex-1 min-w-0 grid gap-2">
        <span>
          {app.enabled
            ? i18nT('pages.appDetailPage.grants_reconsent_notice', { name: appDisplayName(app) })
            : i18nT('pages.appDetailPage.grants_reconsent_notice_disabled', { name: appDisplayName(app) })}
        </span>
        {app.enabled && (
          <span className="text-[13px] text-text">{i18nT('pages.appDetailPage.grants_reconsent_keep', { name: appDisplayName(app) })}</span>
        )}
        {stagedApi.length > 0 && (
          <div>
            <div className="text-[13px]">{i18nT('pages.appDetailPage.grants_reconsent_api')}</div>
            {codes(stagedApi)}
          </div>
        )}
        {stagedEvents.length > 0 && (
          <div>
            <div className="text-[13px]">{i18nT('pages.appDetailPage.grants_reconsent_events')}</div>
            {codes(stagedEvents)}
          </div>
        )}
        <StagedBotScopes scopes={stagedBots} />
        {stagedLinkHosts.length > 0 && (
          <div>
            <div className="text-[13px]">{i18nT('pages.appDetailPage.grants_reconsent_link_hosts')}</div>
            {codes(stagedLinkHosts)}
          </div>
        )}
        {stagedLinkPatterns.length > 0 && (
          <div>
            <div className="text-[13px]">{i18nT('pages.appDetailPage.grants_reconsent_link_patterns')}</div>
            {codes(stagedLinkPatterns, undefined, ' break-all')}
          </div>
        )}
        {stagedFullUrl && (
          <div className="text-[13px]">{i18nT('pages.appDetailPage.grants_reconsent_link_full_url')}</div>
        )}
        {stagedSessionKey && (
          <div className="text-[13px]">{i18nT('pages.appDetailPage.grants_reconsent_link_session_key')}</div>
        )}
        {stagedMessageActions.length > 0 && (
          <div>
            <div className="text-[13px]">{i18nT('pages.appDetailPage.grants_reconsent_message_actions')}</div>
            <ul className="mt-1 text-[13px] list-disc pl-5" data-testid="staged-message-actions">
              {stagedMessageActions.map(e => (
                <li key={e}>{messageActionGrantLabel(e, declaredMessageActions)}</li>
              ))}
            </ul>
          </div>
        )}
        {stagedOpenHosts.length > 0 && (
          <div>
            <div className="text-[13px]">{i18nT('pages.appDetailPage.grants_reconsent_link_open_hosts')}</div>
            {codes(stagedOpenHosts)}
          </div>
        )}
        {stagedFrameSources.length > 0 && (
          <div>
            <div className="text-[13px]">{i18nT('pages.appDetailPage.grants_reconsent_frame_sources')}</div>
            {codes(stagedFrameSources, 'staged-frame-sources')}
          </div>
        )}
        {stagedComposerActions.length > 0 && (
          <div>
            <div className="text-[13px]">{i18nT('pages.appDetailPage.grants_reconsent_composer_actions')}</div>
            {codes(stagedComposerActions)}
          </div>
        )}
        {stagedSelection.length > 0 && (
          <div>
            <div className="text-[13px]">{i18nT('pages.appDetailPage.grants_reconsent_selection_actions')}</div>
            <ul className="mt-1 text-[13px] list-disc pl-5" data-testid="staged-selection-actions">
              {stagedSelection.map(e => (
                <li key={e}>{selectionActionGrantLabel(e, declaredSelectionActions)}</li>
              ))}
            </ul>
          </div>
        )}
        {stagedOpenFullUrl && <div className="text-[13px]">{i18nT('pages.appDetailPage.grants_reconsent_link_open_full_url')}</div>}
        {stagedWorkerAutoApprove && <WorkerAutoApproveStaged />}
        <UserSessionReachStaged staged={stagedUserSessionReach} />
        <DesktopCapabilitiesStaged staged={stagedDesktopCapabilities} />
        {stagedOpenFiles && <OpenFilesStaged />}
        <SessionControlInsertsStaged staged={stagedSessionControlInserts} />
        {app.enabled && (
          <span className="text-[13px] text-muted">{i18nT('pages.appDetailPage.grants_reconsent_undo', { name: appDisplayName(app) })}</span>
        )}
      </div>
      {/* Approval is offered only on a running app: on a disabled one the
          enable route would also switch the app on. Enabling it first runs
          it on the approved set, and the notice stays until approval. */}
      {app.enabled && (
        <Btn primary onClick={approve} disabled={busy}>
          {i18nT('pages.appDetailPage.approve_new_permissions')}
        </Btn>
      )}
    </div>
  )
}
