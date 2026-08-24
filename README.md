# Skylight Wi-Fi Automation

Private household service that keeps each child's Google Wi-Fi devices paused until all of
their Skylight chores, routines, and overdue tasks are complete. A skipped task counts as
satisfied. A day with no tasks unlocks automatically.

## Household rules

| Child | Family Wi-Fi group | Daily cutoff |
| --- | --- | ---: |
| Lanie | Lanie | 9:00 PM America/Chicago |
| Libby | Libby | 9:00 PM America/Chicago |
| Tucker | Tucker | 8:00 PM America/Chicago |

At and after cutoff, the child's Family Wi-Fi group is paused even if all tasks are complete.
Before cutoff, the group is unpaused only when every applicable task is complete or skipped.
Membership remains owned by Google Home; adding or removing a device from a group requires no
automation configuration change. The private Google API currently enforces group state on the
group's resolved stations, but station identities never appear in application configuration.

## Safety

The checked-in configuration is `dry_run: true` and contains no device IDs or credentials.
The service resolves the named Family Wi-Fi groups on every poll, avoids duplicate state
changes, and leaves the current Wi-Fi state untouched whenever either upstream API fails.

Both upstream interfaces are unofficial and may change. `pyskylight` is reverse-engineered;
the Google Wi-Fi client is an alpha library last published in 2023. Validate both before
turning off dry-run mode.

## Local compatibility probe

1. Copy `config.example.yaml` to `config.yaml`.
2. Set `SKYLIGHT_EMAIL`, `SKYLIGHT_PASSWORD`, and `GOOGLE_WIFI_REFRESH_TOKEN` in your shell.
3. Install with `python -m pip install -e '.[dev]'`.
4. List Google Family Wi-Fi groups without changing them:
   `skylight-wifi --config config.yaml --discover-google`
5. Confirm the `Lanie`, `Libby`, and `Tucker` groups are found.
6. Run one complete dry-run poll:
   `skylight-wifi --config config.yaml --once`
7. Verify the decisions in logs before setting `dry_run: false`.

Never paste credentials into an issue, commit, container image, or chat. Store them in a
Kubernetes Secret or your cluster's existing secret manager. Google Home remains the sole
place where Family Wi-Fi membership is managed.

## Kubernetes

The manifests under `k8s/` run one replica with a read-only filesystem and all Linux
capabilities removed. Create the Secret out of band; `secret.example.yaml` is only a schema
and must never be edited with real values or committed.

The deployment starts in dry-run mode. After group discovery and a successful Family Wi-Fi
group test, change `dry_run` to `false` in the ConfigMap.

The repository publishes private `latest`, immutable commit, and Flux-compatible semantic
version image tags to GitHub Container Registry after pushes to `main`.
Your cluster will need an image-pull secret authorized for the private package, unless you
deliberately change the package visibility.
