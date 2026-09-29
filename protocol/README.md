# SteamOS Companion protocol v1

`schema.json` and the fixtures in this directory are the authoritative v1
wire contract for the Decky host and the separate Omarchy client. The host
build packages this directory. The client build consumes a pinned copy; it
does not import a sibling checkout at runtime.

The contract deliberately exposes only enumerated operations. It never carries
raw Steam payloads, arbitrary methods, PIDs, service names, paths, commands,
credentials, or Sunshine Web UI administration.

Mutations use a client-generated `request_id`. A `202 Accepted` response means
the host persisted the operation and may still be processing it. Clients query
`GET /v1/operations/{id}` with the same credential after disconnect instead of
creating a second request. `GET /v1/operations` returns the bounded operation
list, newest first, for the caller's credential plus host-owned display records
(`client_id: "host"`); it never includes another remote credential's records.
Query strings and request bodies are rejected on this route.

Pairing has two bootstrap forms. The normal form is a short authentication
string (SAS) that is **derived on both sides and never transmitted**. After
discovery and certificate pinning, the client generates a random 16-byte
`verification_nonce`, sends it base64url-encoded (unpadded, 22 characters) to
`POST /v1/pair/request`, and derives the 8-digit comparison code locally from
that nonce and the certificate fingerprint it pinned. The host derives the code
from the same nonce and its own certificate fingerprint. Both sides use:

```
code = scrypt(nonce,
              salt = b"steamos-companion:v1:pairing-sas:" + fingerprint,
              n = 2**14, r = 8, p = 1, dklen = 8)
       interpreted big-endian, modulo 10**8, zero-padded to 8 digits
```

`fingerprint` is the ASCII `sha256:<64 lowercase hex>` form of the DER
certificate digest. Because the fingerprint is part of the salt, a
TLS-terminating LAN relay — which necessarily presents its own certificate —
derives a different code than the real host, so the two screens disagree and
the owner sees the mismatch. The relay cannot repair this by choosing a nonce:
finding one that makes the real host display the relay's digits is an offline
search of the 10^8 code space, and scrypt at these parameters (~50 ms per
candidate, 16 MiB working set) puts that far outside the 120-second request
window. This is what the previous client-chosen `verification_code` form failed
to do, and that field is now rejected with `400 pairing_method_unsupported`.

The request also includes `X-SteamOS-Companion-TLS-Binding`, derived from the
exact TLS 1.2 channel used by the client. The host derives the same binding
from its accepted socket and rejects a mismatch before creating a pending
request. Regular authenticated traffic may still negotiate TLS 1.3. Decky
displays the derived short-lived code beside the pending client for visual
comparison. The code is a verification string, not a bearer credential; the
owner must still explicitly approve the matching request before the host mints
a token. The host stores only a hash of the nonce and the derived display
string, and runs the derivation once per request — never per stored record.

The first successful request returns a high-entropy `pairing_session` handle.
The client sends that handle on subsequent approval polls, so a new TLS
connection does not require re-deriving the code. The handle is stored only as
a hash on the host and expires with the pairing request.
The requesting client may cancel its pending request with
`POST /v1/pair/cancel`, authenticated by its private verification nonce and
optional session handle. The host returns the terminal state idempotently, and
owner rejection is reported to the client as `pairing_rejected` instead of an
ambiguous missing request.
The advanced form sends `pairing_id` and a short-lived secret from the full
payload. The legacy host-issued `pairing_code` form remains accepted for
compatibility. Verification nonces expire after 120 seconds and are consumed
with the approved request. The approved response also carries a read-only
`wake_target` object with the host NIC MAC; the client saves it automatically
and chooses its own active LAN interface for sending the magic packet.

The authenticated status response exposes the same wake target and capability
values for `suspend`, `restart`, and `shutdown`. Power commands remain fixed
verbs; they do not accept arbitrary system methods or shell commands.

For display recovery, `POST /v1/display/save-current` accepts exactly
`{request_id, output_id, generation, visible:true}`. The host requires the
owner confirmation, refuses the request while a preview is active, reads the
current mode from the live Decky bridge, verifies the output identity and
generation, and only then persists the mode as the recovery profile. A client
must never infer a recovery profile from an inventory read alone.

## Remote Gaming Mode display order

New hosts may expose `GET /v1/display/order` as an additive resource under
the existing v1 protocol. It is authenticated with `status.read` and returns
`display_order` with a fresh `generation`, `observed_at`, ordered opaque
`output_keys` for the current inventory, bounded output records, the saved
priority in `saved_output_keys`, independent restart flags, an adapter
identifier, and explicit unsupported/stale/ambiguous or previous-reading
reasons. The output record exposes only a friendly display name, connector
label, connected state, and active readback; it is not a Gamescope command
interface.

The client submits only host-issued opaque keys and the observed generation to
`POST /v1/display/order`, with `restart:false` for **Save for next session** or
`restart:true` for **Save and restart Gaming Mode**. The latter is shown only
after confirmation and targets the fixed user `gamescope-session.target` route
on the host. `POST /v1/display/order/automatic` is the explicit reset that
removes only plugin-owned order configuration. The host re-enumerates and
resolves every key, rejects duplicate or stale input, writes atomically, and
returns an operation ID. The client never replays an unresolved restart; it
reconciles that ID after reconnect.
