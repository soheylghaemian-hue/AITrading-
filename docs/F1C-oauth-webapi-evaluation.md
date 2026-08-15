# IBKR OAuth / Web API — Evaluation (Phase F1-C research track)

**Status:** research only — **no implementation**. This document evaluates whether ATP should migrate
its broker connectivity from the current **TWS API (socket, via `ib_async` + IB Gateway GUI)** to an
**OAuth-based IBKR Web API**. It records requirements as published on IBKR Campus / IBKR docs as of
**2026-08**. Items marked **[CONFIRM]** must be verified with IBKR before any implementation decision,
because eligibility and self-service availability change frequently and are account-specific.

Current ATP reality (baseline): read-only broker connector connects to a **headless IB Gateway 10.45**
(Xvfb + VNC for the one-time GUI login), a **paper** account (`DU…`/`DUR…` prefix), `readonly=True`,
`BROKER_EXECUTION_ENABLED=false`. Pain points that motivate this evaluation: the GUI Gateway needs
Xvfb + VNC, a manual GUI login, survives the **daily** reset only with in-app Auto-Restart, and needs a
**manual re-login with 2FA at the weekly reset**. A token-based API would remove the GUI/VNC dependency.

---

## 0. The two OAuth options IBKR offers

| | **OAuth 1.0a (Extended)** | **OAuth 2.0 (unified Web API)** |
|---|---|---|
| Maturity | Established, widely used by 3rd parties | Newer; IBKR is consolidating Client Portal Web API + Digital Account Management + Flex Web Service under one Web API with OAuth 2.0 |
| Client auth | HMAC/RSA signatures + Diffie-Hellman **Live Session Token (LST)** | **`private_key_jwt` only** (RFC 7521/7523): signed JWT `client_assertion` validated against a registered public key. No client secret sent on the wire |
| Who it targets today | Individuals (self-service) **and** third parties | Institutional / approved integrations; self-service reach expanding **[CONFIRM]** |
| Community tooling | Mature (Voyz/ibind, datawookie/ibauth, several clients) | Emerging |

Both paths still require, for **trading/iserver** endpoints, a **brokerage session** initialized via
`/iserver/auth/ssodh/init` and kept alive with `tickle` — i.e. the session concept (and its
daily/weekly auth lifecycle and 2FA at initial auth) does **not** disappear with OAuth; only the **GUI
Gateway process** disappears.

---

## 1. Eligibility

- **OAuth 1.0a:** available via the **Self-Service Portal** for an individual account holder to authorize
  their **own** account, and via IBKR's third-party onboarding for multi-user consumers. Paper accounts
  are supported (see §6).
- **OAuth 2.0:** primarily for approved/institutional integrations today; whether an individual
  self-service account can register an OAuth 2.0 `private_key_jwt` client without an onboarding review is
  **[CONFIRM]**.
- Both effectively assume an **IBKR Pro** account (the same tier required for API/market-data access);
  IBKR Lite API access is restricted. **[CONFIRM]** for the specific ATP account.

## 2. Requirements (high level)

- An IBKR **Pro** account in good standing; API access enabled. **[CONFIRM]**
- Ability to generate and hold **private keys** securely on the server (RSA keypairs; for 2.0 a JWT
  signing key).
- For 1.0a: implement the **Diffie-Hellman Live Session Token** computation (an IB-specific step beyond
  the OAuth 1.0a spec) and request signing.
- For 2.0: implement **signed-JWT client assertions** (`private_key_jwt`).
- A process to **initialize and tickle a brokerage session** for trading endpoints.
- Market-data entitlements are **unchanged** — OAuth does not grant data you are not subscribed to
  (US-equity data for ATP stays on Massive/Polygon regardless).

## 3. Registration

- **OAuth 1.0a:** register an OAuth **consumer** in the **Self-Service Portal**. IBKR issues a
  `oauth_consumer_key` (a 9-character string). For third-party (multi-user) consumers, IBKR's onboarding
  team asks you to supply **public keys** and a **callback URL**. You then generate **access token** +
  **access token secret** for the account being authorized.
- **OAuth 2.0:** register an OAuth 2.0 **client**, uploading the **public key(s)** used to validate your
  `client_assertion` JWTs. Registration path/approval for self-service is **[CONFIRM]**.

## 4. Credentials issued / held

**OAuth 1.0a:**
- `oauth_consumer_key` (9 chars) — issued by IBKR.
- `access_token` + `access_token_secret` — per authorized account.
- **Signature keypair** (RSA) — you hold the private key; sign requests.
- **Encryption keypair** (RSA) — used in the credential/token exchange.
- **Diffie-Hellman** prime + generator — supplied at registration; used to derive the LST.
- **Live Session Token (LST)** — derived at runtime; **short-lived**; signs subsequent requests.

**OAuth 2.0:**
- `client_id`.
- **JWT signing private key** (you hold it) with the matching **public key registered** at IBKR.
- Short-lived **access tokens** minted by presenting a signed JWT (`client_assertion`).

## 5. Certificate / key requirements

- **1.0a:** two RSA keypairs (signature + encryption) plus DH parameters. Public keys are uploaded to
  IBKR; private keys stay on the server (must be protected like any trading secret — file perms, not in
  git, not in logs).
- **2.0:** one JWT signing keypair; **public key registered** with IBKR; private key held securely.
- No browser TLS client-certificate is required for the API calls themselves; the "certificates" here
  are the **application-level signing/encryption keys**.

## 6. Paper-trading support

