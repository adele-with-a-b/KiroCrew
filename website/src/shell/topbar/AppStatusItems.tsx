import { useEffect, useRef, useState, type CSSProperties, type PointerEvent } from 'react'

import ErrorNotice from '../../components/ErrorNotice'
import { KeyCapSequence } from '../../components/ShortcutsModal'
import { Popover, PopoverContent, PopoverTrigger } from '../../components/ui/popover'
import { appIcon } from '../../apps/appIcons'
import { useAppStatusItems, type StatusItemGroup, type StatusItemReading, type StatusItemTone } from '../../hooks/useAppStatusItems'
import { formatChordCaps, useShortcutBindings } from '../../hooks/useKeyboardShortcuts'
import { useShortcutsEnabled } from '../../hooks/useNavShortcutHint'
import { fmtList, fmtRelative } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { errMessage } from '../../utils/thunkError'
import { takeStatusCardRequest, usePendingStatusCard } from './statusCardRequest'
import { setPrimaryStatusGo, useStatusGo, type StatusGo, type StatusRow, type StatusTarget } from './statusGo'

/**
 * App-contributed top-bar readouts (`contributes.statusItems`): small pills on
 * the desktop top bar and in the phone chat header.
 *
 * Every app's items share ONE button holding the pills, so the bar gains one
 * control however many apps and items there are, and a tap or hover opens one
 * card saying what each value means, a section per app. The pill draws only what the app's route
 * answered (`{text, tone, tooltip}`) inside host chrome: the icon comes from the
 * fixed app icon set, the colour from one of four tones, and the label from the
 * manifest. An app cannot draw its own markup here.
 *
 * An answer may also list places (`heading`, `rows[]`) and one primary `go`.
 * The card then draws them as a list a tap goes to, oldest or most urgent
 * first as the app orders them, with the go button under it; the global
 * `status-go` key fires the first item's go in bar order. Where a target goes
 * is `statusGo.ts`.
 */

/** Theme token each tone tints with. `muted` draws the plain elevated chip. */
const TONE_VAR: Record<StatusItemTone, string> = {
  ok: 'var(--ok-text)',
  warn: 'var(--warn)',
  danger: 'var(--danger)',
  muted: 'var(--muted-strong)',
}

/** How long a hover-opened card stays after the pointer leaves, so the pointer
 *  can cross the gap into the card (its hand-off button, on a failure). */
const HOVER_CLOSE_MS = 150

function toneOf(r: StatusItemReading): StatusItemTone {
  return r.value?.tone ?? 'muted'
}

/** The value in words, for the card and the accessible name. */
function textOf(r: StatusItemReading): string {
  return r.value?.text ?? i18nT('app.status_item_unknown_value')
}

/** The value as the pill draws it: a bare `?` when it could not be read, the
 *  card and the accessible name saying it in words. */
function pillTextOf(r: StatusItemReading): string {
  return r.value?.text ?? '?'
}

/** One pill. `size` is the desktop bar (`md`) or the phone header (`sm`). */
function Pill({ reading, size }: { reading: StatusItemReading; size: 'md' | 'sm' }) {
  const tone = toneOf(reading)
  const style = { '--c': TONE_VAR[tone] } as CSSProperties
  const sizing = size === 'sm'
    ? 'h-[22px] gap-1 pl-1.5 pr-[7px] text-[12px] [&_svg]:size-3'
    : 'h-6 gap-[5px] pl-[7px] pr-[9px] text-[12.5px] [&_svg]:size-[13px]'
  const chrome = tone === 'muted'
    ? 'bg-bg-elevated border-border'
    : 'bg-[color-mix(in_srgb,var(--c)_14%,var(--bg-elevated))] border-[color-mix(in_srgb,var(--c)_45%,transparent)]'
  return (
    <span
      data-testid="app-status-pill"
      data-tone={tone}
      style={style}
      className={`inline-flex items-center min-w-0 rounded-full border font-medium tabular-nums whitespace-nowrap ${sizing} ${chrome} ${tone === 'danger' ? 'text-text-strong' : 'text-text'} [&_svg]:shrink-0 [&_svg]:text-[var(--c)]`}
    >
      <span aria-hidden="true" className="inline-flex">{appIcon(reading.item.icon)}</span>
      <span aria-hidden="true" className="min-w-0 truncate">{pillTextOf(reading)}</span>
    </span>
  )
}

