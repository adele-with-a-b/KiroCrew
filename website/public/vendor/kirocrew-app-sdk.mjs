// Vendor stub: re-exports @kirocrew/app-sdk from the host.
const m = window.__kirocrew_modules?.['@kirocrew/app-sdk']
if (!m) throw new Error('[vendor/kirocrew-app-sdk] Host modules not initialized.')
export const {
  useAppApi, useAppEvents, useTheme, useAppInfo, useNavigate, useNotify,
  useNavBadge, useAppNotifications, useChatLauncher, AppApiProvider,
  useImeGuard, useLanguageGeneration, activeLocale,
  fmtNumber, fmtCurrency, fmtDate, fmtTime, fmtDateTime, fmtRelative, compareText,
  // The App Kit seam level, so an app can feature-detect what this host provides.
  APP_SDK_LEVEL,
  // An app's own origin on the dashboard's port (level 8), and why it has one or not (feature-detect by name).
  appWebOrigin, appWebOriginStatus,
  // Marker protocol. Naming an export the host does not provide is a load-time failure for the
  // whole app, so this list is checked against the protocol barrel by chatProtocolBoundary.test.ts.
  parseOptions, deriveFollowUpOptions, extractSteeringAcks, stripPartialOptionMarker,
  // Chat surfaces and the transcript row registry.
  useChatSession, ChatPanel, ChatEmbed, ChatMessageList,
  // The dashboard's pull-request panel, behind the app's declared source paths (level 2).
  PullRequestPanel,
  // Issue lists behind the app's declared `/api/source/issues` (level 6).
  useIssues, IssueList, IssueCard,
  // The dashboard's project picker behind the app's declared listing paths (level 7).
  ProjectPicker,
  // The dashboard's live session list and session acts behind the app's declared chat paths (level 8).
  useHostSessions,
  // What this app's side-panel tab was opened with by its own link-preview card.
  usePanelTabRequest,
  // The way back after a status item's go sent the operator to this tab's chat.
  useStatusGoBack,
  // An app re-reads its own status in every owner window (level 10).
  useAppStatusRefresh,
  // What this app's session control was opened with by its own card or a status go (level 11).
  useSessionControlRequest,
  // An app opens a local file in the default app, on a click (level 12).
  useOpenFile,
  defaultMessageRenderers, mergeRenderers, resolveRenderer, ToolCallPill, GROUPED_ROLES,
  // WS event scope prediction, so a subscription that will never be delivered warns at
  // development time instead of silently receiving nothing.
  checkSubscribeAllowed,
} = m
