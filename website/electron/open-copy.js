"use strict";

/**
 * open-copy.js: open a COPY of a file the dashboard read from its gateway, in
 * the OS default app on the machine in front of the user.
 *
 * Why a copy. A chat file link names a path on the GATEWAY's machine. When the
 * desktop app shows a Crew on another computer, that path does not exist here,
 * so `dashboard:open-file` (which opens a real local file) cannot help. The
 * dashboard instead reads the file through the same routes the side panel uses
 * (`/api/file-html`, `/api/file-read`, `/api/file-raw`), which keep their
 * sensitive-path gate and credential pass, and hands the bytes here. A web page
 * always comes this way, even from a Crew on this machine, because only a copy
 * can be put in a sandbox (below). Edits to a copy never reach the original;
 * the copy is written read-only so an editor says so.
 *
 * What may be opened. The renderer and its agent-authored transcript are
 * untrusted, so the decision is made here, on the NAME the copy gets, because
 * the extension is what picks the app that opens it:
 *   - the types `dashboard:open-file` may open (`openableExts`, passed in by
 *     the registrar so the two cannot drift), whose default apps show text or
 *     an image and run nothing;
 *   - `.html` / `.htm`, which open in the default browser. The file written is
 *     NOT the page: it is a small page of this module's own (`sandboxShell`)
 *     whose only content is one full-window `<iframe sandbox="">` holding the
 *     page as its `srcdoc`. The frame refuses scripts, event handlers, a meta
 *     refresh, navigating the window, forms, popups, plugins and downloads
 *     whatever the page spells, the posture the side panel's own
 *     `sandbox=""` frame gives it. A policy written into the page could not
 *     do that: no Content-Security-Policy directive stops a meta refresh, which
 *     would take the window to the original file with its scripts running.
 * Anything else, a name with no extension, a dotfile, or a body over
 * `MAX_COPY_BYTES` is refused. Each copy goes in its own fresh private folder
 * in the OS temp directory with a sanitized base name, so the page can neither
 * pick where it lands nor overwrite anything. The folders are removed again:
 * the ones this process made at app quit (`removeOwnCopies`), and any older
 * than `COPY_MAX_AGE_MS` at app start and each time a copy is made
 * (`pruneCopies`), so a second build running beside this one keeps its own.
 *
 * Dependency-injected (`fs`, `path`, `os`, `shell`, `openPathHardened`) so
 * test/open-copy.test.js pins every rule without Electron and without
 * launching an app.
 */

/** Same ceiling as the gateway's whole-page route (`_HTML_VIEW_MAX_BYTES`). */
const MAX_COPY_BYTES = 128 * 1024 * 1024;

/** How long a copy is kept while the app runs. */
const COPY_MAX_AGE_MS = 24 * 60 * 60 * 1000;

const HTML_EXTS = new Set([".html", ".htm"]);

/**
 * Name prefix of each copy's own folder, made directly under the OS temp
 * directory by `mkdtemp`. No shared parent folder: on Linux the temp directory
 * is shared by every user, and a fixed parent someone else made first (or
 * pointed elsewhere with a symlink) would let them swap the copy before it
 * opens.
 */
const COPY_PREFIX = "kirocrew-open-copy-";

/** A folder `mkdtemp` made from `COPY_PREFIX` (six random letters or digits). */
const COPY_DIR_RE = /^kirocrew-open-copy-[A-Za-z0-9]{6}$/;

/**
 * The copy's file name: the base name only, with separators, control
 * characters, invisible format characters (direction marks among them) and
 * characters a file system refuses taken out, at most 200 characters, keeping
 * the extension. Empty when nothing usable is left. A direction mark could
 * make the name read with another extension than the one that picks the app.
 *
 * @param {unknown} name
 * @returns {string}
 */