/** The card's row for one item: icon, "Label: value", and the app's explanation. */
function CardRow({ reading }: { reading: StatusItemReading }) {
  const style = { '--c': TONE_VAR[toneOf(reading)] } as CSSProperties
  return (
    <div className="flex items-start gap-2 my-1.5" data-testid="app-status-card-row">
      <span aria-hidden="true" style={style} className="mt-[3px] inline-flex shrink-0 text-[var(--c)] [&_svg]:size-3.5">{appIcon(reading.item.icon)}</span>
      <div className="min-w-0">
        <span className="font-semibold text-text-strong">
          {i18nT('app.status_item_reading', { label: reading.item.label, value: textOf(reading) })}
        </span>
        {reading.value?.tooltip && <div className="text-[12px] text-muted">{reading.value.tooltip}</div>}
        {reading.error && (
          <ErrorNotice
            variant="inline"
            askAgent
            className="mt-1"
            title={i18nT('app.status_item_failed', { label: reading.item.label })}
            message={errMessage(reading.error)}
            testId="app-status-item-error"
          />
        )}
      </div>
    </div>
  )
}

type GoTo = (appName: string, target: StatusTarget) => void

/** One listed place: the lead in the item's tone, the name, then the line under it. */
function PlaceRow({ row, icon, onGo }: { row: StatusRow; icon: string; onGo: (() => void) | null }) {
  const style = { '--c': TONE_VAR[row.tone] } as CSSProperties
  const body = (
    <>
      <span
        style={style}
        data-tone={row.tone}
        className={`row-span-2 self-start mt-px inline-flex items-center gap-1 h-5 px-1.5 rounded-full border text-[11px] font-semibold tabular-nums whitespace-nowrap text-[var(--c)] [&_svg]:size-3 ${row.tone === 'muted' ? 'bg-bg-elevated border-border' : 'bg-[color-mix(in_srgb,var(--c)_14%,var(--bg-elevated))] border-[var(--c)]'}`}
      >
        <span aria-hidden="true" className="inline-flex">{appIcon(icon)}</span>
        {row.lead && <span>{row.lead}</span>}
      </span>
      {row.text && <span className="min-w-0 truncate font-semibold text-text-strong">{row.text}</span>}
      {row.detail && <span className="col-start-2 min-w-0 line-clamp-2 break-words text-[12px] text-muted">{row.detail}</span>}
    </>
  )
  const layout = 'grid grid-cols-[auto_1fr] gap-x-2 gap-y-0.5 w-full items-center text-left rounded-[var(--radius-md)] px-1.5 py-1.5'
  if (!onGo) return <div className={layout} data-testid="app-status-place">{body}</div>
  return (
    <button
      type="button"
      data-testid="app-status-place"
      className={`${layout} bg-transparent border-none cursor-pointer hover:bg-bg-hover focus-visible:outline-2 focus-visible:outline-accent`}
      onClick={onGo}
    >
      {body}
    </button>
  )
}

/** The key that fires the primary go, as caps, while it is on and bound. */
function useStatusGoCaps(): string[] | null {
  const enabled = useShortcutsEnabled()
  const chord = useShortcutBindings()['status-go']?.primary ?? null
  return enabled && chord ? formatChordCaps(chord, 'status-go') : null
}

/** The go button, with its key on the computer. */
function GoButton({ go, keyCaps, onGo }: { go: StatusGo; keyCaps: string[] | null; onGo: () => void }) {
  return (
    <button
      type="button"
      data-testid="app-status-go"
      className="inline-flex shrink-0 items-center gap-1.5 h-7 px-3 rounded-[var(--radius-md)] border-none bg-accent text-accent-fg text-[12px] font-semibold cursor-pointer hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
      onClick={onGo}
    >
      <span>{go.label || i18nT('app.status_item_go')}</span>
      {keyCaps && <span aria-hidden="true" className="flex items-center gap-0.5 [&_kbd]:h-5 [&_kbd]:min-w-[20px] [&_kbd]:text-[11px]"><KeyCapSequence caps={keyCaps} plus /></span>}
    </button>
  )
}

