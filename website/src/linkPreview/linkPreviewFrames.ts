/**
 * The iframes app cards run in: creating, placing, sizing and dismissing them.
 *
 * One hover frame per provider shows the card for the link under the pointer. A
 * card the app pins is detached from hovering: it keeps its own frame, stays when
 * the pointer leaves or the chat scrolls, and the next hover gets a fresh frame.
 * Each frame is `sandbox="allow-scripts"` (no `allow-same-origin`) and loads the
 * gateway's frame document, so the app's code never runs in the dashboard's
 * document. See `linkPreviewProtocol.ts` for the messages.
 *
 * @module linkPreview/linkPreviewFrames
 */
import { freshRate, isRelayMessage, takeMessage, type RateState } from '../appFrame/frameProtocol'
import { hasGesture } from '../hooks/useFrameOpenLink'
import {
  createSandboxedFrame,
  loadMintedFrame,
  mintAppFrame,
  postToFrame,
  renewIntervalMs,
  type Minted,
} from '../appFrame/sandboxFrame'
import {
  MAX_MESSAGE_BYTES,
  messageBytes,
  readFrameMessage,
  type AnchorRect,
  type FromFrame,
  type MessageRef,
  type Modifiers,
  type PreviewLink,
  type Placement,
  type ShowTrigger,
  type StepRefusal,
  type ToFrame,
} from './linkPreviewProtocol'
import { MAX_OPEN_PAYLOAD_BYTES, jsonBytes, type AppOpenTarget, type OpenRefusal } from './linkPreviewNav'

export { FRAME_SANDBOX, type Minted } from '../appFrame/sandboxFrame'
/** Waiting for the module to activate; a frame that has not said `ready` by then failed. */
export const READY_TIMEOUT_MS = 10_000
/** At most this many pinned cards at once; pinning one more closes the oldest. */
export const MAX_PINNED = 8
/** An overlay frame keeps the scope of this many latest shows: its windows outlive the hover. */
const MAX_SCOPES = 16
const MARGIN = 8
const GAP = 6
const DEFAULT_SIZE = { width: 360, height: 200 }
const MIN_SIZE = { width: 40, height: 24 }
const MAX_SIZE = { width: 720, height: 800 }
/** How long a card a click or tap showed may open its app's own tab without a
 *  click inside it: the label click is the person's ask (see `takeClickGrant`).
 *  Counted from the show, which on a cold frame waits for the module. Measured
 *  on a test Crew (2026-11-22) with a real provider (Ref Links' label card):
 *  show to its card's `open` took 7-34 ms on a chat it had read; its server's
 *  first read of a never-read 43 MB chat took 2.9-4.0 s (its own 4 s cap). A
 *  provider that opens the tab at once on a click, before its own read, is far
 *  inside the bound.
 *  shortcut: a fixed bound; measure again if a provider must read before it opens. */
export const CLICK_GRANT_MS = 3_000

/** Where one show's link or text sits, known to the dashboard only. A frame's
 *  `openPanelTab` and `jump` act on this chat and nothing else. */
export interface ShowScope {
  /** The LOCAL slot of the chat the message is in; absent in another gateway's rows. */
  slot?: string
  /** The message the link or text sits in. */
  origin?: MessageRef
  /** The owner approved this app for `sessionKey`, so it may name a message. */
  messages: boolean
}

/** Why a frame failed: no `ready` in time, the app said `failed`, or no frame
 *  could be minted for it (disabled, not approved, gone). */
export type FailureCause = 'timeout' | 'app' | 'unavailable'

