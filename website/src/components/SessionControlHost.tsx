/**
 * SessionControlHost — renders one app-contributed session control in a popover.
 *
 * Mirrors AppHost's loading contract exactly (lazy ESM import → error boundary
 * → Suspense → AppApiProvider), with three differences that matter because this
 * renders inside the composer rather than on a page of its own:
 *
 * 1. **Failure must be local.** A page that throws costs the user that page. A
 *    control that throws sits on the path of every turn, so the boundary here
 *    renders a compact inline notice and leaves the chat untouched — it never
 *    unmounts anything but itself.
 * 2. **The session identity is the payload.** The control is handed
 *    `{ sessionKey, folderId, folderName, cwd }` — the grain apps mis-bind on
 *    today, and nothing more. That is the whole reason the slot exists: an app
 *    cannot discover the active session key from its own page. Fields no
 *    control has named a use for are deliberately absent, because this is a
 *    third-party contract and adding one later is cheaper than removing it.
 * 3. **It is keyed on the session.** Switching chats remounts the control, so a
 *    control holding per-session state cannot leak it across a switch.
 *
 * A control that declares `dock` is drawn in the composer dock instead, between
 * the chat and the message box, under a header the host draws (the app's name,
 * the control's label, a close button), in the dashboard's wide form only: a
 * phone keeps the popover. With `stayOpen` too, a press outside the docked box
 * leaves it open: the person reads and scrolls the chat above while it stays.
 *
 * @module components/SessionControlHost
 */
