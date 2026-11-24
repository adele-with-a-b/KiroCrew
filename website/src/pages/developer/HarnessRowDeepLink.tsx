import { useEffect, useRef } from 'react'
import { useSearchParams } from 'react-router-dom'
import { resolveLegacyHighlightId } from '../../hooks/useSettingHighlight'

/**
 * Settings search must open a harness row before a switch inside its detail can be
 * highlighted. Opens the row when the URL's highlight names the switch, by its
 * config key or by its registry id.
 */
export function HarnessRowDeepLink({
  configKey,
  settingId,
  onOpen,
}: {
  configKey: string
  settingId: string
  onOpen: () => void
}) {
  const [params] = useSearchParams()
  const highlight = params.get('highlight')
  const wanted =
    highlight === `key:${configKey}` ||
    (highlight !== null && resolveLegacyHighlightId(highlight) === settingId)
  const callback = useRef(onOpen)
  callback.current = onOpen
  useEffect(() => {
    if (wanted) callback.current()
  }, [wanted])
  return null
}
