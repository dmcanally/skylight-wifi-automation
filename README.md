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

## Prerequisites

- A UniFi gateway whose Network application has Object Manager, the published
  Integration API, and the object endpoints described under UniFi calls.
- A Skylight account with one frame. The process errors when the account has
  any other number of frames. Each child needs a Skylight profile label.
- For Kubernetes: a cluster on 1.25 or newer, a default StorageClass if the
  chart creates volumes, and the image
  `ghcr.io/dmcanally/skylight-wifi-automation`. Set `backup.timeZone` only on
  1.27 or newer.
- Or Docker, for a single host. The image listens on 8000 and 8080 and runs
  as uid 10001.

Do not publish port 8000 or 8080 on the internet. Keep both on a LAN or a
VPN. There is no SSO and no password recovery.

## UniFi objects and credentials

Create the objects on the gateway. The app only looks them up by name.

1. In UniFi Network, create one Object Manager object per child. Name it to
   match `children[].object_name`, for example `No Internet - child-a`.
   Put that child's devices in the object. Device MAC addresses stay in
   UniFi. They are not config fields in this app.
2. The object generates a Block policy. Leave the policy alone. The app
   pauses and resumes the object. It never calls `/firewall/policies`.
3. Create a local Network Integration API key that is accepted on the site
   list, the object list, and the object PUT. The process sends it only as
   `X-API-KEY`. Confirm that with the curls under Checking the API key
   before setting `dry_run: false`.
4. Export the console certificate that the gateway presents for the hostname
   in `unifi_url`. Paste that PEM into `unifi_tls_ca`. The hostname in the
   URL has to match the certificate. `unifi.local` is the usual name. On
   Kubernetes, set `hostAliases` when cluster DNS does not resolve it.
5. `unifi_site` is optional. Leave it empty on a one-site gateway. Otherwise
   set a site UUID, `internalReference`, or name.

The Skylight login is `SKYLIGHT_EMAIL` and `SKYLIGHT_PASSWORD`.
`children[].skylight_profile` must match the profile label on the frame.

## Install on Kubernetes

The chart is `charts/skylight-wifi-automation`. Chart documentation and the
values table are in
[charts/skylight-wifi-automation/README.md](charts/skylight-wifi-automation/README.md).

The Deployment is one replica and uses strategy Recreate. SQLite has one
writer. A second pod corrupts `app.db`. Probes call `GET /healthz` on port
8000. That handler does not contact Skylight or UniFi. Metrics stay on port
8080, on their own ClusterIP Service, so a NodePort or LoadBalancer does not
publish them.

`CONFIG_SOURCE=db` (the default) imports the chart ConfigMap once, when the
database has no config. After that the UI is the source of truth. Editing
the ConfigMap does not change a database that already has config.

### Create the Secret

`APP_ENCRYPTION_KEY` is required if secrets are stored in the database. It is
the standard base64 encoding of 32 random bytes. `ADMIN_SETUP_TOKEN` is
optional. When set, it must be at least 32 characters. A shorter value is
ignored and setup stays disabled. There is no default password.

```bash
kubectl create namespace skylight-wifi
kubectl -n skylight-wifi create secret generic skylight-wifi-automation \
  --from-literal=APP_ENCRYPTION_KEY="$(openssl rand -base64 32 | tr -d '\n')" \
  --from-literal=ADMIN_SETUP_TOKEN="$(openssl rand -base64 32 | tr -d '\n')"
```

`SKYLIGHT_EMAIL`, `SKYLIGHT_PASSWORD`, and `UNIFI_API_KEY` can be keys in
that Secret, or you can enter them in the UI after the encryption key is
set. With the encryption key present and those three unset, the process
starts and polls stay fail-closed until they are set.

Creating the Secret from Helm values works, and Helm then stores the values
in the release Secret too. `secrets.existingSecret` is the one to use.

### Install from this repository

Replace `console.pem` with the gateway certificate. The chart's default
`unifi_tls_ca` is a placeholder. The process rejects it and exits.

```bash
helm install skylight-wifi-automation charts/skylight-wifi-automation \
  --namespace skylight-wifi \
  --set secrets.existingSecret=skylight-wifi-automation \
  --set-file config.unifi_tls_ca=console.pem
```

