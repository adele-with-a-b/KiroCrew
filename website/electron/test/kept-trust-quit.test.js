// Only the user's own Quit ends the trust they gave in Kiro Crew. It leaves the
// owner-stop mark the gateway's next boot reads (Python `kiro_crew.kept_trust`);
// a relaunch, an update install or a system shutdown quits the app too, and leaves
// none, so the next boot keeps that trust.
const { test } = require("node:test");
const assert = require("node:assert");
const { EventEmitter } = require("node:events");
const fs = require("fs");
const os = require("os");
const path = require("path");

const { markOwnerStop } = require("../runtime/gateway/shutdown");
const { createQuitIntent } = require("../quit-intent");

function tempHome(t) {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "kc-kept-trust-"));
  t.after(() => fs.rmSync(home, { recursive: true, force: true }));
  return home;
}

const markPath = (home) => path.join(home, "kept-trust", "owner-stop");

test("markOwnerStop leaves the mark, sealed to the owner", (t) => {
  const home = tempHome(t);
  assert.strictEqual(markOwnerStop(fs, path, home), true);
  assert.strictEqual(fs.readFileSync(markPath(home), "utf8"), "desktop quit");
  if (process.platform !== "win32") {
    assert.strictEqual(fs.statSync(markPath(home)).mode & 0o777, 0o600);
    assert.strictEqual(fs.statSync(path.dirname(markPath(home))).mode & 0o777, 0o700);
  }
});

test("markOwnerStop leaves an earlier mark as it is", (t) => {
  const home = tempHome(t);
  fs.mkdirSync(path.dirname(markPath(home)));
  fs.writeFileSync(markPath(home), "kirocrew stop");
  assert.strictEqual(markOwnerStop(fs, path, home), false);
  assert.strictEqual(fs.readFileSync(markPath(home), "utf8"), "kirocrew stop");
});

test("markOwnerStop never writes through a linked directory", { skip: process.platform === "win32" }, (t) => {
  const home = tempHome(t);
  const elsewhere = tempHome(t);
  fs.symlinkSync(elsewhere, path.join(home, "kept-trust"), "dir");
  const lines = [];
  assert.strictEqual(markOwnerStop(fs, path, home, (line) => lines.push(line)), false);
  assert.deepStrictEqual(fs.readdirSync(elsewhere), []);
  assert.match(lines.join("\n"), /is a link/);
});

test("markOwnerStop never throws, so Quit still stops", (t) => {
  const home = tempHome(t);
  // A file where the directory must be.
  fs.writeFileSync(path.join(home, "kept-trust"), "");
  const lines = [];
  assert.strictEqual(markOwnerStop(fs, path, home, (line) => lines.push(line)), false);
  assert.match(lines.join("\n"), /trust will be kept/);
});

function fakeApp() {
  const app = new EventEmitter();
  app.relaunches = [];
  app.relaunch = function relaunch(options) {
    this.relaunches.push(options);
  };
  return app;
}

test("a quit nothing else announced is the user's", () => {
  const intent = createQuitIntent({ app: fakeApp(), autoUpdater: new EventEmitter() });
  assert.strictEqual(intent.isOwnerQuit(), true);
});

test("a scheduled relaunch is not the user's quit, and still relaunches", () => {
  const app = fakeApp();
  const intent = createQuitIntent({ app, autoUpdater: new EventEmitter() });
  app.relaunch({ execPath: "/Applications/Kiro Crew.app" });
  assert.deepStrictEqual(app.relaunches, [{ execPath: "/Applications/Kiro Crew.app" }]);
  assert.strictEqual(intent.isOwnerQuit(), false);
});

test("an update install is not the user's quit", () => {
  const autoUpdater = new EventEmitter();
  const intent = createQuitIntent({ app: fakeApp(), autoUpdater });
  autoUpdater.emit("before-quit-for-update");
  assert.strictEqual(intent.isOwnerQuit(), false);
});

test("a system shutdown is not the user's quit", () => {
  const powerMonitor = new EventEmitter();
  const intent = createQuitIntent({ app: fakeApp(), autoUpdater: new EventEmitter() });
  intent.watchPower(powerMonitor);
  powerMonitor.emit("shutdown");
  assert.strictEqual(intent.isOwnerQuit(), false);
});

test("a Windows session end is not the user's quit", () => {
  const app = fakeApp();
  const intent = createQuitIntent({ app, autoUpdater: new EventEmitter() });
  const win = new EventEmitter();
  app.emit("browser-window-created", {}, win);
  win.emit("session-end");
  assert.strictEqual(intent.isOwnerQuit(), false);
});

test("Restart now is not the user's quit, though an armed update quits with no relaunch", () => {
  const app = fakeApp();
  const intent = createQuitIntent({ app, autoUpdater: new EventEmitter() });
  intent.noteRestart("Restart now");
  assert.strictEqual(intent.isOwnerQuit(), false);
  assert.deepStrictEqual(app.relaunches, []);
});

test("the update:relaunch IPC notes the restart before the updater quits", async () => {
  const { harness, updaterHandle, wslEvent } = require("./ipc-registrar-test-helpers");
  const order = [];
  // feed-lane's relaunch with an armed stage: app.quit() and no app.relaunch().
  const updater = updaterHandle({ relaunch: () => order.push("updater.relaunch") });
  const h = harness({
    initAutoUpdate: () => updater,
    noteRestart: (why) => order.push(`noteRestart:${why}`),
  });
  h.registerUpdater(h.registrar);
  const answer = await h.handlers.get("update:relaunch")(wslEvent());
  assert.strictEqual(answer.ok, true);
  assert.deepStrictEqual(order, ["noteRestart:Restart now", "updater.relaunch"]);
});

test("main.js wires Restart now and the quit's verdict to quit-intent", () => {
  const { MAIN_SOURCE } = require("./ipc-registrar-test-helpers");
  assert.match(MAIN_SOURCE, /noteRestart: \(why\) => quitIntent\.noteRestart\(why\)/);
  assert.match(MAIN_SOURCE, /gateway\.stopOnQuit\(\{ ownerStop: quitIntent\.isOwnerQuit\(\) \}\)/);
});