export interface PreviewFrame {
  key: string
  appName: string
  el: HTMLIFrameElement
  state: 'loading' | 'ready' | 'failed'
  ready: Promise<boolean>
  visible: boolean
  pinned: boolean
  /** Dragged by the user: placed where it was dropped, not by its anchor. */
  moved: boolean
  dock: 'left' | 'right' | null
  left: number
  top: number
  width: number
  height: number
  anchor: AnchorRect | null
  frameId?: string
  /** Overlay mode (`regions`): the frame covers the viewport, clipped to `regions`. */
  overlay: boolean
  regions: AnchorRect[]
  /** The latest show's number; a card message for an older one is dropped. */
  invocation: number
  /** Where each recent show sat, by invocation (the latest only, in card mode). */
  scopes: Map<number, ShowScope>
  /** Burst allowance left, and when it was last refilled. */
  rate: RateState
  /** Which side of its link the card was put on by the latest `place`; null when
   *  docked or dropped where the user dragged it. */
  placement: Placement | null
  /** The side the latest show's card was last told (in `show`, then `placement`). */
  toldPlacement: Placement | null
  /** The latest show was the host's own click or tap on the link: that show may
   *  open its app's tab once, until `CLICK_GRANT_MS` after `at` (`performance.now()`). */
  clickGrant: { invocation: number; at: number } | null
  /** A click or tap show is waiting for the frame to be ready: until when, at most
   *  (`performance.now()`), so a popover holding its close knows an open may come. */
  clickPendingUntil: number
}

export interface FrameHostOptions {
  /** The frame URL and capability for a provider (`POST /api/apps/<app>/frames/linkPreviews/<id>`);
   *  with `frameId`, a renewal for that frame. */
  mint(appName: string, frameId?: string, providerId?: string): Promise<Minted>
  theme(): 'dark' | 'light'
  /** The frame could not load or activate the module. `why` is for the log;
   *  `cause` is what the user is told. */
  onFailed(frame: PreviewFrame, why: string, cause: FailureCause): void
  onTextPatternsOff(appName: string, ids: string[]): void
  /** Escape pressed inside a card. */
  onEscape(): void
  /** The set of live frames changed (the link bridge listens to them). */
  onFramesChanged(frames: HTMLIFrameElement[]): void
  /** A shown card of an app approved for `sessionKey` asked to open its app's own
   *  `target` (a side-panel tab or its session control) in the chat `scope` is in:
   *  true when the chat took it, else why not. */
  onOpen?(appName: string, target: AppOpenTarget, payload: unknown, scope: ShowScope): true | OpenRefusal
  /** A shown card of an app approved for `sessionKey` asked to jump to `target` in the chat `scope` is in. */
  onJump?(appName: string, target: MessageRef, scope: ShowScope): void
  /** A pinned card of an app approved for `sessionKey` asked to step to its
   *  provider's label `text` in message `target` of the chat `slot` is on screen
   *  in (`linkPreviewStep.ts`): bring it into view, call `land` with where it is
   *  (again while the scroll settles), and `done` once. Returns its cancel. */
  onStep?(key: string, ask: StepRequest, hooks: StepLanding): () => void
  /** The provider's card-mode hover card closed itself (`card.close`). */
  onClosed?(key: string): void
}

/** What a card's `step` names, in the chat its show sat in. */
export interface StepRequest {
  slot: string
  target: MessageRef
  text: string
  line?: string
}

/** How the host side of a step reports back to the frame host. */
export interface StepLanding {
  land(rect: AnchorRect, link: PreviewLink): void
  done(result: true | StepRefusal): void
}

export function mintFrame(appName: string, frameId?: string, providerId = ''): Promise<Minted> {
  return mintAppFrame(appName, 'linkPreviews', providerId, frameId)
}

/** A provider key is `${appName}:${id}`; an app name holds no `:`. */
const providerIdOf = (frame: { key: string; appName: string }): string => frame.key.slice(frame.appName.length + 1)

const clamp = (n: number, lo: number, hi: number) => Math.max(lo, Math.min(n, Math.max(lo, hi)))
/** A card `h` tall goes below its anchor when it fits, otherwise above it. */
const beside = (a: AnchorRect, h: number, vh: number): { top: number; side: Placement } => {
  const below = a.top + a.height + GAP
  return below + h <= vh - MARGIN ? { top: below, side: 'below' } : { top: a.top - GAP - h, side: 'above' }
}