- **OAuth 1.0a:** **supported.** The flow exposes an `is_paper` boolean (true = a paper account was
  authorized). So a paper-only, read-only ATP integration is feasible on 1.0a.
- **OAuth 2.0:** paper support is **[CONFIRM]** — verify a paper account can be authorized before relying
  on it for ATP's paper phase.

## 7. Trading / order support

- Both paths support the full **`/iserver`** trading surface (place/modify/cancel orders, positions,
  account summary, live orders) **once a brokerage session is initialized**.
- Relevant to ATP: the Web API has its **own read-only mode** and order endpoints; ATP's two execution
  barriers would need to be **re-expressed** for the new surface — (a) never call the order endpoints,
  (b) keep `BROKER_EXECUTION_ENABLED=false` guard. The current guarantee "no `placeOrder` in source" maps
  to "no POST `/iserver/account/{id}/orders` in source".

## 8. Token lifecycle

- **1.0a:** the **access token is long-lived** (persists until revoked/regenerated), but the **Live
  Session Token is short-lived** and must be **recomputed** (via DH) when it expires. Regenerating access
  tokens can invalidate a prior session, so regeneration must be coordinated.
- **2.0:** **access tokens are short-lived**; you re-mint them on demand by presenting a fresh signed JWT.
  No long-term secret is transmitted.

## 9. Session lifecycle

- After obtaining the token/LST, initialize the **brokerage session**: `POST /iserver/auth/ssodh/init`
  (and `validate_sso`), then keep it alive with periodic **`tickle`** (roughly every few minutes; idle
  sessions time out), and close with **`logout`**.
- **This is the critical operational point:** the brokerage session is subject to the **same
  daily/weekly IBKR authentication lifecycle** as the Gateway. 2FA still applies at initial
  authentication. OAuth removes the **GUI**, not the **auth cadence**. Weekly re-authentication may still
  be required; whether it can be fully automated headlessly (no human 2FA) is **[CONFIRM]** and is the
  single most important question for ATP.

## 10. Limitations / risks

- **Auth cadence unchanged:** daily/weekly resets and 2FA still exist; the brokerage session still needs
  tickling. OAuth's benefit is **headless (no GUI/VNC)**, not "no re-auth ever".
- **One concurrent brokerage session** per account — the same competing-session constraint we already
  hit with the Gateway (ONELOGON) applies.
- **Crypto/registration complexity** — DH + RSA signing (1.0a) or `private_key_jwt` (2.0); more moving
  parts than a socket login, and private keys become high-value secrets on the server.
- **Different client stack** — moving off `ib_async` (socket) to REST + WebSocket means the broker
  connector is essentially rewritten; all F1-B read-only + reconciliation guarantees must be re-proven
  against the new surface.
- **Market data** — unchanged; still subscription-gated; ATP keeps Massive for US equities.
- **Rate limits / endpoint differences** — Web API has its own pacing and semantics vs the TWS API.

## 11. Migration impact for ATP

**What we gain:** no IB Gateway GUI, no Xvfb, no x11vnc/noVNC, no manual GUI login for the *daily* cycle;
a headless, token-based, server-friendly integration; the whole VNC maintenance surface disappears.

**What it costs:**
- A **full rewrite** of `src/atp/services/broker.py` from `ib_async` to an OAuth Web-API client
  (REST + WebSocket), including LST/JWT signing and `ssodh` session management + tickler.
- Re-proving the **read-only** posture and both **execution barriers** on the new surface, plus new
  acceptance tests (the current 9 broker tests assume the socket path).
- Secure handling of **new long-lived private keys** on the server.
- Still needing a **weekly re-auth story** (possibly still 2FA-gated) **[CONFIRM]**.

**Recommendation (no action now):**
1. **Keep the current TWS API + read-only Gateway for F1** — it is working, proven, and read-only.
2. Treat **OAuth 2.0 Web API** as a **Phase-2 infrastructure track evaluated before LIVE**, whose primary
   goal is removing the GUI/VNC dependency for unattended operation.
3. Before committing, get IBKR to confirm the three deciding questions:
   - **[CONFIRM]** Can our specific (Pro) account self-register an OAuth **2.0** client, and does it
     support a **paper** account?
   - **[CONFIRM]** Can the **weekly** re-authentication be completed **headlessly** (no human 2FA), or is
     periodic human interaction still required?
   - **[CONFIRM]** Are read-only guarantees and rate limits acceptable for ATP's reconciliation cadence?
4. Do **not** switch mid-F1. Any migration is its own gated phase with its own acceptance suite.

---

### Sources (IBKR public docs & references, 2026-08)
- IBKR Campus — OAuth 1.0a Extended: https://www.interactivebrokers.com/campus/ibkr-api-page/oauth-1-0a-extended/
- IBKR Campus — Trading Web API: https://www.interactivebrokers.com/campus/ibkr-api-page/web-api-trading/
- IBKR Web API — Introduction: https://www.interactivebrokers.com/docs/web-api/introduction
- IBKR Campus — Web API Documentation: https://www.interactivebrokers.com/campus/ibkr-api-page/webapi-doc/
- IBKR — OAuth (2018 primer PDF): https://www.interactivebrokers.com/webtradingapi/oauth.pdf
- Community references: github.com/Voyz/ibind, github.com/datawookie/ibauth

> Facts above are transcribed from IBKR public documentation and community clients as of 2026-08 and may
> be out of date or account-specific; every **[CONFIRM]** must be validated with IBKR before implementation.
