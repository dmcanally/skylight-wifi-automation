# Skylight Wi-Fi Automation

Pauses a UniFi Object Manager block once a child's Skylight tasks and chores
for the day are complete. Access lasts for the rest of the configured local
day, then the cutoff locks that child again.

The switch is the UniFi Object Manager object `No Internet - <child>` on the
UniFi gateway. Active or resumed means blocked. Paused means the child has
internet. Pausing the object hides its generated Block policy. The app does
not edit Policy Table firewall policies, and it does not create or delete
objects.

`dry_run: true` is how this ships. Dry-run makes zero UniFi object writes: it
does not pause and it does not resume. It still writes the daily latch file
when a child earns access. It logs the action it would have taken and does
nothing to the gateway.

## Deploying

The process reads `CONFIG_PATH` (default `/config/config.yaml`) and these
environment variables:

| Variable | Purpose |
| --- | --- |
| `SKYLIGHT_EMAIL` | Skylight login |
| `SKYLIGHT_PASSWORD` | Skylight password |
| `UNIFI_API_KEY` | Network Integration API key, sent only as `X-API-KEY` |
| `CONFIG_PATH` | Config file path. Default `/config/config.yaml` |
| `CONFIG_SOURCE` | `db` (default) or `file` |
| `DATA_DIR` | SQLite directory. Default `/data`. Mount a volume writable by uid 10001 |
| `UI_PORT` | Web UI port. Default `8000` |
| `APP_ENCRYPTION_KEY` | Base64 of 32 bytes. Required to store secrets in the database |
| `ADMIN_SETUP_TOKEN` | At least 32 characters. Required to create the first admin. Unset or shorter disables setup |
| `COOKIE_SECURE` | `false` uses a plain HTTP session cookie. Default is Secure |
| `TRUSTED_PROXIES` | Comma-separated proxy IPs or CIDRs allowed to set `X-Forwarded-For`. Empty by default |
| `ARGON2_CONCURRENCY` | How many password hashes may run at once. Default `2` |
| `APP_ENCRYPTION_KEY_NEW` | Replacement key for `skylight-wifi rotate-key` |
| `LATCH_PATH` | Daily latch file. Default `/tmp/skylight-wifi-latch.json` |

Metrics are served on port `8080`. The UI is served on port `8000`. The
database is `$DATA_DIR/app.db`. The daily latch file defaults to
`/tmp/skylight-wifi-latch.json`.

## Web UI

The UI runs in the same process and event loop as the poller. Static files
are in the image. There is no separate database server, cache, auth provider,
or CDN. Do not publish port `8000` or `8080` to the internet. Keep both on
the LAN or a VPN. There is no SSO and no password recovery.

Mount a volume on `/data` that uid 10001 can write. The image creates that
directory and runs as uid 10001. The volume holds the admin, sessions,
database secrets, overrides, and the audit log. Losing it does not remove
environment-variable secrets. Set `LATCH_PATH=/data/latch.json` if the daily
latch should survive a restart.

`CONFIG_SOURCE=db` imports `CONFIG_PATH` once when the database has no
config, then the database is the source of truth. If the YAML file later
differs, the process logs one warning and does not import it again. Each poll
uses a validated snapshot. A config or secret change is picked up on the next
poll without a restart. Invalid input is rejected and the running snapshot
stays as it was. `CONFIG_SOURCE=file` keeps the file as the only config
source, and the UI will not save settings, children, the UniFi connection, or
overrides.

### First run

1. Replace the placeholder in `unifi_tls_ca` with the console certificate.
   The placeholder text, and a PEM that is not an X.509 certificate, are
   rejected at startup and when the certificate is saved.
2. Set `APP_ENCRYPTION_KEY` to the standard base64 encoding of 32 random
   bytes if secrets should be stored in the database. If it is unset, the
   secrets page is disabled and `SKYLIGHT_EMAIL`, `SKYLIGHT_PASSWORD`, and
   `UNIFI_API_KEY` are used.
3. Set `ADMIN_SETUP_TOKEN` to a random value of at least 32 characters. A
   shorter value is ignored and setup stays disabled. There is no default
   password.
4. Set `COOKIE_SECURE=false` when the UI is plain HTTP. Open
   `http://<lan-host>:8000`. Until an admin exists, the first page is setup.
   Create an admin and use a password of at least 12 characters. `/setup`
   still works directly, and it returns 404 once the admin exists.
