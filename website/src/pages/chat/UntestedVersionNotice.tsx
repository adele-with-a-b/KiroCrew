import { memo, useCallback, useContext, useSyncExternalStore } from 'react'
import { QueryClientContext, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../../api/client'
import type { VersionNoticeDismissals } from '../../api/client/chat'
import { retryPolicy } from '../../api/queryClient'
import ErrorNotice from '../../components/ErrorNotice'
import { isOwnerOnlyRefusal } from '../../components/sessionEnvironmentHooks'
import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import type { ChatMessage } from '../../types'
import NoticeCard from './NoticeCard'

/**
 * The tag on the notice a new session writes when its harness runs a release
 * outside the one Crew tested it on, with `meta.harness` and `meta.version`.
 * Mirrors `NOTICE_KIND` in `src/kiro_crew/dashboard/version_notice_dismissals.py`.
 * A notice with no tag (a KAS relay's, which names no release) has no Dismiss.
 */
export const UNTESTED_VERSION_NOTICE_KIND = 'untested_version_notice'

/** The one read of the dismissed releases. Refreshed by the owner WebSocket
 *  event `version_notice_dismissed` and on every reconnect
 *  (`hooks/useWebSocket.ts`, `hooks/websocket/serverState.ts`); no polling. */
export const VERSION_NOTICE_DISMISSALS_QUERY_KEY = ['version-notice-dismissals'] as const

export interface UntestedVersionSubject {
  harness: string
  version: string
}

/** The harness and release a notice row names, or null when it is not this notice. */
export function untestedVersionSubject(m: ChatMessage): UntestedVersionSubject | null {
  if (m.role !== 'notice' || m.meta?.kind !== UNTESTED_VERSION_NOTICE_KIND) return null
  const { harness, version } = m.meta as { harness?: unknown; version?: unknown }
  if (typeof harness !== 'string' || !harness || typeof version !== 'string' || !version) return null
  return { harness, version }
}

export function isUntestedVersionNotice(m: ChatMessage): boolean {
  return untestedVersionSubject(m) !== null
}

/** Whether *data* holds a dismissal of *subject*'s release. */
export function versionNoticeDismissed(
  data: VersionNoticeDismissals | undefined,
  subject: UntestedVersionSubject,
): boolean {
  const versions = data?.dismissed?.[subject.harness]
  return Array.isArray(versions) && versions.includes(subject.version)
}

/**
 * The dismissed releases. A viewer who is not the owner gets `owner_only` every
 * time, so that refusal is not retried, and the notice keeps showing.
 */
export function useVersionNoticeDismissals() {
  return useQuery<VersionNoticeDismissals>({
    queryKey: VERSION_NOTICE_DISMISSALS_QUERY_KEY,
    queryFn: api.versionNoticeDismissals,
    // Fresh until the gateway says otherwise: the event and the reconnect re-read
    // it, and one transcript can hold many copies of the notice.
    staleTime: Infinity,
    retry: (count, error) => !isOwnerOnlyRefusal(error) && retryPolicy(count, error),
  })
}

/**
 * A predicate for rows that draw nothing because their release was dismissed,
 * for a host that counts drawn rows (`TurnBlock`'s fold). It only watches the
 * cached read the notice rows make, so it fetches nothing, needs no query
 * client (no client: nothing is hidden), and watches only while `watch`.
 */
export function useDismissedVersionNotice(watch: boolean): (m: ChatMessage) => boolean {
  const client = useContext(QueryClientContext)
  const subscribe = useCallback(
    (onChange: () => void) => !watch || !client
      ? () => {}
      : client.getQueryCache().subscribe(event => {
        if (event.query.queryKey[0] === VERSION_NOTICE_DISMISSALS_QUERY_KEY[0]) onChange()
      }),
    [watch, client],
  )
  const data = useSyncExternalStore(subscribe, () => watch && client
    ? client.getQueryData<VersionNoticeDismissals>(VERSION_NOTICE_DISMISSALS_QUERY_KEY)
    : undefined)
  return useCallback(
    (m: ChatMessage) => {
      const subject = untestedVersionSubject(m)
      return subject !== null && versionNoticeDismissed(data, subject)
    },
    [data],
  )
}

/**
 * The untested-version notice with a "Don't show again" link after its copy.
 * The dismissal is stored on the gateway for this harness at this release, so
 * every device hides it, and a session started later at the same release writes
 * no notice at all. A different installed release writes a new notice. Until
 * the first read answers the row draws nothing, so a dismissed notice never
 * flashes on a page load; when the read fails the notice shows, so a lost read
 * costs one more notice, never a hidden warning. Only the owner may dismiss, so
 * a viewer the gateway refuses gets the notice without the link.
 */
export default memo(function UntestedVersionNotice({
  content,
  subject,
  askAgent,
}: {
  content: string
  subject: UntestedVersionSubject
  /** Whether the save-failure notice may offer the agent hand-off. The host
   *  turns it off when navigating would drop its unsaved composer draft. */
  askAgent: boolean
}) {
  useLanguageGeneration()
  const queryClient = useQueryClient()
  const { data, error, isPending } = useVersionNoticeDismissals()
  const dismiss = useMutation({
    mutationFn: () => api.dismissVersionNotice(subject.harness, subject.version),
    onSuccess: next => queryClient.setQueryData(VERSION_NOTICE_DISMISSALS_QUERY_KEY, next),
  })
  if (isPending || versionNoticeDismissed(data, subject)) return null
  const owner = !isOwnerOnlyRefusal(error)
  return (
    <>
      <NoticeCard
        content={content}
        action={owner ? (
          <button
            type="button"
            data-testid="untested-version-notice-dismiss"
            aria-label={i18nT('pages.chat.untestedVersionNotice.dismiss_aria', {
              harness: subject.harness,
              version: subject.version,
            })}
            disabled={dismiss.isPending}
            className="whitespace-nowrap underline underline-offset-2 hover:text-text focus-visible:text-text disabled:opacity-60"
            onClick={() => dismiss.mutate()}
          >
            {i18nT('pages.chat.untestedVersionNotice.dismiss')}
          </button>
        ) : undefined}
      />
      {/* No hand-off when the host passes askAgent={false}: the navigation would
          drop the host's own unsaved composer draft. */}
      <ErrorNotice
        variant="inline"
        message={dismiss.isError ? i18nT('pages.chat.untestedVersionNotice.dismiss_failed') : null}
        askAgent={askAgent}
        testId="untested-version-notice-error"
        className="mt-1"
      />
    </>
  )
})
