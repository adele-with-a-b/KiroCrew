"""The App Kit seam level this gateway provides (adoption pins require a minimum).

Level 9 adds ``permissions.bots``: an app's backend reaches the Bots the owner ticks
for it with its app token. Level 10 adds ``POST /api/apps/<app>/status-changed`` and
the SDK's ``useAppStatusRefresh``: an app has the owner's windows re-read its own
status routes at once.
"""

APP_SDK_LEVEL = 10
