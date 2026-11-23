/**
 * `useOpenFile`: an app's UI opens a local file the way the chat does, on the
 * person's click.
 *
 * The app hands it an absolute path (or a `file://` URL) from inside the click
 * handler of something it drew. The host opens the file in the OS default app
 * through the chat's own route (`POST /api/reveal`, `action: "open"`), so every
 * path rule is that route's: a sensitive path (credential homes, the data home's
 * hidden leaves, `.env`), a directory, a remote browser and a host with no desktop
 * are refused there. The host itself serves the call only when:
 *
 * - the app declares `permissions.openFiles: true` and the owner approved it
 *   (the row's `openFilesGrant`), else `not_granted`;
 * - the path is absolute, or a `file://` URL of one, else `not_absolute`;
 * - it comes within `CLICK_GRANT_MS` of a real click in this app's own surface,
 *   and that click has opened nothing yet, else `no_click`. One click opens one file.
 *
 * Like every `useAppApi` check this runs in the dashboard document, so it is a
 * guardrail for an app that plays by it, not a boundary
 * (`docs/architecture/app-platform-trust-model.md`).
 *
 * The host side is `components/appFileOpen.ts`; this module holds only the
 * contract an app sees, so the SDK barrel publishes nothing of the host's.
 *
 * @module app-sdk/openFile
 */
import { createContext, createElement, useContext, type ReactNode } from 'react'

/** Why a file did not open. `unavailable`: this browser is not on the gateway's
 *  machine, or that machine cannot open apps now (the path may have been copied
 *  instead); `blocked`: the path is one Crew never opens; `failed`: anything else. */
export type OpenFileRefusal = 'not_granted' | 'not_absolute' | 'no_click' | 'unavailable' | 'blocked' | 'failed'

/** What one open came to. `message` is the host's own words for a refusal the
 *  person should see, in their language. */
export type OpenFileResult = { ok: true } | { ok: false; reason: OpenFileRefusal; message?: string }

/** Open `path` (absolute, or a `file://` URL) in the OS default app. */
export type OpenFile = (path: string) => Promise<OpenFileResult>

const refuse: OpenFile = () => Promise.resolve({ ok: false, reason: 'not_granted' })

const OpenFileContext = createContext<OpenFile | null>(null)

/** Host only: the opener the app module below may call. */
export function AppFileOpenScope({ open, children }: { open: OpenFile; children: ReactNode }) {
  return createElement(OpenFileContext.Provider, { value: open }, children)
}

/** This app's file opener. Call it from a click handler. Outside a host that serves
 *  it (an older host, a surface with none) every call resolves `not_granted`. */
export function useOpenFile(): OpenFile {
  return useContext(OpenFileContext) ?? refuse
}
