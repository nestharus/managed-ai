# Local Docker Control Surface (`deploy/local/`)

This local stack runs Managed AI Information behind an explicit IP-hiding internal Docker bridge network (`managed_ai_information_internal`, `internal: true`). The `managed-ai-information` worker has **zero host port bindings**, runs non-root (`10001:10001`) with a read-only root filesystem and dropped capabilities, and stores persistent state in the named volume `managed_ai_information_store` (`/var/lib/managed-ai-information`).

Only the pinned non-root `gateway` container binds a host port, restricted strictly to loopback: `127.0.0.1:${MANAGED_AI_INFORMATION_CONTROL_PORT:-8480}:8080`.

## Bring Up

```bash
docker compose -f deploy/local/compose.yaml up -d --build
```

## Health Check & CLI Access

Verify the loopback control surface:

```bash
curl -fsS http://127.0.0.1:8480/healthz
```

> **Note on `/control/`:** `gateway.conf` reserves the `/control/` upstream proxy route (`http://managed-ai-information:8081/`) for future HTTP control-plane listeners. In the default CLI-exec local mode (`compose.yaml` keeps the worker idle via `signal.pause()` with no HTTP server listening on port `8081`), `GET /healthz` returns `200`, all other root paths return `404`, and `/control/` returns `502` until an explicit control service is attached.

Run Managed AI Information commands inside the isolated worker container:

```bash
docker compose -f deploy/local/compose.yaml exec managed-ai-information \
  managed-ai-information --root /var/lib/managed-ai-information/store init --goal-text "Local containerized store"

docker compose -f deploy/local/compose.yaml exec managed-ai-information \
  managed-ai-information --root /var/lib/managed-ai-information/store status --summary
```

## Store Backup/Restore vs. Derived Cache

| State | Path inside `/var/lib/managed-ai-information/store` | Handling |
| --- | --- | --- |
| **Authoritative store state** | `system.json`, `work/`, `exchange/objects/`, `exchange/packets/`, `exchange/attention/`, `exchange/cold-records/` | Back up while idle or under shared admission lock; restore verbatim before running `reindex`. |
| **Rebuildable derived index** | `exchange/index/head-index.gen*.bin`, `exchange/index/head-index.current.json`, `exchange/index/head-index.delta.jsonl` | Derived cache (`HeadIndex v3`). Safe to discard on restore and rebuild deterministically with `managed-ai-information --root /var/lib/managed-ai-information/store reindex`. |

Example volume backup (tarball outside the repository):

```bash
docker run --rm \
  -v managed-ai-information_managed_ai_information_store:/data:ro \
  -v "$PWD:/backup" \
  python:3.15.2-slim \
  tar -czf /backup/managed-ai-information-store-backup.tar.gz -C /data .
```

## Tear Down & Prune

Stop and remove containers/networks while **preserving** the `managed_ai_information_store` volume:

```bash
docker compose -f deploy/local/compose.yaml down
```

Remove containers, networks, **and** the local volume (destructive to local test state):

```bash
docker compose -f deploy/local/compose.yaml down -v
```