import React, { Suspense, forwardRef, lazy, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { OverlayPortal, useLocalCrewHidden } from './overlayRoot'
import { useNavigate } from 'react-router-dom'
import { RefreshCw, X } from 'lucide-react'
import ErrorNotice from './ErrorNotice'
import Glass from './Glass'
import { Btn } from './ui'
import { useDocumentImeLatch } from '../hooks/useImeGuard'
import { AppApiProvider } from '../app-sdk'
import { SessionControlRequestScope, sessionControlRequestSeq } from '../app-sdk/sessionControlRequest'
import { CLICK_GRANT_MS } from '../linkPreview/linkPreviewFrames'
import { clickOpenTimeLeft } from '../linkPreview/linkPreviewNav'
import { AppFileOpenRoot } from './appFileOpen'
import { i18nT } from '../i18n/t'
import { SESSION_CONTROL_WIDTH, sessionControlChip, type ResolvedSessionControl } from '../hooks/useSessionControls'
import { MOBILE_BREAKPOINT, useIsMobile } from '../hooks/useIsMobile'
import { withBase } from '@/lib/basePath'

/** A matched label in a chat message: a click on one shows its card, which may
 *  open this popover (a link's click follows the link, and never does). */
const LINK_PREVIEW_LABEL = '[data-link-preview-scope] [data-link-preview-text]'
/** How often a held close looks again whether a label press may still open. */
const HOLD_RECHECK_MS = 250

// ---------------------------------------------------------------------------
// Session identity handed to the control
// ---------------------------------------------------------------------------

export interface SessionControlContext {
  /** Session key, e.g. `dashboard:chat-2-1787502679`. Empty pre-slot. */
  sessionKey: string
  /**
   * Folder the chat is filed in, or '' when it is at the top level.
   * A folder is a dashboard grouping with its own id — not a directory — so an
   * app storing a per-folder setting must key on this and not on `cwd`.
   */
  folderId?: string
  /** Folder's display name, for an app that wants to name it back to the user. */
  folderName?: string
  /** Working directory recorded for the session, when known. */
  cwd: string
}

export interface SessionControlHostProps {
  control: ResolvedSessionControl
  session: SessionControlContext
  anchorRect: DOMRect | null
  onClose: () => void
  /** Where a docked box is drawn: the composer dock's `SessionControlDockSlot`. */
  dockSlot?: HTMLElement | null
}

/**
 * The composer dock's place for an app's docked box (`dock`), in the status
 * stack: its own content column and layer, empty (no height) while no docked box
 * is open. `SessionControlHost` portals the box into it.
 */
export const SessionControlDockSlot = forwardRef<HTMLDivElement>(function SessionControlDockSlot(_props, ref) {
  return (
    // `min-h-0`, a flex column: in the stack's flex form the slot is the one
    // child that shrinks, and the box in it with it.
    <div ref={ref} className="px-4 mx-auto w-full relative z-[2] min-h-0 flex flex-col" style={{ maxWidth: 'var(--mc-content-width, 900px)' }} data-testid="session-control-dock-slot" />
  )
})

// ---------------------------------------------------------------------------
// Error boundary — deliberately narrow
// ---------------------------------------------------------------------------

interface EBProps {
  label: string
  appName: string
  onReset: () => void
  children: React.ReactNode
}
interface EBState {
  error: Error | null
}

class ControlErrorBoundary extends React.Component<EBProps, EBState> {
  state: EBState = { error: null }

  static getDerivedStateFromError(error: Error): EBState {
    return { error }
  }

  componentDidCatch(error: Error) {
    // Named loudly: a third-party control failing inside the composer is worth
    // a console entry even though it is contained.
    // eslint-disable-next-line no-console -- surface control crashes for debugging
    console.error(`[SessionControl] ${this.props.appName} control crashed:`, error)
  }

  render() {
    if (!this.state.error) return this.props.children
    return (
      <div className="p-3 w-[280px]">
        <ErrorNotice
          title={i18nT('components.sessionControlHost.failed', { label: this.props.label })}
          message={this.state.error.message}
          askAgent
          className="mb-2"
        />
        <p className="text-[10px] text-muted mb-2">
          {i18nT('components.sessionControlHost.chat_unaffected')}
        </p>
        <button
          className="inline-flex items-center gap-1.5 text-[11px] text-muted hover:text-text px-2 py-1 rounded border border-border bg-transparent cursor-pointer"
          onClick={() => {
            this.setState({ error: null })
            this.props.onReset()
          }}
          title={i18nT('components.sessionControlHost.retry_loading', { label: this.props.label })}
          aria-label={i18nT('components.sessionControlHost.retry_loading', { label: this.props.label })}
        >
          <RefreshCw size={11} /> {i18nT('components.sessionControlHost.retry')}
        </button>
      </div>
    )
  }
}

// ---------------------------------------------------------------------------
// Host
// ---------------------------------------------------------------------------

export default function SessionControlHost({
  control,
  session,
  anchorRect,
  onClose,
  dockSlot = null,
}: SessionControlHostProps) {
  const [resetKey, setResetKey] = useState(0)
  const popRef = useRef<HTMLDivElement | null>(null)
  const navigate = useNavigate()
  // A docked box sits in the composer dock, not on the chip, so it needs no
  // anchor; only a docked box may stay open (the manifest refuses `stayOpen`
  // alone, and the dashboard reads it the same way). It docks only in the
  // dashboard's wide form: on a phone the control is the popover, as before.
  const narrow = useIsMobile()
  const docked = control.dock === true && !narrow
  const stays = docked && control.stayOpen === true
  // A dock-capable box drawn as a popover (narrow) before its chip was measured,
  // as when the window narrows under a box that was docked, anchors on its chip
  // as drawn now. A popover that never docks waits for the measured chip, as
  // before; with no chip on screen there is nothing to anchor on.
  const anchor = anchorRect ?? (control.dock === true && !docked ? sessionControlChip(control.key)?.getBoundingClientRect() ?? null : null)
  // The app's module renders into one element the host keeps for the box's life,
  // and whichever frame is drawn (the dock's or the popover) holds it. Crossing
  // the breakpoint moves that element between frames, so the module is never
  // remounted and keeps its state, its fields' contents and its focus.
  const [bodyEl] = useState(() => {
    const el = document.createElement('div')
    el.style.display = 'contents'
    return el
  })
  const movedFocus = useRef<HTMLElement | null>(null)
  const holdBody = useCallback((frame: HTMLElement | null) => {
    if (!frame) {
      // The frame is going: remember focus inside the module, before the move drops it.
      const active = document.activeElement
      movedFocus.current = active instanceof HTMLElement && bodyEl.contains(active) ? active : null
      return
    }
    if (bodyEl.parentNode !== frame) frame.appendChild(bodyEl)
    const back = movedFocus.current
    movedFocus.current = null
    if (back && bodyEl.contains(back) && document.activeElement !== back) back.focus()
  }, [bodyEl])
  const popupFrame = useCallback((el: HTMLDivElement | null) => {
    popRef.current = el
    holdBody(el)
  }, [holdBody])
  // Tracks IME composition anywhere inside the dialog, so the Escape handler
  // below can tell "dismiss the dialog" from "cancel a composition candidate".
  // Keyed on the box being mounted, which is exactly when Escape is ours.
  const imeLatch = useDocumentImeLatch(!!anchor || docked)

  const bundlePath = withBase(`/apps/${control.appName}/ui/${control.entryPoint}`)

  const LazyControl = useMemo(
    () =>
      lazy(() =>
        import(/* @vite-ignore */ bundlePath).catch(err => {
          // eslint-disable-next-line no-console -- surface bundle load failures for debugging
          console.error(
            `[SessionControl] failed to load ${control.appName}/${control.id} from ${bundlePath}:`,
            err
          )
          // Resolve with a component rather than rejecting: a rejected lazy
          // throws to the nearest boundary on every render attempt, and the
          // message here is more useful than a bare chunk-load error.
          return {
            default: () => (
              <div className="p-3 w-[280px]">
                <ErrorNotice
                  title={i18nT('components.sessionControlHost.could_not_load', { label: control.label })}
                  message={String(err?.message || err)}
                  askAgent
                  messageClassName="break-all"
                />
              </div>
            ),
          }
        })
      ),
    [bundlePath, resetKey] // eslint-disable-line react-hooks/exhaustive-deps
  )

  // Close on outside click / Escape, matching the sibling pill popovers. Not
  // while its Crew is hidden behind a remote one (components/overlayRoot): closing
  // discards the draft, and those keys belong to the Crew in front.
  const crewHidden = useLocalCrewHidden()
  // A press on a label a link-preview card answers may be the person asking
  // this app to open this popover again: the card's open arrives after the
  // click has shown it, and the show may first wait for its frame. So that press
  // holds the close for `CLICK_GRANT_MS`, then, looking every
  // `HOLD_RECHECK_MS`, while the button is still down or a click show may
  // still open something (`clickOpenTimeLeft`), and closes only if no new open
  // came in: an open request wins, and the control is never unmounted between
  // two of them. Any later press (in the popover, on its chip) ends the hold.
  // Refs, since `onClose` is new every render.
  const hold = useRef<number | undefined>(undefined)
  const pressing = useRef(false)
  const closeRef = useRef(onClose)
  closeRef.current = onClose
  // A docked box closed from inside it hands focus back to its chip, so a
  // keyboard user is not dropped on the page body.
  const closeDocked = useCallback(() => {
    const inside = !!popRef.current?.contains(document.activeElement)
    onClose()
    if (inside) sessionControlChip(control.key)?.focus()
  }, [onClose, control.key])
  useEffect(() => () => window.clearTimeout(hold.current), [])
  // Whether focus is inside a box that stays open: only then does it answer
  // Escape, so only then is it the Escape owner the composer's claimants defer
  // to (`chat-input/escapeOwners.ts`). A docked box that does not stay open
  // answers every Escape, as a popover does, and owns it while open.
  const [focusInBox, setFocusInBox] = useState(false)
  useEffect(() => {
    if (!stays) return
    const onIn = (e: FocusEvent) => setFocusInBox(!!popRef.current?.contains(e.target as Node))
    const onOut = (e: FocusEvent) => setFocusInBox(!!e.relatedTarget && !!popRef.current?.contains(e.relatedTarget as Node))
    setFocusInBox(!!popRef.current?.contains(document.activeElement))
    document.addEventListener('focusin', onIn)
    document.addEventListener('focusout', onOut)
    return () => {
      document.removeEventListener('focusin', onIn)
      document.removeEventListener('focusout', onOut)
    }
  }, [stays])
  const ownsEscape = docked && (!stays || focusInBox)
  useEffect(() => {
    if (crewHidden) return
    const onDown = (e: MouseEvent) => {
      const target = e.target as HTMLElement | null
      window.clearTimeout(hold.current)
      // The triggering chip is not "outside". mousedown fires before click, so
      // closing here would let the chip's own toggle re-open it — clicking an
      // open chip would flicker rather than dismiss.
      if (target?.closest?.('[data-session-control-chip]')) return
      // The Back pill after a jump only scrolls the chat back: the box the
      // person jumped from stays open.
      if (target?.closest?.('[data-session-control-keep]')) return
      if (!popRef.current || popRef.current.contains(e.target as Node)) return
      if (target?.closest?.(LINK_PREVIEW_LABEL)) {
        const seen = sessionControlRequestSeq(session.sessionKey, control.key)
        pressing.current = true
        const wait = (ms: number) => {
          hold.current = window.setTimeout(() => {
            if (sessionControlRequestSeq(session.sessionKey, control.key) !== seen) return
            // The button still down: the click, and so its show, has not
            // happened yet. Otherwise look again soon while a show may still
            // open something, so the close follows the frame's real ready.
            if (pressing.current || clickOpenTimeLeft() > 0) wait(HOLD_RECHECK_MS)
            else closeRef.current()
          }, ms)
        }
        wait(CLICK_GRANT_MS)
        return
      }
      onClose()
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      // A box that stays open is the person's to close: an Escape meant for the
      // message box or the chat below it passes; one inside the box closes it.
      if (stays && !popRef.current?.contains(e.target as Node)) return
      // A docked box answers only an Escape no one else took: the composer's
      // claimants (stop the turn, discard a dictation) preventDefault theirs.
      if (docked && e.defaultPrevented) return
      // An IME user presses Escape to cancel a composition candidate, not to
      // dismiss the dialog — and closing here unmounts the control, discarding
      // an unsaved draft with nothing to restore it. The latch owns that
      // distinction (and the stopPropagation/preventDefault split that goes
      // with declining a key), so a declined Escape must simply not close.
      if (!imeLatch.claimKey(e)) return
      if (docked) closeDocked()
      else onClose()
    }
    const onUp = () => { pressing.current = false }
    // A box that stays open takes no outside press: the chat above it keeps
    // its clicks, and the box its draft. Its chip, its close button, Escape in
    // it and its key still close it.
    if (!stays) document.addEventListener('mousedown', onDown)
    document.addEventListener('mouseup', onUp, true)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('mouseup', onUp, true)
      document.removeEventListener('keydown', onKey)
    }
  }, [onClose, imeLatch, crewHidden, session.sessionKey, control.key, docked, stays, closeDocked])

  const subscribeFn = useCallback((event: string, cb: (data: unknown) => void) => {
    const handler = (e: Event) => cb((e as CustomEvent).detail)
    window.addEventListener(`mc:app:${event}`, handler)
    return () => window.removeEventListener(`mc:app:${event}`, handler)
  }, [])

  const navigateFn = useCallback((path: string) => {
    // Navigating away from the composer closes the popover first, otherwise it
    // would linger over the destination. Uses react-router `navigate`, mirroring
    // AppHost's own navigateFn — the dashboard has no `mc:navigate` listener.
    onClose()
    navigate(path)
  }, [onClose, navigate])

  const notifyFn = useCallback(
    (message: string, opts?: { type?: 'info' | 'success' | 'error' }) => {
      window.dispatchEvent(new CustomEvent('mc:notify', { detail: { message, ...opts } }))
    },
    []
  )

  if (!anchor && !docked) return null

  // Global chat-jump chords are deliberately NOT input-gated for digits
  // (`useKeyboardShortcuts.ts:763`, "Digits keep firing in inputs"), and a
  // slot switch unmounts this host — so Ctrl/Alt+digit while typing in a
  // control would switch chats and discard an unsaved draft with nothing to
  // restore it. Isolate every key here EXCEPT Escape, which must still
  // reach the document listener above that closes the popover.
  //
  // This sits on the dialog rather than on the window handler because the
  // control's own key handlers are React descendants and therefore already
  // ran by the time this fires: isolating here costs the control nothing,
  // while stopping the native event before it reaches `window` is what
  // actually prevents the switch.
  const isolateKeys = (e: React.KeyboardEvent) => { if (e.key !== 'Escape') e.stopPropagation() }
  const name = i18nT('components.sessionControlHost.dialog_label', {
    label: control.label,
    appName: control.appDisplayName,
  })
  const body = (
    <ControlErrorBoundary
      label={control.label}
      appName={control.appName}
      onReset={() => setResetKey(k => k + 1)}
    >
      <AppApiProvider
        appName={control.appName}
        appVersion={control.appVersion}
        allowedApiPaths={control.allowedApi}
        allowedEvents={control.allowedEvents}
        subscribeFn={subscribeFn}
        navigateFn={navigateFn}
        notifyFn={notifyFn}
        /* The identity the control is scoped to, sent as `X-Session-Key` on
           every scoped request. Without it the backend's restricted-session
           guard fails open, so a control opened in an incognito chat would be
           allowed the persistent writes incognito exists to deny. The control
           already receives this same key as a prop; this is the half the
           SERVER needs. */
        sessionKey={session.sessionKey}
      >
        <Suspense
          fallback={
            <div className="p-3 text-[11px] text-muted">
              {i18nT('components.sessionControlHost.loading', { label: control.label })}
            </div>
          }
        >
          {/* Keyed on the session: a control holding per-session state must
              not carry it across a chat switch. The scope tells
              `useSessionControlRequest()` which chat and control it is.
              `AppFileOpenRoot` serves `useOpenFile`, approved by the owner, on a
              click in the control itself: the error boundary's retry stays
              outside it, and a chat switch drops its click. */}
          <AppFileOpenRoot key={session.sessionKey} granted={control.openFiles}>
            <SessionControlRequestScope sessionKey={session.sessionKey} control={control.key}>
              <LazyControl key={session.sessionKey} session={session} onClose={docked ? closeDocked : onClose} />
            </SessionControlRequestScope>
          </AppFileOpenRoot>
        </Suspense>
      </AppApiProvider>
    </ControlErrorBoundary>
  )

  // The module, in the element the drawn frame holds (`holdBody`). Its keys are
  // isolated here, as the frame's are: React carries them up this tree, not the
  // frame's.
  const content = createPortal(
    // eslint-disable-next-line jsx-a11y/no-static-element-interactions -- stopPropagation only, as on the dialog below: it isolates global chords from the control, and has no gesture of its own to mirror.
    <div style={{ display: 'contents' }} onKeyDown={isolateKeys}>{body}</div>,
    bodyEl,
  )

  if (docked) {
    // In the composer dock's status stack, above Crew's own question and
    // approval cards and the message box, which stay below it and uncovered.
    // The dock's height is the transcript's bottom padding, so the reply being
    // read can always be scrolled into view above the box. The header is the
    // host's, outside the app's tree: it names the app, so the box cannot pass
    // for one of Crew's own cards, and its close button always works.
    const frame = (
      <Glass
        ref={popRef}
        variant="panel"
        thickness="thick"
        radius={12}
        role="region"
        aria-label={name}
        data-session-control-dock={control.key}
        data-escape-owner={ownsEscape ? '' : undefined}
        className="glass-shadow animate-slide-up mb-2 flex flex-col overflow-hidden min-h-0 max-h-[min(40svh,560px)]"
        onKeyDown={isolateKeys}
      >
        <div className="flex items-center gap-2 h-8 pl-3 pr-1 shrink-0 border-b border-border text-muted-strong">
          <span className="flex-1 min-w-0 truncate text-[12px]">{name}</span>
          <Btn
            type="button"
            className="border-none px-1.5 py-1 text-muted hover:text-text"
            onClick={closeDocked}
            aria-label={i18nT('components.sessionControlHost.close_named', { label: control.label })}
            title={i18nT('components.sessionControlHost.close_named', { label: control.label })}
          >
            <X size={14} />
          </Btn>
        </div>
        <div ref={holdBody} className="flex-1 min-h-0 overflow-y-auto overscroll-contain" />
      </Glass>
    )
    return <>{content}{dockSlot ? createPortal(frame, dockSlot) : frame}</>
  }
  if (!anchor) return null

  // Anchor above the trigger, clamped into the viewport — same approach the
  // approval-mode dropdown uses. The width is clamped too: a fixed 340 overflows
  // any viewport narrower than 356, where `left` has already bottomed out at its
  // 8px minimum and cannot absorb the difference, so the right edge would clip
  // off-screen with no way to scroll to it. Floored at 0 so an absurd viewport
  // (jsdom defaults, a measurement taken mid-teardown) cannot yield a negative
  // width, which is an invalid CSS length rather than a narrow popover.
  // An app may ask for more room on a desktop window (`control.width`, capped
  // at install); a phone keeps the default.
  const wanted = window.innerWidth >= MOBILE_BREAKPOINT ? control.width || SESSION_CONTROL_WIDTH.default : SESSION_CONTROL_WIDTH.default
  const width = Math.max(0, Math.min(wanted, window.innerWidth - 16))
  const left = Math.max(8, Math.min(anchor.left, window.innerWidth - width - 8))
  const bottom = window.innerHeight - anchor.top + 6

  return <>{content}<OverlayPortal>{
    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- stopPropagation only: the dialog is a container that isolates global chords from the control inside it, so there is no gesture here to mirror with a keyboard equivalent. The control's own interactive elements carry their own handlers and are unaffected.
    <div
      ref={popupFrame}
      role="dialog"
      aria-label={name}
      className="fixed z-[9999] animate-slide-up rounded-lg bg-bg-elevated border border-border shadow-lg overflow-hidden"
      style={{ bottom, left, width }}
      onKeyDown={isolateKeys}
    />
  }</OverlayPortal></>
}
