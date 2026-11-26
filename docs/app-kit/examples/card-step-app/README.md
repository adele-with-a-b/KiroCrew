# Card Step Example

A link-preview card for labels such as `[A1]` or `[B12c]` written in chat
messages. It pins itself and steps from label to label with `card.step` (App Kit
seam level 20; see
[`contributes.linkPreviews`](../../manifest-reference.md#contributeslinkpreviews--hover-cards-for-links)).

- `app.json` declares one text pattern and `"sessionKey": true`: a step names a
  message of the chat, so the owner approves the app for it.
- `ui/dist/labels.mjs` is the card. It is plain ESM with no build step and no
  backend, so it knows only the labels its frame has been shown (hover each once),
  in chat order. A real app would list the chat's labels from its own backend.

Install it from this folder:

```bash
kirocrew app install ./docs/app-kit/examples/card-step-app
```

Approve it on its app page, then open a chat whose replies carry labels. Hover a
few of them, then click one and click in its card:

- ↑ and ↓ (the buttons, or the arrow keys with focus in the card) pin the card and
  step it to the previous or next label. A label in view: the card moves and the
  chat stays still. A label far up: the chat scrolls there, lights its message,
  offers "Back to where you were", and the card is shown again beside the label.
  The chat's live region says the card's line, such as `[A3] · 3 of 8`.
- Home and End step to the first or last label seen, however far apart.
- **Go there**, on a label such as `[A4b]`, steps to `[A4]`.

A refusal is shown under the buttons, for example `Not moved: not_found` when the
label is no longer in the chat.
