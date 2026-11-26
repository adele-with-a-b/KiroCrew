import { memo, useMemo, useState } from 'react'

import { usePathKind } from '../hooks/usePathKind'
import { i18nT } from '../i18n/t'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'
import { openFileMarkerPaths } from '../utils/detectOpenFileMarker'
import { fileIcon } from '../utils/fileIcons'
import { pathBasename } from '../utils/pathBasename'
import ErrorNotice from './ErrorNotice'
import { useRevealFailure } from './FilePathMenu'
import { activatePath } from './markdown/pathReferences'

/**
 * One "Open file" chip per file an assistant reply offers with an open-file
 * marker (`<!-- kirocrew:open-file path="/abs/notes.md" -->`, see
 * detectOpenFileMarker). The marker only decides that the chip is shown:
 * rendering, streaming and reloading the transcript open nothing.
 *
 * The marker's path is the agent's text, so a chip asks the gateway about it
 * first, through the same probe a path chip is drawn from (`usePathKind`,
 * `/api/file-kinds`, which reads a credential store or any other refused path
 * as `missing`). Only a path the gateway confirmed as a file goes through
 * `activatePath` (components/markdown/pathReferences.ts), exactly as a click
 * on a path chip does: the default app or the side panel as the setting says,
 * Alt+click the other one. Any other answer, or none yet, keeps the click this
 * chip had before the default app existed: the host's `onFileOpen` with the
 * path alone, so the side panel's own read decides, with its owner gate,
 * sensitive-path refusal and redaction, and the file opens as a plain file tab,
 * never a diff.
 *
 * Renders nothing without a handler (a host with no file viewer) or without a
 * usable marker.
 */
const OpenFileChips = memo(function OpenFileChips({ content, onFileOpen, slot }: {
  /** The reply's text as the bubble shows it. */
  content: string
  /** The host's file opener: the same function the reply's path chips call. */
  onFileOpen?: (path: string) => void
  /** The chat the reply is in, as its path chips get it (`slotKey`): a read
   *  made to open a file outside the side panel names it. Without one, a click
   *  that would read opens the side panel. */
  slot?: string
}) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const paths = useMemo(() => openFileMarkerPaths(content), [content])
  // A failed open is told under the chip that was clicked, and a click on any
  // chip clears it.
  const failure = useRevealFailure(content)
  const [failedPath, setFailedPath] = useState<string | null>(null)
  if (!onFileOpen || paths.length === 0) return null
  return (
    // One chip per row: these are peer actions, and a row of more than two
    // buttons is a toolbar (and wrapping one does not make it less of one).
    <div className="ft-block-reveal flex flex-col items-start gap-1.5 mt-2 mb-1.5" data-testid="open-file-chips">
      {paths.map(path => (
        <OpenFileChip
          key={path}
          path={path}
          onOpen={(other) => {
            failure.clear()
            setFailedPath(path)
            return { onFileOpen, slot, onError: failure.onError, other }
          }}
          error={failedPath === path ? failure.error : null}
          onDismiss={failure.clear}
        />
      ))}
    </div>
  )
})

/** One chip: probes its path, and opens it as the probe allows. */
function OpenFileChip({ path, onOpen, error, onDismiss }: {
  path: string
  /** Clears the row's notice and hands back what the click needs. */
  onOpen: (other: boolean) => { onFileOpen: (path: string) => void; slot?: string; onError: (message: string) => void; other: boolean }
  error: string | null
  onDismiss: () => void
}) {
  const kind = usePathKind(path)
  const Glyph = fileIcon(path)
  const name = pathBasename(path) || path
  return (
    <>
      <button
        type="button"
        onClick={e => {
          const { onFileOpen, slot, onError, other } = onOpen(e.altKey)
          // Not confirmed as a file (refused, missing, a directory, or the
          // probe still out): the side panel's read decides, as before.
          if (kind !== 'file') { onFileOpen(path); return }
          activatePath(path, 'file', false, { onFileOpen, slot }, onError, undefined, undefined, other)
        }}
        className="glass-surface file-chip inline-flex max-w-full min-w-0 min-h-[22px] h-auto items-center gap-1 px-2.5 py-1 whitespace-normal text-left rounded-full text-[11px] font-medium cursor-pointer"
        data-path={path}
        data-path-kind={kind}
        // The accessible name is the visible text ("Open file notes.md"), so
        // a voice command that reads the label reaches the chip; the full
        // path is the tooltip and, through it, the accessible description.
        title={path}
      >
        <Glyph size={12} aria-hidden="true" className="shrink-0 opacity-70" />
        <span className="shrink-0 text-text">{i18nT('components.openFileChips.open_file')}</span>
        {/* A space in the accessible name between the label and the file name;
            whitespace-only text is not a flex item, so the layout is unchanged. */}
        {' '}
        <span className="min-w-0 font-mono text-muted [overflow-wrap:anywhere]">{name}</span>
      </button>
      {/* askAgent on: a reply's chip row holds no draft. */}
      {error && (
        <ErrorNotice variant="inline" message={error} askAgent onDismiss={onDismiss} testId="open-file-chip-error" />
      )}
    </>
  )
}

export default OpenFileChips
