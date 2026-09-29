const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function concat(...parts) {
  return Buffer.concat(parts.map(part => Buffer.from(part)));
}

function varint(value) {
  const bytes = [];
  do {
    let byte = value % 128;
    value = Math.floor(value / 128);
    if (value) byte |= 0x80;
    bytes.push(byte);
  } while (value);
  return Buffer.from(bytes);
}

function field(number, value) { return concat(varint(number * 8), varint(value)); }

function bytesField(number, value) {
  const bytes = Buffer.from(value);
  return concat(varint(number * 8 + 2), varint(bytes.length), bytes);
}

function mode(id, width, height, refresh) {
  return concat(field(1, id), field(2, width), field(3, height), field(4, refresh));
}

function displayState(currentModeId) {
  const modes = [mode(3352, 3440, 1440, 59), mode(2, 3840, 2160, 60)];
  const display = concat(
    field(1, 1),
    bytesField(2, "HDMI-A-1"),
    bytesField(3, "test display"),
    field(6, 0),
    field(10, currentModeId),
    ...modes.map(candidate => bytesField(11, candidate)),
  );
  return concat(bytesField(1, display), field(2, 1), field(3, 1));
}

function monitorInfo(selectedDeviceName) {
  const monitors = [
    concat(bytesField(1, "DP-1"), bytesField(2, "Desk monitor")),
    concat(bytesField(1, "HDMI-A-1"), bytesField(2, "Living room TV")),
  ];
  return concat(bytesField(1, selectedDeviceName), ...monitors.map(value => bytesField(2, value)));
}

function decodeVarints(value) {
  const bytes = Buffer.from(value, "base64");
  const fields = {};
  let offset = 0;
  while (offset < bytes.length) {
    const key = readVarint(bytes, offset);
    offset = key.offset;
    const decoded = readVarint(bytes, offset);
    offset = decoded.offset;
    fields[key.value / 8] = decoded.value;
  }
  return fields;
}

function readVarint(bytes, start) {
  let value = 0;
  let multiplier = 1;
  let offset = start;
  for (;;) {
    const byte = bytes[offset++];
    value += (byte & 0x7f) * multiplier;
    if ((byte & 0x80) === 0) return {value, offset};
    multiplier *= 128;
  }
}

