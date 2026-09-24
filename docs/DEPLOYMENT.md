# Certificate deployment

Pushes issued certificate material to the hosts that actually serve it (reverse
proxies, ingress nodes, VPN boxes). A *deployment target* pins where each file
lands and what to run afterwards; every attempt is written to the audit
`deployment_records` table.

```mermaid
flowchart LR
    A[issue / renew] --> B{assigned? auto_deploy? enabled?}
    B -->|yes| C[build fullchain]
    C --> D[SSH/SFTP push]
    D --> E[reload command]
    E --> F[optional webhook]
    F --> G["deployment_record: success|failed|skipped"]
```

## Model

- **`DeploymentTarget`** — `name`, `host`, `port`, `ssh_user`, encrypted
  `ssh_private_key`/`ssh_password`, `cert_path`, `key_path`, `chain_path`,
  `reload_command`, `webhook_url`, `auto_deploy`, `enabled`.
- **`CertDeploymentTarget`** — many-to-many linking a certificate to targets.
- **`DeploymentRecord`** — one row per deploy attempt: `cert_id`, `target_id`,
  `serial` (which version was pushed), `state`, `detail`, `duration_ms`, timestamp.

Credentials are encrypted at rest via `app/services/key_store.py`
(AES-256-GCM) — SQLite only ever sees the encrypted blob, decrypted in-memory at
deploy time, and the read API never returns them (only
`ssh_key_configured`/`ssh_password_configured` flags).

## Transport

- **SSH/SFTP** (`asyncssh`) when SSH credentials are configured: cert, key and
  full chain are written as temp files and renamed into place (atomic), keys are
  chmod `0600`. An optional `reload_command` (e.g.
  `systemctl reload nginx`) runs via the shell after each successful push.
  A 30s per-operation timeout applies.
- **Webhook-only** — when only `webhook_url` is set, a JSON payload
  (`action: deploy`, cn, serial, cert, chain) is POSTed and the target is
  considered deployed. Use this for hosts you can't SSH to (e.g. CDNs,
  external load balancers).

> **Security notes for the homelab trade-off**: SSH host keys are not pinned
> (`known_hosts=None`) and the reload command is executed as the target SSH
> user, so **target endpoints must be admin-only** — they carry credentials and
> effectively allow remote code execution by design.

## When deployment runs

- **Auto** — after every successful `issue_cert` / `renew_cert`, assigned
  targets with `auto_deploy=true` and `enabled=true` are tried. Failures are
  recorded (never raised) and trigger an error notification via the existing
  webhook/ntfy/email channels.
- **Manual** — `POST /api/certs/{id}/deploy` redeploys to all assigned,
  enabled targets regardless of the `auto_deploy` toggle (operator+).
- **ACME caveat** — keys for ACME-issued certs stay with the CA and are not
  stored in Trove, so SSH deployment to those certs fails with a clear message;
  use a webhook-only target for them.

## API

| Method | Path | Notes |
|--------|------|-------|
| GET/POST | `/api/deployments` | list / create target (create = admin) |
| GET/PATCH/DELETE | `/api/deployments/{id}` | detail / update / delete (admin) |
| GET | `/api/certs/{id}/deployments` | assignments + last attempt per target |
| POST | `/api/certs/{id}/deployments` | assign a target (operator+) |
| DELETE | `/api/certs/{id}/deployments/{target_id}` | unassign (operator+) |
| POST | `/api/certs/{id}/deploy` | manual redeploy (operator+) |
| GET | `/api/certs/{id}/deployments/history` | deployment audit records |

Example target:

```json
{
  "name": "caddy-ingress",
  "host": "192.168.1.5",
  "port": 22,
  "ssh_user": "trove",
  "ssh_private_key": "-----BEGIN OPENSSH PRIVATE KEY-----\n...",
  "cert_path": "/etc/caddy/certs/host.crt",
  "key_path": "/etc/caddy/certs/host.key",
  "chain_path": "/etc/caddy/certs/chain.pem",
  "reload_command": "systemctl reload caddy",
  "auto_deploy": true
}
```

Then `POST /api/certs/{cert_id}/deployments` with `{"target_id": 1}` assigns it.
