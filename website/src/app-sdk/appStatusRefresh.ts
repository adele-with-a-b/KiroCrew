/**
 * useAppStatusRefresh — an app tells the dashboard its own status changed.
 *
 * The dashboard reads an app's status routes on its own schedule: a session
 * control's `statusPath` when its popover closes and when a turn ends, a top-bar
 * status item's about once a minute. A change the app makes on its own page or
 * side-panel tab starts no turn, so the chip would keep its old value and the pill
 * would lag. This hook asks the gateway to have every owner window re-read the
 * app's own status at once (`POST /api/apps/<app>/status-changed`).
 *
 * It takes no app name: the host binds it to the app it mounted, so an app can
 * only refresh its own status. Every call is delivered; two real changes seconds
 * apart are two re-reads. Needs `APP_SDK_LEVEL >= 10`.
 *
 * @module app-sdk/appStatusRefresh
 */
import { useCallback } from 'react'
import { api } from '../api/client'
import { useCtx } from './scopedApi'

/** What a refresh covers. */
export interface AppStatusRefreshOptions {
  /** Re-read this chat's chips only (`dashboard:chat-7`). Omit for every chat.
   *  The app's top-bar status items are re-read either way. */
  sessionKey?: string
}

/**
 * Refresh this app's status in every owner window.
 *
 * @returns A function that resolves once the gateway has sent the refresh, and
 *   rejects with the gateway's error (an app with no declared `statusPath`
 *   answers 404 `app_status_not_declared`).
 *
 * @example
 * const refreshStatus = useAppStatusRefresh()
 * await be.answerLater(label)
 * await refreshStatus({ sessionKey })
 */
export function useAppStatusRefresh(): (opts?: AppStatusRefreshOptions) => Promise<void> {
  const { info } = useCtx()
  const app = info.name
  return useCallback(async (opts?: AppStatusRefreshOptions) => {
    await api.appStatusChanged(app, opts?.sessionKey || undefined)
  }, [app])
}
