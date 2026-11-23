/**
 * Whether a React click came from the person (`isTrusted`), not from a script.
 *
 * On its own so a test can stand in a real click: jsdom marks every event it is
 * handed as untrusted, and `isTrusted` cannot be set on one.
 *
 * @module components/trustedClick
 */
import type { MouseEvent as ReactMouseEvent } from 'react'

export function isTrustedClick(e: ReactMouseEvent): boolean {
  return e.nativeEvent.isTrusted === true
}
