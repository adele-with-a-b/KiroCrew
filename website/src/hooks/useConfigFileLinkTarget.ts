import { useQuery } from '@tanstack/react-query'
import { fetchDashboardConfig } from '../api/dashboardConfigQuery'
import { setFileLinksOpenOutside } from '../components/markdown/fileLinkOutside'

/**
 * Register `dashboard.file_links_open_default_app` where a file link's click
 * reads it (`fileLinkOutside`), from the app shell, the same way
 * `useConfigAutolinkRules` registers the link rules: `MarkdownRenderer` renders
 * outside any query provider in places, so it cannot read the query itself.
 * Applied during render so the pass that delivers a change also clicks with it.
 * Only a config that says `true` turns it on: while the config is loading,
 * after a failed read, and from a gateway that does not know the key, a click
 * opens the side panel.
 */
export function useConfigFileLinkTarget(): void {
  const { data } = useQuery<{ file_links_open_default_app?: boolean }>({
    queryKey: ['dashboardConfig'],
    queryFn: fetchDashboardConfig,
    staleTime: 30_000,
  })
  setFileLinksOpenOutside(data?.file_links_open_default_app === true)
}
