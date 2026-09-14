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
- Worker GPU: L4.
- Worker model/cache data must live on the persistent Modal Volume `raiw-model-cache`, mounted at `/cache`.
- `HF_HOME`, `XDG_CACHE_HOME`, `UV_CACHE_DIR`, and `DIFFSYNTH_MODEL_BASE_PATH` intentionally point inside `/cache`.
- Uploaded images, generated outputs, and per-job logs must remain temporary and must not be persisted to the model-cache Volume.
- Upload limit is 30 MiB.
- Authentication configuration comes from the Modal Secret `raiw-auth`; never commit secret values.
- `raiw-auth` must contain `ADMIN_USER`, `ADMIN_PASSWORD`, and `SESSION_SECRET`.
- `ADMIN_PASSWORD` must contain at least 10 characters; otherwise `bootstrap_admin()` prevents the ASGI app from starting.
- `SESSION_SECRET` must contain at least 32 characters.
- GitHub Actions authentication uses repository secrets `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET`; never print their values.

## Modal resource configuration

Do not add an explicit `ephemeral_disk` request unless the workload actually requires it and the current Modal documentation/runtime limits have been checked first.

On 2026-09-14 a deploy failed because `ephemeral_disk=120_000` MiB was rejected by Modal. The Modal API reported that explicit Function disk requests must be between 524288 and 3145728 MiB. The custom request was removed because model caches already use the persistent `/cache` Volume and job files are small/temporary.

## Deployment

Pushes to `main` that modify either of these files trigger production deployment:

- `modal_app.py`
- `.github/workflows/deploy-modal.yml`

The workflow must:

1. check out the repository;
2. set up Python 3.11 using the current supported `actions/setup-python` major version;
3. install the current Modal CLI;
4. verify the two required GitHub Modal token environment variables are present;
5. verify that the named Modal Secret `raiw-auth` exists;
6. run `modal deploy modal_app.py`;
7. smoke-test the public `/login` endpoint;
8. print recent Modal runtime logs when the smoke test fails.

After any deployment-related change, inspect the newest `Deploy Modal` run and its job log until the final result is known.

## Change log

### 2026-09-14

- Diagnosed the first `Deploy Modal` failure.
- Root cause: invalid explicit `ephemeral_disk=120_000` MiB request.
- Removed the unnecessary explicit ephemeral disk request from `process_image`.
- Updated `actions/setup-python` from v5 to v7 to remove the Node.js 20 compatibility warning and use the current supported release.
- Diagnosed the deployed site's runtime failure: `ADMIN_PASSWORD` inside Modal Secret `raiw-auth` was shorter than the 10-character minimum, so `bootstrap_admin()` crashed before the web app could answer requests.
- Added deployment smoke testing and Modal runtime-log capture to GitHub Actions.
- Added CI verification that the Modal Secret `raiw-auth` exists before deployment.
