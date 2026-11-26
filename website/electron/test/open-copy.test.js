/**
 * A copy of a gateway file opened in the OS default app (open-copy.js). The
 * name and bytes come from a page that shows agent-authored text, so every
 * rule is pinned here with a real temp folder and the OS opener faked: no app
 * is launched.
 */

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { COPY_MAX_AGE_MS, COPY_PREFIX, MAX_COPY_BYTES, SHELL_POLICY, copyName, openCopy, pruneCopies, removeOwnCopies, sandboxShell } = require("../open-copy");

const OPENABLE = new Set([".md", ".txt", ".json", ".png"]);

function harness(answer = "") {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "open-copy-test-"));
  const opened = [];
  const deps = {
    fs,
    path,
    os: { tmpdir: () => tmp },
    shell: {},
    openPathHardened: async (_shell, p) => {
      opened.push(p);
      return answer;
    },
    openableExts: OPENABLE,
    log: () => {},
  };
  return { tmp, opened, deps, done: () => fs.rmSync(tmp, { recursive: true, force: true }) };
}

test("a markdown copy is written read-only in its own private folder and opened", async () => {
  const h = harness();
  try {
    const r = await openCopy(h.deps, "notes.md", Buffer.from("# Plan\n"));
    assert.deepStrictEqual(r, { ok: true });
    assert.strictEqual(h.opened.length, 1);
    const file = h.opened[0];
    assert.strictEqual(path.basename(file), "notes.md");
    // Its own folder, made directly in the temp directory: no shared parent
    // another user could have made first.
    assert.strictEqual(path.dirname(path.dirname(file)), h.tmp);
    assert.ok(path.basename(path.dirname(file)).startsWith(COPY_PREFIX));
    assert.strictEqual(fs.readFileSync(file, "utf8"), "# Plan\n");
    if (process.platform !== "win32") {
      assert.strictEqual(fs.statSync(file).mode & 0o777, 0o400);
      assert.strictEqual(fs.statSync(path.dirname(file)).mode & 0o777, 0o700);
    }
    // A second copy of the same name gets a folder of its own.
    await openCopy(h.deps, "notes.md", Buffer.from("again"));
    assert.notStrictEqual(path.dirname(h.opened[1]), path.dirname(file));
  } finally {
    h.done();
  }
});

test("only an allowed type is opened; the name decides, not the bytes", async () => {
  const h = harness();
  try {
    for (const name of ["run.sh", "app.command", "page.svg", "sheet.csv", "doc.pdf", "tool.app", "README", ".env", "", "x.MD.exe"]) {
      const r = await openCopy(h.deps, name, Buffer.from("echo hi"));
      assert.deepStrictEqual(r, { ok: false, error: "unsupported file type" }, name);
    }
    assert.deepStrictEqual(await openCopy(h.deps, "IMG.PNG", new Uint8Array([0x89, 0x50])), { ok: true });
    assert.strictEqual(h.opened.length, 1);
    assert.deepStrictEqual(fs.readdirSync(h.tmp).length, 1, "a refused name writes nothing");
  } finally {
    h.done();
  }
});

test("a name cannot leave the copy folder or pick another file", async () => {
  const h = harness();
  try {
    await openCopy(h.deps, "../../../../etc/evil.md", Buffer.from("x"));
    await openCopy(h.deps, "C:\\Users\\me\\notes.txt", Buffer.from("x"));
    assert.deepStrictEqual(h.opened.map((p) => path.basename(p)), ["evil.md", "notes.txt"]);
    for (const p of h.opened) assert.strictEqual(path.dirname(path.dirname(p)), h.tmp, p);
  } finally {
    h.done();
  }
  // A direction mark cannot make the name read with another extension.
  const rlo = String.fromCharCode(0x202e);
  const lri = String.fromCharCode(0x2066);
  assert.strictEqual(copyName("report" + rlo + "lmth.md"), "reportlmth.md");
  assert.strictEqual(copyName("a" + lri + "b.txt" + String.fromCharCode(0x200f)), "ab.txt");
  assert.strictEqual(copyName("a\u0000b\nc<d>.md"), "abcd.md");
  assert.strictEqual(copyName("x".repeat(300) + ".md").length, 200);
  assert.ok(copyName("x".repeat(300) + ".md").endsWith(".md"));
  assert.strictEqual(copyName(42), "");
});

test("a body that is not bytes, or is over the cap, is refused", async () => {
  const h = harness();
  try {
    assert.deepStrictEqual(await openCopy(h.deps, "a.md", "text"), { ok: false, error: "no content" });
    assert.deepStrictEqual(await openCopy(h.deps, "a.md", null), { ok: false, error: "no content" });
    const tooBig = Buffer.allocUnsafe(MAX_COPY_BYTES + 1);
    assert.deepStrictEqual(await openCopy(h.deps, "a.md", tooBig), { ok: false, error: "file too large" });
    assert.strictEqual(h.opened.length, 0);
  } finally {
    h.done();
  }
});