`charts/skylight-wifi-automation/files/unifi.local.example.crt` is a
generated certificate for `unifi.local`. Use it only to smoke-test a
cluster. The first start imports it, and the database keeps it.

Example overlays, all with placeholder data:

- `examples/minimal.yaml`
- `examples/nodeport-with-backup.yaml` (NodePort, plain HTTP, a 03:15 backup, NetworkPolicy; addresses are `192.0.2.0/24`)
- `examples/ingress.yaml` (`wifi.example.com`)

```bash
kubectl -n skylight-wifi port-forward svc/skylight-wifi-automation 8000:8000
```

Open `http://127.0.0.1:8000/setup`. Plain HTTP needs
`--set env.cookieSecure=false`.

```bash
helm test skylight-wifi-automation --namespace skylight-wifi
```

### Install from GHCR

`.github/workflows/chart-publish.yaml` pushes the chart as an OCI artifact
when a GitHub Release is published or a tag matching `chart-v*` is pushed.
Until that first publish, install from the repository path above.

```bash
helm install skylight-wifi-automation \
  oci://ghcr.io/dmcanally/charts/skylight-wifi-automation \
  --version 0.1.0 \
  --namespace skylight-wifi \
  --set secrets.existingSecret=skylight-wifi-automation \
  --set-file config.unifi_tls_ca=console.pem
```

The container image and the chart are different GHCR packages. If the chart
package is private, `helm registry login ghcr.io` first, or install from the
git tree.

### First run

1. Replace `unifi_tls_ca` before the first start. The placeholder text, and
   a PEM that is not an X.509 certificate, are rejected at startup and when
   the certificate is saved.
2. Open the UI. Until an admin exists, the first page is setup. Create an
   admin with a password of at least 12 characters. `/setup` still works
   directly, and it returns 404 once the admin exists.
3. Unset `ADMIN_SETUP_TOKEN` and roll the pod. For an external Secret, bump
   `secrets.rolloutToken`. Sign in.

`dry_run` stays true until you turn it off in the UI (database mode) or in
the file (file mode). Leave it true until the API key check below has passed
on a test object.

## Run without Kubernetes

The image entrypoint is `skylight-wifi`. It reads the same environment
variables. The root filesystem can be read-only. `/tmp` has to be writable
(`HOME=/tmp`). `/data` has to exist and be writable by uid 10001.

```bash
mkdir -p data
cp config.example.yaml config.yaml
# Replace the unifi_tls_ca placeholder. The process runs as uid 10001,
# so that user has to be able to write ./data.
sudo chown 10001:10001 data
export APP_ENCRYPTION_KEY="$(openssl rand -base64 32 | tr -d '\n')"
export ADMIN_SETUP_TOKEN="$(openssl rand -base64 32 | tr -d '\n')"
docker run --rm \
  --name skylight-wifi \
  --read-only \
  --user 10001:10001 \
  --tmpfs /tmp:rw,nosuid,size=64m \
  -p 127.0.0.1:8000:8000 \
  -p 127.0.0.1:8080:8080 \
  -v "$PWD/data:/data" \
  -v "$PWD/config.yaml:/config/config.yaml:ro" \
  -e CONFIG_PATH=/config/config.yaml \
  -e CONFIG_SOURCE=db \
  -e DATA_DIR=/data \
  -e LATCH_PATH=/data/latch.json \
  -e COOKIE_SECURE=false \
  -e APP_ENCRYPTION_KEY \
  -e ADMIN_SETUP_TOKEN \
  ghcr.io/dmcanally/skylight-wifi-automation:0.4.0
```

Open `http://127.0.0.1:8000/setup`. A one-shot poll appends `--once` to that
command. `config.yaml` is gitignored.

## Environment

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
| `LATCH_PATH` | Daily latch file. Default `/tmp/skylight-wifi-latch.json`. The chart sets `/data/latch.json` so a restart keeps today's access |

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

An admin can add members and other admins from Users. A member can view the
dashboard and set block or unblock overrides. Members cannot open secrets,
users, or the schedule. The last active admin cannot be removed, disabled, or
demoted. Sign-in lockout stays per account and per client address.

