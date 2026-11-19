# Skylight Wi-Fi Automation

Private household service. Lanie, Libby, and Tucker stay blocked until that
child's Skylight tasks and chores for the day are complete. Access is for the
rest of the America/Chicago day, then the cutoff locks them again.

The switch is the UniFi Object Manager object `No Internet - <child>` on the
UCG Max. Active or resumed means blocked. Paused means the child has internet.
Pausing the object hides its generated Block policy. The app does not edit
Policy Table firewall policies, and it does not create or delete objects.

`dry_run: true` is how this ships. Dry-run makes zero object writes: it does
not pause and it does not resume. It logs the action it would have taken and
does nothing.

Deployed manifests live in the kubernetes repo (`wifi-automation/`). This
repository does not publish the image by hand. A GitHub release tagged
`0.2.0` is what tags `ghcr.io/dmcanally/skylight-wifi-automation:0.2.0` for
linux/amd64 and linux/arm64.

## Rules

Children are blocked by default. A child is unblocked for the rest of the
local day once every required task is complete or skipped. The daily latch
`unlocked_on[child]` keeps that access if a task is unchecked or added later
the same day. The latch is stored in `/tmp` (the pod emptyDir) and is dropped
on the next local date.

At or after the cutoff the child is locked, including when Skylight is
unreachable. Cutoffs come from config: 20:30 for Lanie and Libby, 20:00 for
Tucker.

`empty_day_unlocks` defaults to false. A day with no chores stays blocked.

`manual_unblock` is an optional per-child override. It defaults to false.
While it is true, and only before cutoff, that child is unblocked without
waiting on Skylight. Cutoff still locks them.

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

Environment variables, from the `wifi-automation-secrets` Secret:

| Variable | Purpose |
| --- | --- |
| `SKYLIGHT_EMAIL` | Skylight login |
| `SKYLIGHT_PASSWORD` | Skylight password |
| `UNIFI_API_KEY` | Network Integration API key, sent only as `X-API-KEY` |
| `CONFIG_PATH` | Config file. The deployment sets `/config/config.yaml` |

`GOOGLE_WIFI_REFRESH_TOKEN` is not read.

| Key | Default | Role |
| --- | --- | --- |
| `timezone` | `America/Chicago` | Latch day and cutoffs |
| `poll_seconds` | `60` | Delay between polls |
| `dry_run` | `true` | `true` means no pause and no resume |
| `include_late` | `true` | Count overdue Skylight tasks |
| `include_routines` | `true` | Count routines |
| `empty_day_unlocks` | `false` | `false` keeps an empty day blocked |
| `unifi_url` | | `https://unifi.local/proxy/network/integration/v1` |
| `unifi_site` | discover | Optional site UUID, `internalReference`, or name |
| `unifi_tls_ca` | | PEM trust anchor for `unifi.local` |
| `children[].name` | | Log name |
| `children[].skylight_profile` | | Skylight profile label |
| `children[].object_name` | | Object Manager object, looked up by name every poll |
| `children[].cutoff` | | Local `HH:MM`. At or after this, always lock |
| `children[].manual_unblock` | `false` | Unlock before cutoff without chores |

`config.example.yaml` matches those keys. Values there are not secrets. The
certificate is the public console certificate.

No ConfigMap key beyond that table is required.

## UniFi calls

Site discovery uses the published Integration API:

* `GET /proxy/network/integration/v1/sites?offset=&limit=`

Object Manager objects are not in that OpenAPI document (Network 11.0.99,
44 paths). Firewall-policy `PATCH` cannot set `enabled`, and the generated
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

Run this on Network 11.0.99 against one test object, not a child's. Use a
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

Verification stays on. The hostname check is `unifi.local`, which is the host
in `unifi_url`. `hostAliases` maps that name to `192.168.1.1`. The trust store
is only `unifi_tls_ca`.

The stock certificate is self-signed, CN and DNS SAN `unifi.local`, and it
has no `basicConstraints` extension. The image stays on Python 3.12, and the
SSL context clears only `VERIFY_X509_STRICT`. It never sets `verify=False` or
`CERT_NONE`.

Re-pin `unifi_tls_ca` before the certificate expires on 2028-09-19, and again
after any firmware reset that regenerates it. A mismatch fails the poll, leaves
every object unchanged, and stops `wifi_automation_last_success_timestamp_seconds`.

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
the emptyDir. `.github/workflows/image.yaml` builds linux/amd64 and
linux/arm64. A published GitHub release tags the image with that release tag
and a leading `v` removed, so a release tagged `0.2.0` publishes
`ghcr.io/dmcanally/skylight-wifi-automation:0.2.0`.
