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
 * Level 14 adds a session control's `keys`: a key, changeable in Settings →
 * Shortcuts → Apps, opens its box in the chat on screen with `via: 'key'`.
 * Level 15 adds a session control status answer's `replyChips`: an app supplies
 * the strings of Crew's reply chips for one chat.
 * Level 16 adds a session control's `dock` and `stayOpen`: its box opens in the
 * composer dock, above the message box, and may stay open while the chat is used.
 * Level 17 adds `useSessionControlGo`: on a click in its box, a session control
 * opens its own box in another chat, read there with `via: 'control'`.
 * Level 18 adds a session control's `composerInsert` and `useInsertIntoComposer`:
 * a docked box puts text in its chat's message box at the caret, and reads
 * nothing back but whether it went in.
 * Level 19 adds a session control key's `keepOpen`: each press on the open box
 * hands it the key's `arg` again, as a new request, and never closes it.
 */
export const APP_SDK_LEVEL = 19