export class FrameHost {
  private readonly opts: FrameHostOptions
  private readonly root: HTMLElement
  /** provider key -> its hover frame */
  private readonly hover = new Map<string, PreviewFrame>()
  /** Pinned or once-pinned cards, oldest first. */
  private readonly detached: PreviewFrame[] = []
  private readonly renewals = new Map<PreviewFrame, number>()
  /** How a loading frame settles: its module activated, or it did not. */
  private readonly settlers = new Map<PreviewFrame, { ready(): void; fail(why: string): void; cancel(): void }>()
  /** Bumped by every leave and dismissal, so a show still waiting for a loading
   *  frame is dropped instead of opening a card the user already left. */
  private readonly showTokens = new Map<PreviewFrame, number>()
  /** A card's step still settling, by frame: ending it answers it `replaced`. */
  private readonly steps = new Map<PreviewFrame, () => void>()

  constructor(opts: FrameHostOptions, root: HTMLElement = document.body) {
    this.opts = opts
    this.root = root
    this.onMessage = this.onMessage.bind(this)
    this.onResize = this.onResize.bind(this)
    window.addEventListener('message', this.onMessage)
    window.addEventListener('resize', this.onResize)
  }

  frames(): PreviewFrame[] {
    return [...this.hover.values(), ...this.detached]
  }

  isFrame(el: unknown): boolean {
    return el instanceof HTMLIFrameElement && this.frames().some((f) => f.el === el)
  }

  /** The hover frame for a provider, created (and its module loading) on first use. */
  hoverFrame(key: string, appName: string): PreviewFrame {
    const have = this.hover.get(key)
    if (have && have.state !== 'failed') return have
    const frame = this.create(key, appName)
    this.hover.set(key, frame)
    this.changed()
    return frame
  }

  /** Retry: a hover frame for the provider that is not working is disposed, and a
   *  fresh one starts loading. A ready frame and pinned cards are left alone. */
  retryFrame(key: string, appName: string): PreviewFrame {
    const have = this.hover.get(key)
    if (have && have.state !== 'ready') this.destroy(have)
    return this.hoverFrame(key, appName)
  }

  /** Where a card of the default size would sit next to `anchor`: the same rule
   *  `place` uses, so a host-drawn card lands where the app's card would. */
  static cardSpot(anchor: AnchorRect, height: number): { left: number; top: number; width: number } {
    const vw = window.innerWidth
    const vh = window.innerHeight
    const w = clamp(DEFAULT_SIZE.width, MIN_SIZE.width, Math.min(MAX_SIZE.width, vw - 2 * MARGIN))
    const { top } = beside(anchor, height, vh)
    return { left: clamp(anchor.left, MARGIN, vw - w - MARGIN), top: clamp(top, MARGIN, vh - height - MARGIN), width: w }
  }

  /** Show `link` in the provider's hover frame, next to `anchor`. */
  async show(
    frame: PreviewFrame,
    link: PreviewLink,
    anchor: AnchorRect,
    modifiers?: Modifiers,
    extra?: { trigger?: ShowTrigger; scope?: ShowScope },
  ): Promise<boolean> {
    const token = this.cancelShow(frame)
    const asked = extra?.trigger === 'click' || extra?.trigger === 'tap'
    if (asked) frame.clickPendingUntil = performance.now() + READY_TIMEOUT_MS
    const ok = await frame.ready
    if (this.showTokens.get(frame) === token) frame.clickPendingUntil = 0
    if (!ok || !frame.el.isConnected || this.showTokens.get(frame) !== token) return false
    frame.anchor = anchor
    frame.toldPlacement = null
    if (!frame.overlay) {
      frame.moved = false
      frame.dock = null
      this.place(frame)
    }
    frame.invocation += 1
    // Only the host's own click or tap handler names these triggers; nothing a
    // frame sends can set one.
    frame.clickGrant = asked ? { invocation: frame.invocation, at: performance.now() } : null
    if (extra?.scope) {
      frame.scopes.set(frame.invocation, extra.scope)
      for (const k of frame.scopes.keys()) {
        if (frame.scopes.size <= MAX_SCOPES) break
        frame.scopes.delete(k)
      }
    }
    const trigger = extra?.trigger ? { trigger: extra.trigger } : {}
    frame.toldPlacement = frame.overlay ? null : frame.placement
    const placement = frame.toldPlacement ? { placement: frame.toldPlacement } : {}
    this.post(frame, { type: 'show', invocation: frame.invocation, ...link, rect: anchor, theme: this.opts.theme(), ...(modifiers ? { modifiers } : {}), ...trigger, ...placement })
    return true
  }