Sessions live in the SQLite file on `DATA_DIR`, so a pod restart keeps them.
The cookie value is stored only as a hash. Remember me keeps a session for 30
days, renewed on use, and never past 90 days from when it was created. The
browser cookie for that choice has a 30-day Max-Age and is sent again when
the window moves. Without remember me, the session lasts 12 hours and the
cookie is a browser session cookie. Sessions shows created time, last seen,
and a coarse browser label. A user can revoke their own. An admin can revoke
anyone's. Changing a password revokes every session for that account and
signs the browser that made the change into a new session. That new session
keeps the original absolute cap, so a password change does not extend the 90
days. Disabling or deleting an account revokes all of its sessions.

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

`skylight-wifi reset-admin` deletes admin accounts and their sessions so
`/setup` can create an admin again. Member accounts and their sessions stay.
From a shell in the container:

```bash
skylight-wifi reset-admin
```

### Upgrading

Back up `/data/app.db` before upgrading. On Kubernetes:

```bash
helm upgrade skylight-wifi-automation charts/skylight-wifi-automation \
  --namespace skylight-wifi \
  --reuse-values
```

`--reuse-values` keeps the certificate and Secret name from the last install.
The strategy is Recreate, so the old pod stops before the new one starts.
Do not scale past one replica.

Release 0.4.x migrates that database
in place. 0.3.0 and 0.3.1 cannot read a migrated database. Before it migrates,
the process writes one copy beside the database, `app.db.bak-<timestamp>`,
mode `0600` from the moment the file is created, and keeps only the latest
copy. Sessions that already exist keep their original 7-day lifetime, and
their cookies stay 7-day cookies. To roll back, stop the pod first. Delete
`app.db-wal` and `app.db-shm`, then copy the backup over `app.db`, and start
the 0.3.1 image. Pin the image by digest (`image.digest`) so a rollout cannot
downgrade onto a migrated database. A chart upgrade does not re-import config
that is already in the database.

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

## Backup and restore

The chart can run a nightly backup. Set `backup.enabled=true`. The default
schedule is `15 3 * * *` (03:15). Set `backup.timeZone` to `Etc/UTC` on
Kubernetes 1.27 or newer so that is 03:15 UTC. The job uses the sqlite3
backup API against the live database, then `PRAGMA integrity_check` on the
copy. A good run logs one line:

```text
backup ok file=... bytes=... kept=...
```

Files land on a separate volume at `/backups/app-<UTC timestamp>.db`, mode
`0600`. The default is to keep 7. That count is `backup.retention`. The
CronJob history limits are only how many finished Kubernetes Jobs to keep.

The backup pod mounts the data volume read-write so SQLite can open the WAL
shared-memory file. The script does not modify application tables. It is
pinned to the app's node because the data claim is ReadWriteOnce, and the
volume plugin has to allow two pods on that node. The backup pod has a
deny-all NetworkPolicy. With `WaitForFirstConsumer`, the backups claim stays
Pending until that pod exists, and `helm upgrade --wait` waits for the claim.
Wait on the Deployment instead.

The migration file `app.db.bak-<timestamp>` next to `app.db` is a different
copy. The process writes it before a schema migration and keeps one.

To restore a CronJob backup:

1. Scale the Deployment to 0, and suspend the CronJob
   (`backup.suspend=true`) so nothing writes the file during the copy.
2. Copy the chosen `app-*.db` over `/data/app.db`.
3. Delete `/data/app.db-wal` and `/data/app.db-shm` if they exist.
4. Scale the Deployment back to 1.

The same steps work in Docker: stop the container, replace `data/app.db`,
delete the `-wal` and `-shm` files, and start it again. Restoring a 0.4
database onto 0.3.1 does not work. Use the migration backup and the 0.3.1
image, as described under Upgrading.

## Rules

Children are blocked by default. A child is unblocked for the rest of the
local day once every required task is complete or skipped. The daily latch
`unlocked_on[child]` keeps that access if a task is unchecked or added later
the same day. The latch defaults to `/tmp/skylight-wifi-latch.json` and is
dropped on the next local date. Set `LATCH_PATH` on the data volume, which
the Helm chart does, if it should survive a restart. Dry-run still writes
that file. Only UniFi object writes are disabled.

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

Environment variables are listed under Environment.
`GOOGLE_WIFI_REFRESH_TOKEN` is not read. Helm values that render this file
are under `config` in the chart.

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

