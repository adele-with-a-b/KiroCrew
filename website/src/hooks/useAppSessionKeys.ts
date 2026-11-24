import { useCallback, useEffect, useMemo, useState } from 'react'

import { shortcutPlatform, useShortcutBindings } from './useKeyboardShortcuts'
import type { ResolvedSessionControl } from './useSessionControls'
import {
  APP_SESSION_KEYS_EVENT,
  APP_SESSION_KEYS_KEY,
  chordFromKeyEvent,
  crewKeyOwner,
  loadAppKeyOverrides,
  resolveAppKeys,
  sameChord,
  setAppKeyBinding,
  type CrewKeyOwner,
  type ResolvedAppKey,
} from '../lib/appSessionKeys'
import { isValidChord, type Chord } from '../lib/shortcutRegistry'
import { loadPanelToggleOverrides, PANEL_TOGGLE_SHORTCUTS_EVENT, PANEL_TOGGLE_SHORTCUTS_KEY } from '../lib/panelToggleShortcuts'
import { loadQuickSearchConfig, QUICK_SEARCH_SHORTCUT_EVENT, QUICK_SEARCH_SHORTCUT_KEY } from '../lib/quickSearchShortcut'

/**
 * Every app key of `controls`, resolved live: re-read when the person changes an
 * app key, a panel toggle or the search key (this tab or another), and when the
 * registry's overrides change, so a key Crew takes later turns off at once.
 */
export function useAppSessionKeys(controls: readonly ResolvedSessionControl[]): ResolvedAppKey[] {
  const shortcuts = useShortcutBindings()
  const [overrides, setOverrides] = useState(loadAppKeyOverrides)
  const [panels, setPanels] = useState(loadPanelToggleOverrides)
  const [search, setSearch] = useState(loadQuickSearchConfig)
  useEffect(() => {
    const refresh = () => {
      setOverrides(loadAppKeyOverrides())
      setPanels(loadPanelToggleOverrides())
      setSearch(loadQuickSearchConfig())
    }
    const watched = new Set([APP_SESSION_KEYS_KEY, PANEL_TOGGLE_SHORTCUTS_KEY, QUICK_SEARCH_SHORTCUT_KEY])
    const onStorage = (e: StorageEvent) => { if (e.key && watched.has(e.key)) refresh() }
    const events = [APP_SESSION_KEYS_EVENT, PANEL_TOGGLE_SHORTCUTS_EVENT, QUICK_SEARCH_SHORTCUT_EVENT]
    for (const ev of events) window.addEventListener(ev, refresh)
    window.addEventListener('storage', onStorage)
    return () => {
      for (const ev of events) window.removeEventListener(ev, refresh)
      window.removeEventListener('storage', onStorage)
    }
  }, [])
  return useMemo(
    () => resolveAppKeys(controls, overrides, shortcutPlatform(), { shortcuts, panels, search }),
    [controls, overrides, shortcuts, panels, search],
  )
}

/** Who holds a chord a person tried to record: one of Crew's keys, or another app key. */
export type KeyHolder = { crew: CrewKeyOwner } | { app: ResolvedAppKey }

/** Settings → Shortcuts → Apps editor state, over {@link useAppSessionKeys}. */
export interface AppKeyRecorder {
  recordingId: string | null
  /** The last chord refused while recording, and who holds it; cleared on a new recording. */
  refused: { chord: Chord; holder: KeyHolder } | null
  startRecording: (id: string) => void
  cancelRecording: () => void
  clear: (id: string) => void
}

/**
 * The recorder: the panel toggles' own (capture-phase window keydown, Escape
 * cancels, a chord with no Mod/Ctrl/Alt keeps waiting), plus one refusal they do
 * not need. A key Crew or another app key holds is refused and named, and the
 * recorder keeps listening, so the old key stays until a free one lands.
 */
export function useAppKeyRecorder(keys: readonly ResolvedAppKey[]): AppKeyRecorder {
  const shortcuts = useShortcutBindings()
  const [recordingId, setRecordingId] = useState<string | null>(null)
  const [refused, setRefused] = useState<AppKeyRecorder['refused']>(null)
  const startRecording = useCallback((id: string) => { setRefused(null); setRecordingId(id) }, [])
  const cancelRecording = useCallback(() => { setRefused(null); setRecordingId(null) }, [])
  const clear = useCallback((id: string) => {
    setRefused(null)
    setRecordingId(null)
    setAppKeyBinding(id, null)
  }, [])

  useEffect(() => {
    if (recordingId === null) return
    const platform = shortcutPlatform()
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        e.stopPropagation()
        setRefused(null)
        setRecordingId(null)
        return
      }
      const chord = chordFromKeyEvent(e, platform)
      if (!chord || !isValidChord(chord)) return
      e.preventDefault()
      e.stopPropagation()
      const crew = crewKeyOwner(chord, platform, { shortcuts, panels: loadPanelToggleOverrides(), search: loadQuickSearchConfig() })
      const app = crew ? undefined : keys.find(k => k.id !== recordingId && k.chord && sameChord(k.chord, chord, platform))
      if (crew || app) {
        setRefused({ chord, holder: crew ? { crew } : { app: app! } })
        return
      }
      setAppKeyBinding(recordingId, chord)
      setRefused(null)
      setRecordingId(null)
    }
    window.addEventListener('keydown', onKeyDown, true)
    return () => window.removeEventListener('keydown', onKeyDown, true)
  }, [recordingId, keys, shortcuts])

  return { recordingId, refused, startRecording, cancelRecording, clear }
}
