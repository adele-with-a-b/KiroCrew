/**
 * The answering mode as the link-preview layer makes it: the owner's grants and
 * the app's name from the resolved providers, messages to the card through its
 * frame, and lines in the chat's live region.
 *
 * @module linkPreview/answering/answeringLayer
 */
import { i18nT } from '../../i18n/t'
import { sayStepLine } from '../../pages/chat/turnStep'
import { postToFrame } from '../../appFrame/sandboxFrame'
import type { ResolvedLinkPreview } from '../linkPreviewRegistry'
import { AnsweringMode } from './answeringMode'

export function currentAnsweringMode(providers: () => readonly ResolvedLinkPreview[]): AnsweringMode {
  const find = (key: string) => providers().find((p) => p.key === key)
  return new AnsweringMode({
    granted: (key) => find(key)?.answering === true,
    appTitle: (key, appName) => find(key)?.title ?? appName,
    post: (frame, msg) => postToFrame(frame.el, msg),
    say: (what, count) => {
      sayStepLine(
        what === 'entered' ? i18nT('components.linkPreview.answering.say_entered')
          : what === 'sent' ? i18nT('components.linkPreview.answering.answers_sent', { count })
            : i18nT('components.linkPreview.answering.say_ended'),
      )
    },
  })
}
