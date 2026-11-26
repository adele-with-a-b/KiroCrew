import type { ChatMessage } from '../../types'
import { quoteFromMessage, type MessageQuote } from '../../chat-core/composer/messageQuote'
import { isAskQuestionToolMessage } from '../../utils/askQuestionAnswers'
import { isReplyText, opensRunTurn } from './groupDisplayItems'

/**
 * What a pending question is about: the agent's nearest reply text above the
 * running turn's newest `ask_question` tool row, as the quote record the docked
 * card draws and jumps to.
 *
 * kiro-cli's native card (`native`) is raised by the harness, not by the tool,
 * so it has no such row and the nearest reply text above the transcript's end
 * is its subject; so is it for a card whose row carries no tool identity (a
 * backend that sends none). The walk never leaves the running turn: a reply
 * from an earlier turn is not what this question asks about. `null` when the
 * turn has no reply text loaded above the question.
 */
export function questionSubject(messages: ChatMessage[], native = false): MessageQuote | null {
  let subject: ChatMessage | undefined
  let pastAsk = native
  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i]
    if (opensRunTurn(msg)) break
    // Text written after the ask is not its subject; start again above the row.
    if (!pastAsk && isAskQuestionToolMessage(msg)) { pastAsk = true; subject = undefined; continue }
    if (!isReplyText({ kind: 'single', msg, idx: i })) continue
    if (pastAsk) { subject = msg; break }
    subject ??= msg
  }
  if (!subject) return null
  const mid = (subject.meta as Record<string, unknown> | undefined)?.mid
  return quoteFromMessage('assistant', subject.content, subject.ts, typeof mid === 'string' ? mid : undefined)
}
