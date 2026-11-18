/**
 * The Bot-chat stop warnings pass a `count` and let i18next pick each language's
 * plural form. ja, ko and zh-CN author only `_other`, and ru and bn add `_few` and
 * `_many`, so building a `_one` / `_other` key by hand shows English for one Bot in
 * the first three and never reaches the extra forms in the last two. Build the key
 * by hand again and the zh-CN, ja and ko rows below fail.
 */
import { afterEach, describe, expect, it } from 'vitest'

import { i18next } from '../../i18n/index'
import '../../i18n/all'
import { i18nT } from '../../i18n/t'
import { uninstallBotChatStopFailedNotice } from './backendRefused'

afterEach(async () => {
  if (i18next.language !== 'en') await i18next.changeLanguage('en')
})

const REVOKED = 'pages.appsPage.bot_chat_stop_incomplete_revoked'

describe('Bot-chat stop warnings pick the plural form from the count', () => {
  it('shows Chinese for a single Bot in zh-CN', async () => {
    await i18next.changeLanguage('zh-CN')
    expect(uninstallBotChatStopFailedNotice('Notes', { bot_chat_stop_failed: ['atlas'] }))
      .toBe('atlas 仍在使用 Notes 的工具运行。它们不会获得新工具。')
    expect(i18nT(REVOKED, { count: 1, slugs: 'atlas', name: 'Notes' }))
      .toContain('信任已撤销。atlas 还在用 Notes 的工具运行。')
  })

  it.each(['ja', 'ko'])('shows %s for a single Bot', async lang => {
    await i18next.changeLanguage(lang)
    const text = uninstallBotChatStopFailedNotice('Notes', { bot_chat_stop_failed: ['atlas'] })
    expect(text).not.toContain('still running')
    expect(i18nT(REVOKED, { count: 1, slugs: 'atlas', name: 'Notes' })).not.toContain('Trust removed')
  })

  it('reaches the Russian few form', async () => {
    await i18next.changeLanguage('ru')
    const few = i18next.getResource('ru', 'translation', 'pages.appsPage.uninstall_bot_chat_stop_failed_few')
    expect(uninstallBotChatStopFailedNotice('Notes', { bot_chat_stop_failed: ['a', 'b', 'c'] }))
      .toBe(String(few).replace('{{slugs}}', 'a, b, c').replace('{{name}}', 'Notes'))
  })

  it('keeps the English singular and plural', () => {
    expect(uninstallBotChatStopFailedNotice('Notes', { bot_chat_stop_failed: ['atlas'] }))
      .toBe("The bot atlas is still running with Notes's tools. It will not get new tools from it.")
    expect(uninstallBotChatStopFailedNotice('Notes', { bot_chat_stop_failed: ['atlas', 'nova'] }))
      .toBe("The bots atlas, nova are still running with Notes's tools. They will not get new tools from it.")
  })
})
