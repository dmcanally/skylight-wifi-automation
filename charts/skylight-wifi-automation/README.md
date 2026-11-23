# skylight-wifi-automation

Deploy Skylight Wi-Fi Automation on Kubernetes

![Version: 0.1.0](https://img.shields.io/badge/Version-0.1.0-informational?style=flat-square) ![Type: application](https://img.shields.io/badge/Type-application-informational?style=flat-square) ![AppVersion: 0.4.0](https://img.shields.io/badge/AppVersion-0.4.0-informational?style=flat-square)

![Kubernetes](https://img.shields.io/badge/kubernetes-%3E%3D1.25.0-326ce5)

This chart runs one copy of [Skylight Wi-Fi Automation](https://github.com/dmcanally/skylight-wifi-automation). The process pauses or resumes UniFi Object Manager objects from Skylight task completion. It serves a web UI, stores configuration and sessions in SQLite, and exposes Prometheus metrics.

The Deployment is **one replica** with strategy **Recreate**. SQLite on the data volume has a single writer. A second pod, including one started by a rolling update, can corrupt `app.db`. That is fixed in the template and is not a value.

## What the chart creates

| Resource | When |
| --- | --- |
| Deployment, UI Service, metrics Service, ConfigMap, ServiceAccount | Always |
| PersistentVolumeClaim for `/data` | `persistence.enabled` and no `persistence.existingClaim` |
| Secret | Inline `secrets.*` values are set and `secrets.existingSecret` is empty |
| Ingress | `ingress.enabled` |
| NetworkPolicy for the app | `networkPolicy.enabled` |
| CronJob, backup script ConfigMap, backups claim, deny-all NetworkPolicy | `backup.enabled` |
| ServiceMonitor | `serviceMonitor.enabled` |
| `helm test` Pod | `helm test` |

The UI Service publishes only the UI port. Metrics stay on a ClusterIP Service named `<fullname>-metrics` so a NodePort or LoadBalancer does not publish port 8080. The process always listens on 8080 for metrics. `env.uiPort` must not be 8080.

## Install

Create the namespace and a Secret before installing. `APP_ENCRYPTION_KEY` is the standard base64 encoding of 32 random bytes. `ADMIN_SETUP_TOKEN`, when set, must be at least 32 characters.

```bash
kubectl create namespace skylight-wifi
kubectl -n skylight-wifi create secret generic skylight-wifi-automation \
  --from-literal=APP_ENCRYPTION_KEY="$(openssl rand -base64 32 | tr -d '\n')" \
  --from-literal=ADMIN_SETUP_TOKEN="$(openssl rand -base64 32 | tr -d '\n')"
```

Replace `console.pem` with the gateway console certificate. The default `config.unifi_tls_ca` is a placeholder the app rejects, so the process exits until you replace it.

```bash
helm install skylight-wifi-automation charts/skylight-wifi-automation \
  --namespace skylight-wifi \
  --set secrets.existingSecret=skylight-wifi-automation \
  --set-file config.unifi_tls_ca=console.pem
```

`files/unifi.local.example.crt` is a generated certificate for `unifi.local`. It is only for a smoke test. The first start imports it into the database, and the UI then owns that config.

Prefer `secrets.existingSecret`. If you set `secrets.encryptionKey` or the other inline fields, Helm stores those values in the release Secret as well as the application Secret.

With `CONFIG_SOURCE=db` (the default) the ConfigMap is imported **once**, when the database has no config. Later changes to `config` in this chart do not update the database. Change settings in the UI. A pod annotation checksum still rolls the pod when the rendered ConfigMap changes.

`config.dry_run` defaults to `true`. The process logs the action it would take and does not pause or resume objects.

## First start

1. Port-forward or open the Ingress: `http://<host>:8000/setup`.
2. Create the admin. The password must be at least 12 characters.
3. Remove `ADMIN_SETUP_TOKEN` from the Secret. If the Secret is external, bump `secrets.rolloutToken` so the pod restarts.
4. Sign in. `/setup` returns 404 once an admin exists.

Plain HTTP, including a NodePort without TLS, needs `env.cookieSecure=false`. Behind HTTPS, leave it true and set `env.trustedProxies` to the proxy addresses. The app rejects `*`, `0.0.0.0/0`, and `::/0`.

Liveness, readiness, and startup probes call `GET /healthz`. That handler does not contact Skylight or UniFi.

```bash
helm test skylight-wifi-automation --namespace skylight-wifi
```

## Backup

`backup.enabled` adds a CronJob. The default schedule is `15 3 * * *` (03:15). Set `backup.timeZone` (for example `Etc/UTC`) on Kubernetes 1.27 or newer. Older clusters omit the field and use the controller's timezone.

The job runs `files/backup.py` from the same image. The script copies `app.db` with the sqlite3 backup API, runs `PRAGMA integrity_check` on the copy, and prints:

```text
backup ok file=... bytes=... kept=...
```

It keeps `backup.retention` files (default 7). The data volume is mounted read-write so SQLite can use the WAL shared-memory file. The script does not run `INSERT`, `UPDATE`, or `DELETE` against the source. `backup.pinToAppNode` places the job on the app's node, which a ReadWriteOnce volume needs. The volume plugin must allow two pods on that node to mount the claim. A deny-all NetworkPolicy selects only the backup pods.

On a StorageClass with `WaitForFirstConsumer`, the backups claim stays Pending until the first backup pod exists. `helm upgrade --wait` waits for that claim. Wait on the Deployment, or start one backup Job, instead of waiting on the whole release.

`backup.successfulJobsHistoryLimit` and `backup.failedJobsHistoryLimit` are Kubernetes Job history. They are not the number of backup files.

To restore, scale the Deployment to 0, copy one `app-*.db` over `$DATA_DIR/app.db`, delete `app.db-wal` and `app.db-shm`, and scale back to 1. See the repository README.

## NetworkPolicy

Off by default. When enabled, ingress to the UI port is limited to `networkPolicy.ui`, `networkPolicy.probeCidrs`, and (by default) the helm test pod. Ingress to port 8080 is limited to `networkPolicy.metrics`. Egress allows all destinations so the process can reach DNS, Skylight, and the gateway. Set `networkPolicy.egress.allowAll` to `false` and supply `networkPolicy.egress.rules` to narrow it.

For a NodePort whose policy matches client CIDRs, set `service.externalTrafficPolicy` to `Local`. The default `Cluster` policy replaces the client address with the node address.

Some CNIs apply the policy to kubelet probes. If the pod never becomes Ready, add the node CIDR to `networkPolicy.probeCidrs`.

## Examples

| File | What it shows |
| --- | --- |
| [examples/minimal.yaml](examples/minimal.yaml) | Existing Secret, otherwise the defaults |
| [examples/nodeport-with-backup.yaml](examples/nodeport-with-backup.yaml) | NodePort, plain HTTP, nightly backup, NetworkPolicy. Addresses are RFC 5737 documentation ranges |
| [examples/ingress.yaml](examples/ingress.yaml) | Ingress and TLS for `wifi.example.com` |

`ci/all-features-values.yaml` renders every optional resource for schema checks. The strings in it are not keys. Do not install it.

## Security context

The pod runs as uid 10001, which is the image user, with `runAsNonRoot`, a read-only root filesystem, all capabilities dropped, and `seccompProfile` `RuntimeDefault`. `fsGroup` 10001 lets that user write the data volume. `/tmp` is an emptyDir because the image sets `HOME=/tmp`. The ServiceAccount token is not mounted. The app does not call the Kubernetes API.

There is no CPU limit. Argon2 password hashing can spike, and a limit stalls sign-in. Memory defaults to a 128Mi request and a 384Mi limit.

## Values

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| affinity | object | `{}` | Affinity for the app pod. Backup affinity is separate; see backup.affinity. |
| backup.affinity | object | `{}` | Affinity for the backup pod. When set, pinToAppNode is not applied. |
| backup.backoffLimit | int | `2` | Job backoffLimit. |
| backup.concurrencyPolicy | string | `"Forbid"` | concurrencyPolicy. Forbid avoids two backups at once. |
| backup.enabled | bool | `false` | Create the CronJob, its script ConfigMap, a backups volume, and a deny-all NetworkPolicy. |
| backup.failedJobsHistoryLimit | int | `1` | failedJobsHistoryLimit. |
| backup.persistence.accessMode | string | `"ReadWriteOnce"` | Access mode for the backups claim. |
| backup.persistence.annotations | object | `{}` | Annotations on a chart-created backups claim. |
| backup.persistence.existingClaim | string | `""` | Use this existing PersistentVolumeClaim for backups. |
| backup.persistence.size | string | `"1Gi"` | Size of a chart-created backups claim. |
| backup.persistence.storageClass | string | `""` | StorageClass for the backups claim. Empty uses the cluster default. "-" sets an empty storageClassName. |
| backup.pinToAppNode | bool | `true` | Require the backup pod to run on the same node as the app so a ReadWriteOnce data volume can be mounted twice. |
| backup.resources | object | `{"limits":{"memory":"128Mi"},"requests":{"cpu":"20m","memory":"64Mi"}}` | Backup container resources. |
| backup.retention | int | `7` | How many backup files to keep on the backups volume. |
| backup.schedule | string | `"15 3 * * *"` | Cron schedule. |
| backup.startingDeadlineSeconds | int | `600` | startingDeadlineSeconds. Empty omits the field. |
| backup.successfulJobsHistoryLimit | int | `3` | successfulJobsHistoryLimit. This is Kubernetes job history, not backup file retention. |
| backup.suspend | bool | `false` | CronJob suspend flag. |
| backup.timeZone | string | `""` | CronJob timeZone. Empty omits the field. Etc/UTC requires Kubernetes 1.27+. |
| backup.ttlSecondsAfterFinished | int | `86400` | ttlSecondsAfterFinished for the Job. Empty omits the field. |
| commonAnnotations | object | `{}` | Annotations applied to every chart resource. |
| commonLabels | object | `{}` | Labels applied to every chart resource. |
| config.children | list | `[{"cutoff":"20:30","manual_unblock":false,"name":"child-a","object_name":"No Internet - child-a","skylight_profile":"Child A"}]` | Children. Names, Skylight profile labels, and Object Manager object names must each be unique. cutoff is local HH:MM. |
| config.dry_run | bool | `true` | true makes zero UniFi object writes. The latch file is still written. Leave true until a single object has been checked by hand. |
| config.empty_day_unlocks | bool | `false` | false keeps a day with no chores blocked. |
| config.include_late | bool | `true` | Count overdue Skylight tasks. |
| config.include_routines | bool | `true` | Count routines. |
| config.poll_seconds | int | `60` | Seconds between polls. The app accepts 30 through 3600. |
| config.timezone | string | `"America/Chicago"` | IANA timezone used for the latch day and cutoffs. |
| config.unifi_site | string | `""` | Optional site UUID, internalReference, or name. Empty omits the key and the app discovers a one-site gateway. |
| config.unifi_tls_ca | string | `"-----BEGIN CERTIFICATE-----\n<paste your console certificate>\n-----END CERTIFICATE-----\n"` | PEM console certificate. This placeholder is rejected on purpose. |
| config.unifi_url | string | `"https://unifi.local/proxy/network/integration/v1"` | Integration API base URL. https, and the host must match the console certificate. |
| enableServiceLinks | bool | `false` | Inject Service environment variables. The app does not read them. |
| env.argon2Concurrency | int | `2` | ARGON2_CONCURRENCY. How many password hashes may run at once. |
| env.configPath | string | `"/config/config.yaml"` | CONFIG_PATH. The ConfigMap is mounted at this file's directory. |
| env.configSource | string | `"db"` | CONFIG_SOURCE. db imports the ConfigMap once, then the UI owns config. file keeps the file as the only source and the UI will not save settings. |
| env.cookieSecure | bool | `true` | COOKIE_SECURE. Set false for plain HTTP (a NodePort or port-forward without TLS). Leave true behind HTTPS. |
| env.dataDir | string | `"/data"` | DATA_DIR. Must be the mount path of the data volume. The database is $DATA_DIR/app.db. |
| env.latchPath | string | `""` | LATCH_PATH. Empty uses <dataDir>/latch.json so today's unlock survives a restart. |
| env.logLevel | string | `"INFO"` | LOG_LEVEL passed to Python logging. |
| env.trustedProxies | string | `""` | TRUSTED_PROXIES. Comma-separated proxy IPs or CIDRs allowed to set X-Forwarded-For. Empty ignores the header. The process rejects *, 0.0.0.0/0, and ::/0. |
| env.uiPort | int | `8000` | UI_PORT. Must not be 8080; the metrics server always binds 8080. |
| extraEnv | list | `[]` | Extra environment variables. Appended after the chart's own variables. Use this for APP_ENCRYPTION_KEY_NEW only when you are not using the secret fields below. |
| extraVolumeMounts | list | `[]` | Extra volume mounts on the app container. |
| extraVolumes | list | `[]` | Extra pod volumes. |
| fullnameOverride | string | `""` | Override the full name used in resource names. Truncated to 63 characters. |
| hostAliases | list | [] | hostAliases for the pod. Use this when unifi_url's host is unifi.local and the cluster DNS does not resolve it. |
| image.digest | string | `""` | Optional image digest (sha256:...). When set, the tag is ignored. |
| image.pullPolicy | string | `"IfNotPresent"` | Pull policy. IfNotPresent matches an immutable appVersion tag. |
| image.repository | string | `"ghcr.io/dmcanally/skylight-wifi-automation"` | Container repository. Published by .github/workflows/image.yaml. |
| image.tag | string | `""` | Image tag. Empty uses the chart appVersion. |
| imagePullSecrets | list | `[]` | Secrets used to pull a private image. Each item is {name: pull-secret}. |
| ingress.annotations | object | `{}` | Annotations on the Ingress. |
| ingress.className | string | `""` | IngressClass name. Empty omits ingressClassName. |
| ingress.enabled | bool | `false` | Create an Ingress for the UI Service. Terminate TLS at the ingress. Leave COOKIE_SECURE true, and set env.trustedProxies to the ingress controller addresses. |
| ingress.hosts | list | `[{"host":"wifi.example.com","paths":[{"path":"/","pathType":"Prefix"}]}]` | Hosts and paths. The sample host is example.com placeholder data. |
| ingress.tls | list | `[]` | TLS entries. Each item is {secretName, hosts}. |
| metrics.annotations | object | `{}` | Annotations on the ClusterIP metrics Service. Add prometheus.io scrape annotations here if you are not using a ServiceMonitor. |
| metrics.labels | object | `{}` | Extra labels on the metrics Service. |
| nameOverride | string | `""` | Override the chart name used in resource names and labels. |
| networkPolicy.allowHelmTest | bool | `true` | Allow the helm test pod (component=test) to open the UI port. |
| networkPolicy.egress.allowAll | bool | `true` | Allow all egress. false with an empty rules list denies all egress. |
| networkPolicy.egress.rules | list | `[]` | Egress rules used when allowAll is false. Raw NetworkPolicy egress entries. |
| networkPolicy.enabled | bool | `false` | Create a NetworkPolicy for the app pod. |
| networkPolicy.metrics.cidrs | list | `[]` | CIDRs allowed to open port 8080. |
| networkPolicy.metrics.namespaceSelectors | list | `[]` | namespaceSelectors allowed to open port 8080. A monitoring namespace label is the usual choice. |
| networkPolicy.metrics.podSelectors | list | `[]` | podSelectors allowed to open port 8080. |
| networkPolicy.probeCidrs | list | `[]` | Extra CIDRs allowed to open the UI port. Use this for kubelet or node CIDRs when probes are blocked. |
| networkPolicy.ui.cidrs | list | `[]` | CIDRs allowed to open the UI port. Use documentation or your own LAN ranges, for example 192.0.2.0/24. |
| networkPolicy.ui.namespaceSelectors | list | `[]` | namespaceSelectors allowed to open the UI port. |
| networkPolicy.ui.podSelectors | list | `[]` | podSelectors allowed to open the UI port. Each item is a LabelSelector. |
| nodeSelector | object | `{}` | Node selector for the app pod. The backup CronJob uses the same selector. |
| persistence.accessMode | string | `"ReadWriteOnce"` | Access mode. ReadWriteOnce is correct for SQLite. The backup pod mounts the same claim, so the volume plugin must allow a second mount on the app's node. |
| persistence.annotations | object | `{}` | Annotations on a chart-created data claim. |
| persistence.enabled | bool | `true` | Create or attach a data volume. false uses an emptyDir and the database is lost when the pod is rescheduled. Backup requires true. |
| persistence.existingClaim | string | `""` | Use this existing PersistentVolumeClaim instead of creating one. |
| persistence.size | string | `"1Gi"` | Requested size for a chart-created claim. |
| persistence.storageClass | string | `""` | StorageClass name. Empty uses the cluster default. "-" sets storageClassName to "" so no dynamic provisioner runs. |
| podAnnotations | object | `{}` | Extra annotations on the app pod, merged after the config and secret checksums. |
| podLabels | object | `{}` | Extra labels on the app pod. Do not override app.kubernetes.io/component. |
| podSecurityContext | object | `{"fsGroup":10001,"fsGroupChangePolicy":"OnRootMismatch","runAsGroup":10001,"runAsNonRoot":true,"runAsUser":10001,"seccompProfile":{"type":"RuntimeDefault"}}` | Pod securityContext. uid/gid 10001 matches the image user. fsGroup makes the data volume writable by that user. |
| probes.liveness.enabled | bool | `true` | Enable the liveness probe. |
| probes.liveness.failureThreshold | int | `3` | failureThreshold. |
| probes.liveness.initialDelaySeconds | int | `10` | initialDelaySeconds. Ignored until the startup probe succeeds when that probe is enabled. |
| probes.liveness.periodSeconds | int | `20` | periodSeconds. |
| probes.liveness.successThreshold | int | `1` | successThreshold. Kubernetes requires 1 for liveness. |
| probes.liveness.timeoutSeconds | int | `5` | timeoutSeconds. |
| probes.readiness.enabled | bool | `true` | Enable the readiness probe. |
| probes.readiness.failureThreshold | int | `3` | failureThreshold. |
| probes.readiness.initialDelaySeconds | int | `5` | initialDelaySeconds. |
| probes.readiness.periodSeconds | int | `10` | periodSeconds. |
| probes.readiness.successThreshold | int | `1` | successThreshold. |
| probes.readiness.timeoutSeconds | int | `3` | timeoutSeconds. |
| probes.startup.enabled | bool | `true` | Enable the startup probe. |
| probes.startup.failureThreshold | int | `30` | failureThreshold. 30 * 5s gives the first Skylight login about 150s. |
| probes.startup.initialDelaySeconds | int | `0` | initialDelaySeconds. Zero omits the field. |
| probes.startup.periodSeconds | int | `5` | periodSeconds. |
| probes.startup.successThreshold | int | `1` | successThreshold. |
| probes.startup.timeoutSeconds | int | `3` | timeoutSeconds. |
| resources | object | `{"limits":{"memory":"384Mi"},"requests":{"cpu":"50m","memory":"128Mi"}}` | Container resources. Memory matches a small single-process SQLite workload. No CPU limit, so Argon2 password hashing is not throttled mid-login. |
| revisionHistoryLimit | int | `3` | How many old ReplicaSets to keep. |
| secrets.encryptionKey | string | `""` | Inline APP_ENCRYPTION_KEY. Leave empty when using existingSecret. |
| secrets.encryptionKeyNew | string | `""` | Inline APP_ENCRYPTION_KEY_NEW, used only while running skylight-wifi rotate-key. |
| secrets.existingSecret | string | `""` | Name of a Secret created outside this chart. When set, the inline values below are ignored. |
| secrets.keys | object | `{"encryption":"APP_ENCRYPTION_KEY","encryptionNew":"APP_ENCRYPTION_KEY_NEW","setupToken":"ADMIN_SETUP_TOKEN","skylightEmail":"SKYLIGHT_EMAIL","skylightPassword":"SKYLIGHT_PASSWORD","unifiApiKey":"UNIFI_API_KEY"}` | Key names inside the Secret, whether the chart creates it or you do. |
| secrets.keys.encryption | string | `"APP_ENCRYPTION_KEY"` | Key for the encryption key. |
| secrets.keys.encryptionNew | string | `"APP_ENCRYPTION_KEY_NEW"` | Key for the replacement encryption key. |
| secrets.keys.setupToken | string | `"ADMIN_SETUP_TOKEN"` | Key for the setup token. |
| secrets.keys.skylightEmail | string | `"SKYLIGHT_EMAIL"` | Key for the Skylight email. |
| secrets.keys.skylightPassword | string | `"SKYLIGHT_PASSWORD"` | Key for the Skylight password. |
| secrets.keys.unifiApiKey | string | `"UNIFI_API_KEY"` | Key for the UniFi API key. |
| secrets.rolloutToken | string | `""` | Optional string added to the pod template as an annotation. Bump it to roll the pod after an external Secret changes. |
| secrets.setupToken | string | `""` | Inline ADMIN_SETUP_TOKEN. |
| secrets.skylightEmail | string | `""` | Inline SKYLIGHT_EMAIL. |
| secrets.skylightPassword | string | `""` | Inline SKYLIGHT_PASSWORD. |
| secrets.unifiApiKey | string | `""` | Inline UNIFI_API_KEY. Sent by the app only as the X-API-KEY header. |
| securityContext | object | `{"allowPrivilegeEscalation":false,"capabilities":{"drop":["ALL"]},"readOnlyRootFilesystem":true,"seccompProfile":{"type":"RuntimeDefault"}}` | Container securityContext. The image is built to run with a read-only root filesystem. |
| service.annotations | object | `{}` | Annotations on the UI Service. |
| service.externalTrafficPolicy | string | `""` | externalTrafficPolicy. Set Local on a NodePort when a NetworkPolicy matches client CIDRs; Cluster SNATs the source to the node. |
| service.labels | object | `{}` | Extra labels on the UI Service. |
| service.nodePort | string | `nil` | Fixed nodePort for the UI. Used when type is NodePort or LoadBalancer. Empty lets Kubernetes assign one. |
| service.port | int | `8000` | Service port for the UI. The container still listens on env.uiPort. |
| service.type | string | `"ClusterIP"` | Service type for the UI. ClusterIP, NodePort, or LoadBalancer. |
| serviceAccount.annotations | object | `{}` | Annotations on the ServiceAccount. |
| serviceAccount.automountServiceAccountToken | bool | `false` | Mount a token into the pod. Leave false. |
| serviceAccount.create | bool | `true` | Create a ServiceAccount. The app does not call the Kubernetes API. |
| serviceAccount.name | string | `""` | ServiceAccount name. Empty uses the release fullname. |
| serviceMonitor.annotations | object | `{}` | Annotations on the ServiceMonitor. |
| serviceMonitor.enabled | bool | `false` | Create a monitoring.coreos.com ServiceMonitor for the metrics Service. Requires the Prometheus Operator CRDs. |
| serviceMonitor.honorLabels | bool | `false` | honorLabels. |
| serviceMonitor.interval | string | `"30s"` | Scrape interval. |
| serviceMonitor.labels | object | `{}` | Labels on the ServiceMonitor. Set this so your Prometheus selects it. |
| serviceMonitor.metricRelabelings | list | `[]` | metricRelabelings. |
| serviceMonitor.namespace | string | `""` | Namespace for the ServiceMonitor. Empty uses the release namespace. |
| serviceMonitor.path | string | `"/metrics"` | Metrics path. |
| serviceMonitor.relabelings | list | `[]` | relabelings. |
| serviceMonitor.scheme | string | `"http"` | Scrape scheme. The metrics port is plain HTTP. |
| serviceMonitor.scrapeTimeout | string | `"10s"` | Scrape timeout. |
| serviceMonitor.tlsConfig | object | `{}` | Optional tlsConfig. Empty omits the field. |
| terminationGracePeriodSeconds | int | `30` | Seconds to wait for the process to exit on SIGTERM. |
| tmp | object | `{"sizeLimit":"64Mi"}` | emptyDir mounted at /tmp. The image sets HOME=/tmp. The daily latch is not stored here. |
| tmp.sizeLimit | string | `"64Mi"` | Size limit for the /tmp emptyDir. |
| tolerations | list | `[]` | Tolerations for the app pod. The backup CronJob uses the same tolerations. |
