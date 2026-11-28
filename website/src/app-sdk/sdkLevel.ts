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
 * Level 20 adds a link-preview card's `card.step`: a pinned card moves to another
 * of its provider's labels in the same chat, which the chat brings into view, and
 * the chat's live region says the app's line.
 * Level 21 adds a link-preview provider's `answering` and a pinned card's
 * `card.answering`: on the person's press the card puts its chat into the
 * answering mode, where Crew draws the answer line and holds the keys; the card
 * hands over the message text, and the person's Shift+Enter, received by Crew,
 * sends it once as their message and ends the mode.
 * Level 22 lets the card whose answering mode is on move while the person answers:
 * right after Crew tells it their Enter (`kept`), a declared `key` or a `button`,
 * one `card.step` or `card.jumpTo` counts as their press, though focus is in
 * Crew's line.
 */
export const APP_SDK_LEVEL = 22
