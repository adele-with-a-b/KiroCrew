/**
 * Putting an app's text into the composer as plain, visible text.
 *
 * A composer action's text goes only into an EMPTY composer, so the text is the
 * whole draft. A docked session control's text goes in at the caret, beside what
 * the person typed (`joinAtCaret`). Either way it goes in only when nothing in it
 * would be read as more than text (`composerWouldResolve`), and beside a draft only
 * when the join makes no reader find more than it found before
 * (`joinWouldResolve`). Otherwise the host refuses it. Accepted text carries no
 * mark: what the person types after it is their own and resolves normally.
 *
 * @module composerActions/literalInsert
 */
import { MAX_OFFER_CHARS, displaySafe } from '../appFrame/frameProtocol'
import { IMG_EXT, parseDirs, parseFiles } from '../utils/fileTokens'
import { PASTE_TOKEN_REGEX, type PasteBlock } from '../utils/pasteTokens'

/* Each rule is a SUPERSET of a reader, so whatever that reader resolves, the
   check refuses. The gateway's readers are Python, so the client cannot call them;
   each rule names the reader it covers and the wider shape it tests instead. */

/** A line whose first non-blank character is `/`: a slash command or quick prompt
 *  (`chat_turn/slash_commands.py`, `first_word = message.split()[0]`; the client's
 *  `interceptSlashCommand` reads `raw.trim()`). Blank is JS `\s` (what `trim()`
 *  removes) plus U+0085, which Python's `split()` and `strip()` also remove
 *  (`displaySafe` already drops U+001C to U+001F). */
const LINE_SLASH_RE = /^[\s\u0085]*\//gmu
/** A line whose first non-blank character is `!`: the composer's terminal-command
 *  reader (`useTerminalCommand.terminalCommand`, a separated leading bang) is
 *  narrower, and a terminal command reaches the person's shell. */
const LINE_BANG_RE = /^[\s\u0085]*!/gmu
/** `@` anywhere but straight after a letter or digit (so `a@b.com` stays text):
 *  file and folder mentions (`fileTokens.leadingMentionBoundary`), a leading
 *  `@prompt` or `@knowledge`. */
const AT_RE = /(?<![\p{L}\p{N}])@/gu
/** `$` before a letter or digit, after anything: the gateway's `$skill` reader
 *  (`skills._DOLLAR_SKILL_PATTERN`, `(?<![\w$])\$[a-z0-9]`) is narrower. */
const DOLLAR_RE = /\$[a-z0-9]/giu
/** A path separator followed on the same line by an image suffix. Every path the
 *  gateway's image upgrade matches (`kiro_crew.image_refs._PATH_RE`, POSIX or
 *  Windows) starts with a separator and ends in one of these suffixes on one line. */
/** Markdown image syntax, any raw HTML tag, and any attachment or folder marker,
 *  however spaced or encoded: the transcript renderer passes raw HTML through
 *  (`rehypeRaw`) and fetches a local image, audio or video destination from either
 *  form (`ImgWithFallback`, `remoteMedia`, which decode it first), and
 *  `resolveFileSegment` / `resolveDirSegment` read markers with any blank after them.
 *  Every tag is refused, not only media tags, so no tag the renderer learns later
 *  can carry a destination through. */