function copyName(name) {
  if (typeof name !== "string") return "";
  const base = name.split(/[\\/]/).pop() || "";
  const clean = base.replace(/[\u0000-\u001f\u007f<>:"|?*\p{Cf}]/gu, "").trim();
  if (clean.length <= 200) return clean;
  const dot = clean.lastIndexOf(".");
  const ext = dot > 0 ? clean.slice(dot) : "";
  return ext.length < 20 ? clean.slice(0, 200 - ext.length) + ext : clean.slice(0, 200);
}

/**
 * The shell's policy, which its `srcdoc` frame inherits. The frame's sandbox
 * stops what can run or move; this stops what the page may LOAD, because a
 * page at a `file://` shell could otherwise pull this machine's files in:
 * WebKit loads a `file:` image, stylesheet or frame from inside the
 * sandboxed frame, and on Windows a `file://host/share` address starts an SMB
 * handshake. Allowed: images, media, fonts and stylesheets from `data:` (a
 * single-file mock carries its images that way), `https:` and `http:` (a mock
 * may load a web font or a remote image, as it can in the side panel), and
 * inline styles. Everything else, `file:` first, is refused: scripts, frames,
 * workers, plugins, fetches, form sends and manifests. No `base-uri`: the
 * page's in-page links need the `about:srcdoc` base `sandboxShell` gives it.
 */
const SHELL_POLICY = [
  "default-src 'none'",
  "img-src data: https: http:",
  "media-src data: https: http:",
  "font-src data: https: http:",
  "style-src 'unsafe-inline' data: https: http:",
  "script-src 'none'",
  "object-src 'none'",
  "frame-src 'none'",
  "child-src 'none'",
  "worker-src 'none'",
  "connect-src 'none'",
  "form-action 'none'",
  "manifest-src 'none'",
].join("; ");

/** Text safe inside a double-quoted attribute value. */
function escapeAttr(s) {
  return s.replace(/&/g, "&amp;").replace(/"/g, "&quot;");
}

/** Text safe as element content. */
function escapeText(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/**
 * The file a web page copy is written as: a page of this module's own whose
 * only content is one full-window `<iframe sandbox="">` with the page as its
 * `srcdoc` (escaped, so nothing in it can end the attribute).
 *
 * The page gets `<base href="about:srcdoc" target="_self">` as its first
 * element (after a leading doctype): a `srcdoc` document's links otherwise
 * resolve against the shell's own file, so an in-page `#section` link would
 * load the shell again inside the frame instead of scrolling. The first
 * `<base>` with an `href`, and the first with a `target`, win, so one in the
 * page changes neither (a page `<base target="_top">` would otherwise turn
 * every link into a window navigation, which the frame refuses), and it could
 * never reach the shell outside the frame anyway. The shell carries no script
 * and `SHELL_POLICY`, which the frame inherits (no `file:` load, no script),
 * and the bytes are the UTF-8 the gateway's whole-page route serves.
 *
 * @param {Buffer} bytes
 * @param {string} title
 * @returns {Buffer}
 */
function sandboxShell(bytes, title) {
  let page = bytes.toString("utf8");
  if (page.charCodeAt(0) === 0xfeff) page = page.slice(1);
  const m = /^[\t\n\f\r ]*(?:<!doctype[^>]*>)?/i.exec(page);
  const at = m ? m[0].length : 0;
  page = page.slice(0, at) + "<base href=\"about:srcdoc\" target=\"_self\">" + page.slice(at);
  const t = escapeText(title);
  return Buffer.from(
    "<!DOCTYPE html>\n<html><head><meta charset=\"utf-8\">"
    + `<meta http-equiv="Content-Security-Policy" content="${SHELL_POLICY}">`
    + `<title>${t}</title>`
    + "<style>html,body{margin:0;height:100%;overflow:hidden;background:#fff}"
    + "iframe{display:block;border:0;width:100%;height:100%}</style></head>\n"
    + `<body><iframe sandbox="" title="${escapeAttr(title)}" srcdoc="${escapeAttr(page)}"></iframe></body></html>\n`,
    "utf8",
  );
}

/**
 * Remove this user's copy folders in the OS temp directory older than
 * *maxAgeMs* (0: every one). Only a real directory named like `COPY_PREFIX`
 * made, owned by this user, is touched: a symlink or someone else's folder of
 * that name is left alone. Returns how many were removed; never throws.
 *
 * @param {{fs: typeof import("fs"), path: typeof import("path"), os: {tmpdir: () => string}}} deps
 * @param {number} maxAgeMs
 * @param {number} [now]
 * @returns {number}
 */
function pruneCopies(deps, maxAgeMs, now = Date.now()) {
  const tmp = deps.os.tmpdir();
  let names;
  try {
    names = deps.fs.readdirSync(tmp);
  } catch {
    return 0;
  }
  const uid = typeof process.getuid === "function" ? process.getuid() : null;
  let removed = 0;
  for (const name of names) {
    if (!COPY_DIR_RE.test(name)) continue;
    const dir = deps.path.join(tmp, name);
    try {
      const st = deps.fs.lstatSync(dir);
      if (st.isSymbolicLink() || !st.isDirectory()) continue;
      if (uid !== null && st.uid !== uid) continue;
      if (now - st.mtimeMs < maxAgeMs) continue;
      deps.fs.rmSync(dir, { recursive: true, force: true });
      removed += 1;
    } catch {
      // One entry that cannot be read or removed never stops the rest.
    }
  }
  return removed;
}

/** The copy folders this process made, which `removeOwnCopies` takes at quit. */
const ownCopies = new Set();

/**
 * Remove every copy folder this process made (at app quit). A second build
 * running beside this one keeps its own; `pruneCopies` by age catches what a
 * run that ended without quitting left. Returns how many; never throws.
 *
 * @param {{fs: typeof import("fs")}} deps
 * @returns {number}
 */
function removeOwnCopies(deps) {
  let removed = 0;
  for (const dir of ownCopies) {
    ownCopies.delete(dir);
    try {
      // No `force`: a folder already gone throws and is not counted.
      deps.fs.rmSync(dir, { recursive: true });
      removed += 1;
    } catch {
      // Already gone, or not removable: nothing more to do at quit.
    }
  }
  return removed;
}

/**
 * Write *bytes* as a read-only copy named *name* and open it in the default app.
 *
 * @param {{
 *   fs: typeof import("fs"),
 *   path: typeof import("path"),
 *   os: {tmpdir: () => string},
 *   shell: object,
 *   openPathHardened: (shell: object, p: string) => Promise<string>,
 *   openableExts: Set<string>,
 *   log?: (msg: string) => void,
 * }} deps
 * @param {unknown} name - the file's own name, as the dashboard shows it
 * @param {unknown} bytes - the file's content, as read from the gateway
 * @returns {Promise<{ok: boolean, error?: string}>}
 */
async function openCopy(deps, name, bytes) {
  const safe = copyName(name);
  const dot = safe.lastIndexOf(".");
  const ext = dot > 0 ? safe.slice(dot).toLowerCase() : "";
  const isHtml = HTML_EXTS.has(ext);
  if (!ext || (!isHtml && !deps.openableExts.has(ext))) {
    return { ok: false, error: "unsupported file type" };
  }
  if (!(bytes instanceof Uint8Array) && !(bytes instanceof ArrayBuffer)) {
    return { ok: false, error: "no content" };
  }
  const body = Buffer.from(bytes instanceof ArrayBuffer ? new Uint8Array(bytes) : bytes);
  if (body.length > MAX_COPY_BYTES) return { ok: false, error: "file too large" };
  pruneCopies(deps, COPY_MAX_AGE_MS);
  try {
    // A fresh private folder per copy (`mkdtemp`: created atomically, owner
    // only): two files of one name never meet, and nobody can aim the write.
    // The disk work is asynchronous so a large page does not hold the main
    // process. shortcut: building a page's shell (escaping up to
    // MAX_COPY_BYTES) still runs on it; move it to a worker if that shows.
    const dir = await deps.fs.promises.mkdtemp(deps.path.join(deps.os.tmpdir(), COPY_PREFIX));
    ownCopies.add(dir);
    const file = deps.path.join(dir, safe);
    await deps.fs.promises.writeFile(file, isHtml ? sandboxShell(body, safe) : body, { flag: "wx", mode: 0o400 });
    const err = await deps.openPathHardened(deps.shell, file);
    if (err) {
      if (deps.log) deps.log(`dashboard:open-copy: OS refused ${safe}: ${err}`);
      return { ok: false, error: err };
    }
    return { ok: true };
  } catch (e) {
    if (deps.log) deps.log(`dashboard:open-copy refused: ${e && e.message}`);
    return { ok: false, error: String((e && e.message) || e) };
  }
}

module.exports = {
  COPY_MAX_AGE_MS,
  COPY_PREFIX,
  MAX_COPY_BYTES,
  copyName,
  SHELL_POLICY,
  openCopy,
  pruneCopies,
  removeOwnCopies,
  sandboxShell,
};
