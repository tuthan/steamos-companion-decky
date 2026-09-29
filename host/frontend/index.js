/* SteamOS Companion Decky frontend.
 *
 * The backend owns the LAN API and operation journal. This module is the
 * long-lived Steam bridge: it only calls fixed DisplayManager/System/Settings
 * methods and the two constant Decky Sunshine owner methods needed by
 * commands delivered by the backend. The bridge is kept
 * alive after the settings view closes so a remote client can still recover a
 * display. No command accepts a raw Steam method or payload from the LAN.
 */
(() => (serverAPI) => {
  const tag = "[SteamOS Companion]";
  const MAX_PROTO_BYTES = 64 * 1024;
  const MAX_STATE_BYTES = 256 * 1024;
  const commandPollMs = 500;
  const snapshotPollMs = 2000;
  const initialDisplayManager = window.SteamClient?.System?.DisplayManager;
  const initialSystem = window.SteamClient?.System;
  const initialSettings = window.SteamClient?.Settings;
  const React = window.SP_REACT;
  // API-v0 exposes React through SP_REACT.  When Decky exposes the native
  // component kit to a legacy bundle, use it directly; the small semantic
  // fallbacks keep the same component tree usable by the development harness
  // and by older loaders while retaining stable focus keys and ARIA labels.
  // Legacy API-v0 plugins receive Decky's frontend library as the DFL global.
  // Prefer it so ButtonItem/Focusable participate in Steam's controller
  // navigation; the other names keep newer loaders and the harness working.
  const NativeUI = window.DFL || window.DeckyUI || window.__DECKY_UI__ || serverAPI?.UI || {};
  const UI_COLORS = Object.freeze({
    background: "#0f151d",
    text: "#f3f6fa",
    muted: "#c4ccd6",
    surface: "#1b2632",
    surfaceSelected: "#264c67",
    input: "#131c25",
    border: "#6f8294",
    accent: "#66c0f4",
    disabledSurface: "#3e4853",
    disabledText: "#aab4bf",
    danger: "#ffb4b4",
  });
  const STANDARD_REFRESH_RATES = Object.freeze([50, 59, 60, 90, 100, 119, 120, 144, 165, 240]);
  const STANDARD_RESOLUTIONS = new Set([
    "640x480", "800x600", "1024x768", "1152x864", "1280x720", "1280x800", "1280x1024",
    "1360x768", "1366x768", "1440x900", "1600x900", "1600x1200", "1680x1050", "1920x1080",
    "1920x1200", "2048x1152", "2560x1080", "2560x1440", "2560x1600", "3440x1440", "3840x2160",
    "3840x2400", "5120x1440", "5120x2160", "7680x4320",
  ]);

  function isStandardRefreshRate(value) {
    if (value === null || value === undefined) return true;
    const rate = Number(value);
    return Number.isFinite(rate) && STANDARD_REFRESH_RATES.some(candidate => Math.abs(rate - candidate) < 0.3);
  }

  function isStandardResolution(mode) {
    if (!mode || mode.width === null || mode.width === undefined || mode.height === null || mode.height === undefined) return true;
    return STANDARD_RESOLUTIONS.has(`${Number(mode.width)}x${Number(mode.height)}`);
  }

  function isStandardDisplayMode(mode) {
    return isStandardResolution(mode) && isStandardRefreshRate(mode?.refresh_hz);
  }
  const useRef = React?.useRef || (value => ({current: value}));
  let stopped = false;
  let commandBusy = false;
  let snapshotBusy = false;
  let notify = () => {};
  let latestSnapshot = null;
  let lastSnapshotAt = 0;
  let driverTimer = null;
  let pairingWatchTimer = null;
  let pairingWatchBusy = false;
  let knownPendingPairings = null;
  let sunshineOwnerTimer = null;
  let sunshineOwnerBusy = false;
  let sunshineMonitoringEnabled = false;
  let runtimeRole = "unknown";
  let runtimeReady = false;
  let runtimeStartPromise = null;
  let serverRuntimeStarted = false;
  const wakeEventNames = ["focus", "online", "pageshow"];
  const rpcLogAt = new Map();

  const POWER_METHODS = Object.freeze({
    suspend: "SuspendPC",
    restart: "RestartPC",
    shutdown: "ShutdownPC",
  });
  const SUNSHINE_OWNER_PLUGIN = "Decky Sunshine";
  const SUNSHINE_OWNER_METHODS = Object.freeze({
    // The installed Decky Sunshine plugin is a legacy Decky plugin. Keep
    // these names aligned with its public backend methods; the owner remains
    // the only process controller.
    status: "isSunshineRunning",
    start: "startSunshine",
  });
  const LOADER_API_KEY = "__DECKY_SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED_deckyLoaderAPIInit";
  const PLUGIN_NAME = "SteamOS Companion";
  const BUILD_VERSION = "__STEAMOS_COMPANION_VERSION__";
  const LOADER_API_VERSION = 2;
  const UPDATE_REPOSITORY = "tuthan/steamos-companion-decky";
  const UPDATE_API_URL = `https://api.github.com/repos/${UPDATE_REPOSITORY}/releases/latest`;
  const UPDATE_CHECK_INTERVAL_MS = 6 * 60 * 60 * 1000;
  const UPDATE_MIN_CHECK_INTERVAL_MS = 30 * 1000;
  const UPDATE_INSTALL_TYPE = 2; // Decky's PluginInstallType.UPDATE.
  const UPDATE_ASSET_PREFIX = "steamos-companion-decky-";
  const BACKEND_ARGUMENTS = Object.freeze({
    get_settings: [],
    update_settings: ["changes"],
    set_device_mode: ["mode", "client_name"],
    cancel_mode_change: [],
    dismiss_upgrade_notice: [],
    create_pairing: ["requested_scopes"],
    create_pairing_code: ["requested_scopes"],
    list_pairings: [],
    approve_pairing: ["pairing_id", "scopes"],
    reject_pairing: ["pairing_id"],
    revoke_client: ["client_id"],
    set_sunshine_provider: ["provider"],
    report_sunshine_owner: ["report"],
    next_bridge_command: [],
    report_bridge_result: ["command_id", "result"],
    report_bridge_snapshot: ["snapshot"],
    local_display_outputs: [],
    local_display_preview: ["output_key", "generation"],
    local_display_confirm: ["preview_id"],
    local_display_revert: ["preview_id"],
    local_gamescope_output: ["output_key"],
    local_gamescope_outputs: ["output_keys", "generation", "restart"],
    local_clear_gamescope_output: [],
    local_gamescope_restart: [],
    local_display_order: [],
    local_display_order_save: ["output_keys", "generation", "restart"],
    local_display_order_reset: [],
    // Keep these names while an older panel instance is still in memory.
    local_preferred_monitor: ["output_key"],
    local_clear_preferred_monitor: [],
    local_sunshine_restart: [],
    discover_remote_devices: ["port", "endpoints"],
    begin_discovery: ["port", "endpoints"],
    poll_discovery: ["scan_id"],
    cancel_discovery: ["scan_id"],
    check_remote_device: ["host", "port"],
    request_remote_pairing: ["candidate", "requested_scopes", "replace_existing"],
    poll_remote_pairing: ["pending_id"],
    cancel_remote_pairing: ["pending_id"],
    use_staged_remote: ["use"],
    remote_status: [],
    remote_outputs: [],
    remote_display_order: [],
    remote_display_order_save: ["output_keys", "generation", "restart"],
    remote_display_order_reset: [],
    remote_action_availability: ["action", "output_id", "mode_id", "output_keys", "generation"],
    remote_power: ["action"],
    remote_preview: ["output_id", "mode_id", "generation"],
    remote_confirm_preview: ["preview_id", "visible"],
    remote_restore: ["source", "profile_id"],
    remote_save_current: ["output_id", "generation"],
    remote_sunshine_restart: [],
    check_remote_operation: ["action_id"],
    resend_remote_operation: ["action_id", "acknowledge_earlier_may_have_run"],
    wake_remote: [],
    client_diagnostics: [],
    rename_remote: ["alias"],
    update_remote_endpoint: ["candidate"],
    forget_remote: ["revoke"],
    local_status: [],
  });
  let sunshineOwnerAPI = null;
  let pluginBackendAPI = null;
  let pluginBackendAPIAttempted = false;
  let updateCheckTimer = null;
  let updateCheckPromise = null;
  let updateState = {
    status: "idle",
    currentVersion: null,
    release: null,
    error: null,
    checkedAt: null,
  };
  const updateListeners = new Set();

  function steamSystem() { return window.SteamClient?.System || initialSystem; }
  function displayManager() { return steamSystem()?.DisplayManager || initialDisplayManager; }
  function steamSettings() { return window.SteamClient?.Settings || initialSettings; }
  function powerMethodName(action) { return Object.prototype.hasOwnProperty.call(POWER_METHODS, action) ? POWER_METHODS[action] : null; }

  const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
  const timeout = (promise, milliseconds) => new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("Steam bridge timeout")), milliseconds);
    Promise.resolve(promise).then(
      value => { clearTimeout(timer); resolve(value); },
      error => { clearTimeout(timer); reject(error); }
    );
  });

  function deckyBackend() {
    if (window.DeckyBackend && typeof window.DeckyBackend.call === "function") return window.DeckyBackend;
    try {
      if (typeof DeckyBackend !== "undefined" && typeof DeckyBackend.call === "function") return DeckyBackend;
    } catch (_) {}
    return null;
  }

  function loaderPluginAPI() {
    if (pluginBackendAPI && typeof pluginBackendAPI.call === "function") return pluginBackendAPI;
    if (pluginBackendAPIAttempted) return null;
    pluginBackendAPIAttempted = true;
    const init = window[LOADER_API_KEY];
    if (!init || typeof init.connect !== "function") return null;
    let lastError = null;
    for (const version of [LOADER_API_VERSION, 1]) {
      try {
        const api = init.connect(version, PLUGIN_NAME);
        if (api && typeof api.call === "function") {
          pluginBackendAPI = api;
          return api;
        }
      } catch (error) {
        lastError = error;
      }
    }
    // Older loaders only expose the API-v0 serverAPI object. The caller falls
    // back to it below; do not make a transient compatibility error prevent
    // the plugin from loading.
    if (lastError) console.warn(tag, "Decky Loader plugin API unavailable", boundedString(lastError));
    return null;
  }

  function updateCurrentVersion(value) {
    const candidate = value?.diagnostics?.version || value?.version || value;
    const version = normalizeVersion(candidate) || normalizeVersion(BUILD_VERSION);
    if (!version || updateState.currentVersion === version) return;
    updateState = {...updateState, currentVersion: version};
    for (const listener of updateListeners) {
      try { listener(updateState); } catch (_) {}
    }
  }

  function normalizeVersion(value) {
    const text = String(value ?? "").trim().replace(/^v/i, "");
    const match = text.match(/^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$/);
    if (!match) return null;
    const core = match.slice(1, 4).map(Number);
    if (core.some(value => !Number.isSafeInteger(value) || value < 0)) return null;
    if (match.slice(1, 4).some(value => value.length > 1 && value.startsWith("0"))) return null;
    if (match[4] && match[4].split(".").some(value => !value || (/^0\d/.test(value)))) return null;
    return `${core[0]}.${core[1]}.${core[2]}${match[4] ? `-${match[4]}` : ""}`;
  }

  function compareVersions(left, right) {
    const a = normalizeVersion(left);
    const b = normalizeVersion(right);
    if (!a || !b) return null;
    const parse = value => {
      const [core, pre = ""] = value.split("-");
      return {core: core.split(".").map(Number), pre: pre ? pre.split(".") : []};
    };
    const parsedA = parse(a);
    const parsedB = parse(b);
    for (let index = 0; index < 3; index++) {
      if (parsedA.core[index] !== parsedB.core[index]) return parsedA.core[index] > parsedB.core[index] ? 1 : -1;
    }
    if (!parsedA.pre.length && !parsedB.pre.length) return 0;
    if (!parsedA.pre.length) return 1;
    if (!parsedB.pre.length) return -1;
    const count = Math.max(parsedA.pre.length, parsedB.pre.length);
    for (let index = 0; index < count; index++) {
      const leftPart = parsedA.pre[index];
      const rightPart = parsedB.pre[index];
      if (leftPart === undefined) return -1;
      if (rightPart === undefined) return 1;
      if (leftPart === rightPart) continue;
      const leftNumeric = /^\d+$/.test(leftPart);
      const rightNumeric = /^\d+$/.test(rightPart);
      if (leftNumeric && rightNumeric) return Number(leftPart) > Number(rightPart) ? 1 : -1;
      if (leftNumeric !== rightNumeric) return leftNumeric ? -1 : 1;
      return leftPart > rightPart ? 1 : -1;
    }
    return 0;
  }

  function publishUpdate(patch) {
    updateState = {...updateState, ...patch};
    for (const listener of updateListeners) {
      try { listener(updateState); } catch (_) {}
    }
    return updateState;
  }

  function subscribeUpdates(listener) {
    updateListeners.add(listener);
    return () => updateListeners.delete(listener);
  }

  function updateFetch(url, options = {}) {
    const api = loaderPluginAPI();
    if (api && typeof api.fetchNoCors === "function") return api.fetchNoCors(url, options);
    if (typeof serverAPI?.fetchNoCors === "function") return serverAPI.fetchNoCors(url, options);
    if (typeof fetch === "function") return fetch(url, options);
    throw new Error("Decky network API is unavailable");
  }

  function canCheckUpdates() {
    return Boolean(
      typeof serverAPI?.fetchNoCors === "function"
      || typeof pluginBackendAPI?.fetchNoCors === "function"
      || typeof fetch === "function"
    );
  }

  function validReleaseAsset(asset, expectedName) {
    if (!asset || asset.name !== expectedName || typeof asset.browser_download_url !== "string") return null;
    const prefix = `https://github.com/${UPDATE_REPOSITORY}/releases/download/`;
    if (!asset.browser_download_url.startsWith(prefix)) return null;
    return {name: asset.name, url: asset.browser_download_url, size: Number(asset.size) || null};
  }

  async function checkForUpdate(force = false) {
    if (updateCheckPromise) return updateCheckPromise;
    const now = Date.now();
    if (!force && updateState.checkedAt && now - updateState.checkedAt < UPDATE_MIN_CHECK_INTERVAL_MS) return updateState;
    if (!canCheckUpdates()) return publishUpdate({status: "unavailable", error: "Decky network API is unavailable"});
    updateCheckPromise = (async () => {
      publishUpdate({status: "checking", error: null});
      try {
        const response = await timeout(updateFetch(UPDATE_API_URL, {
          method: "GET",
          headers: {
            Accept: "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
          },
        }), 10000);
        if (!response || response.ok === false || (Number(response.status) >= 400)) throw new Error(`GitHub release lookup failed (${response?.status || "unknown"})`);
        const release = typeof response.json === "function" ? await response.json() : null;
        if (!release || release.draft === true || release.prerelease === true) throw new Error("GitHub returned no stable release");
        const version = normalizeVersion(release.tag_name);
        if (!version) throw new Error("GitHub release tag is not a semantic version");
        const expectedArchive = `${UPDATE_ASSET_PREFIX}${version}.zip`;
        const archive = validReleaseAsset((release.assets || []).find(item => item?.name === expectedArchive), expectedArchive);
        const checksumName = `${expectedArchive}.sha256`;
        const checksumAsset = validReleaseAsset((release.assets || []).find(item => item?.name === checksumName), checksumName);
        if (!archive || !checksumAsset) throw new Error(`Release is missing ${expectedArchive} or its SHA256 asset`);
        const checksumResponse = await timeout(updateFetch(checksumAsset.url, {method: "GET"}), 10000);
        if (!checksumResponse || checksumResponse.ok === false || Number(checksumResponse.status) >= 400) throw new Error("Release checksum could not be downloaded");
        const checksumText = typeof checksumResponse.text === "function" ? await checksumResponse.text() : "";
        if (checksumText.length > 4096) throw new Error("Release checksum is unexpectedly large");
        const checksumMatch = checksumText.match(/^\s*([a-fA-F0-9]{64})\s+(?:\*?)(\S+)\s*$/m);
        if (!checksumMatch || checksumMatch[2] !== expectedArchive) throw new Error("Release checksum is malformed");
        const current = updateState.currentVersion || normalizeVersion(BUILD_VERSION);
        const comparison = compareVersions(version, current);
        if (comparison === null) throw new Error("Installed version is not a semantic version");
        const safeReleaseUrl = typeof release.html_url === "string" && release.html_url.startsWith(`https://github.com/${UPDATE_REPOSITORY}/releases/`) ? release.html_url : `https://github.com/${UPDATE_REPOSITORY}/releases/tag/${encodeURIComponent(release.tag_name)}`;
        const details = {
          tag: boundedString(release.tag_name, 64),
          version,
          zipUrl: archive.url,
          sha256: checksumMatch[1].toLowerCase(),
          releaseUrl: safeReleaseUrl,
          notes: boundedString(release.body, 2048),
          publishedAt: boundedString(release.published_at, 64),
          size: archive.size,
        };
        return publishUpdate({
          status: comparison > 0 ? "available" : "current",
          release: comparison > 0 ? details : null,
          error: null,
          checkedAt: Date.now(),
        });
      } catch (error) {
        return publishUpdate({status: "error", release: null, error: boundedString(error), checkedAt: Date.now()});
      } finally {
        updateCheckPromise = null;
      }
    })();
    return updateCheckPromise;
  }

  function startUpdateWatcher() {
    if (updateCheckTimer || !canCheckUpdates()) return;
    void checkForUpdate();
    updateCheckTimer = setInterval(() => { void checkForUpdate(); }, UPDATE_CHECK_INTERVAL_MS);
  }

  function stopUpdateWatcher() {
    if (updateCheckTimer) clearInterval(updateCheckTimer);
    updateCheckTimer = null;
  }

  async function installUpdate() {
    const release = updateState.release;
    if (!release || !/^[a-f0-9]{64}$/.test(release.sha256)) throw new Error("No verified update is available");
    const loader = deckyBackend();
    if (!loader) throw new Error("Decky installer API is unavailable; use Decky's Install Plugin from ZIP action");
    const reply = await timeout(loader.call(
      "utilities/install_plugin",
      release.zipUrl,
      PLUGIN_NAME,
      release.version,
      release.sha256,
      UPDATE_INSTALL_TYPE,
    ), 10000);
    if (reply?.success === false) throw new Error(boundedString(reply.result || "Decky rejected the update"));
    publishUpdate({status: "installing", error: null});
    const toaster = serverAPI?.toaster;
    if (typeof toaster?.toast === "function") {
      try { toaster.toast({title: "SteamOS Companion update", body: `Decky is ready to install v${release.version}. Confirm the Decky installation prompt.`, duration: 8000}); } catch (_) {}
    }
    return {requested: true, version: release.version};
  }

  function useUpdateState() {
    const [value, setValue] = React.useState(updateState);
    React.useEffect(() => subscribeUpdates(setValue), []);
    return value;
  }

  function sunshineLoaderAPI() {
    // Decky Sunshine 2025.10.27 is a legacy API-v0 plugin. Calling it through
    // loader/call_plugin_method makes Decky interpret the request as an
    // index-argument API call and reject it. The legacy route accepts the
    // kwargs object used by Sunshine's own frontend.
    const legacy = deckyBackend();
    if (legacy) return {kind: "legacy", backend: legacy};
    if (sunshineOwnerAPI && typeof sunshineOwnerAPI.call === "function") return {kind: "modern", api: sunshineOwnerAPI};
    const init = window[LOADER_API_KEY];
    if (init && typeof init.connect === "function") {
      try {
        const api = init.connect(1, SUNSHINE_OWNER_PLUGIN);
        if (api && typeof api.call === "function") {
          sunshineOwnerAPI = api;
          return {kind: "modern", api};
        }
      } catch (error) {
        console.warn(tag, "Decky Loader owner API unavailable", boundedString(error));
      }
    }
    return null;
  }

  async function callSunshineOwner(method) {
    const owner = sunshineLoaderAPI();
    if (!owner) throw new Error("Decky Loader owner bridge is unavailable");
    const reply = owner.kind === "legacy"
      ? await timeout(owner.backend.call("loader/call_legacy_plugin_method", SUNSHINE_OWNER_PLUGIN, method, {}), 4000)
      : await timeout(owner.api.call(method), 4000);
    if (reply?.success === false) throw new Error(boundedString(reply.result || "Decky Sunshine rejected the call"));
    return reply && Object.prototype.hasOwnProperty.call(reply, "result") ? reply.result : reply;
  }

  function ownerRunningValue(value) {
    if (typeof value === "boolean") return value;
    if (value && typeof value === "object" && typeof value.running === "boolean") return value.running;
    // Decky Sunshine v2025.10.27 returns `result and any(...)` from its
    // process observer.  When `flatpak ps` succeeds but Sunshine is stopped,
    // Python returns the empty string rather than the boolean False. Its own
    // frontend intentionally treats only `true` as running, so preserve that
    // compatibility without treating null/undefined or arbitrary values as a
    // trustworthy stopped state.
    if (value === "") return false;
    return null;
  }

  async function connectSunshineOwner() {
    if (stopped || !sunshineMonitoringEnabled || sunshineOwnerBusy) return;
    sunshineOwnerBusy = true;
    try {
      const running = ownerRunningValue(await callSunshineOwner(SUNSHINE_OWNER_METHODS.status));
      if (running === null) throw new Error("Decky Sunshine returned an invalid status");
      if (stopped || !sunshineMonitoringEnabled) return;
      await callBackend("report_sunshine_owner", {report: {available: true, owner: SUNSHINE_OWNER_PLUGIN}});
      console.info(tag, "connected to Decky Sunshine owner");
    } catch (error) {
      if (stopped || !sunshineMonitoringEnabled) return;
      const reason = boundedString(error);
      console.warn(tag, "Decky Sunshine owner unavailable", reason);
      try { await callBackend("report_sunshine_owner", {report: {available: false, reason}}); } catch (_) {}
    } finally {
      sunshineOwnerBusy = false;
    }
  }

  function wakeBridge() {
    if (stopped || !runtimeReady || !roleHasServer(runtimeRole)) return;
    sunshineOwnerAPI = null;
    lastSnapshotAt = 0;
    void readSnapshot("resume", true);
    if (sunshineMonitoringEnabled) void connectSunshineOwner();
  }

  if (typeof window.addEventListener === "function") {
    for (const eventName of wakeEventNames) window.addEventListener(eventName, wakeBridge);
  }

  function boundedString(value, limit = 256) {
    return String(value ?? "").replace(/\x00/g, "").slice(0, limit);
  }

  function roleHasServer(role) { return role === "server" || role === "both"; }
  function roleHasClient(role) { return role === "client" || role === "both"; }

  function stopServerRuntime() {
    serverRuntimeStarted = false;
    if (driverTimer) clearTimeout(driverTimer);
    driverTimer = null;
    if (pairingWatchTimer) clearInterval(pairingWatchTimer);
    pairingWatchTimer = null;
    stopSunshineOwnerWatcher();
    knownPendingPairings = null;
  }

  function stopSunshineOwnerWatcher() {
    if (sunshineOwnerTimer) clearInterval(sunshineOwnerTimer);
    sunshineOwnerTimer = null;
    sunshineOwnerAPI = null;
  }

  function setSunshineMonitoring(enabled) {
    const next = enabled === true;
    if (sunshineMonitoringEnabled === next) return;
    sunshineMonitoringEnabled = next;
    if (!next) {
      stopSunshineOwnerWatcher();
    } else if (runtimeReady && roleHasServer(runtimeRole)) {
      startSunshineOwnerWatcher();
    }
  }

  function startServerRuntime() {
    if (stopped || serverRuntimeStarted || !roleHasServer(runtimeRole)) return;
    serverRuntimeStarted = true;
    void readSnapshot("startup");
    void driverCycle();
    startPairingWatcher();
    startSunshineOwnerWatcher();
  }

  function setRuntimeRole(role) {
    const next = role === "client" || role === "server" || role === "both" ? role : "setup";
    const hadServer = roleHasServer(runtimeRole);
    runtimeRole = next;
    runtimeReady = true;
    if (roleHasServer(next)) {
      startServerRuntime();
    } else if (hadServer || serverRuntimeStarted) {
      stopServerRuntime();
    }
  }

  function logBackendFailure(method, error) {
    const now = Date.now();
    const previous = rpcLogAt.get(method) || 0;
    if (now - previous < 5000) return;
    rpcLogAt.set(method, now);
    console.warn(tag, `backend RPC ${method} failed`, boundedString(error));
  }

  function serialize(value, depth = 0, seen = new WeakSet()) {
    if (value === undefined) return {type: "undefined"};
    if (value === null || typeof value === "boolean") return value;
    if (typeof value === "number") return Number.isFinite(value) ? value : String(value);
    if (typeof value === "bigint") return {type: "bigint", value: String(value)};
    if (typeof value === "string") return value.length <= 4096 ? value : {type: "truncated-string", length: value.length};
    if (typeof value === "function") return {type: "function"};
    if (depth > 5) return {type: "depth-limit"};
    if (value instanceof ArrayBuffer || ArrayBuffer.isView(value)) {
      const bytes = value instanceof ArrayBuffer ? new Uint8Array(value) : new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
      let binary = "";
      for (const byte of bytes.subarray(0, 12000)) binary += String.fromCharCode(byte);
      return {type: "bytes", length: bytes.length, truncated: bytes.length > 12000, base64: btoa(binary)};
    }
    if (typeof value !== "object") return boundedString(value);
    if (seen.has(value)) return {type: "cycle"};
    seen.add(value);
    if (Array.isArray(value)) return value.slice(0, 48).map(item => serialize(item, depth + 1, seen));
    const result = {};
    for (const key of Object.keys(value).slice(0, 48)) {
      try { result[boundedString(key, 96)] = serialize(value[key], depth + 1, seen); }
      catch (error) { result[boundedString(key, 96)] = {error: boundedString(error)}; }
    }
    return result;
  }

  function readVarint(bytes, start) {
    let value = 0;
    let multiplier = 1;
    let offset = start;
    for (let count = 0; count < 10; count++) {
      if (offset >= bytes.length) throw new Error("truncated protobuf varint");
      const byte = bytes[offset++];
      value += (byte & 0x7f) * multiplier;
      if (!Number.isSafeInteger(value)) throw new Error("protobuf varint exceeds safe integer range");
      if ((byte & 0x80) === 0) return {value, offset};
      multiplier *= 128;
    }
    throw new Error("protobuf varint is too long");
  }

  function readMessage(value) {
    const bytes = value instanceof Uint8Array ? value : new Uint8Array(value);
    if (bytes.length > MAX_PROTO_BYTES) throw new Error("protobuf payload exceeds 64 KiB");
    const fields = [];
    let offset = 0;
    while (offset < bytes.length) {
      const key = readVarint(bytes, offset);
      offset = key.offset;
      const number = Math.floor(key.value / 8);
      const wire = key.value % 8;
      if (!Number.isSafeInteger(number) || number < 1 || number > 0x1fffffff) throw new Error("invalid protobuf field number");
      if (wire === 0) {
        const result = readVarint(bytes, offset);
        offset = result.offset;
        fields.push({number, wire, value: result.value});
      } else if (wire === 1) {
        if (offset + 8 > bytes.length) throw new Error("truncated protobuf fixed64 field");
        offset += 8;
      } else if (wire === 2) {
        const length = readVarint(bytes, offset);
        offset = length.offset;
        if (length.value > MAX_PROTO_BYTES || offset + length.value > bytes.length) throw new Error("truncated protobuf bytes field");
        fields.push({number, wire, bytes: bytes.slice(offset, offset + length.value)});
        offset += length.value;
      } else if (wire === 5) {
        if (offset + 4 > bytes.length) throw new Error("truncated protobuf fixed32 field");
        offset += 4;
      } else {
        throw new Error(`unsupported protobuf wire type ${wire}`);
      }
    }
    return fields;
  }

  function firstVarint(fields, number, fallback = null) {
    const field = fields.find(candidate => candidate.number === number && candidate.wire === 0);
    return field ? field.value : fallback;
  }

  function allBytes(fields, number) {
    return fields.filter(candidate => candidate.number === number && candidate.wire === 2).map(candidate => candidate.bytes);
  }

  function firstBytes(fields, number) { return allBytes(fields, number)[0] || null; }

  function decodeUtf8(bytes) {
    if (!bytes) return null;
    return typeof TextDecoder === "function" ? new TextDecoder().decode(bytes).slice(0, 256) : boundedString(String.fromCharCode(...bytes), 256);
  }

  function bytesFromValue(value) {
    if (value instanceof ArrayBuffer) return new Uint8Array(value);
    if (ArrayBuffer.isView(value)) return new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
    if (value && typeof value === "object" && value.type === "bytes" && typeof value.base64 === "string") {
      const binary = atob(value.base64);
      if (binary.length > MAX_PROTO_BYTES) throw new Error("state payload exceeds 64 KiB");
      return Uint8Array.from(binary, character => character.charCodeAt(0));
    }
    if (typeof value === "string") {
      const binary = atob(value);
      if (binary.length > MAX_PROTO_BYTES) throw new Error("state payload exceeds 64 KiB");
      return Uint8Array.from(binary, character => character.charCodeAt(0));
    }
    throw new Error("GetState returned neither bytes nor base64");
  }

  function parseMode(bytes) {
    const fields = readMessage(bytes);
    return {
      id: String(firstVarint(fields, 1, "")),
      width: firstVarint(fields, 2),
      height: firstVarint(fields, 3),
      refresh_hz: firstVarint(fields, 4),
    };
  }

  function parseDisplay(bytes) {
    const fields = readMessage(bytes);
    return {
      id: String(firstVarint(fields, 1, "")),
      name: decodeUtf8(firstBytes(fields, 2)),
      description: decodeUtf8(firstBytes(fields, 3)),
      is_internal: firstVarint(fields, 6) === 1,
      current_mode_id: firstVarint(fields, 10) === null ? null : String(firstVarint(fields, 10)),
      modes: allBytes(fields, 11).map(parseMode).filter(mode => Number.isInteger(mode.width) && Number.isInteger(mode.height)),
      rgb_range: firstVarint(fields, 19, 0),
    };
  }

  function unwrapStateReply(value) {
    if (value && typeof value === "object" && Object.prototype.hasOwnProperty.call(value, "reply"))
      return {payload: value.reply, envelope: {result: value.result ?? null, message: typeof value.message === "string" ? value.message : null}};
    return {payload: value, envelope: null};
  }

  function decodeDisplayState(value) {
    const reply = unwrapStateReply(value);
    const fields = readMessage(bytesFromValue(reply.payload));
    return {
      displays: allBytes(fields, 1).map(parseDisplay),
      is_mode_switching_supported: firstVarint(fields, 2),
      compatibility_mode: firstVarint(fields, 3),
      response: reply.envelope,
    };
  }

  function decodeMonitorInfo(value) {
    const payload = value && typeof value === "object" && Object.prototype.hasOwnProperty.call(value, "reply") ? value.reply : value;
    const fields = readMessage(bytesFromValue(payload));
    return {
      selected_device_name: decodeUtf8(firstBytes(fields, 1)) || "",
      monitors: allBytes(fields, 2).map(bytes => {
        const monitorFields = readMessage(bytes);
        return {
          monitor_device_name: decodeUtf8(firstBytes(monitorFields, 1)) || "",
          monitor_display_name: decodeUtf8(firstBytes(monitorFields, 2)) || "",
        };
      }).filter(monitor => monitor.monitor_device_name),
    };
  }

  async function readPreferredMonitorInfo() {
    const settings = steamSettings();
    if (!settings || typeof settings.GetMonitorInfo !== "function") throw new Error("Settings.GetMonitorInfo unavailable");
    const returned = await timeout(Reflect.apply(settings.GetMonitorInfo, settings, []), 5000);
    return decodeMonitorInfo(returned);
  }

  function encodeVarint(value) {
    if (!Number.isSafeInteger(value) || value < 0) throw new Error("Steam ID is invalid");
    const bytes = [];
    do {
      let byte = value % 128;
      value = Math.floor(value / 128);
      if (value) byte |= 0x80;
      bytes.push(byte);
    } while (value);
    return Uint8Array.from(bytes);
  }

  function concatBytes(...parts) {
    const result = new Uint8Array(parts.reduce((sum, part) => sum + part.length, 0));
    let offset = 0;
    for (const part of parts) { result.set(part, offset); offset += part.length; }
    return result;
  }

  function encodeSetMode(outputId, modeId, rgbRange = 0) {
    const display = Number(outputId);
    const mode = Number(modeId);
    if (!Number.isSafeInteger(display) || !Number.isSafeInteger(mode) || display < 0 || mode < 0) throw new Error("Steam display target is invalid");
    const range = rgbRange === 1 || rgbRange === 2 ? rgbRange : 0;
    const bytes = concatBytes(
      concatBytes(encodeVarint(8), encodeVarint(display)),
      concatBytes(encodeVarint(16), encodeVarint(mode)),
      concatBytes(encodeVarint(24), encodeVarint(range)),
    );
    let binary = "";
    for (const byte of bytes) binary += String.fromCharCode(byte);
    return btoa(binary);
  }

  function normalizedSnapshot(state, reason = "") {
    const displays = state?.displays || [];
    const currentSystem = steamSystem();
    const currentDisplayManager = displayManager();
    const currentSettings = steamSettings();
    const displayReady = Boolean(currentDisplayManager && typeof currentDisplayManager.GetState === "function" && typeof currentDisplayManager.SetMode === "function");
    const preferredMonitorReady = Boolean(currentSettings && typeof currentSettings.SetPreferredMonitor === "function");
    const selectionReason = "Live Gaming Mode screen selection is not verified on this SteamOS build.";
    return {
      ready: displayReady,
      reason: boundedString(reason),
      methods: {
        suspend: typeof currentSystem?.SuspendPC === "function",
        restart: typeof currentSystem?.RestartPC === "function",
        shutdown: typeof currentSystem?.ShutdownPC === "function",
        display: displayReady,
        preferred_monitor: preferredMonitorReady,
        preferred_monitor_readback: false,
        // GetState exposes display modes, but this build does not expose a
        // verified active-connector selector. Keep the local chooser
        // read-only until a fixed adapter with explicit readback exists.
        display_selection: false,
      },
      outputs: displays.slice(0, 8).map(display => ({
        id: display.id,
        output_key: display.id,
        name: display.name,
        display_name: display.name,
        description: display.description,
        is_internal: display.is_internal,
        connector: null,
        gpu_id: null,
        connected: true,
        active: null,
        identity_confidence: "unknown",
        can_switch_live: false,
        can_set_startup_preference: false,
        restart_required: false,
        recovery_available: false,
        current_mode_id: display.current_mode_id,
        modes: display.modes.slice(0, 256),
        // Steam provides no cross-call generation in this legacy bridge. A
        // monotonic frontend generation rejects a target that was refreshed
        // after a hotplug/readback change without exposing raw Steam data.
        generation: snapshotGeneration(display),
        rgb_range: display.rgb_range === 1 || display.rgb_range === 2 ? display.rgb_range : 0,
      })),
      generation: Math.max(0, ...displays.slice(0, 8).map(display => snapshotGeneration(display))),
      active_output_key: null,
      selection: {
        active_output_key: null,
        can_switch_live: false,
        can_set_startup_preference: false,
        restart_required: false,
        recovery_available: false,
        reason: selectionReason,
        adapter: null,
      },
      cpu_temperature: null,
      reported_at: new Date().toISOString(),
    };
  }

  const generations = new Map();
  function snapshotGeneration(display) {
    // A mode switch changes current_mode_id by design. Generation identifies
    // the output/mode inventory instead, so a preview remains confirmable and
    // restorable after Steam reports the switched mode.
    const signature = JSON.stringify({
      id: display.id,
      name: display.name,
      description: display.description,
      is_internal: display.is_internal,
      // Steam can assign fresh IDs while applying a mode. Stable properties
      // keep that expected re-enumeration from invalidating a live preview.
      modes: display.modes.map(mode => ({width: mode.width, height: mode.height, refresh_hz: mode.refresh_hz})),
      rgb_range: display.rgb_range,
    });
    const old = generations.get(display.id);
    if (!old || old.signature !== signature) {
      const next = {signature, generation: (old?.generation || 0) + 1};
      generations.set(display.id, next);
      return next.generation;
    }
    return old.generation;
  }

  async function callBackend(method, args = {}) {
    try {
      const modern = loaderPluginAPI();
      let reply;
      if (modern) {
        const names = BACKEND_ARGUMENTS[method] || [];
        const values = names.map(name => args && Object.prototype.hasOwnProperty.call(args, name) ? args[name] : undefined);
        reply = await timeout(modern.call(method, ...values), 5000);
      } else {
        if (typeof serverAPI?.callPluginMethod !== "function") throw new Error("Decky plugin backend API is unavailable");
        reply = await timeout(serverAPI.callPluginMethod(method, args), 5000);
      }
      // API-v0 wraps plugin results in {success, result}; API-v1 returns the
      // plugin result directly. Keep accepting both shapes for installations
      // that reload the frontend before the backend has been upgraded.
      if (reply?.success === false && Object.prototype.hasOwnProperty.call(reply, "result")) throw new Error(boundedString(reply.result));
      if (reply?.success === true && Object.prototype.hasOwnProperty.call(reply, "result")) return reply.result;
      return reply;
    } catch (error) {
      // Do not log args: pairing payloads and client tokens must stay private.
      logBackendFailure(method, error);
      throw error;
    }
  }

  function showPairingToast(item) {
    const toaster = serverAPI?.toaster;
    if (!toaster || typeof toaster.toast !== "function") return;
    const client = boundedString(item?.client_name || "Omarchy client", 96);
    const code = item?.verification_code ? `Code: ${boundedString(item.verification_code, 32)}` : "Open the plugin to review the pairing request.";
    try {
      toaster.toast({
        title: "SteamOS Companion pairing request",
        body: `${client} requested access. ${code} Open SteamOS Companion and approve only after checking the matching code.`,
        duration: 10000,
      });
    } catch (error) {
      console.warn(tag, "pairing notification failed", boundedString(error));
    }
  }

  async function readPairingRequests() {
    if (stopped || pairingWatchBusy) return;
    pairingWatchBusy = true;
    try {
      const value = await callBackend("list_pairings");
      const pending = Array.isArray(value) ? value.filter(item => item?.status === "pending") : [];
      const current = new Map(pending.filter(item => item && item.pairing_id).map(item => [item.pairing_id, item]));
      if (knownPendingPairings !== null) {
        for (const [pairingId, item] of current) {
          if (!knownPendingPairings.has(pairingId)) showPairingToast(item);
        }
      }
      knownPendingPairings = current;
    } catch (_) {
      // The settings page reports backend failures; the background watcher is
      // best-effort and must not interfere with the long-lived bridge.
    } finally {
      pairingWatchBusy = false;
    }
  }

  function startPairingWatcher() {
    if (typeof serverAPI?.toaster?.toast !== "function") return;
    // The request that created the pending record has just returned. Wait for
    // the interval before the first check so the host's pairing limiter is not
    // hit by an immediate second request.
    if (!pairingWatchTimer) pairingWatchTimer = setInterval(() => { void readPairingRequests(); }, 1000);
  }

  function startSunshineOwnerWatcher() {
    if (!sunshineMonitoringEnabled || sunshineOwnerTimer) return;
    void connectSunshineOwner();
    sunshineOwnerTimer = setInterval(() => { void connectSunshineOwner(); }, 10000);
  }

  async function startRuntime() {
    if (runtimeStartPromise) return runtimeStartPromise;
    runtimeStartPromise = (async () => {
      let value;
      let nextRole = "setup";
      try {
        value = await callBackend("get_settings");
        updateCurrentVersion(value);
        setSunshineMonitoring(value?.settings?.monitor_sunshine === true);
        const effective = value?.mode?.effective ?? value?.device_mode;
        nextRole = effective === "client" || effective === "server" || effective === "both" ? effective : "setup";
      } catch (_) {
        // Keep older API-v0 installations serving the host bridge if the
        // coordinator RPC is unavailable during a Decky reload. A healthy
        // coordinator always returns an explicit role, and Client/setup never
        // enter this fallback.
        nextRole = "server";
      }
      setRuntimeRole(nextRole);
      startUpdateWatcher();
      return runtimeRole;
    })();
    return runtimeStartPromise;
  }

  async function readSnapshot(reason = "poll", force = false) {
    if (stopped || (runtimeReady && !roleHasServer(runtimeRole))) return latestSnapshot;
    if (snapshotBusy) {
      if (!force) return latestSnapshot;
      while (snapshotBusy && !stopped) await delay(10);
      if (stopped) return latestSnapshot;
    }
    snapshotBusy = true;
    try {
      const currentDisplayManager = displayManager();
      if (!currentDisplayManager || typeof currentDisplayManager.GetState !== "function") throw new Error("DisplayManager.GetState unavailable");
      const state = decodeDisplayState(await timeout(Reflect.apply(currentDisplayManager.GetState, currentDisplayManager, []), 5000));
      latestSnapshot = normalizedSnapshot(state, "");
      lastSnapshotAt = Date.now();
      await callBackend("report_bridge_snapshot", {snapshot: latestSnapshot});
      notify();
      return latestSnapshot;
    } catch (error) {
      latestSnapshot = normalizedSnapshot(null, `${reason}: ${boundedString(error)}`);
      latestSnapshot.ready = false;
      latestSnapshot.methods.display = false;
      const currentSystem = steamSystem();
      latestSnapshot.methods.suspend = typeof currentSystem?.SuspendPC === "function";
      latestSnapshot.methods.restart = typeof currentSystem?.RestartPC === "function";
      latestSnapshot.methods.shutdown = typeof currentSystem?.ShutdownPC === "function";
      try { await callBackend("report_bridge_snapshot", {snapshot: latestSnapshot}); } catch (_) {}
      notify();
      return latestSnapshot;
    } finally {
      snapshotBusy = false;
    }
  }

  function findOutput(id) { return latestSnapshot?.outputs?.find(output => output.id === String(id)) || null; }
  function findMode(output, id) { return output?.modes?.find(mode => mode.id === String(id)) || null; }

  async function executeCommand(command) {
    if (!command || !command.command_id || !command.kind || !command.payload) return;
    let result;
    let action = "";
    try {
      if (command.kind === "set_mode") {
        const currentDisplayManager = displayManager();
        const output = findOutput(command.payload.output_id);
        const mode = findMode(output, command.payload.mode_id);
        if (!output || !mode || output.generation !== command.payload.generation)
          throw new Error("display target is stale or no longer advertised");
        const payload = encodeSetMode(output.id, mode.id, output.rgb_range);
        if (!currentDisplayManager || typeof currentDisplayManager.SetMode !== "function") throw new Error("DisplayManager.SetMode unavailable");
        const returned = await timeout(Reflect.apply(currentDisplayManager.SetMode, currentDisplayManager, [payload]), 5000);
        const snapshot = await readSnapshot("mode readback", true);
        result = {ok: true, outcome: "method_returned", returned: serialize(returned), payload_base64: payload, snapshot};
      } else if (command.kind === "select_output") {
        // The currently shipped SteamOS DisplayManager surface has no
        // verified active-connector operation. Do not turn an opaque backend
        // request into a guessed method name or raw Gamescope invocation.
        throw new Error("active Gaming Mode screen selection is not verified on this SteamOS build");
      } else if (command.kind === "set_preferred_monitor") {
        const settings = steamSettings();
        const monitorDeviceName = boundedString(command.payload.monitor_device_name, 128);
        if (!/^[A-Za-z0-9_.:-]{0,128}$/.test(monitorDeviceName)) throw new Error("preferred monitor target is invalid");
        if (!settings || typeof settings.SetPreferredMonitor !== "function") throw new Error("Settings.SetPreferredMonitor unavailable");
        const returned = await timeout(Reflect.apply(settings.SetPreferredMonitor, settings, [monitorDeviceName]), 5000);
        let readback = null;
        try { readback = await readPreferredMonitorInfo(); } catch (_) {}
        result = {
          ok: true,
          outcome: "preferred_monitor_method_returned",
          returned: serialize(returned),
          monitor_device_name: monitorDeviceName,
          readback,
        };
      } else if (command.kind === "power") {
        action = boundedString(command.payload.action, 32);
        const methodName = powerMethodName(action);
        const currentSystem = steamSystem();
        if (!methodName || typeof currentSystem?.[methodName] !== "function") throw new Error(`${methodName || "Power method"} is unavailable`);
        console.info(tag, "dispatching power action", action);
        const returned = await timeout(Reflect.apply(currentSystem[methodName], currentSystem, []), 5000);
        console.info(tag, "power method returned", action);
        result = {ok: true, outcome: "method_returned", returned: serialize(returned), action};
      } else if (command.kind === "sunshine_status") {
        const running = ownerRunningValue(await callSunshineOwner(SUNSHINE_OWNER_METHODS.status));
        if (running === null) throw new Error("Decky Sunshine returned an invalid status");
        result = {ok: true, running, owner: SUNSHINE_OWNER_PLUGIN};
      } else if (command.kind === "sunshine_restart") {
        const returned = await callSunshineOwner(SUNSHINE_OWNER_METHODS.start);
        if (returned === false || (returned && returned.ok === false)) throw new Error("Decky Sunshine rejected restart");
        result = {ok: true, outcome: "method_returned", returned: serialize(returned), owner: SUNSHINE_OWNER_PLUGIN};
      } else {
        throw new Error("unsupported bridge command");
      }
    } catch (error) {
      console.warn(tag, "power/display command failed", action || command.kind, boundedString(error));
      result = {ok: false, reason: boundedString(error), unknown: /timeout|suspend|restart|shutdown|power/i.test(boundedString(error))};
    }
    try { await callBackend("report_bridge_result", {command_id: command.command_id, result}); }
    catch (error) { console.warn(tag, "bridge result was not accepted", boundedString(error)); }
  }

  async function driverCycle() {
    if (stopped || (runtimeReady && !roleHasServer(runtimeRole))) {
      driverTimer = null;
      return;
    }
    if (!commandBusy) {
      commandBusy = true;
      try {
        const command = await callBackend("next_bridge_command");
        if (command) await executeCommand(command);
      } catch (error) {
        // A missing backend during Decky reload is a bounded transient; do not
        // turn it into a remote operation or log request material.
      } finally { commandBusy = false; }
    }
    if (Date.now() - lastSnapshotAt >= snapshotPollMs && !snapshotBusy) void readSnapshot("periodic");
    if (stopped || (runtimeReady && !roleHasServer(runtimeRole))) {
      driverTimer = null;
      return;
    }
    driverTimer = setTimeout(driverCycle, commandPollMs);
  }

  // Server/Both starts the long-lived Steam bridge. Client/setup starts no
  // listener, display polling, pairing watcher, or Sunshine owner watcher.
  void startRuntime();

  function LegacyHostSettings() {
    const [, refresh] = React.useState(0);
    const [now, setNow] = React.useState(() => Date.now());
    const [settings, setSettings] = React.useState(null);
    const [busy, setBusy] = React.useState(false);
    const [message, setMessage] = React.useState("");
    const [pairing, setPairing] = React.useState(null);
    const [address, setAddress] = React.useState("0.0.0.0");
    const [port, setPort] = React.useState(18443);
    const [advertisedHost, setAdvertisedHost] = React.useState("");
    const [detectedHost, setDetectedHost] = React.useState("");
    const [draftDirty, setDraftDirty] = React.useState(false);
    const draftDirtyRef = useRef(false);

    function markDraftDirty() {
      draftDirtyRef.current = true;
      setDraftDirty(true);
    }

    function clearDraftDirty() {
      draftDirtyRef.current = false;
      setDraftDirty(false);
    }

    function cancelDraft() {
      const saved = settings?.settings || {};
      setAddress(saved.listen_address || "0.0.0.0");
      setPort(saved.listen_port || 18443);
      setAdvertisedHost(saved.advertised_host || "");
      clearDraftDirty();
      setMessage("Unsaved listener changes discarded");
    }

    async function load() {
      try {
        const value = await callBackend("get_settings");
        setSunshineMonitoring(value?.settings?.monitor_sunshine === true);
        setSettings(previous => ({
          ...value,
          // Local display order is an additive resource fetched only when
          // its page is opened. Keep it across the global settings poll so
          // the local-only page does not briefly lose its inventory.
          ...(previous?.local_display_order ? {local_display_order: previous.local_display_order} : {}),
        }));
        if (!draftDirtyRef.current) {
          setAddress(value?.settings?.listen_address || "0.0.0.0");
          setPort(value?.settings?.listen_port || 18443);
          setAdvertisedHost(value?.settings?.advertised_host || "");
        }
        setDetectedHost(value?.pairing_host || "");
      } catch (error) { setMessage(`Settings unavailable: ${boundedString(error)}`); }
    }

    React.useEffect(() => {
      notify = () => refresh(value => value + 1);
      void load();
      const settingsTimer = setInterval(() => { void load(); }, 2000);
      const countdownTimer = setInterval(() => setNow(Date.now()), 1000);
      return () => {
        clearInterval(settingsTimer);
        clearInterval(countdownTimer);
        notify = () => {};
      };
    }, []);

    async function run(action) {
      setBusy(true); setMessage("");
      try { await action(); await load(); }
      catch (error) { setMessage(boundedString(error)); }
      finally { setBusy(false); }
    }

    function button(label, action, disabled = false) {
      return React.createElement("button", {
        onClick: () => void run(action),
        disabled: busy || disabled,
        style: {
          display: "block",
          width: "100%",
          minHeight: "44px",
          marginTop: "8px",
          padding: "8px 12px",
          textAlign: "left",
          color: disabled || busy ? UI_COLORS.disabledText : UI_COLORS.text,
          background: disabled || busy ? UI_COLORS.disabledSurface : UI_COLORS.surface,
          border: `1px solid ${UI_COLORS.border}`,
          borderRadius: "4px",
          appearance: "none",
        },
      }, label);
    }

    async function copyPairing() {
      if (!pairing?.payload) return;
      try {
        if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
          await navigator.clipboard.writeText(pairing.payload);
          setMessage("Full pairing payload copied");
        } else {
          setMessage("Clipboard is unavailable; select and copy the full payload");
        }
      } catch (_) { setMessage("Clipboard is unavailable; select and copy the full payload"); }
    }

    function pairingRemainingSeconds(expiresAt) {
      const expiry = Number(expiresAt);
      if (!Number.isFinite(expiry)) return null;
      return Math.max(0, Math.ceil(expiry - now / 1000));
    }

    function pairingCountdown(seconds) {
      if (seconds === null) return "unknown";
      if (seconds <= 0) return "expired";
      const minutes = Math.floor(seconds / 60);
      const remainder = String(seconds % 60).padStart(2, "0");
      return `${minutes}:${remainder}`;
    }

    function renderPendingPairing(item) {
      const remaining = pairingRemainingSeconds(item.expires_at);
      const expired = remaining !== null && remaining <= 0;
      const scopes = item.requested_scopes?.join(", ") || "status.read";
      return React.createElement("div", {key: item.pairing_id, style: {marginTop: "8px", padding: "8px", border: "1px solid rgba(255,255,255,0.25)", borderRadius: "4px"}},
        React.createElement("strong", {style: {display: "block"}}, "New pairing request"),
        React.createElement("span", {style: {display: "block"}}, `${item.client_name || "Omarchy client"} — ${scopes}`),
        item.verification_code && React.createElement("small", {style: {display: "block", marginTop: "6px"}}, "Verification code — compare with Omarchy:"),
        item.verification_code && React.createElement("div", {style: {fontSize: "24px", fontFamily: "monospace", fontWeight: "bold", letterSpacing: "3px", margin: "2px 0 4px"}}, item.verification_code),
        React.createElement("small", {style: {display: "block", fontWeight: "bold"}}, `Expires in: ${pairingCountdown(remaining)}`),
        item.verification_code
          ? React.createElement("small", {style: {display: "block", marginTop: "4px"}}, expired ? "This code has expired. Start a new request from Omarchy." : "Approve only when this code matches exactly on both screens.")
          : React.createElement("small", {style: {display: "block", marginTop: "4px"}}, expired ? "This pairing request has expired." : "Review the requested scopes before approving."),
        button("Reject", async () => { await callBackend("reject_pairing", {pairing_id: item.pairing_id}); }, expired),
        button("Approve", async () => { await callBackend("approve_pairing", {pairing_id: item.pairing_id, scopes: item.requested_scopes}); }, expired)
      );
    }

    const provider = settings?.provider;
    const sunshine = settings?.sunshine;
    const pending = settings?.pending_pairings || [];
    const clients = settings?.clients || [];
    const bridge = settings?.bridge || latestSnapshot;
    const wakeTarget = settings?.wake_target;

    function providerSummary() {
      if (provider?.ready) return `${provider.provider} contract ${provider.contract_version}`;
      if (sunshine?.provider) return `${sunshine.provider} status only — owner bridge unavailable`;
      return `Unavailable — ${provider?.reason || "Decky Sunshine owner plugin is not connected"}`;
    }

    function sunshineSummary() {
      if (!sunshine) return "Unavailable — no observation yet";
      if (sunshine.reason && /provider is not connected|owner plugin was not reachable|owner bridge is unavailable/i.test(String(sunshine.reason))) {
        return "Unavailable — Decky Sunshine is not connected through Decky Loader";
      }
      return `${sunshine.state || "unavailable"}${sunshine.reason ? ` — ${sunshine.reason}` : ""}`;
    }

    return React.createElement("div", {style: {padding: "12px", lineHeight: "1.45", maxWidth: "680px", minHeight: "100%", color: UI_COLORS.text, background: UI_COLORS.background}},
      React.createElement("h2", null, "SteamOS Companion host"),
      React.createElement("p", null, `Host identity: ${settings?.host_id || "Unavailable"}`),
      React.createElement("p", null, `Steam bridge: ${bridge?.ready ? "Ready" : "Unavailable"}${bridge?.reason ? ` — ${bridge.reason}` : ""}`),
      React.createElement("p", null, `TLS pin: ${settings?.tls?.fingerprint || "Unavailable"}${settings?.tls?.reason ? ` — ${settings.tls.reason}` : ""}`),
      React.createElement("h3", null, "Listener"),
      React.createElement("label", null, "Bind address (0.0.0.0 = all interfaces) ", React.createElement("input", {value: address, onChange: event => { setAddress(event.target.value); markDraftDirty(); }})),
      React.createElement("label", {style: {display: "block", marginTop: "4px"}}, "Port ", React.createElement("input", {type: "number", min: 1024, max: 65535, value: port, onChange: event => { setPort(Number(event.target.value)); markDraftDirty(); }})),
      React.createElement("label", {style: {display: "block", marginTop: "4px"}}, "Pairing host/IP override ", React.createElement("input", {value: advertisedHost, placeholder: detectedHost ? `auto-detect (${detectedHost})` : "auto-detect", onChange: event => { setAdvertisedHost(event.target.value); markDraftDirty(); }})),
      React.createElement("small", {style: {display: "block"}}, advertisedHost ? "Using the manually entered pairing host/IP." : `Using the active route address: ${detectedHost || "unavailable"}`),
      React.createElement("small", {style: {display: "block"}}, wakeTarget?.available ? `Wake target advertised after pairing: ${wakeTarget.mac} via ${wakeTarget.interface || "active host interface"}` : `Wake target unavailable: ${wakeTarget?.reason || "no active host interface"}`),
      draftDirty && React.createElement("small", {style: {display: "block", marginTop: "4px"}}, "Unsaved listener changes are preserved while this page refreshes."),
      button("Save listener settings", async () => {
        await callBackend("update_settings", {changes: {listen_address: address, listen_port: port, advertised_host: advertisedHost}});
        clearDraftDirty();
      }),
      draftDirty && React.createElement("button", {onClick: cancelDraft, disabled: busy, style: {display: "block", marginTop: "6px"}}, "Cancel listener edits"),
      React.createElement("h3", null, "Sunshine"),
      React.createElement("label", null, React.createElement("input", {type: "checkbox", checked: settings?.settings?.monitor_sunshine === true, onChange: event => void run(async () => { await callBackend("update_settings", {changes: {monitor_sunshine: event.target.checked}}); })}), " Monitor Sunshine"),
      React.createElement("label", {style: {display: "block", marginTop: "4px"}}, React.createElement("input", {type: "checkbox", checked: settings?.settings?.auto_recover_sunshine !== false, disabled: settings?.settings?.monitor_sunshine !== true, onChange: event => void run(async () => { await callBackend("update_settings", {changes: {auto_recover_sunshine: event.target.checked}}); })}), " Auto-recover Sunshine"),
      React.createElement("p", null, "When enabled, paired clients can see Decky Sunshine status and request a restart only after the Decky owner bridge confirms it is stopped. A read-only process check may keep status visible during a short reload gap; this plugin never starts a separate Sunshine process."),
      React.createElement("p", null, "Automatic recovery makes one owner-plugin start request after a running-to-stopped observation. If the owner cannot recover Sunshine, the manual Recover button remains available."),
      React.createElement("p", null, `Provider: ${providerSummary()}`),
      React.createElement("p", null, `Observation: ${sunshineSummary()}`),
      React.createElement("h3", null, "Pairing"),
      React.createElement("p", null, "Start on Omarchy: generate a code, send the pairing request, then approve here only when the code matches on both screens."),
      button("Advanced: create full pairing payload", async () => { setPairing(await callBackend("create_pairing", {requested_scopes: ["status.read", "power.control", "display.control"]})); }),
      pairing && React.createElement("div", {style: {marginTop: "8px"}},
        React.createElement("p", null, `Expires: ${new Date(pairing.expires_at * 1000).toLocaleTimeString()}`),
        pairing.qr_svg_base64 && React.createElement("img", {alt: "Pairing QR code", src: `data:image/svg+xml;base64,${pairing.qr_svg_base64}`, style: {display: "block", width: "220px", height: "220px", background: "white", padding: "8px"}}),
        React.createElement("textarea", {readOnly: true, value: pairing.payload, rows: 4, style: {width: "100%"}}),
        React.createElement("button", {onClick: () => void copyPairing(), style: {display: "block", marginTop: "6px"}}, "Copy full pairing payload"),
        React.createElement("p", null, "Paste the full payload into the Omarchy client, then approve the pending request below.")),
      React.createElement("h4", null, "Pending requests"),
      pending.length ? pending.map(renderPendingPairing)
        : React.createElement("p", null, "No pending pairing request."),
      React.createElement("h4", null, "Paired clients"),
      clients.length ? clients.map(item => React.createElement("div", {key: item.client_id, style: {marginTop: "6px"}},
        React.createElement("span", null, `${item.name} — ${item.scopes?.join(", ")}`),
        button("Revoke", async () => { await callBackend("revoke_client", {client_id: item.client_id}); })))
        : React.createElement("p", null, "No paired clients."),
      message && React.createElement("p", null, message),
      React.createElement("small", {style: {display: "block", opacity: 0.75}}, settings?.diagnostics?.log_path ? `Backend log: ${settings.diagnostics.log_path}` : "Backend log is unavailable until the plugin responds."),
      React.createElement("p", null, "The frontend bridge continues while this settings view is closed. Remote operations remain bounded and report Requested/Observed separately.")
    );
  }

  function childList(value) {
    return Array.isArray(value) ? value : value == null ? [] : [value];
  }

  function native(type, fallback, props, children) {
    const Component = NativeUI[type] || fallback;
    return React.createElement(Component, props || {}, ...childList(children));
  }

  function PanelSection({title, children}) {
    const sectionProps = {title, style: {color: UI_COLORS.text}};
    if (NativeUI.PanelSection) return React.createElement(NativeUI.PanelSection, sectionProps, ...childList(children));
    return React.createElement("section", {style: {color: UI_COLORS.text}}, React.createElement("h3", {style: {color: UI_COLORS.text}}, title), ...childList(children));
  }

  function PanelSectionRow({focusKey, children}) {
    return native("PanelSectionRow", "div", {
      "data-focus-key": focusKey,
      focusKey,
      style: {display: "block", minWidth: 0, color: UI_COLORS.text},
    }, childList(children));
  }

  function Text({children, muted = false, live = false}) {
    return React.createElement("div", {
      "aria-live": live ? "polite" : undefined,
      style: {display: "block", marginTop: "6px", color: muted ? UI_COLORS.muted : UI_COLORS.text, overflowWrap: "anywhere"},
    }, children);
  }

  function Button({label, onClick, disabled = false, focusKey, autoFocus = false, danger = false}) {
    const props = {
      type: "button",
      onClick,
      disabled,
      autoFocus,
      focusKey,
      "data-focus-key": focusKey,
      "aria-label": label,
      style: {
        display: "block",
        width: "100%",
        minHeight: "44px",
        padding: "8px 12px",
        marginTop: "8px",
        textAlign: "left",
        color: disabled ? UI_COLORS.disabledText : danger ? UI_COLORS.danger : UI_COLORS.text,
        background: disabled ? UI_COLORS.disabledSurface : UI_COLORS.surface,
        border: `1px solid ${danger ? UI_COLORS.danger : UI_COLORS.border}`,
        borderRadius: "4px",
        appearance: "none",
      },
    };
    if (NativeUI.ButtonItem) return React.createElement(NativeUI.ButtonItem, {...props, layout: "below"}, label);
    return React.createElement("button", props, label);
  }

  function SelectableRow({selected, title, description, onClick, focusKey, autoFocus = false, disabled = false}) {
    const label = `${selected ? "Selected. " : ""}${disabled ? "Unavailable. " : ""}${title}. ${description}`;
    const props = {
      type: "button",
      onClick,
      disabled,
      autoFocus,
      focusKey,
      "data-focus-key": focusKey,
      "aria-label": label,
      "aria-pressed": selected,
      style: {
        display: "block",
        width: "100%",
        minHeight: "56px",
        marginTop: "8px",
        padding: "8px",
        textAlign: "left",
        color: disabled ? UI_COLORS.disabledText : UI_COLORS.text,
        border: selected ? `2px solid ${UI_COLORS.accent}` : `1px solid ${UI_COLORS.border}`,
        borderRadius: "4px",
        background: disabled ? UI_COLORS.disabledSurface : selected ? UI_COLORS.surfaceSelected : UI_COLORS.surface,
        appearance: "none",
      },
    };
    const content = [
      React.createElement("strong", {key: "title", style: {display: "block", color: disabled ? UI_COLORS.disabledText : UI_COLORS.text}}, `${selected ? "● " : "○ "}${title}`),
      React.createElement("span", {key: "description", style: {display: "block", marginTop: "3px", color: disabled ? UI_COLORS.disabledText : UI_COLORS.muted}}, description),
    ];
    if (NativeUI.ButtonItem) return React.createElement(NativeUI.ButtonItem, {...props, layout: "below", highlightOnFocus: true}, ...content);
    if (NativeUI.Focusable) return React.createElement(NativeUI.Focusable, {...props, onActivate: onClick}, ...content);
    return React.createElement("button", props, ...content);
  }

  function ToggleRow({label, description, checked, onClick, focusKey, disabled = false}) {
    const accessibleLabel = `${label}. ${checked ? "On" : "Off"}. ${description}`;
    const props = {
      type: "button",
      onClick,
      disabled,
      focusKey,
      "data-focus-key": focusKey,
      "aria-label": accessibleLabel,
      "aria-pressed": checked,
      style: {
        display: "block",
        width: "100%",
        minHeight: "56px",
        marginTop: "8px",
        padding: "8px",
        textAlign: "left",
        color: disabled ? UI_COLORS.disabledText : UI_COLORS.text,
        background: disabled ? UI_COLORS.disabledSurface : UI_COLORS.surface,
        border: `1px solid ${UI_COLORS.border}`,
        borderRadius: "4px",
        appearance: "none",
      },
    };
    const content = [
      React.createElement("strong", {key: "title", style: {display: "block", color: disabled ? UI_COLORS.disabledText : UI_COLORS.text}}, `${checked ? "☑" : "☐"} ${label}`),
      React.createElement("span", {key: "description", style: {display: "block", marginTop: "3px", color: disabled ? UI_COLORS.disabledText : UI_COLORS.muted}}, description),
    ];
    if (NativeUI.ButtonItem) return React.createElement(NativeUI.ButtonItem, {...props, layout: "below", highlightOnFocus: true}, ...content);
    if (NativeUI.Focusable) return React.createElement(NativeUI.Focusable, {...props, onActivate: onClick}, ...content);
    return React.createElement("button", props, ...content);
  }

  function Field({label, value, onChange, type = "text", placeholder = "", error = "", focusKey}) {
    const inputProps = {
      type,
      value: value ?? "",
      placeholder,
      onChange,
      "aria-label": label,
      "aria-invalid": Boolean(error),
      focusKey,
      "data-focus-key": focusKey,
      style: {
        display: "block",
        width: "100%",
        minHeight: "44px",
        boxSizing: "border-box",
        padding: "8px 10px",
        color: UI_COLORS.text,
        background: UI_COLORS.input,
        border: `1px solid ${UI_COLORS.border}`,
        borderRadius: "4px",
        caretColor: UI_COLORS.accent,
      },
    };
    if (NativeUI.TextField) return React.createElement(NativeUI.TextField, {...inputProps, label, description: error || undefined});
    return React.createElement("label", {style: {display: "block", marginTop: "10px", color: UI_COLORS.text}, "data-focus-key": focusKey},
      React.createElement("span", {style: {display: "block", marginBottom: "4px", color: UI_COLORS.text}}, label),
      React.createElement("input", inputProps),
      error && React.createElement("span", {style: {display: "block", color: UI_COLORS.danger, marginTop: "4px"}}, error)
    );
  }

  function Picker({label, value, options, onChange, focusKey}) {
    const nativePicker = NativeUI.Dropdown || NativeUI.Select;
    const pickerOptions = options.map(item => ({label: item.label, data: item.value, value: item.value}));
    if (nativePicker) return React.createElement(nativePicker, {
      label,
      value,
      options: pickerOptions,
      onChange: valueOrEvent => onChange({target: {value: valueOrEvent?.data ?? valueOrEvent?.value ?? valueOrEvent}}),
      focusKey,
      "data-focus-key": focusKey,
      style: {color: UI_COLORS.text, background: UI_COLORS.input},
    });
    return React.createElement("label", {style: {display: "block", marginTop: "10px", color: UI_COLORS.text}, "data-focus-key": focusKey},
      React.createElement("span", {style: {display: "block", marginBottom: "4px", color: UI_COLORS.text}}, label),
      React.createElement("select", {value, onChange, "aria-label": label, style: {display: "block", width: "100%", minHeight: "44px", padding: "8px 10px", color: UI_COLORS.text, background: UI_COLORS.input, border: `1px solid ${UI_COLORS.border}`, borderRadius: "4px"}}, options.map(item => React.createElement("option", {key: item.value, value: item.value, style: {color: UI_COLORS.text, background: UI_COLORS.input}}, item.label)))
    );
  }

  function ConfirmModal({title, body, cancelLabel = "Cancel", confirmLabel, onCancel, onConfirm, busy = false, danger = false}) {
    const modalChildren = [
      React.createElement("h3", {key: "title"}, title),
      React.createElement(Text, {key: "body"}, body),
      // Cancel is deliberately rendered first and initially focused.  The
      // release handler belongs to the focused native control, so opening a
      // modal cannot fall through into its first destructive action.
      React.createElement(Button, {key: "cancel", label: cancelLabel, onClick: onCancel, focusKey: "modal.cancel", autoFocus: true, disabled: busy}),
      React.createElement(Button, {key: "confirm", label: confirmLabel, onClick: onConfirm, focusKey: "modal.confirm", disabled: busy, danger}),
    ];
    if (NativeUI.ModalRoot) return React.createElement(NativeUI.ModalRoot, {"aria-modal": true, role: "dialog", style: {color: UI_COLORS.text}}, ...modalChildren);
    return React.createElement("div", {
      role: "dialog",
      "aria-modal": "true",
      style: {position: "relative", marginTop: "12px", padding: "12px", color: UI_COLORS.text, background: UI_COLORS.background, border: `2px solid ${UI_COLORS.border}`, borderRadius: "6px"},
    }, ...modalChildren);
  }

  function ClientContent() {
    const initialName = "SteamOS handheld";
    const update = useUpdateState();
    const [settings, setSettings] = React.useState(null);
    const [view, setView] = React.useState("loading");
    const [message, setMessage] = React.useState("");
    const [busy, setBusy] = React.useState("");
    const [now, setNow] = React.useState(() => Date.now());
    const [draftMode, setDraftMode] = React.useState("client");
    const [draftName, setDraftName] = React.useState(initialName);
    const [candidate, setCandidate] = React.useState(null);
    const [candidates, setCandidates] = React.useState([]);
    const [scan, setScan] = React.useState(null);
    const [manualHost, setManualHost] = React.useState("");
    const [manualPort, setManualPort] = React.useState("18443");
    const [manualError, setManualError] = React.useState("");
    const [pendingPairing, setPendingPairing] = React.useState(null);
    const [selectedOutputId, setSelectedOutputId] = React.useState("");
    const [selectedModeId, setSelectedModeId] = React.useState("");
    const [selectedProfileId, setSelectedProfileId] = React.useState("");
    const [selectedLocalOutputKey, setSelectedLocalOutputKey] = React.useState("");
    const [localOutputOrder, setLocalOutputOrder] = React.useState([]);
    const [remoteOutputOrder, setRemoteOutputOrder] = React.useState([]);
    const [modal, setModal] = React.useState(null);
    const viewRef = useRef("loading");
    const scanRef = useRef(null);
    const pairingRef = useRef(null);
    const pairingNoticeRef = useRef("");
    const modeDraftTouchedRef = useRef(false);
    const modeDraftInitializedRef = useRef(false);
    const localOutputOrderTouchedRef = useRef(false);
    const localDisplayOrderTouchedRef = useRef(false);
    const remoteOutputOrderTouchedRef = useRef(false);

    function navigate(next) {
      viewRef.current = next;
      setView(next);
      setMessage("");
    }

    function applyRole(value) {
      const role = value?.mode?.effective ?? value?.device_mode;
      setRuntimeRole(role);
      return role;
    }

    async function loadSettings() {
      try {
        const value = await callBackend("get_settings");
        updateCurrentVersion(value);
        setSunshineMonitoring(value?.settings?.monitor_sunshine === true);
        applyRole(value);
        setSettings(previous => ({
          ...value,
          // Local display order is an additive resource fetched only while
          // its page is open. Keep the last complete inventory during the
          // global settings poll so it cannot flash away between refreshes.
          ...(previous?.local_display_order ? {local_display_order: previous.local_display_order} : {}),
        }));
        const client = value?.client || {};
        if (client.client_name && viewRef.current === "loading") setDraftName(client.client_name);
        const selectedMode = value?.mode?.selected;
        if (!modeDraftInitializedRef.current && ["client", "server", "both"].includes(selectedMode)) {
          setDraftMode(selectedMode);
          modeDraftInitializedRef.current = true;
        }
        if (viewRef.current === "loading") {
          if (!value?.setup_complete) navigate("setup");
          else if (roleHasClient(value?.mode?.effective ?? value?.device_mode)) navigate(client.remote ? "remote" : "remote-setup");
          else navigate("this-device");
        }
        if (client.pending_pairing && ["sending", "pending", "waiting"].includes(client.pending_pairing.status) && viewRef.current === "remote-setup") {
          setPendingPairing(client.pending_pairing);
          navigate("pairing");
        }
      } catch (error) {
        setMessage(`SteamOS Companion couldn't load: ${boundedString(error)}`);
      }
    }

    function updateRemote(remoteValue) {
      if (!remoteValue) return;
      setSettings(previous => {
        if (!previous) return previous;
        const oldRemote = previous.client?.remote || {};
        return {
          ...previous,
          client: {...previous.client, remote: {...oldRemote, ...remoteValue}},
        };
      });
    }

    function displayOrderKeys(value) {
      const order = value?.display_order || {};
      const outputs = Array.isArray(order.outputs) ? order.outputs : [];
      const byKey = new Map(outputs.filter(item => item && item.output_key).map(item => [item.output_key, item]));
      const inventoryKeys = Array.isArray(order.output_keys) ? order.output_keys : outputs.map(item => item.output_key);
      const connected = inventoryKeys.map(key => byKey.get(key)).filter(item => item?.connected === true).map(item => item.output_key);
      const saved = Array.isArray(order.saved_output_keys) ? order.saved_output_keys.filter(key => byKey.get(key)?.connected === true) : [];
      const uniqueSaved = [...new Set(saved)];
      return [...uniqueSaved, ...connected.filter(key => !uniqueSaved.includes(key))];
    }

    async function run(method, args = {}, success = "") {
      if (busy) return null;
      setBusy(method);
      setMessage("");
      try {
        const value = await callBackend(method, args);
        if (success) setMessage(success);
        await loadSettings();
        if (method.startsWith("local_display") || method.startsWith("local_gamescope") || method.startsWith("local_preferred")) {
          await loadLocalDisplay();
        }
        if (method.startsWith("local_display_order")) {
          await loadLocalDisplayOrder();
        }
        return value;
      } catch (error) {
        setMessage(boundedString(error));
        return null;
      } finally {
        setBusy("");
      }
    }

    async function loadLocalDisplay() {
      if (!roleHasServer(mode)) return null;
      try {
        const value = await callBackend("local_display_outputs");
        setSettings(previous => previous ? {...previous, local_display: value} : previous);
        if (!localOutputOrderTouchedRef.current) {
          const connected = (value?.outputs || []).filter(output => output.connected === true && output.output_key && output.connector);
          const byConnector = new Map(connected.map(output => [output.connector, output.output_key]));
          const configured = Array.isArray(value?.monitor_switch?.configured_connectors) ? value.monitor_switch.configured_connectors : [];
          const configuredKeys = configured.map(connector => byConnector.get(connector)).filter(Boolean);
          const remainingKeys = connected.map(output => output.output_key).filter(key => !configuredKeys.includes(key));
          setLocalOutputOrder([...configuredKeys, ...remainingKeys]);
        }
        return value;
      } catch (error) {
        setMessage(boundedString(error));
        return null;
      }
    }

    async function loadLocalDisplayOrder() {
      const currentMode = settings?.mode?.effective ?? settings?.device_mode;
      if (!roleHasClient(currentMode) && !roleHasServer(currentMode)) return null;
      try {
        const value = await callBackend("local_display_order");
        setSettings(previous => previous ? {...previous, local_display_order: value} : previous);
        if (!localDisplayOrderTouchedRef.current) setLocalOutputOrder(displayOrderKeys(value));
        return value;
      } catch (error) {
        setMessage(boundedString(error));
        return null;
      }
    }

    async function loadRemoteDisplayOrder() {
      try {
        const value = await callBackend("remote_display_order");
        updateRemote({display_order: value?.display_order || null});
        if (!remoteOutputOrderTouchedRef.current) setRemoteOutputOrder(displayOrderKeys(value));
        return value;
      } catch (error) {
        setMessage(boundedString(error));
        return null;
      }
    }

    async function ensureAvailable(action, fields = {}) {
      try {
        const value = await callBackend("remote_action_availability", {action, ...fields});
        if (!value?.available) {
          setMessage(value?.reason || "That action is unavailable right now.");
          return false;
        }
        return true;
      } catch (error) {
        setMessage(boundedString(error));
        return false;
      }
    }

    React.useEffect(() => {
      void loadSettings();
      const refreshTimer = setInterval(() => { void loadSettings(); }, 2000);
      const clockTimer = setInterval(() => setNow(Date.now()), 1000);
      return () => {
        clearInterval(refreshTimer);
        clearInterval(clockTimer);
      };
    }, []);

    const mode = settings?.mode?.effective ?? settings?.device_mode;
    const client = settings?.client || {};
    const remote = client.remote;
    const remoteVisible = roleHasClient(mode) && Boolean(remote) && ["remote", "display", "remote-display-order", "power", "details"].includes(view);
    const localVisible = roleHasServer(mode) && view === "local-display";
    const localDisplayOrderVisible = (roleHasClient(mode) || roleHasServer(mode)) && view === "local-display-order";

    React.useEffect(() => {
      if (localVisible) {
        localOutputOrderTouchedRef.current = false;
        void loadLocalDisplay();
      }
    }, [localVisible]);

    React.useEffect(() => {
      if (!localDisplayOrderVisible) return undefined;
      localDisplayOrderTouchedRef.current = false;
      void loadLocalDisplayOrder();
      const timer = setInterval(() => { void loadLocalDisplayOrder(); }, 2000);
      return () => clearInterval(timer);
    }, [localDisplayOrderVisible]);

    React.useEffect(() => {
      if (view !== "remote-display-order") return;
      remoteOutputOrderTouchedRef.current = false;
      setRemoteOutputOrder([]);
    }, [view, remote?.host_id, remote?.endpoint]);

    React.useEffect(() => {
      if (!remoteVisible) return undefined;
      let disposed = false;
      let timer = null;
      const cycle = async () => {
        if (disposed) return;
        let connected = false;
        let retryAfter = null;
        let statusValue = null;
        try {
          const value = await callBackend("remote_status");
          statusValue = value;
          if (!disposed) {
            updateRemote(value?.remote);
            connected = value?.connection === "connected";
          }
          if (!disposed && connected) {
            // Outputs are a read-only companion to a successful status read;
            // merging the response preserves the latest status and selection
            // while the preview countdown remains outside inventory identity.
            const outputs = await callBackend("remote_outputs").catch(() => null);
            if (!disposed && outputs) updateRemote({outputs: outputs.outputs || [], profiles: outputs.profiles || [], preview: outputs.preview || null});
            const displayOrder = await callBackend("remote_display_order").catch(() => null);
            if (!disposed && displayOrder) {
              updateRemote({display_order: displayOrder.display_order || null});
              if (!remoteOutputOrderTouchedRef.current) setRemoteOutputOrder(displayOrderKeys(displayOrder));
            }
            const action = value?.last_action;
            if (!disposed && action?.id && ["accepted", "dispatched", "unknown"].includes(action.state)) {
              await callBackend("check_remote_operation", {action_id: action.id}).catch(() => null);
              if (!disposed) await loadSettings();
            }
          }
        } catch (_) {
          connected = false;
          const settingsValue = await callBackend("get_settings").catch(() => null);
          if (!disposed) {
            updateRemote(settingsValue?.client?.remote);
            const retryValue = settingsValue?.client?.remote?.retry_after;
            retryAfter = retryValue == null ? null : Number(retryValue);
          }
        }
        const statusRetryValue = statusValue?.remote?.retry_after;
        if (connected && statusRetryValue != null) retryAfter = Number(statusRetryValue);
        const delayMs = Number.isFinite(retryAfter) && retryAfter >= 0 ? Math.min(3600000, retryAfter * 1000) : (connected ? 2000 : 6000);
        if (!disposed) timer = setTimeout(cycle, delayMs);
      };
      // Resume is a fresh read; it never replays a saved mutation.
      timer = setTimeout(cycle, 0);
      return () => { disposed = true; if (timer) clearTimeout(timer); };
    }, [remoteVisible, remote?.host_id, remote?.endpoint, view]);

    React.useEffect(() => {
      if (!scan || scan.state !== "searching") return undefined;
      let disposed = false;
      let timer = null;
      const poll = async () => {
        if (disposed) return;
        try {
          const value = await callBackend("poll_discovery", {scan_id: scan.scan_id});
          if (disposed || scanRef.current !== scan.scan_id) return;
          if (value.state === "searching") timer = setTimeout(poll, 250);
          else {
            setCandidates(Array.isArray(value.results) ? value.results : []);
            setScan(value);
          }
        } catch (error) {
          if (!disposed) setScan({scan_id: scan.scan_id, state: "failed", results: [], error: boundedString(error)});
        }
      };
      timer = setTimeout(poll, 250);
      return () => { disposed = true; if (timer) clearTimeout(timer); };
    }, [scan?.scan_id, scan?.state]);

    React.useEffect(() => {
      if (view !== "pairing" || !pendingPairing || !["sending", "pending", "waiting"].includes(pendingPairing.status)) return undefined;
      let disposed = false;
      let timer = null;
      const poll = async () => {
        if (disposed || pairingRef.current !== pendingPairing.id) return;
        const value = await callBackend("poll_remote_pairing", {pending_id: pendingPairing.id}).catch(error => ({...pendingPairing, status: "waiting", last_error: boundedString(error)}));
        if (disposed || pairingRef.current !== pendingPairing.id) return;
        if (value?.state === "approved") {
          if (value.needs_confirmation) {
            setMessage(`Use ${value.remote?.name || value.remote?.endpoint || "the new device"} instead of the saved remote device?`);
            setSettings(previous => previous ? ({...previous, client: {...previous.client, staged_remote: value.remote}}) : previous);
            navigate("replace");
          } else {
            navigate("remote");
          }
          await loadSettings();
        } else {
          setPendingPairing(value);
          const notice = value?.last_error || "Waiting for approval…";
          if (pairingNoticeRef.current !== notice) {
            pairingNoticeRef.current = notice;
            setMessage(notice);
          }
          if (!disposed && ["sending", "pending", "waiting"].includes(value?.status)) timer = setTimeout(poll, 1000);
        }
      };
      timer = setTimeout(poll, 1000);
      return () => { disposed = true; if (timer) clearTimeout(timer); };
    }, [view, pendingPairing?.id]);

    function header() {
      const destinations = [
        ...(roleHasClient(mode) ? [{id: "remote", label: "Remote device"}] : []),
        ...((roleHasClient(mode) || roleHasServer(mode)) ? [{id: "this-device", label: "This device"}] : []),
        ...(settings?.setup_complete ? [{id: "settings", label: "Settings"}] : []),
      ];
      return React.createElement(React.Fragment || "div", null,
        React.createElement("h2", {style: {color: UI_COLORS.text}},
          view === "remote" ? "Remote device"
            : view === "remote-display-order" ? "Remote device · Display order"
              : view === "this-device" ? "This device"
                : view === "local-display-order" ? "This device · Display order"
                  : view === "local-display" ? "This device · Display settings"
                    : view === "display" ? "Remote device · Display settings"
                      : "SteamOS Companion"),
        React.createElement("nav", {"aria-label": "Destinations", style: {display: "flex", gap: "6px", flexWrap: "wrap", color: UI_COLORS.text}},
          destinations.map(item => React.createElement(Button, {key: item.id, label: item.label, focusKey: `destination.${item.id}`, onClick: () => navigate(item.id)}))
        )
      );
    }

    function renderSetup() {
      const canName = draftMode === "client" || draftMode === "both";
      return React.createElement(PanelSection, {title: "Choose device mode"},
        React.createElement(PanelSectionRow, {focusKey: "setup.title"}, React.createElement(Text, null, "How will you use this device?")),
        React.createElement(PanelSectionRow, {focusKey: "setup.client"}, React.createElement(SelectableRow, {selected: draftMode === "client", title: "Client", description: "Control another device; arrange this handheld's Gaming Mode screen locally. Recommended", onClick: () => setDraftMode("client"), focusKey: "setup.client", autoFocus: true})),
        React.createElement(PanelSectionRow, {focusKey: "setup.server"}, React.createElement(SelectableRow, {selected: draftMode === "server", title: "Server", description: "Allow paired devices to control this device.", onClick: () => setDraftMode("server"), focusKey: "setup.server"})),
        React.createElement(PanelSectionRow, {focusKey: "setup.both"}, React.createElement(SelectableRow, {selected: draftMode === "both", title: "Both", description: "Control another device, arrange this screen locally, and allow control of this device.", onClick: () => setDraftMode("both"), focusKey: "setup.both"})),
        canName && React.createElement(PanelSectionRow, {focusKey: "setup.client-name"}, React.createElement(Field, {label: "Name shown when pairing", value: draftName, onChange: event => setDraftName(event.target.value), placeholder: initialName, focusKey: "setup.client-name"})),
        React.createElement(PanelSectionRow, {focusKey: "setup.save"}, React.createElement(Button, {label: "Save mode", onClick: async () => {
          const value = await run("update_settings", {changes: {device_mode: draftMode, client_name: draftName}}, "Changing mode…");
          if (value) navigate(draftMode === "server" ? "this-device" : "remote-setup");
        }, disabled: Boolean(busy), focusKey: "setup.save"})),
        React.createElement(PanelSectionRow, {focusKey: "setup.back"}, React.createElement(Button, {label: "Back", onClick: () => {}, focusKey: "setup.back"}))
      );
    }

    async function startScan() {
      const value = await run("begin_discovery", {port: 18443}, "Looking for SteamOS Companion devices…");
      if (value?.scan_id) {
        scanRef.current = value.scan_id;
        setScan(value);
        setCandidates([]);
      }
    }

    function renderRemoteSetup() {
      const pending = client.pending_pairing || pendingPairing;
      const resumable = pending && ["sending", "pending", "waiting"].includes(pending.status);
      return React.createElement(PanelSection, {title: "Connect a remote device"},
        React.createElement(PanelSectionRow, {focusKey: "remote-setup.explanation"}, React.createElement(Text, null, "On the other device, install SteamOS Companion and enable Server or Both. Connect both devices to the same local network.")),
        resumable && React.createElement(PanelSectionRow, {focusKey: "remote-setup.resume"}, React.createElement(Button, {label: "Resume pairing", onClick: () => {setPendingPairing(pending); pairingRef.current = pending.id; navigate("pairing");}, focusKey: "remote-setup.resume"})),
        scan?.state === "searching"
          ? React.createElement(PanelSectionRow, {focusKey: "discovery.cancel"}, React.createElement(Text, {live: true}, "Looking for SteamOS Companion devices…"), React.createElement(Button, {label: "Cancel", onClick: async () => {await run("cancel_discovery", {scan_id: scan.scan_id}); scanRef.current = null; setScan({...scan, state: "cancelled"});}, focusKey: "discovery.cancel"}))
          : React.createElement(PanelSectionRow, {focusKey: "discovery.find"}, React.createElement(Button, {label: "Find devices", onClick: () => void startScan(), disabled: Boolean(busy), focusKey: "discovery.find"})),
        scan?.state === "failed" && React.createElement(PanelSectionRow, {focusKey: "discovery.error"}, React.createElement(Text, {live: true}, scan.error || "The scan failed. Try again.")),
        scan?.state === "complete" && !candidates.length && React.createElement(PanelSectionRow, {focusKey: "discovery.empty"}, React.createElement(Text, null, "No devices found. Check that Server is enabled on the other device.")),
        candidates.length > 0 && React.createElement(PanelSection, {title: "Devices found"}, candidates.map((item, index) => React.createElement(PanelSectionRow, {key: `${item.host_id}.${item.endpoint}`, focusKey: `candidate.${item.host_id}`}, React.createElement(SelectableRow, {selected: candidate?.host_id === item.host_id, title: item.name || item.endpoint, description: `Identity …${String(item.certificate_fingerprint || "").slice(-8)}`, onClick: () => {setCandidate(item); navigate("candidate");}, focusKey: `candidate.${item.host_id}`})))),
        React.createElement(PanelSectionRow, {focusKey: "discovery.manual"}, React.createElement(Button, {label: "Enter address", onClick: () => navigate("manual"), focusKey: "discovery.manual"})),
        React.createElement(PanelSectionRow, {focusKey: "remote-setup.back"}, React.createElement(Button, {label: "Back", onClick: () => navigate(roleHasServer(mode) ? "this-device" : "settings"), focusKey: "remote-setup.back"}))
      );
    }

    function renderManual() {
      return React.createElement(PanelSection, {title: "Enter address"},
        React.createElement(PanelSectionRow, {focusKey: "manual.host"}, React.createElement(Field, {label: "Host or IP address", value: manualHost, onChange: event => {setManualHost(event.target.value); setManualError("");}, placeholder: "192.168.1.42", error: manualError, focusKey: "manual.host"})),
        React.createElement(PanelSectionRow, {focusKey: "manual.port"}, React.createElement(Field, {label: "Port", type: "number", value: manualPort, onChange: event => {setManualPort(event.target.value); setManualError("");}, focusKey: "manual.port"})),
        React.createElement(PanelSectionRow, {focusKey: "manual.check"}, React.createElement(Button, {label: "Check device", disabled: Boolean(busy), onClick: async () => {
          const port = Number(manualPort);
          if (!manualHost || !Number.isInteger(port) || port < 1 || port > 65535) {setManualError("Enter a valid host and port."); return;}
          const value = await run("check_remote_device", {host: manualHost, port}, "");
          if (value) {setCandidate(value); navigate("candidate");}
          else setManualError(`No SteamOS Companion server responded at https://${manualHost}:${port}`);
        }, focusKey: "manual.check"})),
        React.createElement(PanelSectionRow, {focusKey: "manual.back"}, React.createElement(Button, {label: "Back", onClick: () => navigate("remote-setup"), focusKey: "manual.back"}))
      );
    }

    function renderCandidate() {
      if (!candidate) return renderRemoteSetup();
      return React.createElement(PanelSection, {title: "Remote device"},
        React.createElement(PanelSectionRow, {focusKey: "candidate.identity"}, React.createElement(Text, null, candidate.name || candidate.endpoint), React.createElement(Text, {muted: true}, `${candidate.endpoint} · Identity …${String(candidate.certificate_fingerprint).slice(-8)}`)),
        React.createElement(PanelSectionRow, {focusKey: "candidate.permissions"}, React.createElement(Text, null, "Requested access: Read status, Control power, Change display settings.")),
        React.createElement(PanelSectionRow, {focusKey: "candidate.request"}, React.createElement(Button, {label: "Request pairing", disabled: Boolean(busy), onClick: async () => {
          const value = await run("request_remote_pairing", {candidate, requested_scopes: ["status.read", "power.control", "display.control"], replace_existing: Boolean(remote)}, "Waiting for approval…");
          if (value) {setPendingPairing(value); pairingRef.current = value.id; navigate("pairing");}
        }, focusKey: "candidate.request"})),
        React.createElement(PanelSectionRow, {focusKey: "candidate.back"}, React.createElement(Button, {label: "Back to devices", onClick: () => navigate("remote-setup"), focusKey: "candidate.back"}))
      );
    }

    function remaining(expiresAt) {
      const value = Number(expiresAt);
      return Number.isFinite(value) ? Math.max(0, Math.ceil(value - now / 1000)) : null;
    }

    function renderPairing() {
      const item = pendingPairing || client.pending_pairing;
      if (!item) return renderRemoteSetup();
      const seconds = remaining(item.expires_at);
      const code = item.comparison_code || "--------";
      const expired = item.status === "expired" || (seconds !== null && seconds <= 0);
      const rejected = item.status === "rejected";
      const cancelled = item.status === "cancelled";
      const terminal = expired || rejected || cancelled;
      const statusText = rejected
        ? "Pairing was declined on the other device."
        : cancelled
          ? "Pairing request was cancelled."
          : expired
            ? "Expired"
            : `Expires in ${seconds === null ? "unknown" : `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`}`;
      return React.createElement(PanelSection, {title: "Pairing"},
        React.createElement(PanelSectionRow, {focusKey: "pairing.endpoint"}, React.createElement(Text, null, item.name || item.endpoint), React.createElement(Text, {muted: true}, item.endpoint)),
        React.createElement(PanelSectionRow, {focusKey: "pairing.code"}, React.createElement(Text, null, "Compare the code"), React.createElement("div", {role: "status", "aria-label": `Pairing comparison code ${code}`, style: {fontSize: "36px", fontFamily: "monospace", fontWeight: "bold", letterSpacing: "4px", marginTop: "8px", color: UI_COLORS.accent}}, code.replace(/^(....)(....)$/, "$1 $2")), React.createElement(Text, null, statusText)),
        React.createElement(PanelSectionRow, {focusKey: "pairing.instructions"}, React.createElement(Text, null, "On the other device, open SteamOS Companion → This device. Approve only if both codes match."), React.createElement(Text, {live: true}, item.last_error || "Waiting for approval…")),
        React.createElement(PanelSectionRow, {focusKey: "pairing.cancel"}, React.createElement(Button, {label: terminal ? "Back" : "Cancel request", onClick: async () => {
          if (terminal) {
            pairingRef.current = null;
            setPendingPairing(null);
            navigate("remote-setup");
            return;
          }
          const value = await run("cancel_remote_pairing", {pending_id: item.id});
          if (value?.cancelled) {
            pairingRef.current = null;
            setPendingPairing(null);
            navigate("remote-setup");
          }
        }, focusKey: "pairing.cancel"})),
        React.createElement(PanelSectionRow, {focusKey: "pairing.back"}, React.createElement(Button, {label: "Back", onClick: () => navigate("remote-setup"), focusKey: "pairing.back"}))
      );
    }

    function identityName() { return remote?.alias || remote?.name || remote?.endpoint || "remote device"; }
    function modeLabel(value) {
      if (!value) return "Unknown mode";
      const refresh = value.refresh_hz == null ? "Refresh rate unknown" : `${value.refresh_hz} Hz`;
      return `${value.width} × ${value.height} · ${refresh}`;
    }
    function ageLabel(timestamp) {
      const value = Date.parse(timestamp || "");
      if (!Number.isFinite(value)) return "not checked yet";
      const seconds = Math.max(0, Math.floor((now - value) / 1000));
      return seconds <= 1 ? "just now" : `${seconds} seconds ago`;
    }

    function renderActionSummary() {
      const action = client.last_action;
      if (!action) return null;
      const label = action.state === "unknown" ? "Result not confirmed" : action.state === "succeeded" ? (action.outcome || "Action completed") : action.state === "failed" ? `Action failed: ${action.reason || "unknown reason"}` : action.state === "accepted" || action.state === "dispatched" ? `${action.action || "Action"} requested` : "Sending request…";
      return React.createElement(PanelSectionRow, {focusKey: `action.${action.id}`}, React.createElement(Text, {live: true}, label), action.state === "unknown" && React.createElement(Button, {label: "Check status", onClick: () => void run("check_remote_operation", {action_id: action.id}), focusKey: `action.${action.id}.check`}), action.state === "unknown" && action.retry_allowed === true && React.createElement(Button, {label: "Send again — the earlier request may already have run", onClick: () => setModal({kind: "resend", action}), focusKey: `action.${action.id}.resend`}));
    }

    function renderOverview() {
      const status = remote?.status || {};
      const remoteSunshineEnabled = status.sunshine?.enabled === true;
      const connection = remote?.connection?.reachable === false ? "Can't reach" : status.protocol_version ? "Connected" : "Checking device…";
      const stale = remote?.last_checked_at && Date.now() - Date.parse(remote.last_checked_at) > 15000;
      const profiles = remote?.profiles || [];
      const profile = profiles.find(item => item.id === selectedProfileId) || profiles[0];
      const wake = client.wake?.remote_host_id === remote?.host_id ? client.wake : null;
      const wakeSent = wake?.state === "sent";
      const wakeFailed = wake?.state === "failed";
      const wakeSentAt = wakeSent && wake.sent_at ? Date.parse(wake.sent_at) : NaN;
      const checkedAt = remote?.last_checked_at ? Date.parse(remote.last_checked_at) : NaN;
      const wakeResponseObserved = Number.isFinite(wakeSentAt) && Number.isFinite(checkedAt) && checkedAt >= wakeSentAt && remote?.connection?.reachable === true;
      const wakeTimedOut = Number.isFinite(wakeSentAt) && now - wakeSentAt >= 30000;
      return React.createElement(PanelSection, {title: "Remote device"},
        React.createElement(PanelSectionRow, {focusKey: "remote.identity"}, React.createElement(Text, null, identityName()), React.createElement(Text, {muted: true}, remote?.endpoint), React.createElement(Text, {live: true}, stale ? `Status is out of date · Last checked ${ageLabel(remote.last_checked_at)}` : `${connection} · Checked ${ageLabel(remote?.last_checked_at)}`)),
        renderActionSummary(),
        React.createElement(PanelSectionRow, {focusKey: "remote.restore"}, profile ? React.createElement(Text, null, `Saved on ${profile.output_id}: ${modeLabel(profile.mode)}`) : React.createElement(Text, null, "No recovery mode saved"), React.createElement(Button, {label: "Restore saved display", disabled: !profile || Boolean(busy), onClick: async () => {if (await ensureAvailable("restore")) void run("remote_restore", {source: "verified", profile_id: profile?.id}, "Restore requested");}, focusKey: "remote.restore"}), profiles.length > 1 && React.createElement(Button, {label: "Choose saved display", onClick: () => navigate("display"), focusKey: "remote.restore.choose"})),
        React.createElement(PanelSectionRow, {focusKey: "remote.wake"},
          React.createElement(Button, {
            label: wakeSent && !wakeTimedOut && !wakeResponseObserved ? "Waiting for connection…" : wake ? "Send another wake packet" : "Wake device",
            disabled: Boolean(busy) || remote?.wake_target?.available !== true || Boolean(wakeSent && !wakeTimedOut && !wakeResponseObserved),
            onClick: () => void run("wake_remote", {}, "Wake packet sent · Waiting for connection…"),
            focusKey: "remote.wake",
          }),
          remote?.wake_target?.available !== true && React.createElement(Text, {muted: true}, "Wake is not configured for this device"),
          wakeFailed && React.createElement(Text, {live: true}, "Couldn't send the wake packet"),
          wakeSent && React.createElement(Text, {live: true}, "Wake packet sent"),
          wakeSent && React.createElement(Text, {live: true}, wakeResponseObserved ? "Authenticated device response received" : wakeTimedOut ? "No response yet. Wake depends on the device and network." : "Waiting for an authenticated device response…"),
          wakeSent && React.createElement(Text, {live: true}, wakeResponseObserved && status.steam_bridge === "ready" ? "Steam ready" : "Steam readiness not confirmed")),
        React.createElement(PanelSectionRow, {focusKey: "remote.display"}, React.createElement(Button, {label: "Display settings (resolution)  >", onClick: () => navigate("display"), focusKey: "remote.display"})),
        React.createElement(PanelSectionRow, {focusKey: "remote.display-order"}, React.createElement(Button, {label: "Gaming Mode display order  >", onClick: () => navigate("remote-display-order"), focusKey: "remote.display-order"})),
        React.createElement(PanelSectionRow, {focusKey: "remote.power"}, React.createElement(Button, {label: "Power options  >", onClick: () => navigate("power"), focusKey: "remote.power"})),
        remoteSunshineEnabled && React.createElement(PanelSectionRow, {focusKey: "remote.sunshine"}, React.createElement(Text, null, `Sunshine ${status.sunshine.state || "unknown"}`), status.sunshine.state === "stopped" && React.createElement(Button, {label: "Recover Sunshine", onClick: async () => {if (await ensureAvailable("sunshine_restart")) void run("remote_sunshine_restart", {}, "Sunshine recovery requested");}, focusKey: "remote.sunshine.recover"})),
        React.createElement(PanelSectionRow, {focusKey: "remote.details"}, React.createElement(Button, {label: "Connection details  >", onClick: () => navigate("details"), focusKey: "remote.details"}))
      );
    }

    function renderPower() {
      const name = identityName();
      const actions = [
        ["suspend", "Suspend", `The device will sleep and disconnect. Remote wake may not be available.`, "Suspend device"],
        ["restart", "Restart", `Running games and applications may close. The connection will be interrupted.`, "Restart device"],
        ["shutdown", "Shut down", `Running games and applications may close. You may need to turn the device on physically.`, "Shut down device"],
      ];
      if (modal?.kind === "power") {
        const item = actions.find(value => value[0] === modal.action) || actions[0];
        return React.createElement(PanelSection, {title: "Power options"}, React.createElement(ConfirmModal, {title: `${item[1]} ${name}?`, body: `${item[2]} Target: ${name} at ${remote?.endpoint}.`, confirmLabel: item[3], danger: item[0] === "shutdown", busy: Boolean(busy), onCancel: () => setModal(null), onConfirm: async () => {if (!(await ensureAvailable(item[0]))) return; setModal(null); await run("remote_power", {action: item[0]}, `${item[1]} requested`); navigate("remote");}}));
      }
      const capability = remote?.status?.capabilities || {};
      return React.createElement(PanelSection, {title: "Power options"},
        React.createElement(PanelSectionRow, {focusKey: "power.explanation"}, React.createElement(Text, null, `Choose an action for ${name} at ${remote?.endpoint}.`)),
        actions.map(item => {
          const available = capability[item[0]] === "available";
          const reason = available ? item[2] : capability[item[0]] === "unavailable" ? `${item[1]} is unavailable on the remote device.` : `Checking whether ${item[1].toLowerCase()} is available…`;
          return React.createElement(PanelSectionRow, {key: item[0], focusKey: `power.${item[0]}`}, React.createElement(Text, null, item[1]), React.createElement(Text, {muted: true}, reason), React.createElement(Button, {label: item[1], disabled: Boolean(busy) || !available, onClick: () => setModal({kind: "power", action: item[0]}), focusKey: `power.${item[0]}.open` }));
        }),
        React.createElement(PanelSectionRow, {focusKey: "power.back"}, React.createElement(Button, {label: "Back", onClick: () => navigate("remote"), focusKey: "power.back"}))
      );
    }

    function displayOutput() {
      const outputs = remote?.outputs || [];
      return outputs.find(item => item.id === selectedOutputId) || outputs[0] || null;
    }

    function renderPreviewCard(preview) {
      const seconds = remaining(preview?.deadline);
      const expired = seconds !== null && seconds <= 0;
      return React.createElement(PanelSectionRow, {focusKey: `preview.${preview?.preview_id || "active"}`},
        React.createElement(Text, null, `Display preview · ${identityName()}`),
        React.createElement(Text, null, `Can you see the picture on ${identityName()}'s display?`),
        React.createElement(Text, {live: true}, expired ? "Preview time ended · Checking restoration…" : `Reverts in ${seconds === null ? "unknown" : seconds + " seconds"}`),
        React.createElement(Button, {label: "Revert now", disabled: Boolean(busy), onClick: async () => {if (await ensureAvailable("restore_preview")) void run("remote_restore", {source: "preview"}, "Checking display restoration…");}, focusKey: "preview.revert"}),
        React.createElement(Button, {label: "Keep and save", disabled: expired || Boolean(busy), onClick: async () => {if (await ensureAvailable("confirm")) void run("remote_confirm_preview", {preview_id: preview.preview_id, visible: true}, "Checking save result…");}, focusKey: "preview.keep"})
      );
    }

    function renderDisplay() {
      const outputs = remote?.outputs || [];
      const output = displayOutput();
      const profiles = remote?.profiles || [];
      const selectedProfile = profiles.find(item => item.id === selectedProfileId) || profiles[0] || null;
      const showNonstandard = client.show_nonstandard_display_modes === true;
      const currentModeId = output?.current_mode_id == null ? "" : String(output.current_mode_id);
      const allModes = output ? [...(output.modes || [])].sort((a, b) => {
        if (currentModeId && String(a.id) === currentModeId) return -1;
        if (currentModeId && String(b.id) === currentModeId) return 1;
        return (b.width * b.height - a.width * a.height) || ((b.refresh_hz || 0) - (a.refresh_hz || 0));
      }) : [];
      const modes = allModes.filter(item => showNonstandard || isStandardDisplayMode(item) || String(item.id) === currentModeId);
      const hiddenModeCount = allModes.length - modes.length;
      const selected = modes.find(item => item.id === selectedModeId) || modes[0];
      const profile = (remote?.profiles || []).find(item => item.output_id === output?.id) || null;
      const preview = remote?.preview;
      const canSaveCurrent = Boolean(output && output.current_mode_id);
      return React.createElement(PanelSection, {title: "Display settings"},
        React.createElement(PanelSectionRow, {focusKey: "display.target"}, React.createElement(Text, null, `Target device: ${identityName()}`), React.createElement(Text, {muted: true}, output?.name || "No output advertised")),
        outputs.length > 1 && React.createElement(PanelSectionRow, {focusKey: "display.output-picker"}, React.createElement(Picker, {label: "Output", value: output?.id || "", options: outputs.map(item => ({value: item.id, label: item.name || item.id})), onChange: event => {setSelectedOutputId(event.target.value); setSelectedModeId("");}, focusKey: "display.output-picker"})),
        React.createElement(PanelSectionRow, {focusKey: "display.mode-filter"}, React.createElement(Text, {muted: true}, showNonstandard ? "Showing all advertised resolutions and refresh rates." : hiddenModeCount ? `${hiddenModeCount} non-standard ${hiddenModeCount === 1 ? "mode" : "modes"} hidden. Enable them in Settings.` : "Showing common resolutions and refresh rates. The current mode is always shown.")),
        React.createElement(PanelSectionRow, {focusKey: "display.current"}, React.createElement(Text, null, `Current mode: ${modeLabel(output && modes.find(item => item.id === output.current_mode_id))}`), profile ? React.createElement(Text, {muted: true}, `Saved recovery mode: ${modeLabel(profile.mode)} · verified ${profile.verified_at || "time unavailable"}`) : React.createElement(Text, {muted: true}, "No recovery mode saved")),
        profiles.length > 0 && React.createElement(PanelSectionRow, {focusKey: "display.saved-profiles"}, React.createElement(Text, null, "Saved recovery displays"), profiles.map(item => React.createElement(SelectableRow, {key: item.id, selected: selectedProfile?.id === item.id, title: `${item.output_id} · ${modeLabel(item.mode)}`, description: "Select a verified recovery mode", onClick: () => setSelectedProfileId(item.id), focusKey: `display.saved.${item.id}`})), React.createElement(Button, {label: "Restore saved display", disabled: Boolean(busy) || !selectedProfile, onClick: async () => {if (await ensureAvailable("restore")) void run("remote_restore", {source: "verified", profile_id: selectedProfile.id}, "Restore requested");}, focusKey: "display.restore"})),
        !profile && canSaveCurrent && React.createElement(PanelSectionRow, {focusKey: "display.save-current.prominent"}, React.createElement(Text, null, "No recovery mode saved. Check the picture, then save the current display as recovery mode."), React.createElement(Button, {label: "Save current display as recovery mode", disabled: Boolean(busy) || Boolean(preview), onClick: () => setModal({kind: "save-current", output}), focusKey: "display.save-current.prominent"})),
        !modes.length && React.createElement(PanelSectionRow, {focusKey: "display.empty"}, React.createElement(Text, null, allModes.length ? "No common display modes are available. Enable non-standard modes in Settings." : "No display modes are available from the remote device.")),
        modes.map(item => React.createElement(PanelSectionRow, {key: `${output?.id}.${item.id}`, focusKey: `display.mode.${output?.id}.${item.id}`}, React.createElement(SelectableRow, {selected: selected?.id === item.id, title: modeLabel(item), description: item.id === output?.current_mode_id ? "Current" : profile?.mode?.id === item.id ? "Saved" : "Select this mode; selection does not change the display.", onClick: () => setSelectedModeId(item.id), focusKey: `display.mode.${output?.id}.${item.id}`}))),
        React.createElement(PanelSectionRow, {focusKey: "display.preview"}, React.createElement(Button, {label: selected ? `Preview ${modeLabel(selected)}` : "Preview selected mode", disabled: !selected || selected.id === output?.current_mode_id || Boolean(preview) || Boolean(busy), onClick: async () => {if (!(await ensureAvailable("preview", {output_id: output.id, mode_id: selected.id}))) return; const value = await run("remote_preview", {output_id: output.id, mode_id: selected.id, generation: output.generation}, "Applying preview…"); if (value) await loadSettings();}, focusKey: "display.preview"})),
        preview && renderPreviewCard(preview),
        profile && canSaveCurrent && React.createElement(PanelSectionRow, {focusKey: "display.save-current"}, React.createElement(Text, {muted: true}, "Save the current picture as a new recovery mode."), React.createElement(Button, {label: "Save current display as recovery mode", disabled: Boolean(busy) || Boolean(preview), onClick: () => setModal({kind: "save-current", output}), focusKey: "display.save-current"})),
        !profile && !canSaveCurrent && React.createElement(PanelSectionRow, {focusKey: "display.no-save"}, React.createElement(Text, null, "No current display mode is available to save.")),
        modal?.kind === "save-current" && React.createElement(ConfirmModal, {title: "Save current display as recovery mode?", body: `Is the picture on ${identityName()}'s display correct right now? This is what Restore saved display will return to.`, confirmLabel: "Save as recovery mode", busy: Boolean(busy), onCancel: () => setModal(null), onConfirm: async () => {if (!(await ensureAvailable("save_current"))) return; const selectedModal = modal; setModal(null); await run("remote_save_current", {output_id: selectedModal.output.id, generation: selectedModal.output.generation}, "Recovery mode saved");}}),
        React.createElement(PanelSectionRow, {focusKey: "display.back"}, React.createElement(Button, {label: "Back", onClick: () => navigate("remote"), focusKey: "display.back"}))
      );
    }

    function renderRemoteDisplayOrder() {
      const order = remote?.display_order || {};
      const outputs = Array.isArray(order.outputs) ? order.outputs : [];
      const byKey = new Map(outputs.filter(item => item && item.output_key).map(item => [item.output_key, item]));
      const connected = displayOrderKeys({display_order: order}).map(key => byKey.get(key)).filter(Boolean);
      const savedKeys = Array.isArray(order.saved_output_keys) ? order.saved_output_keys : [];
      const savedConnectedKeys = savedKeys.filter(key => byKey.get(key)?.connected === true);
      const unavailableSaved = savedKeys.filter(key => byKey.get(key)?.connected !== true);
      const orderedKeys = (remoteOutputOrder.length ? remoteOutputOrder : connected.map(item => item.output_key))
        .filter(key => byKey.get(key)?.connected === true);
      const orderedTargets = orderedKeys.map(key => byKey.get(key)).filter(Boolean);
      const orderDirty = orderedKeys.join("|") !== savedConnectedKeys.join("|");
      const usable = order.available === true && Number.isInteger(order.generation) && order.generation >= 0;
      const canSave = usable && orderDirty && orderedKeys.length > 0 && !busy;

      const moveOutput = (index, delta) => {
        const target = index + delta;
        if (target < 0 || target >= orderedKeys.length) return;
        const next = [...orderedKeys];
        [next[index], next[target]] = [next[target], next[index]];
        remoteOutputOrderTouchedRef.current = true;
        setRemoteOutputOrder(next);
      };

      const saveOrder = async restart => {
        const value = await run(
          "remote_display_order_save",
          {output_keys: orderedKeys, generation: order.generation, restart},
          restart ? "Remote output order saved; restarting Gaming Mode" : "Remote output order saved"
        );
        if (value) remoteOutputOrderTouchedRef.current = false;
        return value;
      };

      return React.createElement(PanelSection, {title: "Remote Gaming Mode display order"},
        React.createElement(PanelSectionRow, {focusKey: "remote-order.target"},
          React.createElement(Text, null, `Target device: ${identityName()}`),
          React.createElement(Text, {muted: true}, "Changes the remote device only. This device's screens are under This device.")),
        order.reason && React.createElement(PanelSectionRow, {focusKey: "remote-order.status"}, React.createElement(Text, {live: true}, order.reason)),
        !outputs.length && React.createElement(PanelSectionRow, {focusKey: "remote-order.empty"}, React.createElement(Text, null, "No physical screens were advertised by the remote device.")),
        orderedTargets.map((output, index) => React.createElement(PanelSectionRow, {key: output.output_key, focusKey: `remote-order.${output.output_key}`},
          React.createElement(Text, null, `${index + 1}. ${localOutputName(output)}${output.active === true ? " — Active" : ""}`),
          React.createElement(Text, {muted: true}, localOutputDetails(output)),
          React.createElement(Button, {label: "Move up", disabled: !usable || Boolean(busy) || index === 0, onClick: () => moveOutput(index, -1), focusKey: `remote-order.${output.output_key}.up`}),
          React.createElement(Button, {label: "Move down", disabled: !usable || Boolean(busy) || index === orderedTargets.length - 1, onClick: () => moveOutput(index, 1), focusKey: `remote-order.${output.output_key}.down`})
        )),
        outputs.filter(output => output.connected !== true).map(output => React.createElement(PanelSectionRow, {key: `disconnected.${output.output_key}`, focusKey: `remote-order.disconnected.${output.output_key}`},
          React.createElement(Text, null, `${localOutputName(output)} — Disconnected`),
          React.createElement(Text, {muted: true}, localOutputDetails(output)))),
        unavailableSaved.length > 0 && React.createElement(PanelSectionRow, {focusKey: "remote-order.saved-unavailable"}, React.createElement(Text, {muted: true}, `${unavailableSaved.length} saved remote screen${unavailableSaved.length === 1 ? " is" : "s are"} currently disconnected.`)),
        orderDirty && React.createElement(PanelSectionRow, {focusKey: "remote-order.unsaved"}, React.createElement(Text, {live: true}, "Remote display order has unsaved changes.")),
        React.createElement(PanelSectionRow, {focusKey: "remote-order.save"}, React.createElement(Button, {label: "Save for next session", disabled: !canSave, onClick: () => void saveOrder(false), focusKey: "remote-order.save.button"})),
        React.createElement(PanelSectionRow, {focusKey: "remote-order.save-restart"}, React.createElement(Button, {label: "Save and restart Gaming Mode", disabled: !canSave || order.restart_available !== true, onClick: () => setModal({kind: "remote-display-order-restart"}), focusKey: "remote-order.save-restart.button"})),
        React.createElement(PanelSectionRow, {focusKey: "remote-order.automatic"}, React.createElement(Button, {label: "Use automatic display order", disabled: Boolean(busy) || !remote?.display_order || order.unsupported === true, onClick: async () => {const value = await run("remote_display_order_reset", {}, "Remote automatic display order restored"); if (value) {remoteOutputOrderTouchedRef.current = false; setRemoteOutputOrder([]);}}, focusKey: "remote-order.automatic.button"})),
        renderActionSummary(),
        React.createElement(PanelSectionRow, {focusKey: "remote-order.refresh"}, React.createElement(Button, {label: "Refresh", disabled: Boolean(busy), onClick: () => {remoteOutputOrderTouchedRef.current = false; void loadRemoteDisplayOrder();}, focusKey: "remote-order.refresh"})),
        React.createElement(PanelSectionRow, {focusKey: "remote-order.back"}, React.createElement(Button, {label: "Back", onClick: () => navigate("remote"), focusKey: "remote-order.back"})),
        modal?.kind === "remote-display-order-restart" && React.createElement(ConfirmModal, {
          title: "Save and restart remote Gaming Mode?",
          body: `Running games and the Steam UI on ${identityName()} will close. This device will remain running.`,
          confirmLabel: "Save and restart remote",
          busy: Boolean(busy),
          onCancel: () => setModal(null),
          onConfirm: async () => {setModal(null); await saveOrder(true);},
        })
      );
    }

    function renderDetails() {
      const endpoint = String(remote?.endpoint || "");
      const matched = endpoint.match(/^https:\/\/(\[[^\]]+\]|[^:/]+)(?::([0-9]+))?\/?$/i);
      const host = matched ? matched[1].replace(/^\[|\]$/g, "") : "";
      const port = matched && matched[2] ? Number(matched[2]) : 18443;
      const recentActions = Array.isArray(client.operations) ? [...client.operations].sort((a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || ""))).slice(0, 8) : [];
      return React.createElement(PanelSection, {title: "Connection details"},
        React.createElement(PanelSectionRow, {focusKey: "details.identity"}, React.createElement(Text, null, remote?.alias || remote?.name || remote?.endpoint), React.createElement(Text, {muted: true}, `Host ID: ${remote?.host_id || "unknown"}`), React.createElement(Text, {muted: true}, `Certificate: ${remote?.certificate_fingerprint || "unknown"}`)),
        React.createElement(PanelSectionRow, {focusKey: "details.rename"}, React.createElement(Button, {label: "Rename locally", onClick: () => setModal({kind: "rename"}), focusKey: "details.rename"})),
        React.createElement(PanelSectionRow, {focusKey: "details.find"}, React.createElement(Button, {label: "Find device again", disabled: Boolean(busy), onClick: async () => {const value = await run("check_remote_device", {host, port}); if (value && value.host_id === remote.host_id && value.certificate_fingerprint === remote.certificate_fingerprint) await run("update_remote_endpoint", {candidate: value}, "Address updated"); else if (value) setMessage("Device identity changed · Review and pair again");}, focusKey: "details.find"})),
        React.createElement(PanelSectionRow, {focusKey: "details.edit-address"}, React.createElement(Button, {label: "Edit address", onClick: () => setModal({kind: "edit-address", host, port: String(port)}), focusKey: "details.edit-address"})),
        React.createElement(PanelSectionRow, {focusKey: "details.pair-again"}, React.createElement(Button, {label: "Pair with another device", onClick: () => navigate("remote-setup"), focusKey: "details.pair-again"})),
        React.createElement(PanelSectionRow, {focusKey: "details.diagnostics"}, React.createElement(Button, {label: "Export sanitized diagnostics", disabled: Boolean(busy), onClick: async () => {const report = await run("client_diagnostics"); if (report) setModal({kind: "diagnostics", text: JSON.stringify(report, null, 2)});}, focusKey: "details.diagnostics"})),
        React.createElement(PanelSectionRow, {focusKey: "details.history"}, React.createElement(Text, null, "Recent operations"), recentActions.length ? recentActions.map(item => React.createElement(Text, {key: item.id}, `${item.action || "Action"} · ${item.state || "unknown"}`)) : React.createElement(Text, {muted: true}, "No operations recorded")),
        React.createElement(PanelSectionRow, {focusKey: "details.forget"}, React.createElement(Button, {label: "Remove this pairing", onClick: () => setModal({kind: "remove-choice"}), focusKey: "details.forget", danger: true})),
        modal?.kind === "rename" && React.createElement(ConfirmModal, {title: "Rename locally", body: React.createElement(Field, {label: "Local name", value: remote?.alias || remote?.name || "", onChange: event => setModal({...modal, value: event.target.value}), focusKey: "modal.rename"}), confirmLabel: "Save name", busy: Boolean(busy), onCancel: () => setModal(null), onConfirm: async () => {const value = modal.value || ""; setModal(null); await run("rename_remote", {alias: value}, "Local name saved");}}),
        modal?.kind === "diagnostics" && React.createElement(PanelSection, {title: "Sanitized diagnostics"}, React.createElement(Text, null, "This report contains bounded status and action facts without saved credentials or pairing secrets."), React.createElement("textarea", {readOnly: true, value: modal.text, rows: 12, style: {width: "100%"}, "aria-label": "Sanitized diagnostics"}), React.createElement(Button, {label: "Copy report", onClick: async () => {try {await navigator.clipboard.writeText(modal.text); setMessage("Sanitized diagnostics copied");} catch (_) {setMessage("Select the report text to copy it manually");}}, focusKey: "diagnostics.copy"}), React.createElement(Button, {label: "Close", onClick: () => setModal(null), focusKey: "diagnostics.close"})),
        modal?.kind === "edit-address" && React.createElement(ConfirmModal, {title: `Edit address for ${identityName()}`, body: React.createElement("div", null, React.createElement(Field, {label: "Host or IP address", value: modal.host, onChange: event => setModal({...modal, host: event.target.value}), focusKey: "modal.address.host"}), React.createElement(Field, {label: "Port", type: "number", value: modal.port, onChange: event => setModal({...modal, port: event.target.value}), focusKey: "modal.address.port"})), confirmLabel: "Check and save address", busy: Boolean(busy), onCancel: () => setModal(null), onConfirm: async () => {const nextPort = Number(modal.port); if (!modal.host || !Number.isInteger(nextPort) || nextPort < 1 || nextPort > 65535) {setMessage("Enter a valid host and port."); return;} const value = await run("check_remote_device", {host: modal.host, port: nextPort}); if (!value) return; if (value.host_id !== remote.host_id || value.certificate_fingerprint !== remote.certificate_fingerprint) {setMessage("Device identity changed · Address was not saved"); return;} const saved = await run("update_remote_endpoint", {candidate: value}, "Address updated"); if (saved) setModal(null);}}),
        modal?.kind === "remove-choice" && React.createElement(PanelSection, {title: "Remove this pairing"}, React.createElement(Text, null, "Choose whether to remove access on the host or forget only on this device."), React.createElement(Button, {label: "Cancel", onClick: () => setModal(null), focusKey: "remove.cancel", autoFocus: true}), React.createElement(Button, {label: "Remove access on the host", onClick: () => setModal({kind: "revoke"}), focusKey: "remove.revoke", danger: true}), React.createElement(Button, {label: "Forget on this device only", onClick: () => setModal({kind: "forget"}), focusKey: "remove.forget", danger: true})),
        modal?.kind === "revoke" && React.createElement(ConfirmModal, {title: `Remove access to ${identityName()} on the host?`, body: "The host will revoke this credential before the local pairing is removed. If the host cannot confirm removal, the pairing stays here.", confirmLabel: "Remove access and forget", busy: Boolean(busy), onCancel: () => setModal(null), onConfirm: async () => {const value = await run("forget_remote", {revoke: true}, "Access removed on host and pairing forgotten"); if (value) {navigate("remote-setup"); setModal(null); setMessage("Access removed on host and pairing forgotten");}}}),
        modal?.kind === "forget" && React.createElement(ConfirmModal, {title: `Forget ${identityName()} on this device only?`, body: "The host may still list this client. Remove it there if needed.", confirmLabel: "Forget on this device only", busy: Boolean(busy), onCancel: () => setModal(null), onConfirm: async () => {const value = await run("forget_remote", {revoke: false}, "Pairing forgotten on this device"); if (value) {navigate("remote-setup"); setModal(null); setMessage("Pairing forgotten here; the host may still list this client");}}}),
        React.createElement(PanelSectionRow, {focusKey: "details.back"}, React.createElement(Button, {label: "Back", onClick: () => navigate("remote"), focusKey: "details.back"}))
      );
    }

    function renderThisDevice() {
      const server = settings?.server || settings || {};
      const listener = server.listener || {};
      const paused = !listener.running;
      const pending = server.pending_pairings || [];
      const clients = [...(server.clients || [])].sort((left, right) => {
        const rank = item => /omarchy/i.test(item?.name || "") ? 0 : /steam\s*(deck|os)|deck/i.test(item?.name || "") ? 1 : 2;
        return rank(left) - rank(right) || String(left?.name || "").localeCompare(String(right?.name || ""));
      });
      const clientType = item => /omarchy/i.test(item?.name || "") ? "Omarchy" : /steam\s*(deck|os)|deck/i.test(item?.name || "") ? "Steam Deck" : "Client";
      const clientPermissions = item => (item.scopes || []).map(scope => scope === "status.read" ? "Status" : scope === "power.control" ? "Power" : scope === "display.control" ? "Display" : scope).join(" · ");
      const sunshine = server.sunshine || {};
      const sunshineMonitoring = server.settings?.monitor_sunshine === true;
      const sunshineProviderReady = server.provider?.ready === true;
      const sunshineRecoverable = sunshineMonitoring && sunshineProviderReady && sunshine.state === "stopped" && !sunshine.operation_id;
      const sunshineDescription = !sunshineMonitoring
        ? "Off in Advanced settings."
        : sunshineProviderReady
          ? sunshine.state === "stopped"
            ? "Stopped."
            : sunshine.state === "running"
              ? "Running."
              : sunshine.state === "restarting"
                ? "Recovery in progress."
                : sunshine.reason || "Checking status."
        : server.provider?.reason || sunshine.reason || "Owner bridge unavailable.";
      const localOrder = settings?.local_display_order?.display_order || {};
      const localDisplay = settings?.local_display || {};
      const localCount = Array.isArray(localOrder.outputs)
        ? localOrder.outputs.filter(output => output.connected === true).length
        : Number.isInteger(localDisplay.connected_count) ? localDisplay.connected_count : null;
      const localView = roleHasServer(mode) ? "local-display" : "local-display-order";
      return React.createElement(PanelSection, {title: "This device"},
        React.createElement(PanelSection, {title: "Local device"},
          React.createElement(PanelSectionRow, {focusKey: "this-device.local.explanation"}, React.createElement(Text, null, "Controls on this device only"), React.createElement(Text, {muted: true}, "This display order chooses the Gaming Mode screen on this handheld; it never changes the paired remote device.")),
          React.createElement(PanelSectionRow, {focusKey: "this-device.local.status"}, React.createElement(Text, null, localCount === null ? "Screen inventory not loaded" : `${localCount} connected ${localCount === 1 ? "screen" : "screens"}`), localOrder.reason && React.createElement(Text, {muted: true}, localOrder.reason)),
          React.createElement(PanelSectionRow, {focusKey: "this-device.display"}, React.createElement(Button, {label: "Display order  >", onClick: () => navigate(localView), focusKey: "this-device.display"}))
        ),
        roleHasClient(mode) && !roleHasServer(mode) && React.createElement(PanelSectionRow, {focusKey: "this-device.remote-note"}, React.createElement(Text, {muted: true}, "Remote controls are under Remote device.")),
        React.createElement(PanelSectionRow, {focusKey: "this-device.settings"}, React.createElement(Button, {label: "Advanced settings  >", onClick: () => navigate("settings"), focusKey: "this-device.settings"})),
        roleHasServer(mode) && React.createElement(PanelSection, {title: "Remote access to this device"},
          React.createElement(PanelSectionRow, {focusKey: "this-device.status"}, React.createElement(Text, {live: true}, paused ? "Server paused" : `Accepting connections on ${listener.address || "all interfaces"}:${listener.port || 18443}`)),
          React.createElement(PanelSectionRow, {focusKey: "this-device.listener"}, React.createElement(Button, {label: listener.running ? "Pause accepting connections" : "Accept connections", disabled: Boolean(busy), onClick: () => void run("update_settings", {changes: {listen_enabled: !listener.running}}, listener.running ? "Server paused" : "Server listening"), focusKey: "this-device.listener"}))
        ),
        roleHasServer(mode) && React.createElement(PanelSection, {title: "Sunshine"},
          React.createElement(PanelSectionRow, {focusKey: "this-device.sunshine.status"}, React.createElement(Text, null, `Sunshine ${sunshine.state || "unknown"}`), React.createElement(Text, {muted: true}, sunshineDescription)),
          sunshineRecoverable && React.createElement(PanelSectionRow, {focusKey: "this-device.sunshine.recover"}, React.createElement(Button, {label: "Recover Sunshine", disabled: Boolean(busy), onClick: () => void run("local_sunshine_restart", {}, "Sunshine recovery requested"), focusKey: "this-device.sunshine.recover"}))
        ),
        roleHasServer(mode) && pending.length ? React.createElement(PanelSection, {title: "Pairing requests"}, pending.map(item => React.createElement(PanelSectionRow, {key: item.pairing_id, focusKey: `incoming.${item.pairing_id}`}, React.createElement(Text, null, item.client_name || "Unknown client"), item.verification_code && React.createElement(Text, null, `Code: ${item.verification_code}`), React.createElement(Text, {muted: true}, "Confirm the same code on both devices."), React.createElement(Button, {label: "Reject", disabled: Boolean(busy), onClick: () => void run("reject_pairing", {pairing_id: item.pairing_id}, "Pairing rejected"), focusKey: `incoming.${item.pairing_id}.reject`, autoFocus: true}), React.createElement(Button, {label: "Approve", disabled: Boolean(busy), onClick: () => void run("approve_pairing", {pairing_id: item.pairing_id, scopes: item.requested_scopes}, "Pairing approved"), focusKey: `incoming.${item.pairing_id}.approve`})))) : null,
        roleHasServer(mode) && React.createElement(PanelSection, {title: "Paired clients"}, clients.length ? clients.map(item => React.createElement(PanelSectionRow, {key: item.client_id, focusKey: `client.${item.client_id}`}, React.createElement(Text, null, item.name || "Unnamed client"), React.createElement(Text, {muted: true}, clientType(item) + " · Paired"), React.createElement(Text, {muted: true}, clientPermissions(item) || "No permissions"), React.createElement(Button, {label: "Remove access", disabled: Boolean(busy), onClick: () => setModal({kind: "revoke", client: item}), focusKey: `client.${item.client_id}.remove`, danger: true}))) : React.createElement(PanelSectionRow, {focusKey: "this-device.no-clients"}, React.createElement(Text, null, "No paired clients."))),
        roleHasServer(mode) && modal?.kind === "revoke" && React.createElement(ConfirmModal, {title: `Remove ${modal.client.name || "client"}'s access to this device?`, body: "This removes incoming access only; it does not remove this handheld's outgoing pairing.", confirmLabel: "Remove access", busy: Boolean(busy), danger: true, onCancel: () => setModal(null), onConfirm: async () => {const id = modal.client.client_id; setModal(null); await run("revoke_client", {client_id: id}, "Client access removed");}})
      );
    }

    function localOutputName(output) {
      return output?.display_name || output?.name || output?.description || "Unnamed screen";
    }

    function localOutputConnector(output) {
      return output?.connector ? `Connector ${output.connector}` : "Connector unknown";
    }

    function localOutputDetails(output) {
      const details = [output?.connector || "Connector unknown"];
      if (output?.monitor_vendor) details.push(output.monitor_vendor);
      if (Number.isInteger(output?.monitor_product_id)) details.push(`model ${output.monitor_product_id}`);
      return details.join(" · ");
    }

    function renderLocalPreview(preview) {
      const seconds = remaining(preview?.deadline);
      const expired = seconds !== null && seconds <= 0;
      const applying = preview?.deadline == null || ["accepted", "dispatched"].includes(preview?.state);
      const readyToConfirm = preview?.state === "observed_return" && !expired && !preview?.restore_started;
      const targetName = localOutputName((settings?.local_display?.outputs || []).find(item => item.output_key === preview?.target_output_key));
      return React.createElement(PanelSection, {title: "Screen preview"},
        React.createElement(PanelSectionRow, {focusKey: "local-preview.status"},
          React.createElement(Text, null, applying ? `Applying ${targetName} as the Gaming Mode screen…` : `Can you see the picture on ${targetName}?`),
          React.createElement(Text, {live: true}, applying ? "Waiting for active-screen readback…" : expired ? "Preview time ended · Checking restoration…" : `Reverts in ${seconds === null ? "unknown" : `${seconds} seconds`}`)),
        React.createElement(PanelSectionRow, {focusKey: "local-preview.revert"}, React.createElement(Button, {label: "Revert to previous screen", disabled: Boolean(busy) || Boolean(preview?.restore_started), onClick: () => void run("local_display_revert", {preview_id: preview.preview_id}, "Checking baseline restoration…"), focusKey: "local-preview.revert"})),
        React.createElement(PanelSectionRow, {focusKey: "local-preview.keep"}, React.createElement(Button, {label: "Keep this screen", disabled: Boolean(busy) || !readyToConfirm, onClick: () => void run("local_display_confirm", {preview_id: preview.preview_id}, "Gaming Mode screen confirmed"), focusKey: "local-preview.keep"}))
      );
    }

    function renderLocalDisplayLegacy() {
      const local = settings?.local_display || {};
      const selection = local.selection || {};
      const outputs = Array.isArray(local.outputs) ? local.outputs : [];
      const activeKey = local.active_output_key || selection.active_output_key || null;
      const activeState = local.active_state || selection.active_state || "unknown";
      const selectedOutput = outputs.find(item => item.output_key === selectedLocalOutputKey) || outputs.find(item => item.output_key === activeKey) || outputs[0] || null;
      const multiple = outputs.length > 1;
      const inventoryUsable = local.available === true && local.fresh === true && local.previous_reading !== true;
      const activeKnown = activeState === "known" && Boolean(activeKey);
      const switchAvailable = inventoryUsable && selection.can_switch_live === true && selection.recovery_available === true && activeKnown;
      const canSelect = switchAvailable && selectedOutput && selectedOutput.connected !== false && selectedOutput.can_switch_live === true && selectedOutput.output_key !== activeKey && Number.isInteger(local.generation) && !local.preview;
      const monitorSwitch = local.monitor_switch || {};
      const monitorSwitchAvailable = monitorSwitch.available === true && local.previous_reading !== true;
      const monitorSwitchClearAvailable = monitorSwitchAvailable || Boolean(monitorSwitch.configured_connector);
      const monitorTargets = outputs.filter(output => output.connected === true && output.connector);
      const activeGamescopeOutput = outputs.find(output => output.connector === monitorSwitch.active_connector);
      const configuredGamescopeOutput = outputs.find(output => output.connector === monitorSwitch.configured_connector);
      const preview = local.preview;
      const lastOperation = local.last_operation;
      const connectedCount = Number.isInteger(local.connected_count) ? local.connected_count : outputs.filter(item => item.connected === true).length;
      const inventorySource = local.source === "linux-drm-sysfs"
        ? (monitorSwitch.readback_available ? "Read from Linux DRM connector status; Gamescope readback identifies the current scanout." : "Read from Linux DRM connector status; current Gamescope scanout readback is unavailable.")
        : "Reported by the Steam display bridge.";
      const selectionReason = selection.reason || local.reason || "Live Gaming Mode screen selection is unavailable.";
      const stateDescription = activeState === "known"
        ? "The active Gaming Mode screen is identified."
        : activeState === "ambiguous"
          ? "More than one active-screen signal was found; no screen is selected."
          : "The active Gaming Mode screen is unknown; no screen is guessed from order or current mode.";
      if (modal?.kind === "restart-gamescope") {
        return React.createElement(PanelSection, {title: "Gaming Mode monitor switch"}, React.createElement(ConfirmModal, {
          title: "Restart Gaming Mode now?",
          body: "Running games and the Steam UI will close. The screen may be black for several seconds while Gaming Mode starts again on the saved output. Desktop Mode and the device itself will not restart.",
          confirmLabel: "Restart Gaming Mode",
          busy: Boolean(busy),
          onCancel: () => setModal(null),
          onConfirm: async () => {setModal(null); await run("local_gamescope_restart", {}, "Gaming Mode restart requested");},
        }));
      }
      return React.createElement(PanelSection, {title: "Display settings"},
        React.createElement(PanelSectionRow, {focusKey: "local-display.explanation"}, React.createElement(Text, null, "Choose which attached physical screen receives Gaming Mode on this device."), React.createElement(Text, {muted: true}, "This is separate from remote display resolution settings and does not change Desktop Mode.")),
        React.createElement(PanelSectionRow, {focusKey: "local-display.inventory"}, React.createElement(Text, null, `${connectedCount} connected ${connectedCount === 1 ? "screen" : "screens"} detected`), React.createElement(Text, {muted: true}, inventorySource)),
        local.previous_reading && React.createElement(PanelSectionRow, {focusKey: "local-display.previous"}, React.createElement(Text, {live: true}, "Showing a previous display inventory."), React.createElement(Text, {muted: true}, "Refresh before selecting a screen.")),
        !local.available && !local.previous_reading && React.createElement(PanelSectionRow, {focusKey: "local-display.unavailable"}, React.createElement(Text, {live: true}, local.reason || "Steam display inventory is unavailable.")),
        React.createElement(PanelSectionRow, {focusKey: "local-display.active"}, React.createElement(Text, null, `Active screen: ${activeState === "known" ? localOutputName(outputs.find(item => item.output_key === activeKey)) : activeState}`), React.createElement(Text, {muted: true}, stateDescription)),
        preview && renderLocalPreview(preview),
        outputs.length === 0 && React.createElement(PanelSectionRow, {focusKey: "local-display.empty"}, React.createElement(Text, null, "No attached screens are available from the Steam display bridge.")),
        outputs.map(output => {
          const isActive = activeState === "known" && output.output_key === activeKey;
          const isSelected = selectedOutput?.output_key === output.output_key;
          const title = `${localOutputName(output)}${isActive ? " — Active" : ""}`;
          const description = output.connected === false ? `${localOutputConnector(output)} · Disconnected` : `${localOutputConnector(output)} · ${isActive ? "Active Gaming Mode screen" : isSelected ? "Selected" : "Available"}`;
          return React.createElement(PanelSectionRow, {key: output.output_key, focusKey: `local-display.output.${output.output_key}`}, React.createElement(SelectableRow, {selected: isSelected, title, description, disabled: output.connected === false || !inventoryUsable, onClick: () => {if (output.connected !== false && inventoryUsable) setSelectedLocalOutputKey(output.output_key);}, focusKey: `local-display.output.${output.output_key}`}));
        }),
        !multiple && outputs.length === 1 && React.createElement(PanelSectionRow, {focusKey: "local-display.one-output"}, React.createElement(Text, {muted: true}, "Only one attached screen is advertised, so there is no alternate Gaming Mode screen to choose.")),
        React.createElement(PanelSectionRow, {focusKey: "local-display.capability"}, React.createElement(Text, {muted: true}, selectionReason)),
        React.createElement(PanelSectionRow, {focusKey: "local-display.persistence"}, React.createElement(Text, {muted: true}, "Live screen selection remains unverified. The Gamescope output override below controls the next Gaming Mode session and can be cleared.")),
        React.createElement(PanelSection, {title: "Gaming Mode monitor switch"},
          React.createElement(PanelSectionRow, {focusKey: "local-display.monitor-switch.explanation"}, React.createElement(Text, null, "Set the Gamescope output for the next Gaming Mode session"), React.createElement(Text, {muted: true}, "This configures Gamescope's --prefer-output using the DRM connector. It does not switch the current session; leave and re-enter Gaming Mode to apply it.")),
          monitorSwitch.active_connector && React.createElement(PanelSectionRow, {focusKey: "local-display.monitor-switch.active"}, React.createElement(Text, null, `Current Gamescope scanout: ${activeGamescopeOutput ? localOutputName(activeGamescopeOutput) : monitorSwitch.active_connector}`)),
          monitorSwitch.configured_connector && React.createElement(PanelSectionRow, {focusKey: "local-display.monitor-switch.configured"}, React.createElement(Text, null, `Next Gaming Mode output: ${configuredGamescopeOutput ? localOutputName(configuredGamescopeOutput) : monitorSwitch.configured_connector}`)),
          !monitorSwitchAvailable && React.createElement(PanelSectionRow, {focusKey: "local-display.monitor-switch.unavailable"}, React.createElement(Text, {muted: true}, monitorSwitch.reason || "Gamescope output preference is unavailable.")),
          monitorTargets.map(output => React.createElement(PanelSectionRow, {key: `local-display.monitor-switch.${output.output_key}`, focusKey: `local-display.monitor-switch.${output.output_key}`}, React.createElement(Button, {label: `Use ${localOutputName(output)} next session`, disabled: Boolean(busy) || !monitorSwitchAvailable, onClick: () => void run("local_gamescope_output", {output_key: output.output_key}, `Gamescope output saved: ${localOutputName(output)} · Re-enter Gaming Mode to apply`), focusKey: `local-display.monitor-switch.${output.output_key}.button` }))),
          React.createElement(PanelSectionRow, {focusKey: "local-display.monitor-switch.clear"}, React.createElement(Button, {label: "Clear Gaming Mode output override", disabled: Boolean(busy) || !monitorSwitchClearAvailable, onClick: () => void run("local_clear_gamescope_output", {}, "Gamescope output override cleared · Re-enter Gaming Mode to restore defaults"), focusKey: "local-display.monitor-switch.clear.button"})),
          React.createElement(PanelSectionRow, {focusKey: "local-display.monitor-switch.restart"}, React.createElement(Button, {label: "Restart Gaming Mode now", disabled: Boolean(busy) || monitorSwitch.restart_available !== true, onClick: () => setModal({kind: "restart-gamescope"}), focusKey: "local-display.monitor-switch.restart.button"}), React.createElement(Text, {muted: true}, "Applies the saved output without entering Desktop Mode. Running games and the Steam UI will close."))
        ),
        selection.restart_required && React.createElement(PanelSectionRow, {focusKey: "local-display.restart-required"}, React.createElement(Text, {muted: true}, selection.can_set_startup_preference ? "The adapter reports a restart-required startup preference, but this plugin does not save startup screen preferences yet." : "A restart-required route is reported, but no startup preference is saved by this plugin.")),
        lastOperation && lastOperation.state === "failed" && lastOperation.restore_state === "succeeded" && React.createElement(PanelSectionRow, {focusKey: "local-display.last-restored"}, React.createElement(Text, {live: true}, "The screen preview was reverted to the previous Gaming Mode screen.")),
        lastOperation && lastOperation.state === "failed" && lastOperation.restore_state !== "succeeded" && React.createElement(PanelSectionRow, {focusKey: "local-display.last-failure"}, React.createElement(Text, {live: true}, `Last screen selection failed: ${lastOperation.reason || "unknown reason"}`)),
        lastOperation && lastOperation.state === "unknown" && React.createElement(PanelSectionRow, {focusKey: "local-display.last-unknown"}, React.createElement(Text, {live: true}, `Last screen selection result is unknown: ${lastOperation.reason || "check the physical screen before retrying"}`)),
        React.createElement(PanelSectionRow, {focusKey: "local-display.preview-button"}, React.createElement(Button, {label: selectedOutput ? `Preview ${localOutputName(selectedOutput)}` : "Preview selected screen", disabled: !canSelect || Boolean(busy), onClick: () => void run("local_display_preview", {output_key: selectedOutput.output_key, generation: local.generation}, "Checking screen preview…"), focusKey: "local-display.preview-button"})),
        React.createElement(PanelSectionRow, {focusKey: "local-display.refresh"}, React.createElement(Button, {label: "Refresh inventory", disabled: Boolean(busy), onClick: () => void loadLocalDisplay(), focusKey: "local-display.refresh"})),
        React.createElement(PanelSectionRow, {focusKey: "local-display.back"}, React.createElement(Button, {label: "Back", onClick: () => navigate("this-device"), focusKey: "local-display.back"}))
      );
    }

    function renderLocalDisplayOrder() {
      const order = settings?.local_display_order?.display_order || {};
      const outputs = Array.isArray(order.outputs) ? order.outputs : [];
      const byKey = new Map(outputs.filter(item => item && item.output_key).map(item => [item.output_key, item]));
      const connected = displayOrderKeys({display_order: order}).map(key => byKey.get(key)).filter(Boolean);
      const savedKeys = Array.isArray(order.saved_output_keys) ? order.saved_output_keys : [];
      const savedConnectedKeys = savedKeys.filter(key => byKey.get(key)?.connected === true);
      const unavailableSaved = savedKeys.filter(key => byKey.get(key)?.connected !== true);
      const orderedKeys = (localOutputOrder.length ? localOutputOrder : connected.map(item => item.output_key))
        .filter(key => byKey.get(key)?.connected === true);
      const orderedTargets = orderedKeys.map(key => byKey.get(key)).filter(Boolean);
      const orderDirty = orderedKeys.join("|") !== savedConnectedKeys.join("|");
      const usable = order.available === true && Number.isInteger(order.generation) && order.generation >= 0;
      const canSave = usable && orderDirty && orderedKeys.length > 0 && !busy;

      const moveOutput = (index, delta) => {
        const target = index + delta;
        if (target < 0 || target >= orderedKeys.length) return;
        const next = [...orderedKeys];
        [next[index], next[target]] = [next[target], next[index]];
        localDisplayOrderTouchedRef.current = true;
        setLocalOutputOrder(next);
      };

      const saveOrder = async restart => {
        const value = await run(
          "local_display_order_save",
          {output_keys: orderedKeys, generation: order.generation, restart},
          restart ? "Local output order saved; restarting Gaming Mode" : "Local output order saved"
        );
        if (value) localDisplayOrderTouchedRef.current = false;
        return value;
      };

      return React.createElement(PanelSection, {title: "This device · Gaming Mode display order"},
        React.createElement(PanelSectionRow, {focusKey: "local-order.target"},
          React.createElement(Text, null, "Target device: This device"),
          React.createElement(Text, {muted: true}, "Changes this handheld only. Remote controls are under Remote device.")),
        order.reason && React.createElement(PanelSectionRow, {focusKey: "local-order.status"}, React.createElement(Text, {live: true}, order.reason)),
        !outputs.length && React.createElement(PanelSectionRow, {focusKey: "local-order.empty"}, React.createElement(Text, null, "No physical screens were detected on this device.")),
        orderedTargets.map((output, index) => React.createElement(PanelSectionRow, {key: output.output_key, focusKey: `local-order.${output.output_key}`},
          React.createElement(Text, null, `${index + 1}. ${localOutputName(output)}${output.active === true ? " — Active" : ""}`),
          React.createElement(Text, {muted: true}, localOutputDetails(output)),
          React.createElement(Button, {label: "Move up", disabled: !usable || Boolean(busy) || index === 0, onClick: () => moveOutput(index, -1), focusKey: `local-order.${output.output_key}.up`}),
          React.createElement(Button, {label: "Move down", disabled: !usable || Boolean(busy) || index === orderedTargets.length - 1, onClick: () => moveOutput(index, 1), focusKey: `local-order.${output.output_key}.down`})
        )),
        outputs.filter(output => output.connected !== true).map(output => React.createElement(PanelSectionRow, {key: `disconnected.${output.output_key}`, focusKey: `local-order.disconnected.${output.output_key}`},
          React.createElement(Text, null, `${localOutputName(output)} — Disconnected`),
          React.createElement(Text, {muted: true}, localOutputDetails(output)))),
        unavailableSaved.length > 0 && React.createElement(PanelSectionRow, {focusKey: "local-order.saved-unavailable"}, React.createElement(Text, {muted: true}, `${unavailableSaved.length} saved screen${unavailableSaved.length === 1 ? " is" : "s are"} currently disconnected.`)),
        orderDirty && React.createElement(PanelSectionRow, {focusKey: "local-order.unsaved"}, React.createElement(Text, {live: true}, "Local display order has unsaved changes.")),
        React.createElement(PanelSectionRow, {focusKey: "local-order.save"}, React.createElement(Button, {label: "Save for next session", disabled: !canSave, onClick: () => void saveOrder(false), focusKey: "local-order.save.button"})),
        React.createElement(PanelSectionRow, {focusKey: "local-order.save-restart"}, React.createElement(Button, {label: "Save and restart Gaming Mode", disabled: !canSave || order.restart_available !== true, onClick: () => setModal({kind: "local-display-order-restart"}), focusKey: "local-order.save-restart.button"})),
        React.createElement(PanelSectionRow, {focusKey: "local-order.automatic"}, React.createElement(Button, {label: "Use automatic display order", disabled: Boolean(busy) || !settings?.local_display_order, onClick: async () => {const value = await run("local_display_order_reset", {}, "Local automatic display order restored"); if (value) {localDisplayOrderTouchedRef.current = false; setLocalOutputOrder([]);}}, focusKey: "local-order.automatic.button"})),
        React.createElement(PanelSectionRow, {focusKey: "local-order.refresh"}, React.createElement(Button, {label: "Refresh", disabled: Boolean(busy), onClick: () => {localDisplayOrderTouchedRef.current = false; void loadLocalDisplayOrder();}, focusKey: "local-order.refresh"})),
        React.createElement(PanelSectionRow, {focusKey: "local-order.back"}, React.createElement(Button, {label: "Back", onClick: () => navigate("this-device"), focusKey: "local-order.back"})),
        modal?.kind === "local-display-order-restart" && React.createElement(ConfirmModal, {
          title: "Save and restart Gaming Mode on this device?",
          body: "Running games and the Steam UI on this device will close. The paired remote device will not be affected.",
          confirmLabel: "Save and restart this device",
          busy: Boolean(busy),
          onCancel: () => setModal(null),
          onConfirm: async () => {setModal(null); await saveOrder(true);},
        })
      );
    }

    function renderLocalDisplay() {
      const local = settings?.local_display || {};
      const outputs = Array.isArray(local.outputs) ? local.outputs : [];
      const monitorSwitch = local.monitor_switch || {};
      const connected = outputs.filter(output => output.connected === true && output.output_key && output.connector);
      const byKey = new Map(connected.map(output => [output.output_key, output]));
      const orderedTargets = localOutputOrder.map(key => byKey.get(key)).filter(Boolean);
      for (const output of connected) {
        if (!orderedTargets.some(item => item.output_key === output.output_key)) orderedTargets.push(output);
      }
      const orderedKeys = orderedTargets.map(output => output.output_key);
      const orderedConnectors = orderedTargets.map(output => output.connector);
      const configuredConnectors = Array.isArray(monitorSwitch.configured_connectors) ? monitorSwitch.configured_connectors : [];
      const orderDirty = orderedConnectors.join("|") !== configuredConnectors.join("|");
      const activeOutput = outputs.find(output => output.connector === monitorSwitch.active_connector);
      const inventoryUsable = local.available === true && local.fresh === true && local.previous_reading !== true;
      const canSave = inventoryUsable && monitorSwitch.available === true && orderedKeys.length > 0 && !busy;
      const lastOperation = local.last_operation;

      const moveOutput = (index, delta) => {
        const target = index + delta;
        if (target < 0 || target >= orderedKeys.length) return;
        const next = [...orderedKeys];
        const moved = next[index];
        next[index] = next[target];
        next[target] = moved;
        localOutputOrderTouchedRef.current = true;
        setLocalOutputOrder(next);
      };

      const saveOrder = async restart => {
        const value = await run(
          "local_gamescope_outputs",
          {output_keys: orderedKeys, generation: local.generation, restart},
          restart ? "Output order saved; restarting Gaming Mode" : "Output order saved"
        );
        if (value) localOutputOrderTouchedRef.current = false;
        return value;
      };

      return React.createElement(PanelSection, {title: "Display order"},
        React.createElement(PanelSectionRow, {focusKey: "local-display.summary"},
          React.createElement(Text, null, connected.length + " connected " + (connected.length === 1 ? "screen" : "screens")),
          React.createElement(Text, {muted: true}, "Gaming Mode uses the first available screen in this order.")),
        monitorSwitch.active_connector && React.createElement(PanelSectionRow, {focusKey: "local-display.active"},
          React.createElement(Text, null, "Active: " + (activeOutput ? localOutputName(activeOutput) : monitorSwitch.active_connector))),
        local.previous_reading && React.createElement(PanelSectionRow, {focusKey: "local-display.previous"}, React.createElement(Text, {live: true}, "Refresh before saving.")),
        !local.available && !local.previous_reading && React.createElement(PanelSectionRow, {focusKey: "local-display.unavailable"}, React.createElement(Text, {live: true}, local.reason || "Display list unavailable.")),
        orderedTargets.map((output, index) => {
          const active = output.connector === monitorSwitch.active_connector;
          return React.createElement(PanelSectionRow, {key: output.output_key, focusKey: "local-display.order." + output.output_key},
            React.createElement(Text, null, (index + 1) + ". " + localOutputName(output) + (active ? " — Active" : "")),
            React.createElement(Text, {muted: true}, localOutputDetails(output)),
            React.createElement(Button, {label: "Move up", disabled: Boolean(busy) || index === 0, onClick: () => moveOutput(index, -1), focusKey: "local-display.order." + output.output_key + ".up"}),
            React.createElement(Button, {label: "Move down", disabled: Boolean(busy) || index === orderedTargets.length - 1, onClick: () => moveOutput(index, 1), focusKey: "local-display.order." + output.output_key + ".down"}));
        }),
        orderDirty && React.createElement(PanelSectionRow, {focusKey: "local-display.unsaved"}, React.createElement(Text, {live: true}, "Display order has unsaved changes.")),
        React.createElement(PanelSectionRow, {focusKey: "local-display.save"}, React.createElement(Button, {
          label: "Save for next session",
          disabled: !canSave,
          onClick: () => void saveOrder(false),
          focusKey: "local-display.save.button",
        })),
        React.createElement(PanelSectionRow, {focusKey: "local-display.save-restart"}, React.createElement(Button, {
          label: "Save and restart Gaming Mode",
          disabled: !canSave || monitorSwitch.restart_available !== true,
          onClick: () => setModal({kind: "save-restart-gamescope"}),
          focusKey: "local-display.save-restart.button",
        })),
        React.createElement(PanelSectionRow, {focusKey: "local-display.automatic"}, React.createElement(Button, {
          label: "Use automatic display order",
          disabled: Boolean(busy) || (!monitorSwitch.available && !configuredConnectors.length),
          onClick: async () => {
            const value = await run("local_clear_gamescope_output", {}, "Automatic display order restored");
            if (value) {
              localOutputOrderTouchedRef.current = false;
              setLocalOutputOrder(connected.map(output => output.output_key));
            }
          },
          focusKey: "local-display.automatic.button",
        })),
        lastOperation && lastOperation.state === "failed" && React.createElement(PanelSectionRow, {focusKey: "local-display.failure"}, React.createElement(Text, {live: true}, "Last change failed: " + (lastOperation.reason || "unknown reason"))),
        React.createElement(PanelSectionRow, {focusKey: "local-display.refresh"}, React.createElement(Button, {label: "Refresh", disabled: Boolean(busy), onClick: () => {localOutputOrderTouchedRef.current = false; void loadLocalDisplay();}, focusKey: "local-display.refresh"})),
        React.createElement(PanelSectionRow, {focusKey: "local-display.back"}, React.createElement(Button, {label: "Back", onClick: () => navigate("this-device"), focusKey: "local-display.back"})),
        modal?.kind === "save-restart-gamescope" && React.createElement(ConfirmModal, {
          title: "Save and restart Gaming Mode?",
          body: "Running games and Steam UI will close. Gaming Mode will restart using this display order.",
          confirmLabel: "Save and restart",
          busy: Boolean(busy),
          onCancel: () => setModal(null),
          onConfirm: async () => {setModal(null); await saveOrder(true);},
        })
      );
    }

    async function commitMode(target) {
      const value = await run("update_settings", {changes: {device_mode: target, client_name: draftName}}, "Changing mode…");
      if (value) {
        modeDraftTouchedRef.current = false;
        setDraftMode(target);
      }
      return value;
    }

    function destinationAfterSettings() {
      return roleHasClient(mode) ? "remote" : "this-device";
    }

    function updateStatusText() {
      if (update.status === "checking") return "Checking GitHub for a newer release…";
      if (update.status === "installing") return "Decky is preparing the update. Confirm its installation prompt.";
      if (update.status === "available" && update.release) return `Version ${update.release.version} is available.`;
      if (update.status === "current") return `You are running the latest release (v${update.currentVersion || "unknown"}).`;
      if (update.status === "unavailable") return "Automatic update checks are unavailable in this Decky session.";
      if (update.status === "error") return `Update check failed: ${update.error || "unknown error"}`;
      return "Updates are checked automatically when the plugin starts and periodically while Decky is running.";
    }

    function renderUpdates() {
      const release = update.release;
      return React.createElement(PanelSection, {title: "Updates"},
        React.createElement(PanelSectionRow, {focusKey: "settings.updates.status"}, React.createElement(Text, {live: true}, updateStatusText()),
          update.checkedAt && React.createElement(Text, {muted: true}, `Last checked ${new Date(update.checkedAt).toLocaleString()}`)),
        React.createElement(PanelSectionRow, {focusKey: "settings.updates.check"}, React.createElement(Button, {
          label: "Check for updates",
          disabled: Boolean(busy) || update.status === "checking" || update.status === "installing",
          onClick: async () => {
            setBusy("check_for_update");
            setMessage("");
            try {
              await checkForUpdate(true);
              if (updateState.status === "current") setMessage("SteamOS Companion is up to date.");
            } catch (error) {
              setMessage(boundedString(error));
            } finally {
              setBusy("");
            }
          },
          focusKey: "settings.updates.check",
        })),
        release && React.createElement(PanelSectionRow, {focusKey: "settings.updates.install"},
          React.createElement(Text, null, `Verified release ${release.tag}`),
          release.notes && React.createElement(Text, {muted: true}, release.notes),
          React.createElement(Button, {
            label: `Install v${release.version}`,
            disabled: Boolean(busy) || update.status === "installing",
            onClick: async () => {
              setBusy("install_update");
              setMessage("");
              try {
                await installUpdate();
              } catch (error) {
                setMessage(boundedString(error));
              } finally {
                setBusy("");
              }
            },
            focusKey: "settings.updates.install",
          })
        )
      );
    }

    function renderUpgradeNotice() {
      if (!settings?.client?.upgrade_notice || !roleHasServer(mode)) return null;
      return React.createElement(PanelSectionRow, {focusKey: "upgrade.client-available"},
        React.createElement(Text, null, "Client mode is now available"),
        React.createElement(Text, {muted: true}, "You can use this device to control another SteamOS device."),
        React.createElement(Button, {label: "Open Device mode", onClick: () => navigate("settings"), focusKey: "upgrade.client-available.open"}),
        React.createElement(Button, {label: "Dismiss", onClick: () => void run("dismiss_upgrade_notice"), focusKey: "upgrade.client-available.dismiss"})
      );
    }

    function renderSettings() {
      const server = settings?.server || settings || {};
      const savedMode = settings?.mode?.selected ?? mode ?? "client";
      const savedName = client.client_name || initialName;
      const transition = settings?.mode?.transition;
      const canClient = draftMode === "client" || draftMode === "both";
      const modeDirty = draftMode !== savedMode || (canClient && draftName !== savedName);
      const serverWillDisable = roleHasServer(savedMode) && !roleHasServer(draftMode);
      const clientWillDisable = roleHasClient(savedMode) && !roleHasClient(draftMode);
      const destination = destinationAfterSettings();
      const showNonstandard = client.show_nonstandard_display_modes === true;
      const leaveBody = [
        serverWillDisable && "Paired devices will no longer be able to control this device. Their access will be saved for when Server is enabled again.",
        clientWillDisable && "The saved remote pairing remains available when Client is enabled again. An unresolved remote operation may continue on the other device.",
      ].filter(Boolean).join(" ");
      return React.createElement(PanelSection, {title: "Settings"},
        React.createElement(PanelSectionRow, {focusKey: "settings.mode"}, React.createElement(Text, null, "Device mode"), React.createElement(SelectableRow, {selected: draftMode === "client", title: "Client", description: "Control another SteamOS device.", onClick: () => {modeDraftTouchedRef.current = true; setDraftMode("client");}, focusKey: "settings.mode.client"}), React.createElement(SelectableRow, {selected: draftMode === "server", title: "Server", description: "Allow paired devices to control this device.", onClick: () => {modeDraftTouchedRef.current = true; setDraftMode("server");}, focusKey: "settings.mode.server"}), React.createElement(SelectableRow, {selected: draftMode === "both", title: "Both", description: "Control another device and allow control of this device.", onClick: () => {modeDraftTouchedRef.current = true; setDraftMode("both");}, focusKey: "settings.mode.both"})),
        canClient && React.createElement(PanelSectionRow, {focusKey: "settings.client-name"}, React.createElement(Field, {label: "Name shown when pairing", value: draftName || savedName, onChange: event => {modeDraftTouchedRef.current = true; setDraftName(event.target.value);}, focusKey: "settings.client-name"})),
        React.createElement(PanelSectionRow, {focusKey: "settings.save-mode"}, React.createElement(Button, {label: "Save mode", disabled: Boolean(busy) || Boolean(transition) || !modeDirty, onClick: () => {
          if (serverWillDisable || clientWillDisable) setModal({kind: "change-mode", target: draftMode, body: leaveBody});
          else void commitMode(draftMode);
        }, focusKey: "settings.save-mode"})),
        transition && React.createElement(PanelSectionRow, {focusKey: "settings.transition"}, React.createElement(Text, {live: true}, transition.state === "waiting" ? "Waiting for display recovery" : "Changing mode…"), transition.elapsed_seconds > 10 && React.createElement(Text, null, `Still waiting after ${transition.elapsed_seconds} seconds. ${transition.reason || ""}`), React.createElement(Button, {label: "Cancel mode change", onClick: () => void run("cancel_mode_change"), focusKey: "settings.cancel-mode"})),
        settings?.service_errors?.client && React.createElement(PanelSectionRow, {focusKey: "settings.client-error"}, React.createElement(Text, {live: true}, `Client unavailable: ${settings.service_errors.client}`)),
        settings?.service_errors?.server && React.createElement(PanelSectionRow, {focusKey: "settings.server-error"}, React.createElement(Text, {live: true}, `Server unavailable: ${settings.service_errors.server}`)),
        (modal?.kind === "change-mode" || modal?.kind === "discard-mode") && React.createElement(ConfirmModal, {
          title: modal.kind === "discard-mode" ? "Discard changes?" : "Change device mode?",
          body: modal.kind === "discard-mode" ? "Your unsaved device mode changes will be lost." : modal.body,
          cancelLabel: modal.kind === "discard-mode" ? "Keep editing" : "Cancel",
          confirmLabel: modal.kind === "discard-mode" ? "Discard changes" : "Change mode",
          busy: Boolean(busy),
          onCancel: () => setModal(null),
          onConfirm: async () => {
            if (modal.kind === "discard-mode") {
              modeDraftTouchedRef.current = false;
              setDraftMode(savedMode);
              setDraftName(savedName);
              setModal(null);
              navigate(destination);
            } else {
              const target = modal.target;
              setModal(null);
              await commitMode(target);
            }
          },
        }),
        roleHasClient(mode) && React.createElement(PanelSection, {title: "Client"},
          React.createElement(PanelSectionRow, {focusKey: "settings.display-preferences.explanation"}, React.createElement(Text, null, "Display preferences"), React.createElement(Text, {muted: true}, "All advertised modes are shown by default so uncommon recovery modes remain visible.")),
          React.createElement(PanelSectionRow, {focusKey: "settings.display-preferences.toggle"}, React.createElement(ToggleRow, {label: "Show non-standard display modes", description: "Show every advertised resolution and refresh rate. The current mode is always shown.", checked: showNonstandard, disabled: Boolean(busy), onClick: () => void run("update_settings", {changes: {show_nonstandard_display_modes: !showNonstandard}}, showNonstandard ? "Non-standard display modes hidden" : "Non-standard display modes shown"), focusKey: "settings.display-preferences.toggle"}))
        ),
        renderUpdates(),
        React.createElement(PanelSection, {title: "Server"},
          React.createElement(PanelSectionRow, {focusKey: "settings.listen"}, React.createElement(Text, null, server.listener?.running ? "Accepting connections" : "Server paused"), React.createElement(Button, {label: server.listener?.running ? "Pause accepting connections" : "Accept connections", disabled: Boolean(busy) || !roleHasServer(mode), onClick: () => void run("update_settings", {changes: {listen_enabled: !server.listener?.running}}), focusKey: "settings.listen"})),
          React.createElement(PanelSectionRow, {focusKey: "settings.address"}, React.createElement(Text, {muted: true}, `Address: ${server.listener?.address || "all interfaces"}:${server.listener?.port || 18443}`)),
          React.createElement(PanelSectionRow, {focusKey: "settings.sunshine"}, React.createElement(Text, null, "Sunshine monitoring"), React.createElement("label", null, React.createElement("input", {type: "checkbox", checked: server.settings?.monitor_sunshine === true, disabled: !roleHasServer(mode), onChange: event => void run("update_settings", {changes: {monitor_sunshine: event.target.checked}})}), " Monitor Sunshine")),
          React.createElement(PanelSectionRow, {focusKey: "settings.sunshine-auto"}, React.createElement(Text, null, "Sunshine recovery"), React.createElement("label", null, React.createElement("input", {type: "checkbox", checked: server.settings?.auto_recover_sunshine !== false, disabled: !roleHasServer(mode) || server.settings?.monitor_sunshine !== true, onChange: event => void run("update_settings", {changes: {auto_recover_sunshine: event.target.checked}})}), " Auto-recover after a confirmed crash")),
          !roleHasServer(mode) && React.createElement(PanelSectionRow, {focusKey: "settings.local-display-role"}, React.createElement(Text, {muted: true}, "This device's Gaming Mode display order is under This device. Remote display settings and controls are under Remote device."))
        ),
        React.createElement(PanelSectionRow, {focusKey: "settings.back"}, React.createElement(Button, {label: "Back", onClick: () => {
          if (modeDirty) setModal({kind: "discard-mode"});
          else navigate(destination);
        }, focusKey: "settings.back"}))
      );
    }

    function renderReplace() {
      const staged = client.staged_remote;
      return React.createElement(PanelSection, {title: "Replace remote device"}, React.createElement(Text, null, `Use ${staged?.name || staged?.endpoint || "the new device"} instead of ${identityName()}?`), React.createElement(Button, {label: "Use new device", disabled: Boolean(busy), onClick: async () => {const value = await run("use_staged_remote", {use: true}, "Remote device replaced"); if (value) navigate("remote");}, focusKey: "replace.use"}), React.createElement(Button, {label: "Cancel", disabled: Boolean(busy), onClick: async () => {const value = await run("use_staged_remote", {use: false}); if (value) {navigate("remote"); setMessage(value.server_cleanup || "The old device remains selected");}}, focusKey: "replace.cancel", autoFocus: true}));
    }

    function renderBody() {
      if (!settings || view === "loading") return React.createElement(PanelSection, {title: "SteamOS Companion"}, React.createElement(Text, {live: true}, "Checking device…"));
      if (view === "setup") return renderSetup();
      if (view === "remote-setup") return renderRemoteSetup();
      if (view === "manual") return renderManual();
      if (view === "candidate") return renderCandidate();
      if (view === "pairing") return renderPairing();
      if (view === "replace") return renderReplace();
      if (view === "this-device") return renderThisDevice();
      if (view === "local-display-order") return renderLocalDisplayOrder();
      if (view === "local-display") return renderLocalDisplay();
      if (view === "settings") return renderSettings();
      if (view === "power") return renderPower();
      if (view === "display") return renderDisplay();
      if (view === "remote-display-order") return renderRemoteDisplayOrder();
      if (view === "details") return renderDetails();
      if (view === "remote") return renderOverview();
      return renderOverview();
    }

    return React.createElement("div", {style: {padding: "12px", lineHeight: "1.45", maxWidth: "680px", minWidth: 0, minHeight: "100%", color: UI_COLORS.text, background: UI_COLORS.background}},
      header(),
      renderUpgradeNotice(),
      busy && React.createElement(Text, {live: true}, busy === "remote_status" ? "Checking device…" : "Working…"),
      renderBody(),
      message && React.createElement(Text, {live: true}, message)
    );
  }

  function PluginIcon() {
    if (!React) return null;
    return React.createElement("svg", {
      viewBox: "0 0 24 24",
      width: 24,
      height: 24,
      fill: "none",
      role: "img",
      "aria-label": "SteamOS Companion",
      focusable: "false",
    },
    React.createElement("g", {fill: "none", stroke: "#f2f4f5", strokeWidth: 1.8, strokeLinecap: "round", strokeLinejoin: "round"},
      React.createElement("rect", {x: 3, y: 4, width: 18, height: 13, rx: 2}),
      React.createElement("path", {d: "M8 10.5h8m-3-3 3 3-3 3M12 17v3m-4 0h8"})
    ));
  }

  return {
    name: "SteamOS Companion",
    icon: PluginIcon(),
    content: React ? React.createElement(ClientContent) : null,
    onDismount() {
      stopped = true;
      stopUpdateWatcher();
      stopServerRuntime();
      if (driverTimer) clearTimeout(driverTimer);
      if (pairingWatchTimer) clearInterval(pairingWatchTimer);
      stopSunshineOwnerWatcher();
      if (typeof window.removeEventListener === "function") {
        for (const eventName of wakeEventNames) window.removeEventListener(eventName, wakeBridge);
      }
      notify = () => {};
    },
  };
})()
