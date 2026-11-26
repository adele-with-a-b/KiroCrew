/**
 * Whether an event on Crew's page is the person's own press: made by the
 * browser from their keyboard or pointer (`isTrusted`), not dispatched by any
 * script. The answering mode sends only on such a press, so no message, relay
 * or re-dispatched key can stand in for it. Its own module so tests that drive
 * the page with script-made events can stand in for the browser here alone.
 *
 * @module linkPreview/answering/personPress
 */
export function isPersonPress(e: Event): boolean {
  return e.isTrusted === true
}