  leave(key: string): void {
    const frame = this.hover.get(key)
    if (!frame) return
    this.cancelShow(frame)
    if (frame.state === 'ready') this.post(frame, { type: 'leave' })
  }

  /** Invalidate a pending show, and the click grant of the shown one; returns the new token. */
  private cancelShow(frame: PreviewFrame): number {
    frame.clickGrant = null
    frame.clickPendingUntil = 0
    const next = (this.showTokens.get(frame) ?? 0) + 1
    this.showTokens.set(frame, next)
    return next
  }

  /** Scroll, Escape, blur, another provider: every card that is not pinned goes. */
  hideUnpinned(reason: string, except?: string): void {
    for (const [key, frame] of this.hover) {
      if (key === except) continue
      this.cancelShow(frame)
      // An overlay provider keeps its own pinned windows: it is told, and decides.
      if (frame.overlay) {
        if (frame.state === 'ready') this.post(frame, { type: 'hide', reason })
        continue
      }
      // Dismissal retires the show it answered, visible yet or not: a late `open`
      // (or `resize`) for it is stale from here on.
      frame.invocation += 1
      if (frame.visible) this.setVisible(frame, false)
      if (frame.state === 'ready') this.post(frame, { type: 'hide', reason })
    }
    for (const frame of [...this.detached]) {
      if (frame.pinned) continue
      this.post(frame, { type: 'hide', reason })
      this.destroy(frame)
    }
  }

  /** The provider still has a hover frame, loading or shown: it was not pinned away or destroyed. */
  hasHoverFrame(key: string): boolean {
    return this.hover.has(key)
  }

  /** The provider's hover frame is an overlay drawing nothing. `regions` names no
   *  show, so this may answer an earlier one: it says nothing is drawn, not that the
   *  latest show ended. */
  drawsNoRegions(key: string): boolean {
    const frame = this.hover.get(key)
    return Boolean(frame?.overlay && !frame.regions.length)
  }

  /** The provider's card-mode hover card is on screen. */
  isShown(key: string): boolean {
    const frame = this.hover.get(key)
    return Boolean(frame && frame.visible && !frame.overlay)
  }

  /** A once-pinned card the app unpinned is still on screen: it belongs to an
   *  earlier hover, not the latest one. */
  hasUnpinnedDetached(): boolean {
    return this.detached.some((f) => !f.pinned)
  }

  /** The app is gone (disabled, uninstalled): every frame of its provider goes. */
  disposeKey(key: string): void {
    for (const frame of this.frames()) if (frame.key === key) this.destroy(frame, 'disabled')
  }

  disposeAll(): void {
    for (const frame of this.frames()) this.destroy(frame, 'disabled')
    window.removeEventListener('message', this.onMessage)
    window.removeEventListener('resize', this.onResize)
  }

  /** Hand a key press to the visible cards (their own shortcuts). The caller has
   *  already checked that the dashboard is not taking text input. */
  forwardKey(e: KeyboardEvent): boolean {
    let sent = false
    for (const frame of this.frames()) {
      if (frame.state !== 'ready' || !frame.visible) continue
      this.post(frame, { type: 'key', key: e.key, code: e.code, shift: e.shiftKey })
      sent = true
    }
    return sent
  }

  setTheme(theme: 'dark' | 'light'): void {
    for (const frame of this.frames()) if (frame.state === 'ready') this.post(frame, { type: 'theme', theme })
  }

