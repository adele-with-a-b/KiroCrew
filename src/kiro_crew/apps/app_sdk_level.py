"""The App Kit seam level this gateway provides (adoption pins require a minimum).

Level 9 adds ``permissions.bots``: an app's backend reaches the Bots the owner ticks
for it with its app token. Level 10 adds ``POST /api/apps/<app>/status-changed`` and
the SDK's ``useAppStatusRefresh``: an app has the owner's windows re-read its own
status routes at once. Level 11 adds the session-control open: a link-preview card's
``card.openSessionControl``, a status target's ``sessionControl``, the SDK's
``useSessionControlRequest``, and the ``opened`` answer to a card's open. Level 12
adds ``permissions.openFiles`` and the SDK's ``useOpenFile``: an app's UI opens a
local file in the default app on a click.
"""

APP_SDK_LEVEL = 12
