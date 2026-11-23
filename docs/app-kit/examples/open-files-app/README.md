# Open Files Example

A session control that shows a file path and opens that file in its default app
when you click the path. It uses `useOpenFile` from `@kirocrew/app-sdk`
(App Kit seam level 12; see
[Opening a local file](../../api-reference.md#opening-a-local-file)).

- `app.json` declares `permissions.openFiles: true`. The trust prompt and the
  app's page show it. An update that adds it waits for your approval there.
- `ui/dist/session-control.mjs` is the control. It is plain ESM with no build
  step. It calls `openFile(path)` from the path's click handler, because one
  real click inside the control opens one file.

Install it from this folder:

```bash
kirocrew app install ./docs/app-kit/examples/open-files-app
```

Then open a chat, click the control's chip under the message box, type a path
and click it. A path Kiro Crew keeps private (a `.env`, a credential folder)
is refused with the dashboard's own message. So is a relative path, or a
browser that is not on the gateway's machine.
