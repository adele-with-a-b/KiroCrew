/**
 * The App Kit seam level this dashboard provides. Mirrors
 * `src/kiro_crew/apps/app_sdk_level.py`; `test/test_app_sdk_level_mirror.py` pins
 * the two equal. An app bundle reads it (namespace import, feature-detected) to
 * tell an older host from one that has the seams it was built against. Level 9
 * adds `permissions.bots`: an app's backend reaches the Bots the owner ticks for it
 * with its app token. Level 10 adds `useAppStatusRefresh` and
 * `POST /api/apps/<app>/status-changed`: an app re-reads its own status chips and top-bar items at once.
 * Level 11 adds the session-control open: `card.openSessionControl`, a status target's
 * `sessionControl`, `useSessionControlRequest`, and the `opened` answer to a card's open.
 * Level 12 adds `permissions.openFiles` and `useOpenFile`: an app opens a local file on a click.
 * Level 13 adds `useSessionControlJump`: a session control scrolls its own chat to
 * one of its messages, with the Back pill.
 */
export const APP_SDK_LEVEL = 13