5. Unset `ADMIN_SETUP_TOKEN` and sign in.

Empty secret fields are left unchanged. Clear removes the database copy and
the environment variable is used again. A value is shown only as set or not
set, with its source (`db` or `env`) and, for a database value, when it was
updated.

`COOKIE_SECURE=false` is for plain HTTP on a LAN, including the
`http://<lan-host>:8000/setup` flow. The session cookie is then `swa_session`
without a `__Host-` prefix, because browsers reject a `__Host-` cookie that
is not Secure. Leave `COOKIE_SECURE` unset on HTTPS. The cookie is then
`__Host-swa_session` with Secure, Path=/, and no Domain. Both names are
HttpOnly and SameSite=Strict.

`DATA_DIR` must already exist and be writable by uid 10001. If it is missing
or not writable, startup logs one line, `DATA_DIR is not writable: ...`, and
exits. Writability is a real temp-file create and delete. The database file
and its WAL and SHM files are mode `0600`. When this process owns the
directory, it sets the directory to mode `0700`. If it does not own the
directory, or that mode change is refused, it logs a warning and continues.

`TRUSTED_PROXIES` is empty unless you set it. The process only reads
`X-Forwarded-For` when the connecting peer is in that list (an IP or a CIDR),
and uvicorn is given the same list as `forwarded_allow_ips`. A lockout is per
address, so failures from one client do not lock a different address. Do not
put every client in the list. An untrusted peer cannot choose its address by
sending the header.

Liveness and readiness probes should use `GET :8000/healthz`. The image
health check uses that URL. Prometheus stays on `:8080/metrics`.

`skylight-wifi reset-admin` deletes the admin and its sessions so `/setup`
can be used again. From a shell in the container:

```bash
skylight-wifi reset-admin
```

### Rotating the encryption key

Database secrets are AES-256-GCM ciphertext bound to the secret name. A row
written by an older build, without that binding, is re-encrypted on the next
read. Changing `APP_ENCRYPTION_KEY` without rewriting the rows makes those
rows unreadable. The process then falls back to the environment variable.

To rotate the key without retyping the secrets:

1. Keep the current key in `APP_ENCRYPTION_KEY`.
2. Put the new 32-byte key, base64-encoded, in `APP_ENCRYPTION_KEY_NEW`.
3. Run `skylight-wifi rotate-key` with the pod scaled to 0, or restart the pod
   right away after running it.
4. Move the new key to `APP_ENCRYPTION_KEY`, unset `APP_ENCRYPTION_KEY_NEW`,
   and restart.

The command rewrites every stored secret and does not print the values.

### Schedule overrides

A child has at most one active override. Block and Unblock replace whatever
is active. The override lasts until the earlier of the child's next cutoff
and the next local midnight in the configured timezone, and then the normal
evaluator resumes. Both instants are computed with `zoneinfo` when the
override is saved and stored as a UTC timestamp. A cutoff of 00:00 is the
same instant as midnight, so it is not applied twice. An unblock set after
today's cutoff therefore lasts until midnight. That case requires the
operator to type `UNBLOCK`. The page shows the computed expiry before the
word is accepted, and the server checks it again. Clear ends an override
immediately. Expiry is evaluated on every poll, including after a restart.
`dry_run` still prevents UniFi writes. An override only changes the action
that would have been logged.

In file mode, `manual_unblock: true` still unlocks that child until cutoff.
In database mode the field is imported once as an unblock override and is
not stored on the child. Old YAML that still contains the field loads.

An active manual unblock still unblocks that child when Skylight is
unreachable. The operator's override wins over the fail-closed hold. A
manual block still blocks. If there is no active override, a Skylight outage
leaves the object unchanged until cutoff.

## Rules

Children are blocked by default. A child is unblocked for the rest of the
local day once every required task is complete or skipped. The daily latch
`unlocked_on[child]` keeps that access if a task is unchecked or added later
the same day. The latch is stored in `/tmp/skylight-wifi-latch.json` and is
dropped on the next local date. Dry-run still writes that file. Only UniFi
object writes are disabled.

At or after the cutoff the child is locked, including when Skylight is
unreachable. Cutoffs come from config: 20:30 for child-a and child-b, 20:00
for child-c.

`empty_day_unlocks` defaults to false. A day with no chores stays blocked.

In file mode, `manual_unblock` is an optional per-child override. It defaults
to false. While it is true, and only before cutoff, that child is unblocked
without waiting on Skylight. Cutoff still locks them. In database mode the
same flag is migrated once into a manual unblock, described under Web UI.

