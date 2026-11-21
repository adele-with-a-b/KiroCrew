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

/** Hard cap on status items drawn across ALL apps.
 *
 * The backend caps each app at 2; this caps the bar as a whole. The top bar is
 * one row shared with the readout capsule and the bell, so N apps must not be
 * able to grow it without bound. Items past the cap are dropped, in the stable
 * key order below, rather than wrapped.
 */
export const MAX_STATUS_ITEMS = 4

/** How often each item's route is polled. "About once a minute" is the contract
 *  the manifest reference states; an app that needs fresher numbers belongs on
 *  its own page, not in a shared bar. */
export const STATUS_ITEM_POLL_MS = 60_000

/** Query-key root shared by every status-item probe. */
export const APP_STATUS_ITEM_KEY = 'app-status-item'

/** Longest pill text drawn, in code points. The pill shares a row with the
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
    for (const item of declared) {
      if (!item || typeof item !== 'object') continue
      const id = typeof item.id === 'string' ? item.id : ''
      const label = typeof item.label === 'string' ? item.label.trim() : ''
      const statusPath = safeStatusPath(item.statusPath)
      // All three are required server-side. Without a route there is no value to
      // draw, and without a label the pill has no accessible name.
      if (!id || !label || !statusPath) continue
      const key = `${app.name}:${id}`
      if (seen.has(key)) continue
      seen.add(key)
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
  return out.slice(0, MAX_STATUS_ITEMS)
}

/** Strip what could reorder or hide text (the Unicode C category and the
 *  line/paragraph separators), then cap by code points so a pair never splits. */
function cleanText(raw: unknown, max: number): string {
  if (typeof raw !== 'string') return ''
  return Array.from(raw.replace(/[\p{C}\p{Zl}\p{Zp}]/gu, '').trim()).slice(0, max).join('')
}

/**
 * Normalize one route answer, or `null` when it has nothing to draw.
 *
 * Exported for tests: an app is a third party, so every field is untrusted and
 * the fallbacks are the behaviour. An empty `text` hides the pill — an app says
 * "nothing worth showing" that way, rather than with a blank pill.
 */
export function normalizeStatusItemValue(raw: unknown): StatusItemValue | null {
  const r = (raw ?? {}) as { text?: unknown; tone?: unknown; tooltip?: unknown }
  const text = cleanText(r.text, MAX_STATUS_TEXT)
  if (!text) return null
  const tone = typeof r.tone === 'string' && KNOWN_TONES.has(r.tone) ? (r.tone as StatusItemTone) : 'muted'
  return { text, tone, tooltip: cleanText(r.tooltip, MAX_STATUS_TOOLTIP) }
}

/** One item with its latest answer. */
export interface StatusItemReading {
  item: ResolvedStatusItem
  /** `null` while the first poll is in flight, after a failure, or for an empty answer. */
  value: StatusItemValue | null
  /** The probe's failure, or `null`. */
  error: Error | null
  /** When `value` was last fetched (epoch ms), or 0 before the first answer. */
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
      // A third-party app that is down must not be hammered from every page.
      retry: false,
    })),
  })
  const groups: StatusItemGroup[] = []
  items.forEach((item, i) => {
    const r = results[i]
    const reading: StatusItemReading = {
      item,
      value: r?.data ?? null,
      error: (r?.error as Error | null | undefined) ?? null,
      updatedAt: r?.dataUpdatedAt ?? 0,
    }
    // Nothing to draw yet: neither an answer nor a failure.
    if (!reading.value && !reading.error) return
    const last = groups[groups.length - 1]
    // `items` is sorted by key, whose prefix is the app name, so one app's items
    // are adjacent and grouping needs no map.
    if (last && last.appName === item.appName) last.readings.push(reading)
    else groups.push({ appName: item.appName, appDisplayName: item.appDisplayName, readings: [reading] })
  })
  return groups
}
