# ITURA v2 — isolated rebuild

**Status: tested development foundation, not production certified.** The existing application is a reference for business rules and feature parity. The new runtime never imports legacy routers or workers, and its migrations create only `v2_*` tables. Supabase project `hlyuzyuzmghjhjddpxrf` was renamed from `pulse-kpi-portal` to `koolbot`. Its private `itura` schema contains the 14 v2 tables; existing KPI tables were preserved. No webhook or AWS cutover has occurred.

## Implemented

- Individual Argon2id accounts, hashed/revocable database sessions, HttpOnly cookies, same-origin/CSRF protection and live membership checks. No URL authentication keys or browser-local auth secrets.
- Workspaces; independent WhatsApp/web connections; channel-specific contact identities; stable conversation IDs; scoped agent/admin/analyst access; optimistic ownership/control updates.
- Signed WhatsApp POST events, exact destination validation, durable intake and replay deduplication before acknowledgement. Web visitors receive server-issued sessions and cannot choose another customer's conversation ID.
- A database inbox/outbox and bounded Vercel Queue subscriber (or standalone worker for container deployments). AI jobs recover from expired processing leases. Sends with ambiguous provider acceptance are quarantined rather than automatically duplicated. Known provider rate limits retry with bounded backoff.
- Rechecks for global automation pause, human takeover, newer inbound messages, account disablement, conversation version, opt-out, disabled channels and the WhatsApp free-text window.
- Grounded product IDs and Decimal prices; validated structured AI responses; deterministic configured greeting; payment-detail guard; human attention for unsupported media and exhausted AI retries.
- React console: recorded overview counts, automatically refreshed inbox/transcript, agent heartbeat/away state, safe text rendering, customer records, product editing, account connection metadata, team account suspension, approved AI settings and audit history.
- Lightweight website widget with safe text rendering, per-visitor sessions, exact-origin enforcement and delivered agent replies in the same inbox.
- Fixed versioned schema migration, readiness/liveness endpoints, non-root API image and generated production console routing.

## Verification

Fresh checks: **46 backend/integration/provider-contract/configuration tests pass**, **17 console tests pass**, and TypeScript plus production build pass. Tests use synthetic data and provider/model stubs. The deployed Python API's `/health/ready` returned HTTP 200 against the restricted Supabase role on 14 October 2026. Migration creation/repetition and column parity were checked on SQLite. These checks do not replace PostgreSQL concurrency tests, real Meta/Groq calls, live browser/accessibility QA, load tests or a backup restore drill.

## Run locally

1. Install Python dependencies: `python -m pip install -r requirements-v2.txt` (or the fully pinned Python 3.12 lockfile).
2. Set `ITURA_LOCAL_DEVELOPMENT=1`, `ITURA_DATABASE_URL=sqlite:///./itura-development.db`, `ITURA_ORIGIN=http://localhost:5173` in your process environment. Never use these development settings in production.
3. Apply migration: `python -m alembic -c alembic-v2.ini upgrade head`.
4. Provision the first administrator interactively: `python -m platform_v2.bootstrap`. There is no public bootstrap/reset-key bypass.
5. API: `python -m uvicorn platform_v2.main:app --host 127.0.0.1 --port 8000`.
6. Worker, as a separate process: `python -m platform_v2.worker`.
7. In `console/`, run `npm ci --ignore-scripts`, then `npm run dev`.

All cloud credentials are optional for synthetic tests. Real AI and WhatsApp require rotated secrets supplied through the secret manager and referenced by name in the console. Automated replies start disabled. New incoming conversations use AI only after automation is approved and enabled.

## Staging deployment target

