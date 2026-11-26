/**
 * Whether a React click came from the person (`isTrusted`), not from a script.
 *
 * On its own so a test can stand in a real click: jsdom marks every event it is
 * handed as untrusted, and `isTrusted` cannot be set on one.
 *
 * @module components/trustedClick
 */
import type { KeyboardEvent as ReactKeyboardEvent, MouseEvent as ReactMouseEvent } from 'react'

export function isTrustedClick(e: ReactMouseEvent): boolean {
  return e.nativeEvent.isTrusted === true
}

/** The same for a key press. */
export function isTrustedKey(e: ReactKeyboardEvent): boolean {
  return e.nativeEvent.isTrusted === true
}

/** The same for an event the host hears itself, off React (an app key on the document). */
export function isTrustedEvent(e: Event): boolean {
  return e.isTrusted === true
}