## Checking the API key

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

## Troubleshooting

| Symptom | What it means |
| --- | --- |
| The process exits before it listens | `unifi_tls_ca` is still the placeholder or is not a certificate; `DATA_DIR` is missing or not writable (`DATA_DIR is not writable`); `CONFIG_SOURCE` is not `db` or `file`; `TRUSTED_PROXIES` is `*`, `0.0.0.0/0`, or `::/0`; or `APP_ENCRYPTION_KEY` is unset while `SKYLIGHT_EMAIL`, `SKYLIGHT_PASSWORD`, and `UNIFI_API_KEY` are also unset |
| Pod stays Pending | The data claim has no StorageClass, or the backup pod cannot mount a ReadWriteOnce volume that is already attached |
| Readiness never goes green with a NetworkPolicy | The CNI is applying the policy to kubelet probes. Add the node CIDR to `networkPolicy.probeCidrs` |
| `/setup` is 404 | An admin already exists. Sign in, or run `skylight-wifi reset-admin` |
| Setup says it is disabled | `ADMIN_SETUP_TOKEN` is unset or shorter than 32 characters |
| Sign-in does nothing on plain HTTP | `COOKIE_SECURE` is still true. Set `env.cookieSecure=false` and restart |
| ConfigMap edits do nothing | The database already has config. Change it in the UI. The log line is `config file differs from the database; database is the source of truth` |
| Polls fail and objects do not change | Skylight or the gateway is unreachable, the certificate does not match `unifi_url`, or the object name is missing or duplicated. `dry_run: true` also means no object writes, and those skips are not poll failures |
| `wifi_automation_last_success_timestamp_seconds` is absent | No poll has finished without error yet. A dry-run poll that finishes counts as success |
| Backup job logs `database not found` | The app has not created `app.db` yet, or the backup pod is not mounting the data volume |

`kubectl logs` on the app pod is the first place to look. The lines do not
include the API key, the Skylight password, or a client MAC or IP address.

## Security

- Keep the UI and the metrics port off the internet. The chart's UI Service
  defaults to ClusterIP. Metrics are a separate ClusterIP.
- Run the chart's security context: uid 10001, non-root, read-only root
  filesystem, all capabilities dropped, `RuntimeDefault` seccomp. The
  ServiceAccount token is not mounted.
- Store `APP_ENCRYPTION_KEY` in a Secret you create. Do not put it in
  `values.yaml`. Helm keeps inline values in the release Secret.
- Unset `ADMIN_SETUP_TOKEN` after the admin exists. There is no password
  recovery. `skylight-wifi reset-admin` is the way back, and it leaves member
  accounts in place.
- `COOKIE_SECURE` stays true on HTTPS. The cookie is then `__Host-swa_session`.
  On plain HTTP it is `swa_session`. Both are HttpOnly and SameSite=Strict.
- `TRUSTED_PROXIES` lists only the proxies that may set `X-Forwarded-For`.
- `dry_run` stays true until an API key check on a test object has passed.
- Database files are mode `0600`. When the process owns `DATA_DIR` it sets
  the directory to `0700`.
- A NetworkPolicy can limit the UI to a CIDR and metrics to the monitoring
  namespace. The backup pod's policy denies all ingress and egress.
- Do not commit `config.yaml`, API keys, or the encryption key. `config.yaml`
  is gitignored.

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

The container runs as uid 10001. It is built to run with a read-only root
filesystem. `/tmp` must be writable. Mount a volume on `/data` writable by
uid 10001. The UI listens on port 8000 and Prometheus stays on port 8080.
The image `HEALTHCHECK` is `GET /healthz`.

This repository does not publish the image by hand.
`.github/workflows/image.yaml` builds linux/amd64 and linux/arm64 on pushes
to `main` and on a published GitHub Release. A release tags the image with
that release tag and a leading `v` removed, so a release tagged `0.4.0`
publishes `ghcr.io/dmcanally/skylight-wifi-automation:0.4.0`. Pushes to
`main` also publish `:latest` and `:<git sha>`.

`.github/workflows/chart-publish.yaml` pushes the Helm chart to
`oci://ghcr.io/dmcanally/charts/skylight-wifi-automation` on a published
Release or a `chart-v*` tag. It does not run on an ordinary push to `main`.