- Shared Supabase project **koolbot**, organization **Koolboks**, region **eu-central-1**. Set `ITURA_DATABASE_SCHEMA=itura`; the application excludes `public` from its search path.
- Vercel Services (Beta) deploys the React frontend and Python API together. Project root is `v2`; `vercel.json` owns each service's build settings and routes API/webhook/health requests to the backend.
- `pyproject.toml` registers the bounded `platform_v2.queue_worker:drain` subscriber. Durable jobs remain in PostgreSQL; Vercel Queue messages wake the worker. The standalone worker is still available for container deployments using `Dockerfile.v2`.
- Set `ITURA_DATABASE_URL` with SSL and the correct session/direct pooler for persistent services. A dedicated least-privileged backend role needs explicitly verified grants; browser clients never connect with a privileged key.
- Migration enables RLS and removes anon/authenticated grants from all v2 application tables when those Supabase roles exist. The backend must still enforce every workspace/conversation boundary. Test advisors and direct Data API denial before enabling access.
- Vercel deployment and branch hostnames are accepted automatically from platform-provided environment variables. Set `ITURA_ORIGIN` explicitly for an approved custom domain. The React frontend uses same-origin API routes.
- Apply schema migration explicitly before starting the API. Provision a staging administrator, add disabled connections, verify provider credentials and both webhook methods, then enable synthetic-test connections.
- Run Meta verification using `/webhooks/whatsapp/CONNECTION_ID`. No old webhook or DNS is switched by this source change.

### First staging administrator

The database connection, schema and administrator email are configured for Preview on `rebuild/itura-v2` only. In the Vercel project's Environment Variables settings, add `ITURA_INITIAL_ADMIN_PASSWORD` as a **Sensitive** Preview variable scoped to `rebuild/itura-v2`. Choose a unique password of at least 12 characters; never paste it into chat or commit it. Redeploy that branch. Startup creates the first workspace and administrator only if both tables are empty, under a PostgreSQL transaction lock. It never changes existing accounts. The configured email is `morawolekan@gmail.com`.

After first successful login, remove the initial-password environment variable and redeploy. Automation starts disabled. Live login, widget delivery and the cloud queue require end-to-end verification before production promotion.

## Outstanding release gates and feature parity

This is the first implementation tranche of the authorized full rewrite. A clean build is not a release certificate.

| Gate | Current state |
|---|---|
| Supabase database | Private `itura` schema, restricted runtime LOGIN role, backend-only RLS policies and credential stored as a Sensitive Vercel Preview variable. Live API readiness passes. Existing KPI tables remain inaccessible to the runtime role. |
| GitHub source synchronization | Reviewed source synchronized to `rebuild/itura-v2` through the browser; draft PR #1 remains unmerged. Connector source writes still return HTTP 403. |
| API/worker host and Vercel staging | Combined frontend/Python API deployed and readiness verified. Bounded queue subscriber implemented; live delivery remains unverified. First admin awaits a user-entered password in the deployment secret manager. |
| PostgreSQL locking/concurrent-worker verification | Not tested against live PostgreSQL. Required before scaling workers. |
| Provider end-to-end | Stub contracts tested; real Meta verification/send/media/status and model evaluation not run. |
| Browser/accessibility/responsive verification | Component checks and responsive CSS implemented; live browser checks not performed in this environment. |
| Legacy feature parity | Pending: department/custom-role permissions, routing/capacity/overflow, request-based ownership approval, tracked escalation SLA/notifications, lead/contact CRUD/import/export, templates/broadcasts/campaigns, follow-up automation, media uploads/voice transcription, full analytics and scoring, settings drafts/test/publication/rollback, account recovery/invites/MFA. Preserve these requirements; do not retire the legacy runtime before parity is verified. |
| Authentication integration | Development foundation uses local identity. Supabase Auth/MFA/account recovery and managed invite flows are still an explicit production decision/gate. |
| Credential administration | Frontend binds protected secret references; encrypted in-app rotation/OAuth is not implemented yet. |
| Knowledge and AI | Structured grounding and limited context implemented; hybrid/vector retrieval, usage/cost traces and real red-team evaluations remain pending. |
| Production data | No backup/dump available; import/reconciliation and migration rollback have not been executed. |
| Monitoring/retention/load | Queue/error states recorded; worker heartbeat alerts, rate-limit/event cleanup, retention jobs and load targets need verification and implementation. |

## Cutover rules

Recover a restorable old database/asset backup and approved live settings before removing any infrastructure. Rehearse import into staging, reconcile IDs/counts/ownership/assets, drain old outbound jobs, and stop old schedulers before switching webhook/traffic. Maintain one source of write authority. Rollback must reconcile post-cutover data, not simply reverse DNS. Old AWS resources are retained until restore and cutover gates pass.

The uploaded credentials were excluded. Use rotated credentials for further live testing; do not commit `.env`, keys, customer exports or real passwords.