(async () => {
  let currentModeId = 3352;
  let snapshotReported = false;
  let modeWrites = 0;
  let suspendCalls = 0;
  let restartCalls = 0;
  let shutdownCalls = 0;
  let sunshineStatusCalls = 0;
  let sunshineRestartCalls = 0;
  let preferredMonitorCalls = 0;
  let preferredMonitor = "";
  let loaderConnectCalls = 0;
  let legacyPluginCalls = 0;
  let settingsCalls = 0;
  let pairingListCalls = 0;
  const updateFetches = [];
  const results = [];

  const dm = {
    GetState() { return Promise.resolve(new Uint8Array(displayState(currentModeId))); },
    SetMode(payload) {
      const fields = decodeVarints(payload);
      assert.equal(fields[1], 1);
      currentModeId = fields[2];
      modeWrites++;
      return Promise.resolve("ok");
    },
  };
  const system = {
    DisplayManager: dm,
    SuspendPC() { suspendCalls++; return Promise.resolve("suspend-requested"); },
    RestartPC() { restartCalls++; return Promise.resolve("restart-requested"); },
    ShutdownPC() { shutdownCalls++; return Promise.resolve("shutdown-requested"); },
  };
  const settings = {
    SetPreferredMonitor(value) { preferredMonitorCalls++; preferredMonitor = value; return Promise.resolve("preferred-monitor-requested"); },
    GetMonitorInfo() { return Promise.resolve(new Uint8Array(monitorInfo(preferredMonitor))); },
  };
  const React = {
    useState(initial) { return [initial, () => {}]; },
    useEffect(effect) { effect(); },
    createElement(type, props, ...children) { return {type, props: props || {}, children}; },
  };
  const source = fs.readFileSync(path.join(__dirname, "index.js"), "utf8");
  assert.match(source, /local_gamescope_outputs:\s*\["output_keys", "generation", "restart"\]/, "ordered monitor saves must use a fixed backend contract");
  assert.match(source, /Save for next session/, "the display-order UI must offer a non-disruptive save");
  assert.match(source, /Save and restart Gaming Mode/, "the display-order UI must offer a confirmed apply path");
  assert.match(source, /Move up/, "the display order must be controller-reorderable");
  assert.match(source, /remote_display_order_save:\s*\["output_keys", "generation", "restart"\]/, "remote display order must use a fixed backend contract");
  assert.match(source, /callBackend\("list_pairings"\)/, "the pairing watcher must use the bounded pairing RPC");
  assert.match(source, /local-display-order/, "Client mode must expose a local-only display-order destination");
  assert.match(source, /Changes the remote device only/, "remote display-order UI must identify its target");
  assert.match(source, /paired remote device will not be affected/, "local display-order UI must identify its target");
  const realSetTimeout = setTimeout;
  const fastSetTimeout = (callback, milliseconds, ...args) => realSetTimeout(callback, Math.min(milliseconds, 8), ...args);
  const realSetInterval = setInterval;
  const fastPairingSetInterval = (callback, milliseconds, ...args) => realSetInterval(callback, milliseconds === 1000 ? 8 : milliseconds, ...args);
  const factory = vm.runInNewContext(source, {
    window: {
      SteamClient: {System: system, Settings: settings},
      DeckyBackend: {
        call(route, pluginName, methodName) {
          assert.equal(route, "loader/call_legacy_plugin_method");
          assert.equal(pluginName, "Decky Sunshine");
          // Decky Sunshine v2025.10.27 returns an empty string when its
          // `result and any(...)` process probe sees no running Flatpak.
          if (methodName === "isSunshineRunning") { sunshineStatusCalls++; return Promise.resolve({success: true, result: ""}); }
          if (methodName === "startSunshine") { sunshineRestartCalls++; return Promise.resolve({success: true, result: true}); }
          throw new Error(`unexpected Sunshine method ${methodName}`);
        },
      },
      __DECKY_SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED_deckyLoaderAPIInit: {
        connect(version, pluginName) {
          assert.ok(version === 2 || version === 1);
          if (pluginName === "SteamOS Companion") {
            return {
              call(methodName, ...args) {
                if (methodName === "get_settings") {
                  settingsCalls++;
                  return Promise.resolve({
                    mode: {effective: "server"},
                    device_mode: "server",
                    settings: {monitor_sunshine: true},
                    diagnostics: {version: "0.5.1"},
                  });
                }
                if (methodName === "list_pairings") {
                  pairingListCalls++;
                  return Promise.resolve([
                    {pairing_id: "pair-pending", status: "pending", client_name: "New client"},
                    {pairing_id: "pair-approved", status: "approved", client_name: "Existing client"},
                  ]);
                }
                if (methodName === "report_bridge_snapshot") {
                  snapshotReported = true;
                  return Promise.resolve({accepted: true});
                }
                if (methodName === "next_bridge_command") {
                  return Promise.resolve(snapshotReported ? commands.shift() || null : null);
                }
                if (methodName === "report_bridge_result") {
                  results.push({command_id: args[0], result: args[1]});
                  return Promise.resolve({accepted: true});
                }
                if (methodName === "report_sunshine_owner") {
                  assert.ok(args[0] && typeof args[0].available === "boolean");
                  return Promise.resolve({ready: true});
                }
                throw new Error(`unexpected modern backend method ${methodName}`);
              },
            };
          }
          loaderConnectCalls++;
          assert.equal(pluginName, "Decky Sunshine");
          return {
            call(methodName) {
              if (methodName === "isSunshineRunning") { sunshineStatusCalls++; return Promise.resolve(false); }
              if (methodName === "startSunshine") { sunshineRestartCalls++; return Promise.resolve(true); }
              throw new Error(`unexpected Sunshine method ${methodName}`);
            },
          };
        },
      },
      SP_REACT: React,
    },
    console: {info() {}, warn() {}},
    setTimeout: fastSetTimeout,
    clearTimeout,
    setInterval: fastPairingSetInterval,
    clearInterval,
    ArrayBuffer,
    Uint8Array,
    WeakSet,
    TextDecoder,
    atob: value => Buffer.from(value, "base64").toString("binary"),
    btoa: value => Buffer.from(value, "binary").toString("base64"),
  });
  const commands = [
    {command_id: "bridge-mode", operation_id: "op-mode", kind: "set_mode", payload: {output_id: "1", mode_id: "2", generation: 1}},
    {command_id: "bridge-power", operation_id: "op-power", kind: "power", payload: {action: "suspend"}},
    {command_id: "bridge-restart", operation_id: "op-restart", kind: "power", payload: {action: "restart"}},
    {command_id: "bridge-shutdown", operation_id: "op-shutdown", kind: "power", payload: {action: "shutdown"}},
    {command_id: "bridge-preferred-monitor", operation_id: "op-preferred-monitor", kind: "set_preferred_monitor", payload: {monitor_device_name: "DP-1"}},
    {command_id: "bridge-sunshine-status", operation_id: "op-sunshine-status", kind: "sunshine_status", payload: {}},
    {command_id: "bridge-sunshine-restart", operation_id: "op-sunshine-restart", kind: "sunshine_restart", payload: {}},
  ];
  const plugin = factory({
    toaster: {toast() {}},
    fetchNoCors: async (url, options = {}) => {
      updateFetches.push({url, options});
      if (url.endsWith("/releases/latest")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            tag_name: "v0.5.1",
            draft: false,
            prerelease: false,
            html_url: "https://github.com/tuthan/steamos-companion-decky/releases/tag/v0.5.1",
            assets: [
              {name: "steamos-companion-decky-0.5.1.zip", browser_download_url: "https://github.com/tuthan/steamos-companion-decky/releases/download/v0.5.1/steamos-companion-decky-0.5.1.zip", size: 100},
              {name: "steamos-companion-decky-0.5.1.zip.sha256", browser_download_url: "https://github.com/tuthan/steamos-companion-decky/releases/download/v0.5.1/steamos-companion-decky-0.5.1.zip.sha256", size: 100},
            ],
          }),
        };
      }
      return {ok: true, status: 200, text: async () => `${"a".repeat(64)}  steamos-companion-decky-0.5.1.zip\n`};
    },
    callPluginMethod: async (method, args = {}) => {
    legacyPluginCalls++;
    if (method === "get_settings") {
      return {success: true, result: {
        mode: {effective: "server"},
        device_mode: "server",
        settings: {monitor_sunshine: true},
      }};
    }
    if (method === "report_bridge_snapshot") {
      snapshotReported = true;
      return {success: true, result: {accepted: true}};
    }
    if (method === "next_bridge_command") {
      return {success: true, result: snapshotReported ? commands.shift() || null : null};
    }
    if (method === "report_bridge_result") {
      results.push({command_id: args.command_id, result: args.result});
      return {success: true, result: {accepted: true}};
    }
    if (method === "report_sunshine_owner") {
      assert.ok(args.report && typeof args.report.available === "boolean");
      return {success: true, result: {ready: true}};
    }
    throw new Error(`unexpected backend method ${method}`);
    },
  });

  await new Promise(resolve => realSetTimeout(resolve, 40));
  const settingsCallsBeforePairingWatcher = settingsCalls;
  await new Promise(resolve => realSetTimeout(resolve, 60));
  assert.ok(pairingListCalls > 0, "the server watcher should poll pairings");
  assert.equal(settingsCalls, settingsCallsBeforePairingWatcher, "the pairing watcher must not poll full settings");
  await new Promise(resolve => realSetTimeout(resolve, 250));
  assert.equal(plugin.icon.type, "svg", "the plugin should expose a native SVG icon");
  assert.equal(plugin.icon.props["aria-label"], "SteamOS Companion");
  assert.equal(plugin.icon.children[0].type, "g", "the plugin icon should use a monochrome glyph");
  assert.equal(plugin.icon.children[0].props.stroke, "#f2f4f5");
  assert.equal(updateFetches.length, 2, "the updater should fetch the release and checksum through Decky");
  assert.equal(updateFetches[0].options.headers["X-GitHub-Api-Version"], "2022-11-28");
  plugin.onDismount();
  assert.equal(modeWrites, 1, "the bridge must apply the advertised mode once");
  assert.equal(suspendCalls, 1, "the bridge must invoke suspend only for a fixed power command");
  assert.equal(restartCalls, 1, "the bridge must invoke restart only for a fixed power command");
  assert.equal(shutdownCalls, 1, "the bridge must invoke shutdown only for a fixed power command");
  assert.deepEqual(results.map(item => item.command_id), ["bridge-mode", "bridge-power", "bridge-restart", "bridge-shutdown", "bridge-preferred-monitor", "bridge-sunshine-status", "bridge-sunshine-restart"]);
  assert.equal(results[0].result.ok, true);
  assert.equal(results[0].result.snapshot.outputs[0].current_mode_id, "2");
  assert.equal(results[0].result.snapshot.outputs[0].generation, 1, "a mode switch must not invalidate the output generation");
  assert.equal(results[0].result.snapshot.methods.display_selection, false, "unverified Steam output selection must stay disabled");
  assert.equal(results[0].result.snapshot.methods.preferred_monitor, true, "Steam preferred-monitor test capability should be advertised when the fixed method exists");
  assert.equal(results[0].result.snapshot.outputs[0].identity_confidence, "unknown", "legacy display IDs are not enough for local selection");
  assert.equal(results[1].result.ok, true);
  assert.equal(results[2].result.action, "restart");
  assert.equal(results[3].result.action, "shutdown");
  assert.equal(results[4].result.ok, true);
  assert.equal(results[4].result.monitor_device_name, "DP-1");
  assert.equal(results[4].result.readback.selected_device_name, "DP-1");
  assert.equal(results[5].result.running, false);
  assert.equal(results[6].result.ok, true);
  assert.equal(loaderConnectCalls, 0, "the legacy owner route must be preferred for Decky Sunshine");
  assert.equal(legacyPluginCalls, 0, "API-v1 backend calls should use the positional loader API");
  assert.ok(sunshineStatusCalls >= 2, "the bridge must probe and monitor through Decky Sunshine");
  assert.equal(sunshineRestartCalls, 1, "the bridge must invoke the owner restart method only for a fixed command");
  assert.equal(preferredMonitorCalls, 1, "the bridge must invoke the fixed preferred-monitor method only for a fixed command");
  console.log("PASS: production Decky bridge uses API v1, validates OTA metadata, decodes state, preserves display generations, invokes power actions, tests preferred-monitor selection, and delegates Sunshine to its owner");
})().catch(error => { console.error(error); process.exitCode = 1; });
