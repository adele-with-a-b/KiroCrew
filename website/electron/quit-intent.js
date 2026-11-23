"use strict";

/**
 * Whether this quit is the user ending Kiro Crew, which ends the trust they gave
 * (the gateway's `kiro_crew.kept_trust`), or an exit they did not choose, which
 * keeps it.
 *
 * Every quit runs `before-quit`, the user's Quit and the others alike. The others
 * each announce themselves first, and each one is noted here:
 * - a relaunch (`app.relaunch`, which the update lanes, the update relaunch IPC
 *   and the GPU-crash fallback all call before they quit), and Restart now,
 *   which quits with no `app.relaunch` when an armed update installs on that
 *   quit (`noteRestart`, from the `update:relaunch` IPC);
 * - an update install (`before-quit-for-update` from the native autoUpdater);
 * - a system shutdown, reboot or logout (`powerMonitor` `shutdown` on macOS and
 *   Linux, a window's `session-end` on Windows).
 * A quit with none of these is the user's: the menu's Quit, Cmd+Q, the close
 * button set to quit, the last window closed off macOS, a dialog's Quit.
 *
 * @param {object} deps
 * @param {object} deps.app  Electron's app.
 * @param {object} [deps.autoUpdater]  Electron's native autoUpdater.
 * @param {(line: string) => void} [deps.log]
 */
function createQuitIntent({ app, autoUpdater, log }) {
  let notTheUsers = "";

  function note(why) {
    if (notTheUsers) return;
    notTheUsers = why;
    log?.(`quit: ${why}, so the trust given in Kiro Crew is kept`);
  }

  const relaunch = app.relaunch.bind(app);
  app.relaunch = (...args) => {
    note("a relaunch is scheduled");
    return relaunch(...args);
  };
  autoUpdater?.on?.("before-quit-for-update", () => note("an update is installing"));
  app.on("browser-window-created", (_event, win) => {
    win?.on?.("session-end", () => note("the session is ending"));
  });

  return {
    /** Watch for a system shutdown. Call once the app is ready (macOS refuses earlier). */
    watchPower(powerMonitor) {
      powerMonitor.on("shutdown", () => note("the system is shutting down"));
    },
    /**
     * Note a restart that quits without app.relaunch: Restart now on an armed
     * update, which the installer relaunches. Its `before-quit-for-update` comes
     * only after `before-quit`, too late to count.
     */
    noteRestart(why) {
      note(`${why} is a restart`);
    },
    /** True unless something other than the user started this quit. */
    isOwnerQuit() {
      return !notTheUsers;
    },
  };
}

module.exports = { createQuitIntent };