If Skylight or the UCG is unreachable or returns an error, the current object
state is left unchanged and the failure is logged. The cutoff lock is the
exception, and only when the UCG read succeeded. On startup the process does
not write any object until that poll has read every configured object.

Each poll logs one line per child. `unlock=True` with `lock=False` would
pause the object. `unlock=False` with `lock=True` would resume it. Both
fields are always present. A fail-safe poll that does not choose either
action logs `unlock=False` and `lock=False`. The line never contains the API
key, the Skylight password, or a client MAC or IP address.

## Configuration

Environment variables and the config file path are listed under Deploying.
`GOOGLE_WIFI_REFRESH_TOKEN` is not read.

| Key | Default | Role |
| --- | --- | --- |
| `timezone` | `America/Chicago` | Latch day and cutoffs |
| `poll_seconds` | `60` | Delay between polls |
| `dry_run` | `true` | `true` means no pause and no resume. The latch file is still written. |
| `include_late` | `true` | Count overdue Skylight tasks |
| `include_routines` | `true` | Count routines |
| `empty_day_unlocks` | `false` | `false` keeps an empty day blocked |
| `unifi_url` | | `https://unifi.local/proxy/network/integration/v1` |
| `unifi_site` | discover | Optional site UUID, `internalReference`, or name |
| `unifi_tls_ca` | | Only trust anchor loaded for `unifi.local` |
| `children[].name` | | Log name |
| `children[].skylight_profile` | | Skylight profile label |
| `children[].object_name` | | Object Manager object, looked up by name every poll |
| `children[].cutoff` | | Local `HH:MM`. At or after this, always lock |
| `children[].manual_unblock` | `false` | File mode only. Database mode migrates `true` into an override |

`config.example.yaml` matches those keys. Values there are examples. Paste
the gateway console certificate over the placeholder in `unifi_tls_ca`.

## UniFi calls

Site discovery uses the published Integration API:

* `GET /proxy/network/integration/v1/sites?offset=&limit=`

Object Manager objects are not in that OpenAPI document for a current UniFi
Network version. Firewall-policy `PATCH` cannot set `enabled`, and the generated
policy is missing while the object is paused. The object itself is the
object-oriented network config. The same `X-API-KEY` is sent. The site path
segment is the site `internalReference` from the Integration site list, not
the site UUID.

The v2 paths below are undocumented. A Network firmware update can change or
remove them. `wifi_automation_poll_failures_total`
(`WifiAutomationPollFailing`) and
`wifi_automation_last_success_timestamp_seconds`
(`WifiAutomationLastSuccessStale`, `WifiAutomationMetricsAbsent`) are how
that shows up: the poll leaves object state unchanged and the alerts fire.

* `GET /proxy/network/v2/api/site/{internalReference}/object-oriented-network-configs?offset=&limit=`
* `GET /proxy/network/v2/api/site/{internalReference}/object-oriented-network-config/{id}`
* `PUT /proxy/network/v2/api/site/{internalReference}/object-oriented-network-config/{id}`

A write is allowed only when the object's `name` is an exact match for one
configured `object_name`. Zero matches or more than one match skips that
child, logs an error, and counts a poll failure. Other children are not
written by that miss. Immediately before a PUT the process GETs that object
again and changes only `enabled`. `enabled: false` pauses it (internet on).
`enabled: true` resumes it (blocked). It then GETs the object once more. If
`enabled` is not the value just written, that poll fails and the next poll
is the retry. A non-2xx response, 401, 403, a non-boolean or missing
`enabled`, an unexpected document shape, or a pagination error does the
same: no further writes, and `wifi_automation_poll_failures_total` increments.

The client never sends `POST` or `DELETE`, and it never calls
`/firewall/policies`. Info logs never include the object body. Debug logs
include only the object id, name, and `enabled`.

## Gate 4 evidence

Run this on a current UniFi Network version against one test object, not a child's. Use a
throwaway client. The local `X-API-KEY` has to be accepted on all three
calls: `GET /proxy/network/integration/v1/sites`, the v2 list GET, and the
v2 PUT. A 401 or 403 on any of them means the key is not accepted.

Before saving any output, delete the `X-API-KEY` value, every MAC address,
and every client IP address. Do not paste the PUT body.

1. `GET /sites` proves the key on the Integration API. Keep `internalReference`.