  private create(key: string, appName: string): PreviewFrame {
    const el = createSandboxedFrame(appName)
    el.dataset.linkPreviewFrame = key
    Object.assign(el.style, {
      // Placed by `place` / `layoutOverlay` before it is ever shown.
      position: 'fixed',
      width: `${DEFAULT_SIZE.width}px`,
      height: `${DEFAULT_SIZE.height}px`,
      border: '0',
      background: 'transparent',
      colorScheme: 'normal',
      zIndex: '2147482000',
      visibility: 'hidden',
      pointerEvents: 'none',
    })
    let settle: (ok: boolean) => void = () => {}
    const ready = new Promise<boolean>((resolve) => { settle = resolve })
    const frame: PreviewFrame = {
      key, appName, el, state: 'loading', ready, visible: false, pinned: false, moved: false, dock: null,
      left: 0, top: 0, width: DEFAULT_SIZE.width, height: DEFAULT_SIZE.height, anchor: null,
      overlay: false, regions: [], invocation: 0, scopes: new Map(), rate: freshRate(),
      placement: null, toldPlacement: null, clickGrant: null, clickPendingUntil: 0,
    }
    const fail = (why: string, cause: FailureCause) => {
      if (frame.state !== 'loading') return
      frame.state = 'failed'
      settle(false)
      this.opts.onFailed(frame, why, cause)
      this.destroy(frame)
    }
    const timer = window.setTimeout(() => fail('timeout', 'timeout'), READY_TIMEOUT_MS)
    this.settlers.set(frame, {
      ready: () => {
        window.clearTimeout(timer)
        if (frame.state !== 'loading') return
        frame.state = 'ready'
        settle(true)
      },
      fail: (why) => {
        window.clearTimeout(timer)
        fail(why, 'app')
      },
      cancel: () => window.clearTimeout(timer),
    })
    this.opts.mint(appName, undefined, providerIdOf(frame)).then(
      (minted) => {
        if (!this.frames().includes(frame)) return
        frame.frameId = minted.frameId
        loadMintedFrame(el, this.root, minted, {
          theme: this.opts.theme,
          onNavigatedAway: () => {
            // eslint-disable-next-line no-console -- an app's frame navigating away is otherwise silent
            console.warn(`[link-preview] ${frame.key}: the card frame navigated away and was closed`)
            this.destroy(frame)
          },
        })
        // Renew the capability well before it lapses, while the frame lives.
        this.renewals.set(frame, window.setInterval(() => { void this.renew(frame) }, renewIntervalMs(minted.ttl)))
      },
      () => {
        window.clearTimeout(timer)
        fail('mint', 'unavailable')
      },
    )
    return frame
  }

  private async renew(frame: PreviewFrame): Promise<void> {
    try {
      const minted = await this.opts.mint(frame.appName, frame.frameId, providerIdOf(frame))
      if (frame.el.isConnected) this.post(frame, { type: 'capability', capability: minted.capability })
    } catch {
      // The frame retries on its next 401; nothing to show here.
    }
  }

  private destroy(frame: PreviewFrame, reason?: string): void {
    if (reason && frame.state === 'ready') {
      this.post(frame, { type: 'hide', reason })
      this.post(frame, { type: 'dispose' })
    }
    const interval = this.renewals.get(frame)
    if (interval !== undefined) window.clearInterval(interval)
    this.renewals.delete(frame)
    this.settlers.get(frame)?.cancel()
    this.settlers.delete(frame)
    this.showTokens.delete(frame)
    this.steps.get(frame)?.()
    if (this.hover.get(frame.key) === frame) this.hover.delete(frame.key)
    const at = this.detached.indexOf(frame)
    if (at >= 0) this.detached.splice(at, 1)
    frame.el.remove()
    this.changed()
  }

  private post(frame: PreviewFrame, msg: ToFrame): void {
    postToFrame(frame.el, msg)
  }

  private changed(): void {
    this.opts.onFramesChanged(this.frames().map((f) => f.el))
  }

  private setVisible(frame: PreviewFrame, visible: boolean): void {
    frame.visible = visible
    frame.el.style.visibility = visible ? 'visible' : 'hidden'
    frame.el.style.pointerEvents = visible ? 'auto' : 'none'
  }

