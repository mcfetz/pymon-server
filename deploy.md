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
   - Value: `https://portainer.familie-heise.de/api/webhooks/090c565f-06b6-45d5-8af2-93be67ae5246`
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

## Portainer: Container einrichten

1. **Stacks → Add stack**
2. Compose-Inhalt aus `docker-compose.yml` einfügen (Image-Zeile anpassen):

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

3. **Webhook aktivieren**: Stack → **Webhooks** → Enable → die generierte URL
   als `PORTAINER_WEBHOOK_URL` Secret in GitHub hinterlegen (siehe oben).

## Manueller Redeploy

```bash
curl -fsS -X POST \
  "https://portainer.familie-heise.de/api/webhooks/090c565f-06b6-45d5-8af2-93be67ae5246"
```
