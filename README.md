# Skylight Wi-Fi Automation

Private household service that keeps each child's Google Wi-Fi devices paused until all of
their Skylight chores, routines, and overdue tasks are complete. A skipped task counts as
satisfied. A day with no tasks unlocks automatically.

## Household rules

| Child | Devices | Daily cutoff |
| --- | ---: | ---: |
| Lanie | 4 | 9:00 PM America/Chicago |
| Libby | 6 | 9:00 PM America/Chicago |
| Tucker | 3 | 8:00 PM America/Chicago |

At and after cutoff, devices are paused even if all tasks are complete. Before cutoff, all
configured devices are unpaused only when every applicable task is complete or skipped.

## Safety

The checked-in configuration is `dry_run: true` and contains no device IDs or credentials.
The service refuses a partially populated child device list, avoids duplicate state changes,
and leaves the current Wi-Fi state untouched whenever either upstream API fails.

Both upstream interfaces are unofficial and may change. `pyskylight` is reverse-engineered;
the Google Wi-Fi client is an alpha library last published in 2023. Validate both before
turning off dry-run mode.

## Local compatibility probe

1. Copy `config.example.yaml` to `config.yaml`.
2. Set `SKYLIGHT_EMAIL`, `SKYLIGHT_PASSWORD`, and `GOOGLE_WIFI_REFRESH_TOKEN` in your shell.
3. Install with `python -m pip install -e '.[dev]'`.
4. List Google devices without changing them:
   `skylight-wifi --config config.yaml --discover-google`
5. Put exactly 4 Lanie IDs, 6 Libby IDs, and 3 Tucker IDs into `config.yaml`.
6. Run one complete dry-run poll:
   `skylight-wifi --config config.yaml --once`
7. Verify the decisions in logs before setting `dry_run: false`.

Never paste credentials into an issue, commit, container image, or chat. Store them in a
Kubernetes Secret or your cluster's existing secret manager. Do not include phones, routers,
cluster nodes, the Skylight, or other household infrastructure in a child's device list.

## Kubernetes

The manifests under `k8s/` run one replica with a read-only filesystem and all Linux
capabilities removed. Create the Secret out of band; `secret.example.yaml` is only a schema
and must never be edited with real values or committed.

The deployment starts in dry-run mode. After discovery and a successful single-device test,
add the verified device IDs and change `dry_run` to `false` in the ConfigMap.

The repository publishes a private image to GitHub Container Registry after pushes to `main`.
Your cluster will need an image-pull secret authorized for the private package, unless you
deliberately change the package visibility.