const RENDERED_REF_RE = /!\[|<[\p{L}!/?]|\[attached_(?:file|dir)\b/giu
const IMAGE_PATH_RE = new RegExp(`[/\\\\][^\\n\\r\\u2028\\u2029]*?${IMG_EXT.source.replace(/\$$/, '')}`, 'giu')

/** How often a global `re` matches `text`; `match` starts from 0, so no state carries. */
const hits = (re: RegExp, text: string): number => text.match(re)?.length ?? 0

/** One count per reader the rules above cover, in their order. */
const READERS: readonly ((text: string) => number)[] = [
  t => hits(LINE_SLASH_RE, t),
  t => hits(LINE_BANG_RE, t),
  t => hits(AT_RE, t),
  t => hits(DOLLAR_RE, t),
  t => hits(PASTE_TOKEN_REGEX, t),
  t => parseFiles(t).length,
  t => parseDirs(t).length,
  t => hits(IMAGE_PATH_RE, t),
  t => hits(RENDERED_REF_RE, t),
]

/** Whether anything in `draft` would be read as more than text by the composer or
 *  the gateway: a command, a mention, a skill, a staged-paste token, an attachment
 *  or folder marker, an image path. */
export function composerWouldResolve(draft: string): boolean {
  return READERS.some(read => read(draft) > 0)
}

/** Whether `joined` (the draft with an app's text put in) gives any reader more
 *  than `draft` gave it: the text joined the person's own characters into a
 *  command, a mention, a skill, a token, a marker or a path. What the person typed
 *  that already resolves is theirs, so only a rise counts. */
export function joinWouldResolve(draft: string, joined: string): boolean {
  return READERS.some(read => read(joined) > read(draft))
}

/** An app's offered text as the composer takes it: newlines as `\n`, with control,
 *  bidirectional override and invisible characters dropped. */
export function cleanOfferedText(raw: string): string {
  return visibleOnly(displaySafe(raw.replace(/\r\n?/g, '\n')))
}

/** An offered text read on its own, before anything about the box is looked at, so
 *  a text refused here gets the same answer whatever the box holds: not a string or
 *  blank once cleaned (`not_allowed`), over `MAX_OFFER_CHARS` (`too_large`), or
 *  read as more than text alone (`not_literal`). */
export type OfferedText = { ok: true; text: string } | { ok: false; code: 'not_allowed' | 'too_large' | 'not_literal' }

export function readOfferedText(raw: unknown): OfferedText {
  if (typeof raw !== 'string') return { ok: false, code: 'not_allowed' }
  if (raw.length > MAX_OFFER_CHARS) return { ok: false, code: 'too_large' }
  const text = cleanOfferedText(raw)
  if (!text.trim()) return { ok: false, code: 'not_allowed' }
  if (composerWouldResolve(text)) return { ok: false, code: 'not_literal' }
  return { ok: true, text }
}

/** Attachment and folder markers whole, for `caretOutOfTokens`. */
const MARKER_SPAN_RE = /\[attached_(?:file|dir)\b[^\]\n]*\]?/giu
/** The readers above whose matches are a place in the draft. */
const SPAN_RES: readonly RegExp[] = [LINE_SLASH_RE, LINE_BANG_RE, AT_RE, DOLLAR_RE, PASTE_TOKEN_REGEX, MARKER_SPAN_RE, IMAGE_PATH_RE, RENDERED_REF_RE]

/** Where an insert meant for `at` goes so it neither splits nor sticks to anything
 *  in `draft` that resolves. Each match, widened to the whitespace-free runs at its
 *  ends, is a token; a point inside one, or at its first character (where the text
 *  would sit glued in front of it), moves to the token's end. A point at a token's
 *  end stays: `joinAtCaret` puts a space there. */
export function caretOutOfTokens(draft: string, at: number): number {
  let p = Math.max(0, Math.min(at, draft.length))
  for (let moved = true, rounds = 0; moved && rounds < 8; rounds++) {
    moved = false
    for (const re of SPAN_RES) {
      for (const m of draft.matchAll(re)) {
        let s = m.index ?? 0
        let e = s + m[0].length
        while (s > 0 && !/\s/u.test(draft[s - 1])) s--
        while (e < draft.length && !/\s/u.test(draft[e])) e++
        if (s <= p && p < e) { p = e; moved = true }
      }
    }
  }
  return p
}

/** `text` put into `draft` at offset `at`, or at the end when `at` is null, and the
 *  caret just after it. Nothing of the draft is replaced. When the text before the
 *  point is not empty and does not end in whitespace, one space goes first, so the
 *  text never runs on from the person's last word. The text's own trailing spaces
 *  stay, except before a space or a newline already there, so there is never a
 *  double space. */
export function joinAtCaret(draft: string, at: number | null, text: string): { value: string; caret: number } {
  const i = at == null ? draft.length : Math.max(0, Math.min(at, draft.length))
  const before = draft.slice(0, i)
  const after = draft.slice(i)
  const lead = before && !/\s$/u.test(before) ? ' ' : ''
  const body = /^[ \n]/u.test(after) ? text.replace(/ +$/u, '') : text
  return { value: `${before}${lead}${body}${after}`, caret: i + lead.length + body.length }
}

/** Code points that draw nothing (zero-width, tag characters, variation
 *  selectors, fillers): text built from them would reach the agent unseen. */
const INVISIBLE_RE = /\p{Default_Ignorable_Code_Point}/gu

/** `text` with every code point that draws nothing removed, so the draft holds
 *  only what the person can read before sending. */
export function visibleOnly(text: string): string {
  return text.replace(INVISIBLE_RE, '')
}

/** Whether the composer holds nothing of the person's: no text, no collapsed paste.
 *  Only such a composer takes an app's text. */
export function draftIsEmpty(value: string, pasteBlocks: readonly PasteBlock[]): boolean {
  return value.trim() === '' && pasteBlocks.length === 0
}
