# Authentication and Permissions

## JWT

`security.py` issues HS256 tokens (`sub`=email, `iat`, `exp`; TTL
`QUES_JWT_TTL_MINUTES`, default 480). Dependencies:

- `require_user` — valid token + `users.status ∈ {active, approved}` (DB lookup
  per request; role changes apply without re-login).
- `require_admin` — `role='admin'` (else 403).
- `require_write_user` / `require_admin_write` — additionally refuse administrators
  whose server-side `access_mode` is `readonly` (403).

Tokens carry no role claims; all authorization is re-read from SQLite. No refresh
tokens or logout endpoint (not implemented).

## Subject-level RBAC (teachers)

`teacher_subjects` maps teachers to subject names. `accessible_subjects()` returns
`None` for admins (unrestricted) or the teacher's set. Read routes filter
(`filter_subjects`, `subject_allowed`); write routes on subject-bound resources
enforce `_require_subject_resource_write` — teachers may only mutate resources for
assigned subjects; generic (`subject_id: null`) resources are administrator-managed.

## TOTP 2FA

Located in `src/masterdeorca/functions/totp/` (service, Fernet crypto, QR
rendering, schemas, exceptions) using `pyotp` + `cryptography.Fernet`
(`QUES_TOTP_ENCRYPTION_KEY`).

- `POST /auth/totp/enroll` starts enrollment; the secret is generated once and
  reused across repeated enroll calls (never rotates until disabled).
  Re-enrollment while enabled requires the current code.
- `POST /auth/totp/confirm` activates after a valid code.
- Login is two-stage when enabled: `POST /auth/login` returns
  `{requires_totp, challenge_token}`; `POST /auth/totp/verify` exchanges code +
  challenge for the JWT. Challenge tokens are single-use, TTL 5 min, capped at 5
  live challenges per user.
- Replay protection via matched time-step in `totp_last_used_step`; failed-attempt
  counter with cooldown (threshold 5, 120 s) → HTTP 429 + `Retry-After`.
- `GET /auth/totp/status`, `DELETE /auth/totp` (disable keeps the secret).
- `totp_rate_limits` table exists but is unused (cooldown via `users` columns).

## Admin access modes

`admins.access_mode` (`edit` default / `readonly`). `GET/PUT /auth/access-mode`
(admin only). Read-only admins keep read access; mutating routes return 403.
Switching back to `edit` is always allowed.

## Login flow

`POST /auth/login` → `database.verify_user()` (Argon2 via pwdlib). TOTP-disabled
accounts receive the JWT directly; TOTP-enabled get the challenge flow. 401 on bad
credentials, 429 in cooldown, 403 for pending/disapproved accounts.

## Endpoints (summary)

See [api-reference.md](api-reference.md) §Authentication. Full table:
`BACKEND_ARCHITECTURE.md` §14/§15.2.

## Per-subject permissions (teachers)

Each assigned subject can carry four independent boolean flags stored in
`user_subject_permissions`: `can_view_papers`, `can_upload_process`,
`can_manage_question_bank`, `can_generate_papers`. Defaults in schema are true (1),
but absent rows mean no access under current policy. Assignment uses two mechanisms:
legacy `teacher_subjects` table (fallback) and canonical `user_subject_assignments` +
`user_subject_permissions` (preferred). Subject access is resolved at request time from
the database (no cached token claims). Only `generate_papers` is actually enforced in route
guards today; the other three columns exist but are not checked in any route.

## Related

- [database.md](database.md)
- [api-reference.md](api-reference.md)
