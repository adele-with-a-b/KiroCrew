/**
 * The insert a session control's box hands its module (`useInsertIntoComposer`).
 *
 * Only a control that may insert (`composerInsert`, declared with `dock` and
 * approved by the owner) gets one, so the SDK's hook is null for any other. The
 * insert answers only while its box is open, drawn in the dock and its Crew in
 * front: a box gone or behind a remote Crew answers `stale`, one drawn as a popover
 * (the narrow form) `not_allowed`, and so does a handle kept after the owner
 * withdrew the grant. It is one function for the box's life and reads all of this
 * through refs, so a handle the module kept still asks the box as it is now. The
 * act window (`INSERT_ACT_MS`) is the SDK hook's.
 *
 * @module components/sessionControlInsert
 */
import { useEffect, useMemo, useRef } from 'react'
import type { ScopeComposerInsert } from '../app-sdk/sessionControlRequest'

export function useDockedBoxInsert({ composerInsert, docked, crewHidden, insertIntoComposer }: {
  /** The control's resolved `composerInsert`: declared with `dock`, and approved. */
  composerInsert: boolean
  /** The box is drawn in the dock now, not as a popover. */
  docked: boolean
  /** Its Crew is behind a remote one. */
  crewHidden: boolean
  /** The composer's insert into the box's own chat. */
  insertIntoComposer?: ScopeComposerInsert
}): ScopeComposerInsert | undefined {
  const live = useRef({ composerInsert, docked, crewHidden, insertIntoComposer })
  live.current = { composerInsert, docked, crewHidden, insertIntoComposer }
  const open = useRef(false)
  useEffect(() => {
    open.current = true
    return () => { open.current = false }
  }, [])
  const insert = useMemo<ScopeComposerInsert>(() => (text) => {
    const now = live.current
    if (!open.current || now.crewHidden || !now.insertIntoComposer) return 'stale'
    if (!now.composerInsert || !now.docked) return 'not_allowed'
    return now.insertIntoComposer(text)
  }, [])
  return composerInsert ? insert : undefined
}
