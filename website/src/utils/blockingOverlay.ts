/** An open modal dialog (ARIA-marked, or a Radix dialog in its open state)
 *  owns the keyboard, so a page-wide key may not act under it (push-to-talk's
 *  start, an app's session-control key). A Radix Popover also renders
 *  `role="dialog"` + `data-state="open"`, but it is non-modal and sits in a
 *  popper wrapper, so it does not count; nor does a plain `role="dialog"`
 *  popover such as a session control's box. */
export function hasBlockingOverlay(): boolean {
  if (document.querySelector('[aria-modal="true"]')) return true
  for (const el of document.querySelectorAll('[role="dialog"][data-state="open"]')) {
    if (!el.closest('[data-radix-popper-content-wrapper]')) return true
  }
  return false
}
