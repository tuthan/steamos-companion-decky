# SteamOS Companion

Control and monitor one SteamOS device from another over your local network.
SteamOS Companion is a two-client system that shares one v1 protocol:

- **Decky plugin** — runs on SteamOS as a **Client**, **Server**, or **Both**.
- **Omarchy client** — runs as a Linux desktop bar widget and controls a paired
  Decky host.

**Website:** <https://steamos-companion.atas.tech/> ·
**Releases:** <https://github.com/tuthan/steamos-companion-decky/releases>

![SteamOS Companion This device view](assets/main.png)

![SteamOS Companion Gaming Mode display order](assets/display-order.png)

## Clients

| Client | Runs on | Role |
| --- | --- | --- |
| [SteamOS Companion for Decky](https://github.com/tuthan/steamos-companion-decky) | SteamOS with Decky Loader | Client, Server, or Both |
| [SteamOS Companion for Omarchy](https://github.com/tuthan/steamos-companion-omarchy) | A Linux desktop with Omarchy | Client for a Decky Server or Both host |

## What it does

- **Status and power.** See whether the paired device is reachable, wake it,
  and run power actions behind a confirmation.
- **Remote display settings.** Change resolution and refresh rate with a timed
  preview that reverts unless you keep it, and restore a saved display profile
  after a bad mode.
- **Gaming Mode display order.** Reorder the physical screens Gamescope should
  prefer, locally or on the paired device, and restart Gaming Mode to apply.
- **Sunshine monitoring (opt-in).** Watch the Decky Sunshine owner plugin and
  recover it when it stops.
- **Self-updating.** Checks the latest stable release and installs it through
  Decky Loader, so the plugin never needs `sudo`.

## Security model

- Pairing shows a comparison code derived on both devices and never sent over
  the network. Approve on the server only when both codes match.
- Certificates are pinned, every client gets its own scoped token, and a server
  accepts at most four paired clients.
- Traffic stays on the local network over HTTPS on port 18443. Discovery is an
  explicit, bounded scan; manual address entry is always available.

Full contract: [protocol/README.md](protocol/README.md).

## Install

Choose the client you want to use. The device being controlled must run the
Decky plugin in **Server** or **Both** mode.

### Decky plugin

Requires [Decky Loader](https://decky.xyz).

- **Decky UI.** Download `steamos-companion-decky-<version>.zip` from Releases
  and install it with Decky's plugin installer.
- **SSH.** From a local checkout, install the latest release with
  `ssh deck@steamdeck.local 'bash -s' < install.sh`. Append `-- v0.5.16` to pin a version.
- **On the device.**
  `curl -fsSL https://raw.githubusercontent.com/tuthan/steamos-companion-decky/main/install.sh | bash`

On first launch pick **Server** or **Both** on the device you want to control,
or **Client** on the SteamOS device you control it from. Discover the server
from the Client screen or enter its address, then approve the pairing on the
server.

### Omarchy desktop client

On a Linux desktop running Omarchy, install the client from its reviewed
repository:

```sh
omarchy plugin add https://github.com/tuthan/steamos-companion-omarchy.git --enable
```

Open the widget's **Settings**, choose **Find hosts**, select the Decky host,
and request pairing. Compare the code with the one shown by Decky, then approve
it on the host. The Omarchy client is directory-installed and does not use the
Decky ZIP or checksum artifact.

## Repository

| Path | Contents |
| --- | --- |
| `host/` | Decky plugin: Python backend and dependency-free frontend |
| `protocol/` | Protocol v1 contract shared with all clients |
| `site/` | Landing page, deployed to GitHub Pages by the `Pages` workflow |
| `tests/` | Unit tests |

## Development

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -m compileall -q host protocol
node --check host/frontend/index.js
node host/frontend/test_frontend.cjs
python3 host/build.py   # writes artifacts/steamos-companion-decky-<version>.zip and its SHA256
```

## Releases

Bump the version in `host/package.json`, commit, and push a matching tag:

```sh
git tag v0.5.17
git push origin v0.5.17
```

The `Release` workflow builds the ZIP and SHA256 file and attaches both to the
GitHub release.

## License

[MIT](LICENSE). SteamOS Companion is an independent open-source project and is
not affiliated with or endorsed by Valve Corporation.
