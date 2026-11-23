/**
 * useAppStatusItems — discovers and polls app-contributed top-bar readouts.
 *
 * Apps declare these in `app.json` under `contributes.statusItems[]` (the
 * backend half is `kiro_crew/apps/manifest_status_items.py`). A status item is a
 * glanceable value the operator wants in view on every page — a countdown, a
 * health word — so the manifest carries only what is fixed (id, label, icon,
 * route) and the value comes from polling the app's own route about once a
 * minute. Nothing here knows any app: which app, which label and which route all
 * come from the installed-app list.
 *
 * @module hooks/useAppStatusItems
 */
import { useQueries, useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import { safeStatusPath } from './useSessionControls'
import {
  cleanStatusText,
  MAX_STATUS_HEADING,
  normalizeStatusGo,
  normalizeStatusRows,
  type StatusGo,
  type StatusRow,
} from '../shell/topbar/statusGo'

/** Hard cap on status items DRAWN across ALL apps.
 *
 * The backend caps each app at 2; this caps the bar as a whole. The top bar is
 * one row shared with the readout capsule and the bell, so N apps must not be
 * able to grow it without bound. The cap counts drawable readings, so an item
 * answering "nothing to show" gives its slot to the next one; past the cap
 * items are dropped, in the stable key order below, rather than wrapped.
 */
export const MAX_STATUS_ITEMS = 4

/** Mirrors of the install-time limits (`manifest_status_items.py`), so a
 *  hand-edited manifest cannot draw or poll more than an installed one could. */
export const MAX_STATUS_ITEMS_PER_APP = 2
export const MAX_STATUS_LABEL = 40

/** How often each item's route is polled. "About once a minute" is the contract
 *  the manifest reference states; an app that needs fresher numbers belongs on
 *  its own page, not in a shared bar. */
export const STATUS_ITEM_POLL_MS = 60_000

/** Query-key root shared by every status-item probe. */
export const APP_STATUS_ITEM_KEY = 'app-status-item'

/** Longest pill text drawn, in grapheme clusters. The pill shares a row with the
 *  readout capsule, so the value must stay a glance: `54m`, `↓ 38m left`. */
export const MAX_STATUS_TEXT = 24

/** Longest explanation line drawn in the detail card. */
export const MAX_STATUS_TOOLTIP = 200

/** Tones the dashboard draws. An unknown value is drawn `muted`. */
export type StatusItemTone = 'ok' | 'warn' | 'danger' | 'muted'
const KNOWN_TONES = new Set<string>(['ok', 'warn', 'danger', 'muted'])

export interface ResolvedStatusItem {
  /** Composite key, unique across apps: `${appName}:${item.id}`. */
  key: string
  appName: string
  appDisplayName: string
  id: string
  /** Accessible name and the detail card's row heading. Never drawn on the pill. */
  label: string
  icon: string
  /** Backend route, already through the same allowlist as a session control's. */
  statusPath: string
  /** Process-backed apps are served at `/apps/<app>/api/`, hook apps at `/api/apps/<app>/`. */
  processBacked: boolean
}

/** One answer from an item's route, normalized. */
export interface StatusItemValue {
  text: string
  tone: StatusItemTone
  tooltip: string
  /** The card's line over the rows ("3 questions waiting"); empty = none. */
  heading: string
  /** Places the card lists, each optionally a way to go there. */
  rows: StatusRow[]
  /** The one primary place to go, which the go key fires; null = none. */
  go: StatusGo | null
}

interface AppLike {
  name?: string
  displayName?: string
  enabled?: boolean
  manifest?: {
    displayName?: string
    contributes?: {
      statusItems?: { id?: string; label?: string; icon?: string; statusPath?: string }[]
    }
    backend?: { entryPoint?: string }
  }
}

/**
 * Flatten installed apps into drawable status items.
 *
 * Exported for tests: the filtering is the whole behaviour, and a stale or
 * hand-edited manifest must not be able to put an unbounded or unkeyed item in
 * the bar. The server validates at install; this re-checks only what rendering
 * depends on.
 */
export function resolveStatusItems(apps: AppLike[]): ResolvedStatusItem[] {
  const out: ResolvedStatusItem[] = []
  const seen = new Set<string>()
  for (const app of Array.isArray(apps) ? apps : []) {
    if (!app || typeof app.name !== 'string' || !app.name) continue
    // `enabled === false` is explicit; older payloads omit it and are enabled.
    if (app.enabled === false) continue
    const declared = app.manifest?.contributes?.statusItems
    if (!Array.isArray(declared)) continue
    let perApp = 0
    for (const item of declared) {
      if (!item || typeof item !== 'object') continue
      const id = typeof item.id === 'string' ? item.id : ''
      const label = cleanStatusText(item.label, Infinity)
      const statusPath = safeStatusPath(item.statusPath)
      // All three are required server-side. Without a route there is no value to
      // draw, and without a label the pill has no accessible name.
      if (!id || !label || !statusPath) continue
      if (Array.from(label).length > MAX_STATUS_LABEL) continue
      const key = `${app.name}:${id}`
      if (seen.has(key)) continue
      seen.add(key)
      if (++perApp > MAX_STATUS_ITEMS_PER_APP) break
      out.push({
        key,
        appName: app.name,
        appDisplayName: app.manifest?.displayName || app.displayName || app.name,
        id,
        label,
        icon: typeof item.icon === 'string' ? item.icon : '',
        statusPath,
        processBacked: Boolean(app.manifest?.backend?.entryPoint),
      })
    }
  }
  // Byte order, not `localeCompare`: these are machine keys, and the pill order
  // must not change with the UI language.
  out.sort((a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : 0))
  return out
}

/**
 * Normalize one route answer, or `null` when it has nothing to draw.
 *
 * Exported for tests: an app is a third party, so every field is untrusted and
 * the fallbacks are the behaviour. An empty `text` hides the pill — an app says
 * "nothing worth showing" that way, rather than with a blank pill.
 */
export function normalizeStatusItemValue(raw: unknown): StatusItemValue | null {
  const r = (raw ?? {}) as { text?: unknown; tone?: unknown; tooltip?: unknown; heading?: unknown; rows?: unknown; go?: unknown }
  const text = cleanStatusText(r.text, MAX_STATUS_TEXT)
  if (!text) return null
  const tone = typeof r.tone === 'string' && KNOWN_TONES.has(r.tone) ? (r.tone as StatusItemTone) : 'muted'
  return {
    text,
    tone,
    tooltip: cleanStatusText(r.tooltip, MAX_STATUS_TOOLTIP),
    heading: cleanStatusText(r.heading, MAX_STATUS_HEADING),
    rows: normalizeStatusRows(r.rows),
    go: normalizeStatusGo(r.go),
  }
}

/** One item with its latest answer. */
export interface StatusItemReading {
  item: ResolvedStatusItem
  /** `null` while the first poll is in flight, after a failure, or for an empty answer. */
  value: StatusItemValue | null
  /** The probe's failure, or `null`. */
  error: Error | null
  /** When this item was last polled, answered or failed (epoch ms), or 0 before the first poll. */
  updatedAt: number
}

/** One app's readings, drawn as ONE control so the bar gains one button per app. */
export interface StatusItemGroup {
  appName: string
  appDisplayName: string
  readings: StatusItemReading[]
}

/** Module-level so React Query re-runs it only when the app list changes. */
function selectStatusItems(d: unknown): ResolvedStatusItem[] {
  const apps = Array.isArray(d) ? d : (d as { apps?: unknown[] })?.apps
  return resolveStatusItems((apps as AppLike[]) || [])
}
const NO_ITEMS: ResolvedStatusItem[] = []

/**
 * Fetch the declared items and poll each one's route.
 *
 * Fails closed: the top bar renders on every page, so an unreachable app list
 * draws no pills rather than an error in the bar. The app list's own failure is
 * reported where the list is the subject (the Apps pages); a permission refusal
 * is the true "no apps for you" state and is never an error. A failing ITEM is
 * different — its app is installed and declared a readout the operator expects
 * to see — so its reading carries the error and the pill says so.
 */
export function useAppStatusItems(): StatusItemGroup[] {
  // The shared ['apps'] key, so enabling, disabling or removing an app (which
  // invalidate it) adds or drops its pills without a reload.
  const { data } = useQuery({
    queryKey: ['apps'],
    queryFn: () => (typeof api?.listApps === 'function' ? api.listApps() : []),
    select: selectStatusItems,
  })
  const items = data ?? NO_ITEMS
  const results = useQueries({
    queries: items.map(item => ({
      // The item key first: it is the only field unique across items, and two
      // items of one app may share a route.
      queryKey: [APP_STATUS_ITEM_KEY, item.key, item.statusPath, item.processBacked],
      queryFn: async () => {
        if (typeof api?.appSessionStatus !== 'function') return null
        return normalizeStatusItemValue(await api.appSessionStatus(item.appName, item.statusPath, {}, item.processBacked))
      },
      refetchInterval: STATUS_ITEM_POLL_MS,
      // Stale after one interval, so a return to the tab, a remount or a
      // reconnect re-reads at once instead of showing the old value for up to a
      // minute (the shared client's default never goes stale).
      staleTime: STATUS_ITEM_POLL_MS,
      // A third-party app that is down must not be hammered from every page.
      retry: false,
    })),
  })
  const groups: StatusItemGroup[] = []
  let drawn = 0
  items.forEach((item, i) => {
    const r = results[i]
    // A failed refetch keeps the last answer in `data`; drawing it would show an
    // old value in its old tone for as long as the app is down.
    const failed = r?.isError === true
    const reading: StatusItemReading = {
      item,
      value: failed ? null : (r?.data ?? null),
      error: failed ? ((r?.error as Error | null | undefined) ?? null) : null,
      updatedAt: Math.max(r?.dataUpdatedAt ?? 0, r?.errorUpdatedAt ?? 0),
    }
    // Nothing to draw yet: neither an answer nor a failure. Skipped before the
    // cap, so a hidden item does not hold a slot.
    if (!reading.value && !reading.error) return
    if (drawn++ >= MAX_STATUS_ITEMS) return
    const last = groups[groups.length - 1]
    // `items` is sorted by key, whose prefix is the app name, so one app's items
    // are adjacent and grouping needs no map.
    if (last && last.appName === item.appName) last.readings.push(reading)
    else groups.push({ appName: item.appName, appDisplayName: item.appDisplayName, readings: [reading] })
  })
  return groups
}
