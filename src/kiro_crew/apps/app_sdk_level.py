"""The App Kit seam level this gateway provides (adoption pins require a minimum).

Level 9 adds ``permissions.bots``: an app's backend reaches the Bots the owner ticks
for it with its app token. Level 10 adds ``POST /api/apps/<app>/status-changed`` and
the SDK's ``useAppStatusRefresh``: an app has the owner's windows re-read its own
status routes at once. Level 11 adds the session-control open: a link-preview card's
``card.openSessionControl``, a status target's ``sessionControl``, the SDK's
``useSessionControlRequest``, and the ``opened`` answer to a card's open. Level 12
adds ``permissions.openFiles`` and the SDK's ``useOpenFile``: an app's UI opens a
local file in the default app on a click. Level 13 adds the SDK's
``useSessionControlJump``: a session control scrolls its own chat to one of its
messages, with the Back pill. Level 14 adds a session control's ``keys``: a key,
changeable in Settings → Shortcuts → Apps, opens its box in the chat on screen, and
the box reads the key's ``arg`` with ``via: 'key'``. Level 15 adds a session control
status answer's ``replyChips``: an app supplies the strings of Crew's reply chips for
one chat. Level 16 adds a session control's ``dock`` and ``stayOpen``: its box opens in
the composer dock, between the chat and the message box, and may stay open while the
person reads and clicks the chat. Level 17 adds the SDK's ``useSessionControlGo``: on a
click in its box, a session control opens its own box in another chat, with a request
the box reads as ``via: 'control'``. Level 18 adds a session control's
``composerInsert`` and the SDK's ``useInsertIntoComposer``: a docked box puts text in
its chat's message box at the caret, beside the draft, and reads nothing back but
whether it went in. Level 19 adds a session control key's ``keepOpen``: each press on
the open box hands it the key's ``arg`` again, as a new request, and never closes it.
"""

APP_SDK_LEVEL = 19
