# SaltBox Side-by-Side Deployment Acceptance Matrix

> [!IMPORTANT]
> **Status**: `DRAFT — PENDING READ-ONLY HOST DISCOVERY AND SPECIALIZATION`
> Execute this verification protocol following deployment on the SaltBox host to confirm complete isolation and proper routing.

---

## Acceptance Verification Protocol

| # | Verification Dimension | Target Contract / Pass Condition | Test Command / Execution Method | Expected Output / Validation Proof |
| :-: | :--- | :--- | :--- | :--- |
| **1** | **Simultaneous Execution** | Both upstream Mylar and `mylar-modern` active simultaneously | `docker ps --filter "name=mylar"` | Both containers list status `Up` |
| **2** | **Unique Routing & Identities** | Distinct subdomains route to their respective backends | `curl -s -I https://<discovered-mylar-domain>/home`<br/>`curl -s -I https://${SUBDOMAIN}.${DOMAIN}/home` | Returns HTTP 200 (direct) or HTTP 302 (SSO redirect to Authelia/Authentik portal). **HTTP 404 or 502 indicates failure.** |
| **3** | **Database Separation** | Separate SQLite files in separate paths with distinct data | `stat <discovered-prod-appdata>/mylar.db`<br/>`stat ${APPDATA_DIR}/config/mylar.db` | Distinct inode numbers and independent file sizes |
| **4** | **Appdata Isolation** | Config and runtime files written only within `${APPDATA_DIR}/config` | `ls -la ${APPDATA_DIR}/config` | Contains independent `config.ini`, fresh `mylar.db`, and `logs/` |
| **5** | **Test Library Visibility** | Staging instance sees only `${TEST_COMICS_DIR}` | `docker exec mylar-modern ls -la /comics` | Lists only test comic fixtures; production files invisible |
| **6** | **Downloader Initial State** | Downloader integrations are explicitly disabled in initial staging | Check `config.ini` in `${APPDATA_DIR}/config`: `grep -E '^(nzb_downloader|torrent_downloader|enable_torznab|newznab) =' ${APPDATA_DIR}/config/config.ini` | `nzb_downloader = 3` (None), `enable_torznab = False`, `newznab = False`; no client API keys |
| **7** | **Deterministic Non-Root Identity** | PID 1 inside container is Python running as non-root UID/GID | `docker exec mylar-modern sh -c "tr '\0' ' ' < /proc/1/cmdline"`<br/>`docker exec mylar-modern grep -E '^(Uid|Gid):' /proc/1/status` | Process is `python3 ... Mylar.py`; `Uid` and `Gid` match `${PUID}` and `${PGID}` |
| **8** | **Health & Readiness** | Container health check reports healthy via Python standard library probe | `docker inspect --format '{{.State.Health.Status}}' mylar-modern` | Returns `healthy` |
| **9** | **Restart Persistence** | State, indexed creators, and configuration survive container restart | `docker compose -f docker-compose.yml restart`<br/>`docker inspect --format '{{.State.Health.Status}}' mylar-modern` | Returns `healthy`; SQLite tables intact |
| **10** | **Production Instance Safety** | Zero downtime or configuration mutation in production Mylar | `docker logs --tail 20 <discovered-mylar-container>`<br/>`curl -s -I https://<discovered-mylar-domain>/home` | Upstream log shows zero errors; response intact |
| **11** | **Recoverable Decommissioning** | System can be uninstalled safely to a dated quarantine location | `docker compose down`<br/>`tar -czvf /opt/mylar-modern-quarantine.tar.gz -C /opt mylar-modern`<br/>`mv /opt/mylar-modern /opt/mylar-modern-quarantined` | Staging data safely archived and quarantined before eventual deletion; production unaffected |

---

## Pass / Fail Guidelines

- **Immediate Blockers**: Any failure in Dimensions 3, 4, 5, 6, 7, or 10 constitutes an immediate deployment blocker requiring container halt (`docker compose down`).
- **HTTP Response Interpretation**: In Traefik environments with active SSO middleware (Authelia or Authentik), an HTTP `302 Found` redirecting to the login portal represents a **successful routing and authentication match**. HTTP `404 Not Found` or `502 Bad Gateway` indicates misconfigured labels or load balancer target ports.
