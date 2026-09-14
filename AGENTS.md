# AGENTS.md

## Project purpose

This repository deploys a Modal-hosted web interface around `remove-ai-watermarks`.
The production entry point is `modal_app.py`; automatic deployment is handled by `.github/workflows/deploy-modal.yml`.

## Before changing the project

1. Read this file first.
2. Check the current Modal and dependency documentation before changing APIs, resource parameters, or deployment syntax.
3. Keep this file updated when deployment constraints, architecture, secrets, storage, or operational requirements change.
4. Prefer the smallest change that fixes the root cause and verify the resulting GitHub Actions run.

## Architecture and invariants

- Runtime: Python 3.11 on Modal.
- The Modal app name is derived from `MODAL_APP_NAME` (or, in GitHub Actions, the current `GITHUB_REPOSITORY`), normalized to a safe fork-specific name; never hardcode a public `.modal.run` URL.
- Worker GPU: L4.
- Worker model/cache data must live on the persistent Modal Volume `raiw-model-cache`, mounted at `/cache`.
- `HF_HOME`, `XDG_CACHE_HOME`, `UV_CACHE_DIR`, and `DIFFSYNTH_MODEL_BASE_PATH` intentionally point inside `/cache`.
- Uploaded images, generated outputs, and per-job logs must remain temporary and must not be persisted to the model-cache Volume.
- Upload limit is 30 MiB.
- Authentication configuration comes from the Modal Secret `raiw-auth`; never commit secret values.
- `raiw-auth` must contain `ADMIN_USER`, `ADMIN_PASSWORD`, and `SESSION_SECRET`.
- `ADMIN_PASSWORD` must contain at least 10 characters; otherwise `bootstrap_admin()` prevents the ASGI app from starting.
- `SESSION_SECRET` must contain at least 32 characters.
- The Secret reference must declare all three `required_keys`. Validate `SESSION_SECRET` before `bootstrap_admin()` in the web factory so the `/login` deployment smoke test cannot pass with an invalid session-signing key.
- GitHub Actions authentication uses repository secrets `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET`; never print their values.
- FastAPI/Starlette `Request` annotations used inside the nested `web()` factory must be concrete runtime types. Do not re-enable postponed annotations with `from __future__ import annotations` unless the request types are moved to module scope or otherwise made resolvable by FastAPI.

## Modal resource configuration

Do not add an explicit `ephemeral_disk` request unless the workload actually requires it and the current Modal documentation/runtime limits have been checked first.

On 2026-09-14 a deploy failed because `ephemeral_disk=120_000` MiB was rejected by Modal. The Modal API reported that explicit Function disk requests must be between 524288 and 3145728 MiB. The custom request was removed because model caches already use the persistent `/cache` Volume and job files are small/temporary.

## Deployment

Pushes to `main` that modify either of these files trigger production deployment:

- `modal_app.py`
- `.github/workflows/deploy-modal.yml`

The workflow must:

1. check out the repository;
2. derive a fork-specific Modal app name from the current GitHub repository;
3. set up Python 3.11 using the current supported `actions/setup-python` major version;
4. restore the cached `.modal-venv` environment when available;
5. install the pinned Modal CLI only on cache miss, then save the virtual environment cache;
6. verify the two required GitHub Modal token environment variables are present;
7. verify that the named Modal Secret `raiw-auth` exists;
8. record the UTC deployment start timestamp;
9. run `modal deploy modal_app.py` and extract the actual `.modal.run` web URL from the deploy output instead of hardcoding it;
10. allow a short rollout grace period before health checks so a request is not sent to the previous revision during cutover;
11. smoke-test the deployed `/login` endpoint;
12. print Modal runtime logs for the resolved app name and only from the current deployment timestamp onward when the smoke test fails.

The Modal CLI cache key must include OS, architecture, the resolved Python version, the pinned Modal CLI version, and a manual cache revision. Bump the revision if a cached environment must be invalidated manually. Do not cache secrets.

Modal-generated `.modal.run` URLs are derived from the workspace/environment source plus the web-function label. The arbitrary `modal.run` hostname prefix is not a free-form setting. Use `label=` to control the function label portion or a Modal custom domain where the workspace plan supports custom domains.

