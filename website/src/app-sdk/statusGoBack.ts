/**
 * `useStatusGoBack()`: the way back after a status item sent the operator to
 * the chat this app's tab is in (`contributes.statusItems`, a row's or the go's
 * `target`). A function while that go is on screen, else null. Call it once the
 * tab has done what it was opened for (say, the question was answered): the
 * dashboard switches back to the chat the operator left, as the header's crumb
 * does. Say so with `useNotify()` if it helps.
 *
 * @module app-sdk/statusGoBack
 */
import { useStatusGoBack as useHostStatusGoBack } from '../shell/topbar/statusGo'

export function useStatusGoBack(): (() => void) | null {
  return useHostStatusGoBack()?.back ?? null
}
