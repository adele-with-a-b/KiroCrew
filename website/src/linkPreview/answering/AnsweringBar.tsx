/**
 * What Crew draws while a card's answering mode is on (`answeringMode.ts`):
 *
 * - under the pinned card, in the card's width: Crew's answer line, the preview
 *   line (the count, the chat's name and the exact message, cut with "..."; a
 *   click opens it whole), and the button row: the buttons the card asked for and
 *   Crew's own "keep, next", which reads "send prompt" while Shift is held;
 * - the mode line under the message box: the app, the card's place, the keys.
 *
 * Keys: in Crew's line and on the page (but never in the message box or any
 * other text field, which keep their own keys), Enter keeps, Shift+Enter sends,
 * Escape leaves, and the card's declared keys go to the card. A send happens only
 * on the person's own press (`isPersonPress`), once, never on key repeat.
 *
 * @module linkPreview/answering/AnsweringBar
 */
import { useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'
import { i18nT } from '../../i18n/t'
import { isEditableTarget } from '../../utils/editableTarget'
import type { AnsweringMode, ModeState } from './answeringMode'
import { keyName } from './answeringProtocol'
import { isPersonPress } from './personPress'

interface Box { left: number; top: number; width: number }

const sameBox = (a: Box | null, b: Box | null) =>
  a === b || (!!a && !!b && a.left === b.left && a.top === b.top && a.width === b.width)

/** Follows `read()` every frame while mounted, re-rendering only when it moves. */
function useFollow(read: () => Box | null): Box | null {
  const [box, setBox] = useState<Box | null>(() => read())
  const readRef = useRef(read)
  readRef.current = read
  useLayoutEffect(() => {
    let raf = 0
    let last: Box | null = null
    const tick = () => {
      const next = readRef.current()
      if (!sameBox(last, next)) { last = next; setBox(next) }
      raf = requestAnimationFrame(tick)
    }
    tick()
    return () => cancelAnimationFrame(raf)
  }, [])
  return box
}

/** Which keys a press is: the send, the keep, the leave, or neither. */
function pressOf(e: { key: string; shiftKey: boolean; ctrlKey: boolean; altKey: boolean; metaKey: boolean; isComposing?: boolean }): 'send' | 'keep' | 'leave' | null {
  if (e.isComposing) return null
  if (e.key === 'Escape') return 'leave'
  if (e.key !== 'Enter' || e.ctrlKey || e.altKey || e.metaKey) return null
  return e.shiftKey ? 'send' : 'keep'
}

/** The person's key in Crew's bar or on the page, as the mode takes it. True when taken. */
function takeKey(mode: AnsweringMode, e: KeyboardEvent, inLine: boolean): boolean {
  const press = pressOf(e)
  if (press === 'leave') return mode.escape()
  if (press === 'send') {
    if (!e.repeat && isPersonPress(e)) void mode.pressSend()
    return true
  }
  if (press === 'keep') {
    // On the page, Enter on a focused button or link stays its own click.
    if (!inLine) return false
    if (!e.repeat) mode.keep(e)
    return true
  }
  const name = keyName(e)
  return name !== null && mode.key(name, e)
}

export default function AnsweringBar({ mode }: { mode: AnsweringMode }) {
  const s = useSyncExternalStore(mode.subscribe, mode.getSnapshot)
  return s ? <AnsweringOn key={s.token} mode={mode} s={s} /> : null
}

function AnsweringOn({ mode, s }: { mode: AnsweringMode; s: ModeState }) {
  const barRef = useRef<HTMLDivElement | null>(null)
  const lineRef = useRef<HTMLInputElement | null>(null)
  const card = useFollow(() => {
    const r = s.frame.el.getBoundingClientRect()
    return r.width > 0 ? { left: r.left, top: r.bottom, width: r.width } : null
  })
  const modeLine = useFollow(() => {
    const box = document.querySelector('[data-testid="input-wrapper"]')?.getBoundingClientRect()
    return box && box.width > 0 ? { left: box.left, top: box.bottom + 6, width: box.width } : null
  })

  // Focus moves to Crew's line when the mode starts; the message box's draft is
  // left as it was. When the mode ends with focus in Crew's bar, it goes back to
  // the message box rather than to nowhere.
  useEffect(() => {
    lineRef.current?.focus({ preventScroll: true })
    const bar = barRef.current
    return () => {
      if (bar && bar.contains(document.activeElement)) {
        document.querySelector<HTMLElement>('[data-composer-input]')?.focus({ preventScroll: true })
      }
    }
  }, [])

  // The mode's keys, in Crew's bar and on the page: everywhere but another text
  // field (the message box keeps its own Enter and Shift+Enter). Shift held
  // anywhere turns "keep, next" into "send prompt".
  useEffect(() => {
    const onDown = (e: KeyboardEvent) => {
      if (e.key === 'Shift') mode.setShift(true)
      const inBar = !!barRef.current?.contains(e.target as Node)
      if (!inBar && isEditableTarget(e)) return
      if (takeKey(mode, e, e.target === lineRef.current)) { e.preventDefault(); e.stopPropagation() }
    }
    const onUp = (e: KeyboardEvent) => { if (e.key === 'Shift') mode.setShift(false) }
    const onBlur = () => mode.setShift(false)
    document.addEventListener('keydown', onDown, true)
    document.addEventListener('keyup', onUp, true)
    window.addEventListener('blur', onBlur)
    return () => {
      document.removeEventListener('keydown', onDown, true)
      document.removeEventListener('keyup', onUp, true)
      window.removeEventListener('blur', onBlur)
    }
  }, [mode])

  const sending = s.status === 'sending'
  const h = s.handed && s.handed.count > 0 && s.handed.text ? s.handed : null
  const keepLabel = sending
    ? i18nT('components.linkPreview.answering.sending')
    : s.shift ? i18nT('components.linkPreview.answering.send_prompt') : i18nT('components.linkPreview.answering.keep_next')
  const keepButton = s.setup.keepAt === null ? null : (
    <button
      key="keep"
      type="button"
      data-testid="answering-keep"
      data-armed={s.shift ? 'send' : 'keep'}
      disabled={sending}
      aria-busy={sending}
      className={`inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-[13px] ${s.shift || sending ? 'border-accent bg-accent/80 text-white' : 'border-border bg-bg text-text hover:border-accent'}`}
      onClick={(e) => {
        if (!isPersonPress(e.nativeEvent)) return
        if (e.shiftKey) void mode.pressSend()
        else mode.keep(e.nativeEvent)
      }}
    >
      {keepLabel}
      {!sending ? <kbd className="rounded border border-current/40 px-1 text-[11px] font-mono opacity-80">{s.shift ? '⇧↵' : '↵'}</kbd> : null}
    </button>
  )
  const row = s.setup.buttons.map((b) => (
    <button
      key={b.id}
      type="button"
      data-testid="answering-button"
      disabled={sending}
      className="inline-flex items-center gap-1.5 rounded-md border border-border bg-bg px-2.5 py-1 text-[13px] text-text hover:border-accent"
      onClick={(e) => { if (isPersonPress(e.nativeEvent)) mode.button(b.id, e.nativeEvent) }}
    >
      {b.label}
      {b.hint ? <kbd className="rounded border border-border px-1 text-[11px] font-mono text-muted">{b.hint}</kbd> : null}
    </button>
  ))
  if (keepButton) row.splice(Math.min(s.setup.keepAt ?? 0, row.length), 0, keepButton)

  return (
    <>
      {card ? (
        <div
          ref={barRef}
          role="group"
          aria-label={i18nT('components.linkPreview.answering.bar_label', { app: s.appTitle })}
          data-testid="answering-bar"
          className="fixed z-[2147482001] rounded-b-lg border border-t-0 border-border bg-bg-elevated px-3 pb-3 pt-2 text-text shadow-lg"
          style={{ left: card.left, top: card.top - 1, width: card.width }}
        >
          <input
            ref={lineRef}
            data-testid="answering-line"
            aria-label={i18nT('components.linkPreview.answering.line_label')}
            value={s.line}
            placeholder={s.setup.hint}
            disabled={sending}
            autoComplete="off"
            spellCheck
            className="w-full rounded-md border border-accent/60 bg-bg px-3 py-2 text-[14px] text-text placeholder:text-muted focus:outline-none focus:ring-1 focus:ring-accent"
            onChange={(e) => mode.setLine(e.target.value)}
          />
          <div className="mt-2 flex min-w-0 items-center gap-2 text-[12px] text-muted" data-testid="answering-preview">
            <span className="shrink-0 rounded border border-border px-1 text-[10px] leading-4">{i18nT('components.linkPreview.answering.crew_tag')}</span>
            {h ? (
              <>
                <span className="shrink-0">{i18nT('components.linkPreview.answering.answers_to', { count: h.count, chat: s.chatTitle })}</span>
                <button
                  type="button"
                  data-testid="answering-preview-text"
                  aria-expanded={s.full}
                  title={i18nT('components.linkPreview.answering.open_whole')}
                  className="min-w-0 truncate text-left text-text hover:underline"
                  onClick={() => mode.toggleFull()}
                >
                  {h.text}
                </button>
              </>
            ) : (
              <span data-testid="answering-nothing" className={s.note === 'nothing' ? 'text-warn' : ''}>{i18nT('components.linkPreview.answering.nothing_kept')}</span>
            )}
            {s.note === 'updating' ? <span role="status" className="shrink-0 text-warn">{i18nT('components.linkPreview.answering.updating')}</span> : null}
            {s.note === 'refused' ? <span role="status" className="shrink-0 text-warn" title={s.refusal}>{i18nT('components.linkPreview.answering.not_sent')}</span> : null}
          </div>
          {h && s.full ? (
            <div data-testid="answering-preview-full" className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded border border-border bg-bg p-2 text-[13px] text-text">{h.text}</div>
          ) : null}
          {row.length ? <div className="mt-2 flex flex-wrap items-center gap-2">{row}</div> : null}
        </div>
      ) : null}
      <ModeLine s={s} box={modeLine} />
    </>
  )
}

function ModeLine({ s, box }: { s: ModeState; box: Box | null }) {
  const sending = s.status === 'sending'
  // A narrow window keeps Crew's own keys; the card's are dropped first.
  const keys: { k: string; what: string; card?: boolean }[] = sending ? [] : [
    ...s.setup.keys.filter(k => k.label).map(k => ({ k: shortKey(k.key), what: k.label, card: true })),
    { k: '↵', what: i18nT('components.linkPreview.answering.help_next') },
    { k: '⇧↵', what: i18nT('components.linkPreview.answering.help_send') },
  ]
  return (
    <div
      data-testid="answering-mode-line"
      role="status"
      className="fixed z-[2147482001] flex items-center gap-3 overflow-hidden whitespace-nowrap rounded-lg border border-accent/60 bg-bg-elevated pr-2 text-[12px] text-text shadow"
      style={box ? { left: box.left, top: box.top, width: box.width } : { left: 16, right: 16, bottom: 8 }}
    >
      <span className="bg-accent/20 px-2.5 py-1 font-semibold uppercase tracking-wide text-accent">
        {i18nT('components.linkPreview.answering.mode_name')}<span className="hidden font-normal normal-case tracking-normal opacity-80 sm:inline"> · {s.appTitle}</span>
      </span>
      {sending
        ? <span className="text-accent">{i18nT('components.linkPreview.answering.sending')}</span>
        : s.setup.place ? <span className="text-accent">{s.setup.place}</span> : null}
      <span className="ml-auto flex items-center gap-2 text-muted">
        {keys.map(({ k, what, card }) => (
          <span key={k + what} className={`${card ? 'hidden sm:inline-flex' : 'inline-flex'} items-center gap-1`}>
            <kbd className="rounded border border-border px-1 font-mono text-[11px]">{k}</kbd>{what}
          </span>
        ))}
        <span className="inline-flex items-center gap-1">
          <kbd className="rounded border border-border px-1 font-mono text-[11px]">esc</kbd>{i18nT('components.linkPreview.answering.help_leave')}
        </span>
      </span>
    </div>
  )
}

/** One part of a declared key name (`answeringProtocol.DECLARED_KEY_RE`) as a glyph. */
function glyphOf(part: string): string {
  if (part === 'Ctrl') return '^'
  if (part === 'Alt') return '⌥'
  if (part === 'Shift') return '⇧'
  if (part === 'ArrowUp') return '↑'
  if (part === 'ArrowDown') return '↓'
  if (part === 'Tab') return '⇥'
  return part
}

/** A declared key as the mode line writes it (`Ctrl+Alt+R` as `^⌥R`). */
export function shortKey(key: string): string {
  return key.split('+').map(glyphOf).join('')
}