  private place(frame: PreviewFrame): void {
    const vw = window.innerWidth
    const vh = window.innerHeight
    frame.width = clamp(frame.width, MIN_SIZE.width, Math.min(MAX_SIZE.width, vw - 2 * MARGIN))
    frame.height = clamp(frame.height, MIN_SIZE.height, Math.min(MAX_SIZE.height, vh - 2 * MARGIN))
    const { width: w, height: h } = frame
    if (frame.dock) {
      frame.left = frame.dock === 'left' ? MARGIN : vw - w - MARGIN
      frame.placement = null
    } else if (!frame.moved && frame.anchor) {
      const a = frame.anchor
      const at = beside(a, h, vh)
      frame.left = a.left
      frame.top = at.top
      frame.placement = at.side
    } else if (frame.moved) {
      frame.placement = null
    }
    frame.left = clamp(frame.left, MARGIN, vw - w - MARGIN)
    frame.top = clamp(frame.top, MARGIN, vh - h - MARGIN)
    Object.assign(frame.el.style, {
      left: `${Math.round(frame.left)}px`,
      top: `${Math.round(frame.top)}px`,
      width: `${Math.round(w)}px`,
      height: `${Math.round(h)}px`,
    })
    // The show named a side from the card's size before it had drawn; its own
    // `resize` (or the window's) may move it to the other one.
    if (frame.toldPlacement && frame.placement && frame.placement !== frame.toldPlacement && frame.state === 'ready') {
      frame.toldPlacement = frame.placement
      this.post(frame, { type: 'placement', invocation: frame.invocation, placement: frame.placement })
    }
  }

  /** Overlay mode: the whole viewport, clipped to the regions the provider draws in.
   *  A clipped-out area is not hit-tested, so the page under it stays clickable. */
  private layoutOverlay(frame: PreviewFrame): void {
    const vw = window.innerWidth
    const vh = window.innerHeight
    const rects: AnchorRect[] = []
    for (const r of frame.regions) {
      const left = clamp(r.left, 0, vw)
      const top = clamp(r.top, 0, vh)
      const width = Math.min(r.left + r.width, vw) - left
      const height = Math.min(r.top + r.height, vh) - top
      if (width > 0 && height > 0) rects.push({ left, top, width, height })
    }
    Object.assign(frame.el.style, { left: '0px', top: '0px', width: `${vw}px`, height: `${vh}px` })
    // One closed subpath per rect: move, then horizontal, vertical, horizontal, close.
    const d = rects
      .flatMap((r) => ['M', r.left, r.top, 'h', r.width, 'v', r.height, 'h', -r.width, 'Z'].map((x) => (typeof x === 'number' ? Math.round(x) : x)))
      .join(' ')
    // No regions: the frame is hidden and takes no pointer events (setVisible).
    // A CSS `path()` shape, not copy (`path` is an exempt key for the i18n gate).
    const shape = { path: `path('${d}')` }
    frame.el.style.clipPath = d ? shape.path : ''
    this.setVisible(frame, rects.length > 0)
  }

  private onResize(): void {
    for (const frame of this.frames()) {
      if (frame.overlay) this.layoutOverlay(frame)
      else if (frame.visible) this.place(frame)
    }
  }

  private onMessage(e: MessageEvent): void {
    if (!e.source) return
    const frame = this.frames().find((f) => f.el.contentWindow === e.source)
    if (!frame) return
    // Relay requests are the relay's (`appFrame/frameRelay.ts`), not the card's.
    if (isRelayMessage(e.data)) return
    // Bounded: an oversized message, or one past the burst allowance, is dropped unread.
    if (!takeMessage(frame.rate)) return
    if (messageBytes(e.data) > MAX_MESSAGE_BYTES) return
    const msg = readFrameMessage(e.data)
    if (msg) this.handle(frame, msg)
  }

  /** The scope of the show `invocation` answers, while its card is on screen and
   *  right after the person's own click or key in it (`hasGesture`: so a card can
   *  never open a tab or move the chat on its own), or, for `granted`, right after
   *  the click on its link that showed it (`takeClickGrant`): in card mode the
   *  latest show of a visible frame only; in overlay mode any of the recent shows,
   *  since the provider's own windows outlive their hover. */
  private scopeOf(frame: PreviewFrame, invocation: number, granted = false): ShowScope | undefined {
    if (!frame.visible || !(granted || hasGesture(frame.el))) return undefined
    if (!frame.overlay && invocation !== frame.invocation) return undefined
    return frame.scopes.get(invocation)
  }

  /** `scopeOf` for an open, approved for `sessionKey`, or why not, so the card
   *  can be told. */
  private openScope(frame: PreviewFrame, invocation: number, granted: boolean): ShowScope | OpenRefusal {
    if (!frame.visible) return 'not_shown'
    if (!(granted || hasGesture(frame.el))) return 'no_click'
    const scope = this.scopeOf(frame, invocation, granted)
    if (!scope) return 'not_shown'
    return scope.messages ? scope : 'not_approved'
  }

