/**
 * The trust gate names `permissions.openFiles` before the owner says yes: an app
 * that declares it can open a file in its default app when the person clicks a
 * path it shows. `trustGrants` reads only the manifest's literal `true`.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'

vi.mock('../i18n/t', () => ({
  i18nT: (key: string, vars?: Record<string, unknown>) =>
    vars && Object.keys(vars).length ? `${key} ${Object.values(vars).join(' ')}` : key,
}))

import TrustAppModal, { type TrustAppTarget } from '../components/appstore/TrustAppModal'
import { trustGrants } from '../components/appstore/trustGrants'

const K = 'components.appstore.trustAppModal'

const BASE: TrustAppTarget = {
  name: 'crew-app',
  displayName: 'Crew App',
  origin: 'registry',
  trustRepository: 'https://git.example.test/org/crew-app',
  retryInstalls: true,
}

function renderModal(app: TrustAppTarget) {
  render(
    <TrustAppModal app={app} pending={false} failed={false} granted={false} onCancel={() => {}} onConfirm={() => {}} />,
  )
  return within(screen.getByRole('dialog'))
}

describe('TrustAppModal open-files disclosure', () => {
  it('names the declaration in its own box when the app declares it', () => {
    const dialog = renderModal({ ...BASE, ...trustGrants({ openFiles: true }) })
    expect(dialog.getByTestId('trust-open-files')).toBeTruthy()
    expect(dialog.getByText(`${K}.open_files_heading`)).toBeTruthy()
    expect(dialog.getByText(`${K}.open_files_desc`)).toBeTruthy()
  })

  it('shows no such box for an app that does not declare it, or declares anything but true', () => {
    const dialog = renderModal({ ...BASE, ...trustGrants({ openFiles: 'yes' as unknown as boolean }) })
    expect(dialog.queryByTestId('trust-open-files')).toBeNull()
    expect(trustGrants({}).openFiles).toBe(false)
  })
})
