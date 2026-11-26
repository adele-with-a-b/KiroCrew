/**
 * The "N answers sent" fold of a person's row sent from a card's answering mode
 * (`meta.answers`, kept by the gateway only on a person's own send:
 * `dashboard/chat_api/answers_meta.py`). The row shows Crew's line in place of
 * the message, and the whole message opens behind "Show full message", like a
 * scripted send's banner (`UserMessage.tsx`). The words are Crew's; only the
 * app's name comes from the row.
 *
 * @module linkPreview/answering/answersFold
 */
import { i18nT } from '../../i18n/t'
import { MAX_ANSWERS } from './answeringProtocol'

export interface AnswersFold { count: number; app: string }

/** The row's fold, or null when it has none or not in its one shape. */
export function readAnswersFold(meta: unknown): AnswersFold | null {
  const raw = meta && typeof meta === 'object' ? (meta as { answers?: unknown }).answers : undefined
  if (!raw || typeof raw !== 'object') return null
  const { count, app } = raw as { count?: unknown; app?: unknown }
  if (typeof count !== 'number' || !Number.isInteger(count) || count < 1 || count > MAX_ANSWERS) return null
  if (typeof app !== 'string' || !app.trim() || app.length > 64) return null
  return { count, app: app.trim() }
}

/** The fold's one line: "✓ 2 answers sent · <app>". */
export function answersFoldLine(fold: AnswersFold): string {
  return `✓ ${i18nT('components.linkPreview.answering.answers_sent', { count: fold.count })} · ${fold.app}`
}
