/**
 * "Open this status item's card": what `/chat?status=<app>:<item>` asks for.
 *
 * The chat page reads the URL; the card belongs to whichever status bar is
 * mounted (the desktop top bar or the phone chat header). This is the hand-off
 * between them. A request waits for that item's first answer for at most
 * {@link STATUS_CARD_WAIT_MS}, so a link opened before the app answered still
 * lands, and an item that never answers opens nothing. It only ever opens a
 * card: a URL never fires a go.
 *
 * @module shell/topbar/statusCardRequest
 */
import { useSyncExternalStore } from 'react'

/** How long a request waits for its item to have something to show. */
export const STATUS_CARD_WAIT_MS = 15_000

interface CardRequest {
  key: string
  at: number
}

let request: CardRequest | null = null
const listeners = new Set<() => void>()

function emit(): void {
  for (const fn of [...listeners]) fn()
}

/** Ask the mounted bar to open the card of item `key` (`${appName}:${item.id}`). */
export function requestStatusCard(key: string, now: number = Date.now()): void {
  request = { key, at: now }
  emit()
}

/**
 * Take the request for one of `keys` if it is still fresh: the caller opens
 * that card. A stale request is dropped.
 */
export function takeStatusCardRequest(keys: readonly string[], now: number = Date.now()): string | null {
  if (!request) return null
  if (now - request.at > STATUS_CARD_WAIT_MS) {
    request = null
    return null
  }
  if (!keys.includes(request.key)) return null
  const { key } = request
  request = null
  return key
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn)
  return () => { listeners.delete(fn) }
}

const getKey = () => request?.key ?? null

/** The key waiting to be opened, or null. Re-renders the bar when one arrives. */
export function usePendingStatusCard(): string | null {
  return useSyncExternalStore(subscribe, getKey, getKey)
}