test("an OS refusal is reported, not swallowed", async () => {
  const h = harness("no application can open it");
  try {
    assert.deepStrictEqual(await openCopy(h.deps, "a.txt", Buffer.from("x")), { ok: false, error: "no application can open it" });
  } finally {
    h.done();
  }
});

const BASE = "<base href=\"about:srcdoc\" target=\"_self\">";

/** The shell's own markup with the srcdoc value taken out, and that value decoded. */
function splitShell(text) {
  const m = /srcdoc="([^"]*)"/.exec(text);
  assert.ok(m, "the shell holds one srcdoc attribute");
  const inner = m[1].replace(/&quot;/g, "\"").replace(/&amp;/g, "&");
  return { outer: text.slice(0, m.index) + text.slice(m.index + m[0].length), inner };
}

test("a web page is written as a shell whose only frame is sandbox=\"\" and holds the page", async () => {
  const h = harness();
  try {
    const page = "<!DOCTYPE html>\n<html><head><style>h1{color:red}</style></head><body><h1 id=C>C</h1><a href=\"#C\">C</a></body></html>";
    assert.deepStrictEqual(await openCopy(h.deps, "mock.html", Buffer.from(page)), { ok: true });
    const { outer, inner } = splitShell(fs.readFileSync(h.opened[0], "utf8"));
    assert.ok(outer.startsWith("<!DOCTYPE html>\n<html><head><meta charset=\"utf-8\">"));
    assert.strictEqual(outer.match(/<iframe\b/g).length, 1);
    // The frame's sandbox carries no token: no allow-scripts, allow-top-navigation,
    // allow-forms, allow-popups or allow-same-origin.
    assert.match(outer, /<iframe sandbox="" title="mock\.html" >/);
    assert.ok(outer.includes(`<meta http-equiv="Content-Security-Policy" content="${SHELL_POLICY}">`));
    // The page whole, with only the base for its in-page links added after its doctype.
    assert.strictEqual(inner, "<!DOCTYPE html>" + BASE + "\n<html><head><style>h1{color:red}</style></head><body><h1 id=C>C</h1><a href=\"#C\">C</a></body></html>");
    // `.htm` is the same type.
    assert.deepStrictEqual(await openCopy(h.deps, "old.HTM", Buffer.from("<p>x</p>")), { ok: true });
    assert.strictEqual(splitShell(fs.readFileSync(h.opened[1], "utf8")).inner, BASE + "<p>x</p>");
  } finally {
    h.done();
  }
});

test("a refresh tag, a base, a script and a frame break-out in the page all stay inside the sandboxed frame", async () => {
  const h = harness();
  try {
    const page = "<!doctype html><meta http-equiv=\"refresh\" content=\"0;url=file:///ws/mock.html\">"
      + "<base href=\"https://example.invalid/\" target=\"_top\">"
      + "<p title=\"a&quot;b\">x</p>\"></iframe><script>parent.location='https://example.invalid/'</script>"
      + "<iframe srcdoc=\"&lt;script&gt;\"></iframe>";
    assert.deepStrictEqual(await openCopy(h.deps, "a&b.html", Buffer.from(page)), { ok: true });
    const { outer, inner } = splitShell(fs.readFileSync(h.opened[0], "utf8"));
    // Nothing of the page reaches the shell's own markup.
    for (const bad of ["refresh", "<script", "<base", "example.invalid", "</iframe><script"]) {
      assert.ok(!outer.includes(bad), `the shell holds no ${bad}`);
    }
    assert.strictEqual(outer.match(/<iframe\b/g).length, 1);
    assert.match(outer, /<title>a&amp;b\.html<\/title>/);
    // The page itself is carried whole, the refresh included, behind the base.
    assert.strictEqual(inner, "<!doctype html>" + BASE + page.slice("<!doctype html>".length));
  } finally {
    h.done();
  }
});

