# Authentication

Homelab Console supports two explicit human-login modes. `password` (the
default) uses username + password followed by a Telegram-delivered second
factor. `telegram_only` uses the username only to identify the account and
requires Telegram approval or the Telegram-delivered OTP as the sole factor.
Auth never depends on Home Assistant or any infrastructure provider.
The API and database currently run on the home application host, so login remains
available during an individual provider or Sentinel WireGuard outage but not
during a complete home application host/home-site outage. Recovery codes keep web login
independent of Telegram availability.

## Login modes

`AUTH_LOGIN_MODE=password` is the secure default:

- Passwords are hashed with **Argon2id**. Plaintext passwords are never
  stored or logged.
- Failed attempts are rate-limited per account and per IP (see
  [Rate limiting](#rate-limiting)).
- On success, the server creates a **login challenge** rather than a
  session — the second factor is still required.

`AUTH_LOGIN_MODE=telegram_only` is an explicit single-factor operator mode:

- the login request accepts a username and rejects a password field;
- an active matching account may create a short-lived Telegram challenge;
- possession of the allowed Telegram identity is the only authentication
  factor, so the bot token, numeric user/chat IDs and webhook secret become
  critical authentication material;
- password recovery codes are unavailable; direct database access is the
  recovery path if Telegram is lost;
- existing password hashes remain stored so rollback to `password` mode does
  not require an account migration.

## Second factor: Telegram approval or OTP

Two delivery modes for the second factor:

1. **Telegram approval buttons (preferred)** — the server sends an inline
   approve/deny message to the operator's Telegram chat. Tapping "Approve"
   completes the login.
2. **Short-lived, single-use OTP** — a numeric/alphanumeric code delivered
   via Telegram, verified via `POST /api/auth/verify-otp`. The OTP itself is
   never stored in plaintext; it is stored as an HMAC and compared
   constant-time.

In tests, `AUTH_NOTIFICATION_ADAPTER=test` logs the notification (the
approval prompt or OTP) to the console instead of sending it over Telegram,
and is rejected by the live runtime.

## Login challenge lifecycle

A `LoginChallenge` record is created after the configured account check and
tracks:

- a unique challenge `id`
- an **expiry** timestamp (short-lived)
- a **max attempts** counter for OTP verification
- **single-use** semantics — once completed, consumed, or expired, it cannot
  be reused
- a **nonce** bound to the challenge, used by the Telegram approval callback
- **IP address and user-agent** metadata captured at creation
- a full audit trail (`AuditEvent`) for creation, each verification attempt,
  approval/denial, expiry, and completion

Flow:

```
POST /api/auth/login          (username, plus password in password mode)
        │
        ▼
  account check OK? ──no──► rejected, rate-limited
        │yes
        ▼
  LoginChallenge created, notification sent (Telegram or dev log)
        │
GET /api/auth/challenge/{id}  (poll challenge status, e.g. from the web UI)
        │
        ├── operator taps Approve/Deny in Telegram ──► POST /api/telegram/webhook
        │                                                 (validated by
        │                                                  TELEGRAM_WEBHOOK_SECRET,
        │                                                  callback nonce checked)
        │
        └── or operator enters the OTP ──► POST /api/auth/verify-otp
                                                (challenge id + code)
        │
        ▼
POST /api/auth/complete       (challenge id)
        │
        ▼
  Session created, cookie set, challenge marked consumed
```

A challenge that expires, exceeds max attempts, or is denied cannot be
completed; the operator must start a new login.

## Sessions

- Sessions are **server-side records** (not pure JWTs) — a session can be
  revoked server-side at any time (logout, admin action, security incident)
  without waiting for expiry.
- The session cookie is `HttpOnly`, `Secure` in live mode (forced by
  `APP_ENV=live`, see `.env.example`), and
  `SameSite=Strict`.
- The session identifier **rotates at login** — a new session is always
  issued, never reused from a prior anonymous state.
- `SESSION_TTL_MINUTES` bounds session lifetime (default 720 = 12h);
  expiry is enforced server-side against the stored session record.

## CSRF

State-changing requests (anything beyond a simple `GET`) require a CSRF
token in addition to the session cookie. The token is issued alongside the
session and must be echoed back on write requests; the cookie alone is not
sufficient to mutate state, which limits classic CSRF against the
cookie-based session.

## Rate limiting

Rate limits apply independently to:

- login attempts (per account and per IP),
- login-challenge creation,
- OTP verification attempts (bounded by the challenge's own max-attempts
  counter as well),
- Telegram callback processing (approve/deny nonce consumption).

Limits are intentionally strict given the public-VPS threat model — see
[`security.md`](security.md).

## Recovery codes

Recovery codes are the documented fallback when Telegram is unreachable
(bot down, Telegram outage, lost phone, etc.):

- Generated at account setup, shown to the operator **once**.
- Stored **hashed** (never in plaintext) in the database.
- Each code is **single-use**; using one consumes it.
- Every generation and use is audited (`AuditEvent`).
- Controlled by `AUTH_RECOVERY_ENABLED` (default `true`) in `password` mode.
  Recovery is always unavailable in `telegram_only`, where Telegram is a hard
  dependency for login.

### If Telegram is down

These steps apply only in `password` mode. In `telegram_only`, recovery codes
cannot bypass Telegram.

1. Attempt the normal login (username + password) to create a login
   challenge as usual — this step doesn't depend on Telegram.
2. Instead of waiting for the Telegram approval/OTP, use
   `POST /api/auth/recovery` with a saved recovery code to complete
   authentication.
3. Each recovery code only works once — generate a fresh set (and store
   them somewhere safe, e.g. a password manager) after using one, since
   using your last code would otherwise leave you without an offline
   fallback.
4. If both Telegram and your recovery codes are unavailable, there is no
   API-level bypass by design; regaining access requires direct database
   access on the home application host.

## SMS

SMS as a second factor or recovery channel is **explicitly deferred** — not
implemented, not planned for this milestone. Telegram (approval or OTP) plus
recovery codes are the only supported paths.

## Telegram bot commands

The bot itself (see [`mcp.md`](mcp.md) for the adjacent MCP surface) is
validated by `TELEGRAM_ALLOWED_USER_ID` and `TELEGRAM_ALLOWED_CHAT_ID` —
**never by username**, since usernames are not a stable or trustworthy
identity in Telegram's API. The bot is a compact operations dashboard with
progressive inline navigation. The home screen exposes only Incidents, Tasks,
Luna and More; automation, MCP and the full status live under More. Callback
navigation edits the current Telegram message when possible, with a
new-message fallback when Telegram no longer permits editing. Callback queries
are acknowledged immediately so Telegram does not leave a loading indicator
active while a slower view is rendered. Supported commands:

- `/start` or `/menu` — open the lightweight home screen.
- `/status` — full summary: providers, incidents, tasks, watcher, MCP and
  router state.
- `/tasks` — active task queue with per-task detail buttons.
- `/incidents` — open incidents with detail and "already handled" actions.
- `/watchers` — automation state and recent watcher runs.
- `/mcp` — MCP client roster and freshness.
- `/luna` — compatibility command for the fixed Operations overview shortcut.
- `/provider [claude|codex]` — switch the active Conversation Service
  **model** provider. This never touches infrastructure provider
  configuration (Proxmox, etc.) — see [`providers.md`](providers.md)
  for that distinction.
- `/approve <id>` / `/deny <id>` — resolve a pending approval (login
  challenge or a high-risk tool approval), via a short-lived single-use
  callback nonce.

Inline buttons use callback data such as `nav:tasks`, `task:detail:<id>`,
`incident:detail:<id>`, `incident:handled:<id>`, `luna:summary` and
`luna:triage`. Menus use compact, bounded lists and keep consequential actions,
such as incident resolution, in the detail view. These callbacks only read
control-plane state or call existing typed services. They do not expose
arbitrary tool execution, shell, SSH, raw HTTP, or infrastructure write
actions.

Ordinary Telegram text is tool-free chat. The **Operations** panel provides
fixed overview/network/storage/security/automation/alert summaries plus a
single-use **Ask live question** prompt. The latter is bound to the authorized
user and chat, expires after five minutes and is consumed after one reply; it
does not enable a sticky operational mode.

Telegram operational menus, inline actions, approval/task/Luna notifications,
and controlled error messages use US English. Free-text Conversation Service
replies remain Italian, and user
content and historical records are not translated.
