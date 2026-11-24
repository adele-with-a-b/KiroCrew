import { memo } from 'react'

import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import NoticeCard from './NoticeCard'

/**
 * The restart receipt of a chat a chat keeper kept running: one line, and a
 * closed fold with what the keeper measured across the restart. `content` is the
 * line's localized copy and `details` the fold's lines (`gatewayNotice.ts`,
 * `restartReceiptDetails`), so this draws and words nothing of its own.
 */
export default memo(function RestartReceiptNotice({ content, details }: { content: string; details: string[] }) {
  useLanguageGeneration()
  return (
    <NoticeCard
      content={content}
      tone="info"
      body={
        <>
          {content}
          <details className="mt-1 text-[12px]" data-testid="restart-receipt-details">
            <summary className="cursor-pointer underline underline-offset-2 hover:text-text">
              {i18nT('pages.chat.gatewayNotice.restart_receipt_details')}
            </summary>
            <ul className="mt-1 space-y-0.5">
              {details.map(line => <li key={line}>{line}</li>)}
            </ul>
          </details>
        </>
      }
    />
  )
})