After any deployment-related change, inspect the newest `Deploy Modal` run and its job log until the final result is known.

## Change log

### 2026-09-14

- Diagnosed POST `/login` HTTP 500 caused by a missing or short `SESSION_SECRET`. The previous README placeholder was only 31 characters; replaced it with instructions to generate a private random key.
- Added required Secret key checks and startup validation of the session-signing key. Repair the runtime Secret separately while preserving the administrator credentials, then redeploy the same app/environment; never generate or rotate a key automatically at container startup.
- Repaired the invalid runtime `SESSION_SECRET` with `modal.Secret.update`, preserving the other keys and never logging values. GitHub Actions run 34881586640 verified an authenticated login, the secure session cookie, protected home HTTP 200, and the existing single app ID. The temporary repair/check steps must be removed after verification rather than becoming an automatic key-rotation policy.

- Made the Modal app identity fork-aware: the app name is derived automatically from the current GitHub repository, while `MODAL_APP_NAME` remains available as an explicit override for local/non-GitHub deploys.
- Updated the workflow log command to use the same resolved app name and kept public URL discovery based on the URL emitted by `modal deploy`.
- Hardened URL extraction against Modal CLI line wrapping; verified the resulting deploy and `/login` smoke test successfully.
- Updated the README so fork users do not copy a fixed repository or `.modal.run` URL.

- Diagnosed the first `Deploy Modal` failure.
- Root cause: invalid explicit `ephemeral_disk=120_000` MiB request.
- Removed the unnecessary explicit ephemeral disk request from `process_image`.
- Updated `actions/setup-python` from v5 to v7 to remove the Node.js 20 compatibility warning and use the current supported release.
- Diagnosed the deployed site's runtime failure: `ADMIN_PASSWORD` inside Modal Secret `raiw-auth` was shorter than the 10-character minimum, so `bootstrap_admin()` crashed before the web app could answer requests.
- Added deployment smoke testing and Modal runtime-log capture to GitHub Actions.
- Added CI verification that the Modal Secret `raiw-auth` exists before deployment.
- Added a persistent GitHub Actions cache for a dedicated `.modal-venv`; a valid cache hit skips Modal CLI installation entirely. The workflow pins Modal CLI 1.5.5 and uses `actions/cache` v6 restore/save actions.
- Fixed FastAPI HTTP 422 responses on `/` and `/login` by removing postponed annotations so locally imported `Request` and `UploadFile` types are resolved when nested route functions are defined.
- Added a no-content `/favicon.ico` route to avoid the browser-generated 404 noise.
- Removed the hardcoded public Modal URL from CI; the workflow now detects the real URL emitted by `modal deploy`.
- Runtime log capture now starts from the current deploy timestamp so stale password errors from previous deployments are not mixed into current diagnostics.
- Added a six-second Modal rollout grace period before the smoke test; verified the next run reached `/login` with HTTP 200 on the first checked request after rollout.

## Single-app lifecycle (2026-09-14)

- CI uses MODAL_ENVIRONMENT (repository variable, default main) consistently.
- CI resolves the app name using modal_app.APP_NAME, never a separate shell normalizer.
- Optional repository variable MODAL_APP_NAME pins identity across repository rename/transfer; otherwise each fork derives its own name from github.repository. Keep name/environment unchanged for updates.
- Serialize deploys with cancel-in-progress: false. Deploy with --name and --strategy recreate to terminate old containers before replacement. This can interrupt in-flight jobs and briefly interrupt service; do not force GPU always-on.
- After every deploy verify exactly one non-stopped app in the exact project-name scope, and /login HTTP 200. Never stop apps by broad prefix or stop unrelated workspace apps.
- One-time cleanup stopped verified IDs ap-luKxlY0j9FM0kFu4NTmr1n and ap-ZpPqrsdvDLI3EegV7kA43m. Replacement ID observed: ap-nxqtEZbJMP0FR7J2yv4OTx. Historical stopped entries may remain visible; they are not running apps.
- Keep the cleanup operation out of ordinary deploys. Do not delete volumes, user Dict or secrets.