/** A reading that lists places: heading, the rows, then its note and the go. */
function PlacesBlock({ reading, keyCaps, goTo, size }: { reading: StatusItemReading; keyCaps: string[] | null; goTo: GoTo; size: 'md' | 'sm' }) {
  const v = reading.value!
  const { appName, icon } = reading.item
  const goButton = v.go && <GoButton go={v.go} keyCaps={keyCaps} onGo={() => goTo(appName, v.go!.target)} />
  // On the phone the go sits by the heading, in reach of the thumb before the
  // list; on a computer it closes the card, with its key.
  const goOnTop = size === 'sm' && !!goButton
  return (
    <div className="my-1" data-testid="app-status-places">
      {(v.heading || goOnTop) && (
        <div className="flex items-center justify-between gap-2 px-1.5 pt-0.5 pb-1.5">
          <span className="min-w-0 font-semibold text-text-strong">{v.heading}</span>
          {goOnTop && goButton}
        </div>
      )}
      {v.rows.map((row, i) => (
        <PlaceRow key={i} row={row} icon={icon} onGo={row.target ? () => goTo(appName, row.target!) : null} />
      ))}
      {(v.tooltip || (v.go && !goOnTop)) && (
        <div className="flex items-center justify-between gap-2 px-1.5 pt-2">
          <span className="min-w-0 text-[12px] text-muted">{v.tooltip}</span>
          {!goOnTop && goButton}
        </div>
      )}
    </div>
  )
}

function hasPlaces(r: StatusItemReading): boolean {
  return !!r.value && (r.value.rows.length > 0 || !!r.value.heading || !!r.value.go)
}

/** Clock for "Updated N s ago": ticks only while the card is open. */
function useNowWhile(active: boolean): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return
    setNow(Date.now())
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [active])
  return now
}

