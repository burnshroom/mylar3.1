# SaltBox Preflight Checklist & Read-Only Host Discovery

> [!IMPORTANT]
> **Status**: `DRAFT — PENDING READ-ONLY HOST DISCOVERY AND SPECIALIZATION`
> Run the read-only script below on your SaltBox host to discover the exact environment facts needed to specialize `.env` and `docker-compose.yml`.

---

## 1. Required Host Parameters Checklist

| # | Parameter | Description | Discovered Host Value | Required For |
| :-: | :--- | :--- | :--- | :--- |
| **1** | `DOCKER_NETWORK` | External Docker network attached to Traefik (e.g. `saltbox`) | `[ ]` | Compose network attachment |
| **2** | `DOMAIN` | Primary SaltBox root domain (e.g. `yourdomain.tld`) | `[ ]` | Traefik Host routing rule |
| **3** | `SUBDOMAIN` | Staging subdomain (default: `mylar-modern`) | `[ ]` | Traefik Host routing rule |
| **4** | `TRAEFIK_HTTP_ENTRYPOINT` | Traefik entrypoint for HTTP (e.g. `web`) | `[ ]` | HTTP redirect router |
| **5** | `TRAEFIK_HTTPS_ENTRYPOINT` | Traefik entrypoint for HTTPS (e.g. `websecure`) | `[ ]` | HTTPS ingress router |
| **6** | `TRAEFIK_HTTP_MIDDLEWARES` | Middleware for HTTP redirect (e.g. `redirect-to-https@docker`) | `[ ]` | HTTP router middleware label |
| **7** | `TRAEFIK_HTTPS_MIDDLEWARES` | Middleware chain for secure HTTPS routing | `[ ]` | HTTPS router middleware label |
| **8** | `TRAEFIK_CERTRESOLVER` | Traefik ACME challenge provider (e.g. `cf` or `letsencrypt`) | `[ ]` | Traefik TLS resolver label |
| **9** | `TRAEFIK_TLS_OPTIONS` | Optional TLS options label (e.g. `securetls@file`) | `[ ]` | Optional TLS options label |
| **10** | `PUID` | Numeric UID of the `saltbox` user (e.g. `1000`) | `[ ]` | Non-root runtime identity |
| **11** | `PGID` | Numeric GID of the `saltbox` user (e.g. `1000`) | `[ ]` | Non-root runtime identity |
| **12** | `TZ` | Host timezone string (e.g. `America/New_York` or `Etc/UTC`) | `[ ]` | Container timezone |
| **13** | `HOST_PORT` | Optional loopback debug port (e.g. `8095` for `127.0.0.1`) | `[ ]` | Optional loopback override |

---

## 2. Sanitized Read-Only Host Discovery Script

> [!CAUTION]
> **User Privacy Review**: Before returning the output of this script, please review it to ensure that any hostnames, domain names, or internal path structures align with your privacy preferences.

```bash
#!/usr/bin/env bash
# ==============================================================================
# SaltBox Read-Only Preflight Discovery Script (Hardened / Zero Secrets)
# ==============================================================================
set -euo pipefail

echo "======================================================================"
echo "SALTBOX HOST READ-ONLY PREFLIGHT DISCOVERY"
echo "Timestamp: $(date -u +'%Y-%m-%dT%H:%M:%SZ')"
echo "======================================================================"

echo -e "\n[1] HOST IDENTITY & TIMEZONE"
echo "User:       $(id -un) (UID: $(id -u), GID: $(id -g))"
echo "Timezone:   $(timedatectl 2>/dev/null | grep 'Time zone' | awk '{print $3}' || cat /etc/timezone 2>/dev/null || date +%Z)"

echo -e "\n[2] DOCKER NETWORKS"
docker network ls --format 'table {{.Name}}\t{{.Driver}}\t{{.Scope}}' | grep -E 'saltbox|proxy|web|NAME' || true

echo -e "\n[3] RUNNING REVERSE PROXY & AUTH CONTAINERS"
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}' | grep -E 'traefik|authelia|authentik|NAMES' || true

echo -e "\n[4] CANDIDATE MYLAR CONTAINERS (Sanitized: Environment Variables Redacted)"
MYLAR_CANDIDATES=$(docker ps -a --format '{{.Names}}' | grep -iE 'mylar' || true)
if [ -n "$MYLAR_CANDIDATES" ]; then
    for c in $MYLAR_CANDIDATES; do
        echo "----------------------------------------------------------------------"
        echo "Candidate Container: $c"
        echo "Status: $(docker inspect "$c" --format '{{.State.Status}}')"
        echo "Image:  $(docker inspect "$c" --format '{{.Config.Image}}')"

        echo "--- Allowlisted Traefik & SaltBox Labels ---"
        docker inspect "$c" --format '{{range $k, $v := .Config.Labels}}{{println $k "=" $v}}{{end}}' | \
            grep -E '^(traefik\.http\.routers\..*\.(rule|entrypoints|middlewares|tls\.certresolver|tls\.options)|traefik\.http\.services\..*\.loadbalancer\.server\.port|traefik\.docker\.network|com\.github\.saltbox\.saltbox_managed)=' || echo "No matching allowlisted Traefik labels."

        echo "--- Container Mounts (Host Paths Disclosed) ---"
        docker inspect "$c" --format '{{range .Mounts}}{{println .Source " -> " .Destination " (" .Mode ")"}}{{end}}' || true

        echo "--- Attached Networks (IPs Redacted) ---"
        docker inspect "$c" --format '{{range $k, $v := .NetworkSettings.Networks}}{{println $k}}{{end}}' || true
    done
else
    echo "No candidate Mylar containers found on this host."
fi

echo -e "\n[5] TRAEFIK NETWORK ATTACHMENTS (Sanitized)"
TRAEFIK_NAME=$(docker ps --format '{{.Names}}' | grep -E '^traefik' | head -n1 || true)
if [ -n "$TRAEFIK_NAME" ]; then
    echo "Traefik Container: $TRAEFIK_NAME"
    echo "Traefik Networks:  $(docker inspect "$TRAEFIK_NAME" --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}')"
fi

echo -e "\n[6] CANDIDATE LOOPBACK PORT CHECKS (Checking 8090 & 8095 Only)"
if command -v ss >/dev/null 2>&1; then
    ss -tulpn | grep -E ':(8090|8095)\b' || echo "Ports 8090 and 8095 are free on all interfaces."
elif command -v netstat >/dev/null 2>&1; then
    netstat -tulpn | grep -E ':(8090|8095)\b' || echo "Ports 8090 and 8095 are free on all interfaces."
fi

echo -e "\n[7] APPDATA BASE DIRECTORY CHECK"
for d in /opt /opt/appdata /opt/saltbox; do
    if [ -d "$d" ]; then
        echo "Found Directory: $d (Owner: $(stat -c '%U:%G (%a)' "$d" 2>/dev/null || stat -f '%u:%g (%p)' "$d"))"
    fi
done

echo -e "\n======================================================================"
echo "END OF PREFLIGHT DISCOVERY"
echo "======================================================================"
```
