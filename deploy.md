# Deployment

## Pipeline-Ablauf

```
git push main
    │
    ▼
GitHub Actions (.github/workflows/docker.yml)
    │
    ├─ Build multi-arch image (amd64 + arm64)
    ├─ Push → ghcr.io/mcfetz/pymon-server:latest
    │
    └─ POST https://portainer.familie-heise.de/api/webhooks/...
              │
              ▼
         Portainer pulls new image and restarts the container
```

Der Deploy-Schritt läuft nur bei echten Pushes (nicht bei Pull Requests).

## GitHub Secret einrichten

Die Portainer-Webhook-URL ist als Secret hinterlegt, damit sie nicht im
Klartext im Repository steht:

1. GitHub → Repository → **Settings** → **Secrets and variables** → **Actions**
2. **New repository secret**
   - Name: `PORTAINER_WEBHOOK_URL`
   - Value: `https://portainer.familie-heise.de/api/webhooks/<token>`
     (Token: Portainer → **Webhooks** → beim Service `pymon-server`)
3. Save

## Image

| Registry | Image |
|----------|-------|
| GitHub Container Registry | `ghcr.io/mcfetz/pymon-server` |

| Tag | Wann |
|-----|------|
| `latest` | Jeder Push auf `main` |
| `main` | Jeder Push auf `main` |
| `1.2.3` / `1.2` / `1` | Git-Tag `v1.2.3` |

## Portainer: Webhook einrichten

Die Services laufen als Swarm-Services (nicht als Portainer-Stacks). Der
Deploy-Webhook ist ein Portainer-**Service-Webhook** (Force-Update +
`:latest`-Repull):

1. Portainer → **Webhooks** → **Add webhook**
2. **Type** Service, **Environment** `local`, **Service** `pymon-server`
3. Die generierte URL als `PORTAINER_WEBHOOK_URL`-Secret in GitHub hinterlegen
   (siehe oben)

## Service anlegen (falls neu nötig)

Compose-Inhalt aus `docker-compose.yml` (Image-Zeile anpassen):

```yaml
services:
  pymon-server:
    image: ghcr.io/mcfetz/pymon-server:latest
    container_name: pymon-server
    restart: unless-stopped
    ports:
      - "5000:5000"
    volumes:
      - pymon-data:/data
    environment:
      PYMON_CORS_ORIGINS: "https://pymon.familie-heise.de"
      PYMON_FRONTEND_URL: "https://pymon.familie-heise.de"
      PYMON_ADMIN_PASSWORD: "sicheres-passwort"

volumes:
  pymon-data:
```

## Manueller Redeploy

```bash
curl -fsS -X POST \
  "https://portainer.familie-heise.de/api/webhooks/<token>"
```

## PostgreSQL backend

The default remains the SQLite file. A deployment can switch to a dedicated
PostgreSQL service without touching the existing `conf/` files.

### 1. Create the database service

Run this on the Swarm manager (docker13), pinned to the pymon node so the
local volume stays with the service:

```bash
docker service create \
  --name pymon-db \
  --network pymon \
  --constraint 'node.labels.app.pymon==1' \
  --replicas 1 \
  --mount type=volume,source=pymon-db-data,target=/var/lib/postgresql/data \
  --env POSTGRES_DB=pymon \
  --env POSTGRES_USER=pymon \
  --env POSTGRES_PASSWORD='<strong-password>' \
  --health-cmd 'pg_isready -U pymon -d pymon' \
  --health-interval 10s \
  --health-timeout 5s \
  --health-retries 10 \
  postgres:17-alpine
```

Keep the password out of git and shell history where possible. The database is
only reachable on the internal `pymon` overlay network.

### 2. Stop the app and copy once

The migration is intentionally one-shot. Stop `pymon-server`, then run the
migration in a temporary container against the existing `pymon-server` volume:

```bash
docker service scale pymon-server=0

docker run --rm --network pymon \
  -v pymon-server:/data \
  -e PYMON_DATA_DIR=/data \
  -e PYMON_DATABASE_URL='postgresql://pymon:<strong-password>@pymon-db:5432/pymon' \
  -e PYMON_MIGRATE_SQLITE_TO_PSQL=1 \
  -e PYMON_MIGRATE_FAST=1 \
  ghcr.io/mcfetz/pymon-server:latest \
  python -c 'from psql_migration import run_sqlite_to_psql_migration; run_sqlite_to_psql_migration()'
```

The fast path streams `metrics` with PostgreSQL `COPY`; small tables are copied
in batches. It refuses to run if the target already contains metrics. The
source SQLite volume is left untouched for rollback.

If the metrics copy completed but a later step failed (for example an idle TCP
connection during the small-table copy), verify that the target metric count
matches `_db_stats` in SQLite and finish with:

```bash
-e PYMON_MIGRATE_RESUME=1
```

Resume skips the already-copied metrics and reference tables, requiring the
target to have matching metric rows and no note/alarm/audit rows yet.

### 3. Start the app on PostgreSQL

```bash
docker service update \
  --env-add 'PYMON_DATABASE_URL=postgresql://pymon:<strong-password>@pymon-db:5432/pymon' \
  --replicas 1 \
  pymon-server
```

Do **not** set `PYMON_MIGRATE_SQLITE_TO_PSQL=1` on the long-running service;
it is only for the one-off copy.

### 4. Verify

- `GET /api/admin/maintenance/stats` reports the migrated metric count
- A metric query, alarm list and metric note work
- New metric notes and audit entries get fresh IDs (sequences are repaired)
- `docker service logs -f pymon-server` has no migration/DB errors

### Rollback

```bash
docker service update --env-rm PYMON_DATABASE_URL pymon-server
```

This returns to SQLite. Any rows written to PostgreSQL after the switch are not
in SQLite, so decide the rollback window before starting the migration.