```bash
curl --fail-with-body --cacert ucg.pem \
  -H "X-API-KEY: ${UNIFI_API_KEY}" \
  -H "Accept: application/json" \
  "https://unifi.local/proxy/network/integration/v1/sites?offset=0&limit=25"
```

2. `GET` the v2 object list. This proves the key on that GET. Note the test
   object's `id` and `enabled`. Follow `offset` until `offset + count`
   reaches `totalCount`.

```bash
curl --fail-with-body --cacert ucg.pem \
  -H "X-API-KEY: ${UNIFI_API_KEY}" \
  -H "Accept: application/json" \
  "https://unifi.local/proxy/network/v2/api/site/${SITE_REF}/object-oriented-network-configs?offset=0&limit=200"
```

3. Pause. `GET` the one object, copy it, set only `enabled` to `false`, and
   `PUT` it. This proves the key on the v2 PUT. `GET` the object again.
   `enabled` must be `false`.

```bash
curl --fail-with-body --cacert ucg.pem \
  -H "X-API-KEY: ${UNIFI_API_KEY}" \
  -H "Accept: application/json" \
  "https://unifi.local/proxy/network/v2/api/site/${SITE_REF}/object-oriented-network-config/${OBJECT_ID}"

curl --fail-with-body --cacert ucg.pem \
  -X PUT \
  -H "X-API-KEY: ${UNIFI_API_KEY}" \
  -H "Accept: application/json" \
  -H "Content-Type: application/json" \
  --data-binary @pause.json \
  "https://unifi.local/proxy/network/v2/api/site/${SITE_REF}/object-oriented-network-config/${OBJECT_ID}"

curl --fail-with-body --cacert ucg.pem \
  -H "X-API-KEY: ${UNIFI_API_KEY}" \
  -H "Accept: application/json" \
  "https://unifi.local/proxy/network/v2/api/site/${SITE_REF}/object-oriented-network-config/${OBJECT_ID}"
```

4. Resume. `PUT` the same document with only `enabled` set to `true`. `GET`
   the object again. `enabled` must be `true`.

Do not `PATCH` or `PUT` `/proxy/network/integration/v1/sites/{siteId}/firewall/policies/{id}`.

## TLS

Verification stays on. The hostname check uses the host in `unifi_url`
(for example `unifi.local`).

The TLS context is `ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)`. That constructor
does not load the system CA store. The only trust anchor is
`load_verify_locations(cadata=unifi_tls_ca)`. A public certificate such as
`github.com` is rejected because it was not signed by that pin.

The stock certificate is self-signed, CN and DNS SAN `unifi.local`, and it
has no `basicConstraints` extension. The image stays on Python 3.12, and the
SSL context clears only `VERIFY_X509_STRICT`. It never sets `verify=False` or
`CERT_NONE`.

Re-pin `unifi_tls_ca` before the certificate expires, and again after any
firmware reset that regenerates it. A mismatch fails the poll, leaves every
object unchanged, and stops `wifi_automation_last_success_timestamp_seconds`.

## Metrics

The process serves Prometheus text on `:8080/metrics`.

| Metric | Meaning |
| --- | --- |
| `wifi_automation_poll_failures_total` | Polls that hit a Skylight error, a UniFi error (including 401/403, a non-2xx write, a bad `enabled` value, a pagination error, or a name that is missing or duplicated), or a verify-after-write mismatch. Dry-run skips are not counted. |
| `wifi_automation_last_success_timestamp_seconds` | Unix time of the last poll that finished without error, including a dry-run poll. It is absent until that happens. |

## Local check

```bash
python -m pip install -e '.[dev]'
cp config.example.yaml config.yaml
export SKYLIGHT_EMAIL='<skylight email>'
export SKYLIGHT_PASSWORD='<skylight password>'
export UNIFI_API_KEY='<Network Integration API key>'
skylight-wifi --config config.yaml --once
```

`config.yaml` is gitignored. Do not commit secret values.

## Image

The container runs as uid 10001, with a read-only root filesystem. `/tmp` is
writable. Mount a volume on `/data` writable by uid 10001. The UI listens on
port 8000 and Prometheus stays on port 8080. This repository does not publish
the image by hand.
`.github/workflows/image.yaml` builds linux/amd64 and linux/arm64. A published
GitHub release tags the image with that release tag and a leading `v` removed,
so a release tagged `0.2.1` publishes
`ghcr.io/dmcanally/skylight-wifi-automation:0.2.1`.