test("the shell's policy, which its frame inherits, refuses file: and every load but data:, https: and http:", () => {
  const directives = new Map(SHELL_POLICY.split("; ").map((d) => [d.split(" ")[0], d.split(" ").slice(1)]));
  assert.deepStrictEqual(directives.get("default-src"), ["'none'"]);
  for (const kind of ["img-src", "media-src", "font-src"]) {
    assert.deepStrictEqual(directives.get(kind), ["data:", "https:", "http:"], kind);
  }
  assert.deepStrictEqual(directives.get("style-src"), ["'unsafe-inline'", "data:", "https:", "http:"]);
  for (const kind of ["script-src", "object-src", "frame-src", "child-src", "worker-src", "connect-src", "form-action", "manifest-src"]) {
    assert.deepStrictEqual(directives.get(kind), ["'none'"], kind);
  }
  // No file: (nor a wildcard, a blob:, or 'self', which at a file:// shell
  // would mean this machine's files) anywhere in it.
  assert.ok(!/file:|\*|blob:|'self'/.test(SHELL_POLICY));
  // No base-uri: the page's in-page links need the about:srcdoc base.
  assert.ok(!directives.has("base-uri"));
  // It is the shell's first policy, ahead of the frame.
  const shell = sandboxShell(Buffer.from("<p>x</p>"), "p.html").toString("utf8");
  assert.ok(shell.indexOf(SHELL_POLICY) < shell.indexOf("<iframe"));
});

test("the in-page base goes right after a doctype, or first", () => {
  const inner = (s) => splitShell(sandboxShell(Buffer.from(s, "utf8"), "p.html").toString("utf8")).inner;
  // A byte-order mark goes; white space and a doctype stay in front.
  const bom = String.fromCharCode(0xfeff);
  assert.strictEqual(inner(bom + "  <!doctype html><p>"), "  <!doctype html>" + BASE + "<p>");
  assert.strictEqual(inner("<html><p>"), BASE + "<html><p>");
  // Only white space and a doctype are skipped, never a comment or a tag.
  assert.strictEqual(inner("<!-- c --><!doctype html>"), BASE + "<!-- c --><!doctype html>");
  assert.strictEqual(inner("<base href=\"x/\"><!doctype html>"), BASE + "<base href=\"x/\"><!doctype html>");
});

test("pruning removes only this app's real copy folders, by age", () => {
  const h = harness();
  try {
    const old = fs.mkdtempSync(path.join(h.tmp, COPY_PREFIX));
    fs.writeFileSync(path.join(old, "a.md"), "x", { mode: 0o400 });
    const young = fs.mkdtempSync(path.join(h.tmp, COPY_PREFIX));
    const twoDaysAgo = (Date.now() - 2 * 24 * 3600 * 1000) / 1000;
    fs.utimesSync(old, twoDaysAgo, twoDaysAgo);
    // Look-alikes that must survive: a symlink, a file, another name.
    const target = fs.mkdtempSync(path.join(h.tmp, "keep-"));
    fs.symlinkSync(target, path.join(h.tmp, COPY_PREFIX + "LINKED"));
    fs.writeFileSync(path.join(h.tmp, COPY_PREFIX + "AFILE0"), "x");
    fs.mkdirSync(path.join(h.tmp, COPY_PREFIX + "toolong"));
    assert.strictEqual(pruneCopies(h.deps, COPY_MAX_AGE_MS), 1);
    assert.ok(!fs.existsSync(old) && fs.existsSync(young));
    assert.strictEqual(pruneCopies(h.deps, 0), 1);
    assert.ok(!fs.existsSync(young));
    for (const kept of [target, path.join(h.tmp, COPY_PREFIX + "LINKED"), path.join(h.tmp, COPY_PREFIX + "AFILE0"), path.join(h.tmp, COPY_PREFIX + "toolong")]) {
      assert.ok(fs.existsSync(kept), kept);
    }
    // A missing temp directory is no error.
    assert.strictEqual(pruneCopies({ ...h.deps, os: { tmpdir: () => path.join(h.tmp, "gone") } }, 0), 0);
  } finally {
    h.done();
  }
});

test("at quit only the copies this process made are removed; another build's stay", async () => {
  const h = harness();
  try {
    removeOwnCopies(h.deps); // the earlier tests' copies, whose folders are gone
    assert.deepStrictEqual(await openCopy(h.deps, "mine.md", Buffer.from("x")), { ok: true });
    const mine = path.dirname(h.opened[0]);
    const theirs = fs.mkdtempSync(path.join(h.tmp, COPY_PREFIX));
    assert.strictEqual(removeOwnCopies(h.deps), 1);
    assert.ok(!fs.existsSync(mine));
    assert.ok(fs.existsSync(theirs));
    assert.strictEqual(removeOwnCopies(h.deps), 0, "each folder is removed once");
  } finally {
    h.done();
  }
});

test("making a copy removes the copies older than a day", async () => {
  const h = harness();
  try {
    const old = fs.mkdtempSync(path.join(h.tmp, COPY_PREFIX));
    const twoDaysAgo = (Date.now() - 2 * 24 * 3600 * 1000) / 1000;
    fs.utimesSync(old, twoDaysAgo, twoDaysAgo);
    assert.deepStrictEqual(await openCopy(h.deps, "n.md", Buffer.from("x")), { ok: true });
    assert.ok(!fs.existsSync(old));
    assert.ok(fs.existsSync(h.opened[0]));
  } finally {
    h.done();
  }
});
