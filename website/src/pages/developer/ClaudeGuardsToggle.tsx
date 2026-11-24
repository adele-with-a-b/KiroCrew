import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { api } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { SettingsToggle } from '../../components/settings'
import { HarnessRowDeepLink } from './HarnessRowDeepLink'

export const CLAUDE_GUARDS_CONFIG_KEY = 'agent.claude_guards'

/**
 * The Claude Code row's switch for the broader starting-mode pin: every Claude Code
 * chat that carries the product's tools is moved to the asking mode before its first
 * prompt, so the gate judges its calls. Read when each chat starts, so no restart.
 */
export function ClaudeGuardsToggle() {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const config = useQuery<{ agent?: { claude_guards?: boolean } }>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  const save = useMutation({
    mutationFn: (value: boolean) => api.patchConfig(CLAUDE_GUARDS_CONFIG_KEY, value),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
  })

  return (
    <div className="space-y-2">
      <SettingsToggle
        configKey="agent.claude_guards"
        label={t('pages.developer.agentBackendTab.claude_guards_label')}
        description={t('pages.developer.agentBackendTab.claude_guards_description')}
        hint={t('pages.developer.agentBackendTab.claude_guards_hint')}
        checked={config.data?.agent?.claude_guards === true}
        disabled={!config.data || config.isError}
        busy={save.isPending}
        onChange={value => save.mutate(value)}
      />
      {save.isError && (
        <ErrorNotice
          message={t('pages.developer.agentBackendTab.claude_guards_save_failed')}
          variant="inline"
          askAgent
        />
      )}
    </div>
  )
}

/** Settings search must open the Claude Code row before its switch can be highlighted. */
export function ClaudeGuardsDeepLink({ onOpen }: { onOpen: () => void }) {
  return (
    <HarnessRowDeepLink
      configKey={CLAUDE_GUARDS_CONFIG_KEY}
      settingId="agent.guard-claude-code-chats"
      onOpen={onOpen}
    />
  )
}