function AppStatusButton({ groups, size, goTo }: { groups: StatusItemGroup[]; size: 'md' | 'sm'; goTo: GoTo }) {
  const [open, setOpen] = useState(false)
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  // A hover-opened card is a glance: it neither takes focus on open nor hands it
  // to the pill on close, so typing in the composer is never interrupted.
  // A click or keyboard open is a deliberate one and moves focus as Radix does.
  const viaHover = useRef(false)
  const now = useNowWhile(open)
  const cancelClose = () => {
    if (closeTimer.current) clearTimeout(closeTimer.current)
    closeTimer.current = null
  }
  useEffect(() => cancelClose, [])
  // Hover is a mouse affordance only: a touch press also fires pointerenter,
  // and opening there would race the tap's own toggle.
  const hoverOpen = (e: PointerEvent) => {
    if (e.pointerType !== 'mouse') return
    cancelClose()
    if (!open) viaHover.current = true
    setOpen(true)
  }
  const hoverClose = (e: PointerEvent) => {
    if (e.pointerType !== 'mouse') return
    cancelClose()
    closeTimer.current = setTimeout(() => setOpen(false), HOVER_CLOSE_MS)
  }
  const readings = groups.flatMap(g => g.readings)
  // The phone has no key to teach.
  const goCaps = useStatusGoCaps()
  const keyCaps = size === 'md' ? goCaps : null
  // `/chat?status=<app>:<item>` asks for this card. It opens once that item has
  // something to show; a click open, so focus moves into the card.
  const pending = usePendingStatusCard()
  const keys = readings.map(r => r.item.key).join('\n')
  useEffect(() => {
    if (!pending || !takeStatusCardRequest(keys.split('\n'))) return
    cancelClose()
    viaHover.current = false
    setOpen(true)
  }, [pending, keys])
  const go: GoTo = (appName, target) => {
    cancelClose()
    setOpen(false)
    goTo(appName, target)
  }
  const wide = readings.some(hasPlaces)
  // `short`, not `narrow`: a narrow unit list joins with a bare space (or with
  // nothing, in ja), which runs "label: value" pairs together when read aloud.
  const label = fmtList(
    readings.map(r => i18nT('app.status_item_reading', { label: r.item.label, value: textOf(r) })),
    { type: 'unit', style: 'short' },
  )
  const cardName = fmtList(groups.map(g => g.appDisplayName), { type: 'unit', style: 'short' })
  const updatedAt = Math.max(...readings.map(r => r.updatedAt))
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          data-testid="app-status-items"
          aria-label={label}
          className="inline-flex items-center gap-1.5 min-w-0 rounded-full bg-transparent border-none p-0 cursor-pointer focus-visible:outline-2 focus-visible:outline-accent"
          onPointerEnter={hoverOpen}
          onPointerLeave={hoverClose}
          onClick={e => {
            // The pointer hovered the card open on its way to the click; the
            // trigger's toggle would close it under the pointer, so keep it.
            cancelClose()
            viaHover.current = false
            if (open) e.preventDefault()
          }}
        >
          {readings.map(r => <Pill key={r.item.key} reading={r} size={size} />)}
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="end"
        sideOffset={6}
        className={`${wide ? 'w-[400px]' : 'w-[300px]'} max-w-[calc(100vw-1rem)] rounded-[var(--radius-lg)] border-border-strong px-3.5 py-3 text-[13px] shadow-lg`}
        data-testid="app-status-card"
        aria-label={cardName}
        onPointerEnter={hoverOpen}
        onPointerLeave={hoverClose}
        onOpenAutoFocus={e => { if (viaHover.current) e.preventDefault() }}
        onCloseAutoFocus={e => {
          if (viaHover.current) e.preventDefault()
          viaHover.current = false
        }}
      >
        {groups.map(g => (
          <section key={g.appName} aria-label={g.appDisplayName} className="mb-1" data-app={g.appName}>
            {groups.length > 1 && <div className="text-[12px] font-semibold text-muted mb-0.5">{g.appDisplayName}</div>}
            <div className="flex items-center gap-1.5 mb-1">
              {g.readings.map(r => <Pill key={r.item.key} reading={r} size="md" />)}
            </div>
            {g.readings.map(r => hasPlaces(r)
              ? <PlacesBlock key={r.item.key} reading={r} keyCaps={keyCaps} goTo={go} size={size} />
              : <CardRow key={r.item.key} reading={r} />)}
          </section>
        ))}
        {updatedAt > 0 && (
          <div className="mt-2 text-[12px] text-muted" data-testid="app-status-updated">
            {i18nT('app.status_item_updated', { ago: fmtRelative(updatedAt, { now, numeric: 'always' }) })}
          </div>
        )}
      </PopoverContent>
    </Popover>
  )
}

/**
 * Every app's status items, in stable key order. Renders nothing while no app
 * declares one, so the stock bar is unchanged.
 */
export default function AppStatusItems({ size = 'md' }: { size?: 'md' | 'sm' }) {
  const groups = useAppStatusItems()
  const goTo = useStatusGo()
  // The go the key fires: the first item's, in bar order, that offers one.
  const first = groups.flatMap(g => g.readings).find(r => r.value?.go)
  const primaryApp = first?.item.appName ?? null
  const primaryTarget = first?.value?.go?.target ?? null
  const primaryKey = primaryTarget ? JSON.stringify(primaryTarget) : ''
  useEffect(() => {
    if (!primaryApp || !primaryTarget) return
    return setPrimaryStatusGo(() => goTo(primaryApp, primaryTarget))
    // `primaryKey` stands for the target: a new poll answer is a new object.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [goTo, primaryApp, primaryKey])
  if (groups.length === 0) return null
  return (
    // Shrinkable, and the pills' values truncate, so the row gives way before
    // the bell or the phone's session title. The desktop ladder measures
    // max-content widths, so its rungs still fold first; the row only shrinks
    // once the last rung leaves the group too wide. On the phone it takes at
    // most 65% of the centre cell, the title keeping the rest.
    <div className={`flex items-center gap-1.5 min-w-0 overflow-hidden${size === 'sm' ? ' max-w-[65%]' : ''}`} data-testid="app-status-items-bar">
      <AppStatusButton groups={groups} size={size} goTo={goTo} />
    </div>
  )
}