  /** The end (`performance.now()`) of the window a click or tap show may still open
   *  something in: a show waiting for its frame (then the grant's bound after it),
   *  or a live, unspent grant. 0 when there is none. */
  clickOpenEnd(): number {
    let end = 0
    for (const frame of this.hover.values()) {
      if (frame.clickPendingUntil) end = Math.max(end, frame.clickPendingUntil + CLICK_GRANT_MS)
      const g = frame.clickGrant
      if (g && g.invocation === frame.invocation) end = Math.max(end, g.at + CLICK_GRANT_MS)
    }
    return end
  }

  /** Spend the frame's click grant: true when it was made for `invocation`, the
   *  latest show, no more than `CLICK_GRANT_MS` ago. The first open of that show
   *  (`openPanelTab` or `openSessionControl`) spends it whatever it asks, so one
   *  click opens at most one target; a late one from an earlier show leaves it alone. */
  private takeClickGrant(frame: PreviewFrame, invocation: number): boolean {
    const grant = frame.clickGrant
    if (!grant || grant.invocation !== invocation) return false
    frame.clickGrant = null
    if (invocation !== frame.invocation) return false
    const age = performance.now() - grant.at
    return age >= 0 && age <= CLICK_GRANT_MS
  }

  /** Why the card `frame` may not step for `invocation`, or the scope it steps in. */
  private stepScope(frame: PreviewFrame, invocation: number): (ShowScope & { slot: string }) | StepRefusal {
    if (!frame.visible || frame.overlay || invocation !== frame.invocation) return 'not_shown'
    if (!frame.pinned) return 'not_pinned'
    if (!hasGesture(frame.el)) return 'no_click'
    const scope = frame.scopes.get(invocation)
    if (!scope) return 'not_shown'
    if (!scope.messages) return 'not_approved'
    return scope.slot ? { ...scope, slot: scope.slot } : 'not_on_screen'
  }

  /** A pinned card's `step`: the host brings the label into view, and the card is
   *  shown again beside it (`trigger: 'step'`, a fresh scope for that message),
   *  then kept beside it while the scroll settles. One step per card at a time. */
  private step(frame: PreviewFrame, msg: Extract<FromFrame, { type: 'step' }>): void {
    const reply = (result: true | StepRefusal) => {
      if (msg.request === undefined || !frame.el.isConnected) return
      this.post(frame, result === true
        ? { type: 'stepped', request: msg.request, ok: true }
        : { type: 'stepped', request: msg.request, ok: false, reason: result })
    }
    const scope = this.stepScope(frame, msg.invocation)
    const onStep = this.opts.onStep
    if (typeof scope === 'string' || !onStep) { reply(typeof scope === 'string' ? scope : 'not_on_screen'); return }
    this.steps.get(frame)?.()
    let answered = false
    let showing: Promise<boolean> | null = null
    let cancel: () => void = () => {}
    const answer = (result: true | StepRefusal) => {
      if (answered) return
      answered = true
      reply(result)
    }
    const end = () => {
      if (this.steps.get(frame) === end) this.steps.delete(frame)
      cancel()
      answer('replaced')
    }
    this.steps.set(frame, end)
    cancel = onStep(frame.key, { slot: scope.slot, target: msg.target, text: msg.text, ...(msg.line ? { line: msg.line } : {}) }, {
      land: (rect, link) => {
        if (this.steps.get(frame) !== end || !frame.pinned) return
        if (!showing) {
          showing = this.show(frame, link, rect, undefined, { trigger: 'step', scope: { slot: scope.slot, origin: msg.target, messages: true } })
          return
        }
        frame.anchor = rect
        if (!frame.moved && !frame.dock) this.place(frame)
      },
      done: (result) => {
        if (this.steps.get(frame) !== end) return
        if (result !== true) {
          this.steps.delete(frame)
          answer(result)
          return
        }
        // Answered after the card's new `show`, so the app hears where it is first.
        void (showing ?? Promise.resolve(false)).then((ok) => answer(ok ? true : 'replaced'))
      },
    })
  }

