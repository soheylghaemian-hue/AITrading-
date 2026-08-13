# Secure public read-only Dashboard API (Phase 6)

Target architecture — the browser NEVER talks to IBKR, and IB Gateway (port 4002) is NEVER public:

```
Browser → gigbay.de (Vercel/Next.js)
        → same-origin /api/dashboard/*  (Vercel SERVER proxy, injects read token)
        → Cloudflare Tunnel (HTTPS, authenticated)
        → private Dashboard API  127.0.0.1:8000   (read-only, paper, no execution)
        → Risk Engine / read-model
        → IB Gateway  127.0.0.1:4002   (localhost only)
        → IBKR
```

Secrets live only server-side: the read token is a **Vercel server env** (`DASHBOARD_API_READ_TOKEN`,
never `NEXT_PUBLIC_*`) and the backend env (`ATP_DASHBOARD_READ_TOKEN`). Nothing is in the client
bundle, the GitHub repo, or Cloudflare.

## 1. Start the private backend (localhost only)

```bash
export ATP_DASHBOARD_TOKEN="<owner-token>"          # controls emergency-stop / risk-config
export ATP_DASHBOARD_READ_TOKEN="<long-random-read-token>"
export ATP_DASHBOARD_CORS_ORIGINS="https://www.gigbay.de"
export ATP_RISK_CONFIG_PATH="$HOME/.atp/risk_config.json"
PYTHONPATH=src python3 examples/serve_dashboard.py   # binds 127.0.0.1:8000 ONLY
```

## 2. Install cloudflared (required — not yet installed)

No Homebrew present, so use the official pkg:

1. Open https://github.com/cloudflare/cloudflared/releases/latest
2. Download **cloudflared-darwin-arm64.pkg** (Apple Silicon) or **-amd64.pkg** (Intel).
3. Install it (double-click). Verify: `cloudflared --version`.

## 3. Create the tunnel to 127.0.0.1:8000 ONLY

Quick (ephemeral) tunnel — gives a temporary https URL, no account needed:
```bash
cloudflared tunnel --url http://127.0.0.1:8000
# prints e.g. https://random-words.trycloudflare.com  → this is <secure-api-host>
```

Named tunnel on your own domain (stable, recommended) needs `cloudflared tunnel login` in the
browser (Cloudflare account) — do that yourself; never share credentials. It only ever points at
`http://127.0.0.1:8000` — never 4002, never 0.0.0.0.

## 4. Point Vercel at the tunnel (server env — NOT NEXT_PUBLIC)

```bash
printf "https://<secure-api-host>" | vercel env add DASHBOARD_API_URL production --cwd frontend
printf "<long-random-read-token>" | vercel env add DASHBOARD_API_READ_TOKEN production --cwd frontend
vercel deploy --prod --yes --cwd frontend
```

`NEXT_PUBLIC_API_URL` stays **unset** in production (the browser uses the same-origin proxy).

## Security invariants (enforced/tested)

- IB Gateway `:4002` stays localhost-only; the tunnel targets `127.0.0.1:8000` only.
- The proxy forwards ONLY the read-model paths (summary, positions, risk, agents, opportunities,
  performance, governance, system, notifications, reconciliation, …) + token-gated control paths.
  No broker/IBKR/order endpoints exist.
- CORS is locked to `https://www.gigbay.de`. Read endpoints require the read token. Rate limited
  per IP. Emergency-stop/risk-config require the owner token; the Risk Engine is the sole authority.
- No secret in the client bundle, the repo, or Cloudflare.
