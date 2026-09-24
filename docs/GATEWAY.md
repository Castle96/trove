# API Gateway — `/gw/<slug>` proxying, consumers, keys, rate limits, TLS

Trove ships a small Kong-style gateway. The control-plane API lives under
`/api/gateway` (`app/api/routes_gateway.py`), while the hot path lives in
`app/api/gateway_proxy.py` at `GET/POST/PUT/PATCH/DELETE/HEAD /gw/{slug}/{path}`.

## Model

```
Gateway (slug, base_url, tls_*)  ── children ──  GatewayRoute (path, upstream_url, methods,
    │                                              auth_mode, rate_limit_rpm, timeout_ms, ...)
    └─ GatewayConsumer (name, enabled)
         └─ GatewayApiKey (hashed, prefix, label, expires_at)
    └─ GatewayLog  (per-request rows)
```

- A **gateway** is a named collection with a unique `slug` (it is the URL
  prefix: `/gw/<slug>/...`).
- **Routes** match a path pattern (`/v1/*`, `/*`, ...) against an
  `upstream_url`; `strip_prefix` removes the matched prefix before forwarding.
- **Consumers** own **API keys**. Keys are stored as SHA-256 hashes; the
  plaintext is returned exactly once at creation.
- **Rate limits** are per-route (requests per minute) and keyed by API key (or
  IP for open routes). Over-limit requests get `429` + `Retry-After`.
- **TLS** — a gateway can be given a `tls_domain`; `issue-tls` / `renew-tls`
  run the shared PKI pipeline to mint its certificate (state machine:
  `none` → `pending` → `valid` | `error`). Set `issuer` to pick the subject
  (e.g. "Let's Encrypt" or the local CA).

## Authentication on the hot path

The hot path performs its **own** auth using the route's `auth_mode`:

- `open` — no credential required.
- `api_key` — `X-API-Key: <key>` or `Authorization: Bearer <key>`. The key
  must belong to a consumer and be enabled, unexpired, and within the route's
  rate limit. Results: `401` unknown/disabled/expired key, `403` disabled key,
  `429` rate limited, `404` unknown gateway/route, `502` unreachable upstream,
  `504` route timeout.

The hot path is **not** protected by the control-plane bearer token — that is
intentional, so external services can call your apps through the gateway with
only their gateway API key.

## Creating a gateway

```bash
# control plane needs a bearer token (or run in open mode)
BASE=http://localhost:8000
curl -s -X POST $BASE/api/gateway -H 'Content-Type: application/json' \
  -d '{"slug":"shop","name":"Shop API","enabled":true}' > gw.json
GID=$(python -c 'import json,sys;print(json.load(open("gw.json"))["id"])')

curl -s -X POST $BASE/api/gateway/$GID/routes -H 'Content-Type: application/json' \
  -d '{"name":"v1","methods":["GET"],"path":"/v1/*","upstream_url":"http://10.0.0.20:8000","auth_mode":"api_key","rate_limit_rpm":300,"timeout_ms":5000,"strip_prefix":false,"enabled":true}' \
  > route.json

curl -s -X POST $BASE/api/gateway/$GID/consumers -H 'Content-Type: application/json' \
  -d '{"name":"MyApp"}' > consumer.json
CID=$(python -c 'import json,sys;print(json.load(open("consumer.json"))["id"])')
curl -s -X POST $BASE/api/gateway/consumers/$CID/keys -H 'Content-Type: application/json' \
  -d '{"label":"prod"}' > key.json   # key.json["key"] is shown once

# call it
curl -s -H "X-API-Key: $(python -c 'import json;print(json.load(open("key.json"))["key"])')" \
  $BASE/gw/shop/v1/hello
```

## Observability

- `GET /api/gateway/logs?gateway_id=…` — recent proxied requests (newest
  first, `limit` 1-500), `DELETE` to clear.
- `POST /api/gateway/test` — fire a direct request from the console
  (`{method, url, timeout_ms}`) and see status/latency/reason without touching
  gateway state.

## Notes & gotchas

- Deleting a gateway/consumer explicitly removes child rows and invalidates the
  key hash cache — SQLite does not enforce foreign-key cascades, so the proxy
  would otherwise keep accepting orphaned keys.
- Route `timeout_ms` → upstream timeout (1054 style: `Gateway Timeout` 504).
- Key hashes are cached in-process for the hot path; `rotate`, `delete` and
  `update` call `gateway_service.invalidate_key()` so changes apply instantly.
- The public hot path is registered before the SPA mount so `/gw/*` never
  falls through to the dashboard.