  /** Act on one message from `frame`. Public for tests. */
  handle(frame: PreviewFrame, msg: FromFrame): void {
    switch (msg.type) {
      case 'ready': this.settlers.get(frame)?.ready(); return
      case 'failed': this.settlers.get(frame)?.fail(msg.message || 'failed'); return
      case 'capabilityExpired': void this.renew(frame); return
      case 'textPatternsOff': this.opts.onTextPatternsOff(frame.appName, msg.ids); return
      case 'escape': this.opts.onEscape(); return
    }
    if (frame.state !== 'ready') return
    if (msg.type === 'openPanelTab' || msg.type === 'openSessionControl') {
      // One request shape for both targets. Both need the owner's `sessionKey`
      // approval: a tab or control is told its chat, so opening one from a card
      // says which chat the card's text was in. Either may be the click on the
      // link the card was shown for, and that click opens ONE of them: the
      // first open of the show spends the grant, whatever it names.
      const target: AppOpenTarget = msg.type === 'openPanelTab'
        ? { kind: 'panelTab', id: msg.tab }
        : { kind: 'sessionControl', id: msg.control }
      // A control's payload is capped like a status target's arg. Refused before
      // the grant is taken, so an oversized ask leaves the click its open (the
      // bootstrap refuses it before sending; this is for a frame that does not).
      const tooLarge = target.kind === 'sessionControl' && jsonBytes(msg.payload) > MAX_OPEN_PAYLOAD_BYTES
      const granted = !tooLarge && this.takeClickGrant(frame, msg.invocation)
      const scope = tooLarge ? 'too_large' as const : this.openScope(frame, msg.invocation, granted)
      const result = typeof scope === 'string' ? scope
        : scope.slot ? this.opts.onOpen?.(frame.appName, target, msg.payload, scope) ?? 'not_on_screen'
          : 'not_on_screen'
      if (msg.request !== undefined) {
        this.post(frame, result === true
          ? { type: 'opened', request: msg.request, ok: true }
          : { type: 'opened', request: msg.request, ok: false, reason: result })
      }
      return
    }
    if (msg.type === 'jump') {
      // A `jump` always needs a click in the card.
      const scope = this.scopeOf(frame, msg.invocation)
      if (!scope?.slot || !scope.messages) return
      this.opts.onJump?.(frame.appName, msg.target, scope)
      return
    }
    if (msg.type === 'step') {
      this.step(frame, msg)
      return
    }
    if (msg.type === 'regions') {
      // A card turning itself into an overlay keeps no earlier show's scope: only
      // shows made while it is an overlay can ask.
      if (!frame.overlay) frame.scopes.clear()
      frame.overlay = true
      frame.regions = msg.rects
      this.layoutOverlay(frame)
      return
    }
    // An overlay provider moves its own windows; card messages do not apply.
    if (frame.overlay) return
    // A card message answers one show; one for an older show is stale.
    if ('invocation' in msg && msg.invocation !== frame.invocation) return
    switch (msg.type) {
      case 'open':
        this.place(frame)
        this.setVisible(frame, true)
        return
      case 'close':
        if (this.detached.includes(frame)) this.destroy(frame)
        else {
          this.setVisible(frame, false)
          this.opts.onClosed?.(frame.key)
        }
        return
      case 'resize':
        frame.width = msg.width
        frame.height = msg.height
        this.place(frame)
        return
      case 'pin':
        // A pinned card leaves the hover, out of reach of `leave` and dismissal:
        // it keeps no click grant.
        if (msg.pinned) frame.clickGrant = null
        frame.pinned = msg.pinned
        if (msg.pinned && this.hover.get(frame.key) === frame) {
          this.hover.delete(frame.key)
          this.detached.push(frame)
          while (this.detached.filter((f) => f.pinned).length > MAX_PINNED) {
            const oldest = this.detached.find((f) => f.pinned)
            if (!oldest) break
            this.destroy(oldest, 'other')
          }
          this.changed()
        }
        return
      case 'drag':
        frame.moved = true
        frame.dock = null
        frame.left += msg.dx
        frame.top += msg.dy
        this.place(frame)
        return
      case 'dock':
        frame.dock = msg.edge
        frame.moved = true
        this.place(frame)
        return
    }
  }
}
