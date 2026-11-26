# Docked Notes Example

A session control whose box opens above the message box and stays open while
you read, scroll and click the chat. It keeps one note per chat while the page
is open. It uses `dock` and `stayOpen` (App Kit seam level 16; see
[`contributes.sessionControls`](../../manifest-reference.md#contributessessioncontrols--a-per-chat-control-in-the-composer)).

- `app.json` declares `"dock": true` and `"stayOpen": true` on the control, and a
  key (`Alt+Shift+J`) that opens and closes it.
- `ui/dist/session-control.mjs` is the control. It is plain ESM with no build
  step. Kiro Crew draws the box's header (the app's name, the label and a close
  button), so the module draws only its content.

Install it from this folder:

```bash
kirocrew app install ./docs/app-kit/examples/docked-control-app
```

Then open a chat and click the Notes chip under the message box (or press the
key). Type a note, then scroll and click the chat: the box stays. Close it with
the chip, the × in its header, Escape while typing in it, or, when the key
opened it, the key again.

The box also lists the notes you wrote in other chats. A click on one opens the
box in that chat (`useSessionControlGo`, App Kit seam level 17; see
[Session Control Go](../../api-reference.md#session-control-go)), where it says it
came from another chat and offers the way back.
