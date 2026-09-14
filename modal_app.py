import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

import modal

def _normalize_modal_app_name(value: str) -> str:
    """Return a stable, URL-safe Modal app name for this repository/fork."""
    normalized = re.sub(r"[^a-z0-9]+", "-", (value or "").casefold())
    normalized = re.sub(r"-+", "-", normalized).strip("-")
    if not normalized:
        normalized = "remove-ai-watermarks-modal"
    if not normalized.startswith("raiw-"):
        normalized = f"raiw-{normalized}"

    # Keep the name within a conservative Modal/URL-friendly length while
    # retaining a deterministic suffix to avoid collisions between long forks.
    if len(normalized) > 64:
        suffix = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]
        normalized = f"{normalized[:55].rstrip('-')}-{suffix}"
    return normalized


def _resolve_modal_app_name() -> str:
    # GitHub Actions supplies owner/repository automatically. MODAL_APP_NAME
    # remains an override for local or non-GitHub deployments.
    raw_name = (
        os.environ.get("MODAL_APP_NAME")
        or os.environ.get("GITHUB_REPOSITORY")
        or "remove-ai-watermarks-modal"
    )
    return _normalize_modal_app_name(raw_name)


APP_NAME = _resolve_modal_app_name()
AUTH_SECRET_NAME = os.environ.get("RAIW_AUTH_SECRET_NAME", "raiw-auth")

# ---------- Costi usati solo per la stima "ore free" ----------
# Fonte Modal pricing, 14/09/2026. Il consumo reale può variare.
FREE_COMPUTE_USD = Decimal("30")
L4_USD_PER_SEC = Decimal("0.000222")
CPU_USD_PER_CORE_SEC = Decimal("0.0000131")
MEM_USD_PER_GIB_SEC = Decimal("0.00000222")
WORKER_CPU_CORES = Decimal("2")
WORKER_MEMORY_GIB = Decimal("48")
ESTIMATED_WORKER_USD_PER_SEC = (
    L4_USD_PER_SEC
    + CPU_USD_PER_CORE_SEC * WORKER_CPU_CORES
    + MEM_USD_PER_GIB_SEC * WORKER_MEMORY_GIB
)

MAX_UPLOAD_BYTES = 30 * 1024 * 1024
SESSION_TTL_SECONDS = 24 * 60 * 60
JOB_TOKEN_TTL_SECONDS = 24 * 60 * 60
LOG_RETURN_LIMIT = 120_000

ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".avif"}
USERNAME_RE = re.compile(r"^[a-z0-9._-]{3,32}$")

app = modal.App(APP_NAME)

# Persistono solo modelli/cache e dati utenti. Nessuna foto o output viene salvato qui.
cache_vol = modal.Volume.from_name("raiw-model-cache", create_if_missing=True)
users_store = modal.Dict.from_name("raiw-users-v1", create_if_missing=True)

CACHE_DIR = "/cache"
BASE_ENV = {
    "HF_HOME": f"{CACHE_DIR}/huggingface",
    "XDG_CACHE_HOME": f"{CACHE_DIR}/xdg",
    "UV_CACHE_DIR": f"{CACHE_DIR}/uv",
    "DIFFSYNTH_MODEL_BASE_PATH": f"{CACHE_DIR}/diffsynth_models",
    "DIFFSYNTH_DOWNLOAD_SOURCE": "huggingface",
    "TOKENIZERS_PARALLELISM": "false",
    "PYTHONUNBUFFERED": "1",
}

web_image = (
    modal.Image.debian_slim(python_version="3.11")
    .uv_pip_install("fastapi[standard]", "python-multipart")
)

worker_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install(
        "git",
        "ffmpeg",
        "libgl1",
        "libglib2.0-0",
        "libgomp1",
        "build-essential",
        "pkg-config",
    )
    .uv_pip_install("remove-ai-watermarks[visible,heif,qwen-zimage,migan]")
)

auth_secret = modal.Secret.from_name(
    AUTH_SECRET_NAME,
    required_keys=["ADMIN_USER", "ADMIN_PASSWORD", "SESSION_SECRET"],
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_username(username: str) -> str:
    return (username or "").strip().casefold()


def user_key(username: str) -> str:
    return f"user:{normalize_username(username)}"


def sanitize_filename(name: str) -> str:
    name = Path(name or "input.jpg").name
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return name or "input.jpg"


def output_suffix(source_name: str) -> str:
    ext = Path(source_name).suffix.lower()
    return ext if ext in {".jpg", ".jpeg", ".png", ".webp"} else ".png"


def password_hash(password: str, salt: bytes) -> bytes:
    # scrypt è disponibile nella stdlib e non richiede dipendenze esterne.
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=2**15,
        r=8,
        p=1,
        dklen=32,
        maxmem=128 * 1024 * 1024,
    )


def make_password_record(password: str) -> dict[str, str]:
    if len(password) < 10:
        raise ValueError("La password deve contenere almeno 10 caratteri.")
    salt = secrets.token_bytes(16)
    digest = password_hash(password, salt)
    return {
        "salt": base64.urlsafe_b64encode(salt).decode("ascii"),
        "hash": base64.urlsafe_b64encode(digest).decode("ascii"),
    }


def verify_password(password: str, record: dict) -> bool:
    try:
        salt = base64.urlsafe_b64decode(record["password"]["salt"].encode("ascii"))
        expected = base64.urlsafe_b64decode(record["password"]["hash"].encode("ascii"))
        actual = password_hash(password, salt)
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def new_user_record(username: str, password: str, role: str = "user") -> dict:
    username = normalize_username(username)
    if not USERNAME_RE.fullmatch(username):
        raise ValueError("Username: 3-32 caratteri, solo a-z, 0-9, punto, trattino o underscore.")
    if role not in {"user", "admin"}:
        raise ValueError("Ruolo non valido.")
    return {
        "username": username,
        "password": make_password_record(password),
        "role": role,
        "enabled": True,
        "deleted": False,
        "created_at": utc_now_iso(),
        "last_login_at": None,
        "last_job_at": None,
        "jobs_ok": 0,
        "jobs_failed": 0,
    }


def get_user(username: str) -> dict | None:
    rec = users_store.get(user_key(username))
    if not rec or rec.get("deleted"):
        return None
    return rec


def list_users() -> list[dict]:
    rows: list[dict] = []
    for key in list(users_store.keys()):
        key = str(key)
        if not key.startswith("user:"):
            continue
        rec = users_store.get(key)
        if rec and not rec.get("deleted"):
            rows.append(rec)
    rows.sort(key=lambda r: (r.get("role") != "admin", r.get("username", "")))
    return rows


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64u_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def signing_secret() -> bytes:
    value = os.environ.get("SESSION_SECRET", "")
    if len(value) < 32:
        raise RuntimeError(
            f"SESSION_SECRET mancante o troppo corto (minimo 32 caratteri) nel Secret {AUTH_SECRET_NAME}."
        )
    return value.encode("utf-8")


def sign_payload(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    body = b64u(raw)
    sig = b64u(hmac.new(signing_secret(), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_signed_payload(token: str, *, expected_kind: str) -> dict | None:
    try:
        body, sig = token.split(".", 1)
        expected = b64u(hmac.new(signing_secret(), body.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(b64u_decode(body))
        if payload.get("kind") != expected_kind:
            return None
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


def create_session_token(username: str) -> str:
    now = int(time.time())
    return sign_payload(
        {
            "kind": "session",
            "u": normalize_username(username),
            "iat": now,
            "exp": now + SESSION_TTL_SECONDS,
            "nonce": secrets.token_hex(8),
        }
    )


def create_job_token(username: str, call_id: str) -> str:
    now = int(time.time())
    return sign_payload(
        {
            "kind": "job",
            "u": normalize_username(username),
            "call": call_id,
            "iat": now,
            "exp": now + JOB_TOKEN_TTL_SECONDS,
        }
    )


def safe_origin(request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True
    try:
        return urlparse(origin).netloc == request.headers.get("host")
    except Exception:
        return False


def mark_job_result(username: str, ok: bool) -> None:
    try:
        key = user_key(username)
        rec = users_store.get(key)
        if not rec or rec.get("deleted"):
            return
        field = "jobs_ok" if ok else "jobs_failed"
        rec[field] = int(rec.get(field, 0)) + 1
        rec["last_job_at"] = utc_now_iso()
        users_store[key] = rec
    except Exception:
        # Le statistiche utente non devono mai far fallire l'elaborazione.
        pass


@app.function(
    image=worker_image,
    gpu="L4",
    cpu=2.0,
    memory=49_152,
    timeout=60 * 60,
    startup_timeout=60 * 60,
    volumes={CACHE_DIR: cache_vol},
    env=BASE_ENV,
)
def process_image(input_bytes: bytes, original_name: str, username: str) -> dict:
    """Elabora una foto e non lascia file di job persistenti.

    Tutti i file input/output/log sono in TemporaryDirectory e vengono cancellati
    anche in caso di errore. Solo la cache modelli è persistente nel Volume.
    """
    safe_name = sanitize_filename(original_name)
    started = time.monotonic()
    log_text = ""
    ok = False

    try:
        with tempfile.TemporaryDirectory(prefix="raiw-job-") as tmp:
            root = Path(tmp)
            input_path = root / safe_name
            output_path = root / f"{Path(safe_name).stem}_clean{output_suffix(safe_name)}"
            input_path.write_bytes(input_bytes)

            cmd = [
                "remove-ai-watermarks",
                "all",
                str(input_path),
                "-o",
                str(output_path),
                "--force",
                "--pipeline",
                "auto",
                "--backend",
                "auto",
            ]

            print(f"[job] Avvio elaborazione: {safe_name}", flush=True)
            log_chunks: list[str] = []
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env={**os.environ, **BASE_ENV},
                bufsize=1,
            )

            def stream_output() -> None:
                if proc.stdout is None:
                    return
                for line in iter(proc.stdout.readline, ""):
                    log_chunks.append(line)
                    print(line, end="", flush=True)
                proc.stdout.close()

            reader = threading.Thread(target=stream_output, name="raiw-log-stream", daemon=True)
            reader.start()
            deadline = time.monotonic() + 55 * 60
            next_heartbeat = time.monotonic() + 30

            while proc.poll() is None:
                now = time.monotonic()
                if now >= deadline:
                    proc.kill()
                    proc.wait()
                    reader.join(timeout=5)
                    log_text = "".join(log_chunks)
                    print("[job] Timeout: processo terminato.", flush=True)
                    return {
                        "status": "error",
                        "message": "Elaborazione interrotta per timeout.",
                        "log": log_text[-LOG_RETURN_LIMIT:],
                        "elapsed_seconds": round(time.monotonic() - started, 1),
                    }
                if now >= next_heartbeat:
                    elapsed = round(now - started, 1)
                    print(f"[job] Elaborazione ancora in corso ({elapsed} s).", flush=True)
                    next_heartbeat = now + 30
                time.sleep(1)

            reader.join(timeout=5)
            log_text = "".join(log_chunks)
            print(
                f"[job] Processo terminato con codice {proc.returncode} "
                f"in {round(time.monotonic() - started, 1)} s.",
                flush=True,
            )

            if proc.returncode != 0:
                return {
                    "status": "error",
                    "message": f"Elaborazione fallita (codice {proc.returncode}).",
                    "log": log_text[-LOG_RETURN_LIMIT:],
                    "elapsed_seconds": round(time.monotonic() - started, 1),
                }

            if not output_path.is_file() or output_path.stat().st_size == 0:
                return {
                    "status": "error",
                    "message": "Il programma non ha prodotto un file valido.",
                    "log": log_text[-LOG_RETURN_LIMIT:],
                    "elapsed_seconds": round(time.monotonic() - started, 1),
                }

            payload = output_path.read_bytes()
            ok = True
            return {
                "status": "done",
                "output_name": output_path.name,
                "output_bytes": payload,
                "output_size": len(payload),
                "log": log_text[-LOG_RETURN_LIMIT:],
                "elapsed_seconds": round(time.monotonic() - started, 1),
            }
    except Exception as exc:
        return {
            "status": "error",
            "message": f"Errore: {type(exc).__name__}: {exc}",
            "log": log_text[-LOG_RETURN_LIMIT:],
            "elapsed_seconds": round(time.monotonic() - started, 1),
        }
    finally:
        mark_job_result(username, ok)


LOGIN_HTML = """<!doctype html>
<html lang='it'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Accesso · Remove AI Watermarks</title>
<style>
body{font-family:system-ui;background:#f5f5f7;margin:0;display:grid;place-items:center;min-height:100vh;color:#171717}
.card{background:white;width:min(390px,calc(100% - 36px));padding:28px;border-radius:18px;box-shadow:0 12px 40px #0001}
h1{margin:0 0 8px}.muted{color:#666}input,button{width:100%;box-sizing:border-box;padding:12px 14px;border-radius:10px;margin-top:10px}
input{border:1px solid #ccc}button{border:0;background:#111;color:#fff;font-weight:700;cursor:pointer}.err{color:#b42318;margin-top:12px}
</style></head><body><div class='card'><h1>🧹 Accesso</h1><div class='muted'>Remove AI Watermarks</div>
<form method='post' action='/login'><input name='username' autocomplete='username' placeholder='Username' required autofocus>
<input name='password' type='password' autocomplete='current-password' placeholder='Password' required>
<button type='submit'>Entra</button></form>__ERROR__</div></body></html>"""

HOME_HTML = """<!doctype html>
<html lang='it'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Remove AI Watermarks</title>
<style>
:root{font-family:system-ui;background:#f5f5f7;color:#171717}body{margin:0}.wrap{max-width:760px;margin:auto;padding:26px 16px 50px}
.top{display:flex;justify-content:space-between;align-items:center;gap:12px}.top a{color:#111;text-decoration:none}.card{background:white;padding:22px;border-radius:18px;box-shadow:0 8px 28px #0000000d;margin-top:18px}
h1{margin:0}.muted{color:#666}.drop{border:2px dashed #bbb;border-radius:14px;padding:24px;margin-top:16px;text-align:center}button{border:0;border-radius:10px;background:#111;color:#fff;padding:12px 18px;font-weight:700;cursor:pointer}
progress{width:100%;height:14px;margin-top:14px}.ok{color:#087a2f}.err{color:#b42318;white-space:pre-wrap}.free{display:flex;justify-content:space-between;gap:14px;align-items:end}.big{font-size:30px;font-weight:800}.tiny{font-size:12px;color:#777}.hidden{display:none}
</style></head><body><div class='wrap'>
<div class='top'><div><h1>🧹 Remove AI Watermarks</h1><div class='muted'>Ciao, __USERNAME__</div></div><div>__ADMIN_LINK__ <a href='/logout'>Esci</a></div></div>
<div class='card'><b>Carica una foto</b><div class='muted'>JPG, PNG, WebP, HEIC/HEIF o AVIF · max 30 MB</div>
<form id='form'><div class='drop'><input type='file' name='file' accept='.jpg,.jpeg,.png,.webp,.heic,.heif,.avif' required></div><br><button type='submit'>Elabora foto</button></form>
<progress id='progress' class='hidden'></progress><div id='status' class='muted' style='margin-top:12px'>In attesa.</div>
<div id='actions' class='hidden' style='margin-top:14px'><a id='download' href='#'>⬇️ Scarica risultato</a> · <a id='logs' href='#' target='_blank'>📄 Log</a></div></div>
<div class='card free'><div><div class='muted'>Credito “free” stimato questo mese</div><div id='credit' class='big'>…</div><div id='freeNote' class='tiny'>Calcolo dai dati di fatturazione Modal.</div></div><div style='text-align:right'><div class='muted'>Ore residue stimate</div><div id='hours' class='big'>…</div><div class='tiny'>L4 + 2 CPU + 48 GiB RAM; web/cache esclusi.</div></div></div>
</div><script>
const form=document.getElementById('form'),statusBox=document.getElementById('status'),progress=document.getElementById('progress'),actions=document.getElementById('actions'),download=document.getElementById('download'),logs=document.getElementById('logs');
async function freeStatus(){try{const r=await fetch('/api/free-status');const d=await r.json();document.getElementById('credit').textContent=d.credit_label;document.getElementById('hours').textContent=d.hours_label;document.getElementById('freeNote').textContent=d.note;}catch(e){document.getElementById('credit').textContent='n/d';document.getElementById('hours').textContent='n/d';}}
async function poll(token){const started=Date.now();for(;;){const r=await fetch('/result/'+encodeURIComponent(token));if(r.status===202){const elapsed=Math.floor((Date.now()-started)/1000);statusBox.textContent='⏳ Elaborazione in corso… '+elapsed+' s';await new Promise(x=>setTimeout(x,4000));continue;}const d=await r.json();progress.classList.add('hidden');if(d.status==='done'){statusBox.innerHTML='<span class="ok">✅ Completato</span> · '+d.output_name+' · '+d.elapsed_seconds+' s';download.href='/download/'+encodeURIComponent(token);logs.href='/logs/'+encodeURIComponent(token);actions.classList.remove('hidden');freeStatus();}else{statusBox.innerHTML='<span class="err">❌ '+(d.message||'Errore')+'</span>';logs.href='/logs/'+encodeURIComponent(token);actions.classList.remove('hidden');}break;}}
form.addEventListener('submit',async e=>{e.preventDefault();actions.classList.add('hidden');progress.classList.remove('hidden');statusBox.textContent='📤 Caricamento…';const body=new FormData(form);const r=await fetch('/submit',{method:'POST',body});if(!r.ok){progress.classList.add('hidden');let t=await r.text();statusBox.innerHTML='<span class="err">❌ '+t+'</span>';return;}const d=await r.json();statusBox.textContent='🚀 Job avviato…';poll(d.job_token);});
freeStatus();
</script></body></html>"""

ADMIN_HTML_HEAD = """<!doctype html><html lang='it'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Utenti · Admin</title><style>body{font-family:system-ui;background:#f5f5f7;margin:0;color:#171717}.wrap{max-width:1050px;margin:auto;padding:26px 16px 50px}.card{background:white;padding:20px;border-radius:16px;margin-top:16px;box-shadow:0 8px 28px #0000000d}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:10px;border-bottom:1px solid #eee;vertical-align:top}input,select,button{padding:9px;border-radius:8px;border:1px solid #ccc}button{background:#111;color:#fff;border:0;cursor:pointer}.danger{background:#b42318}.muted{color:#666;font-size:13px}.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}a{color:#111}</style></head><body><div class='wrap'><div class='row' style='justify-content:space-between'><h1>👤 Gestione utenti</h1><a href='/'>← Home</a></div>"""


def bootstrap_admin() -> None:
    username = normalize_username(os.environ.get("ADMIN_USER", ""))
    password = os.environ.get("ADMIN_PASSWORD", "")
    if not USERNAME_RE.fullmatch(username):
        raise RuntimeError("ADMIN_USER non valido nel Secret raiw-auth.")
    if len(password) < 10:
        raise RuntimeError("ADMIN_PASSWORD deve avere almeno 10 caratteri.")
    key = user_key(username)
    if users_store.get(key) is None:
        users_store[key] = new_user_record(username, password, role="admin")


def current_user_from_request(request) -> dict | None:
    token = request.cookies.get("raiw_session")
    if not token:
        return None
    payload = verify_signed_payload(token, expected_kind="session")
    if not payload:
        return None
    rec = get_user(payload.get("u", ""))
    if not rec or not rec.get("enabled"):
        return None
    return rec


def require_job_for_user(token: str, username: str) -> str | None:
    payload = verify_signed_payload(token, expected_kind="job")
    if not payload or normalize_username(payload.get("u", "")) != normalize_username(username):
        return None
    return str(payload.get("call", "")) or None


def estimate_free_status() -> dict:
    """Stima il credito Starter residuo e lo converte nel profilo worker configurato."""
    try:
        ws = modal.Workspace.from_context()
        summary = ws.billing.summary("this month")
        metered = Decimal(summary.metered_cost)
        adjustments = dict(summary.adjustments or {})

        # Preferiamo il consumo di voci esplicitamente riconducibili a crediti.
        credit_used = Decimal("0")
        found_credit_key = False
        for key, value in adjustments.items():
            if "credit" in str(key).casefold():
                found_credit_key = True
                amount = Decimal(value)
                if amount < 0:
                    credit_used += -amount
                elif amount > 0:
                    credit_used += amount

        if not found_credit_key:
            # Fallback conservativo: se Modal cambia i nomi delle adjustment,
            # usiamo il metered cost come consumo del budget incluso.
            credit_used = min(max(metered, Decimal("0")), FREE_COMPUTE_USD)

        remaining = max(FREE_COMPUTE_USD - credit_used, Decimal("0"))
        hours = remaining / (ESTIMATED_WORKER_USD_PER_SEC * Decimal(3600))
        return {
            "remaining_usd": float(remaining),
            "hours": float(hours),
            "metered_usd": float(metered),
            "credit_label": f"${remaining:.2f} / ${FREE_COMPUTE_USD:.0f}",
            "hours_label": f"≈ {hours:.1f} h",
            "note": "Stima sul credito mensile Starter; il consumo reale include anche web, cache e possibili variazioni tariffarie.",
        }
    except Exception:
        hours = FREE_COMPUTE_USD / (ESTIMATED_WORKER_USD_PER_SEC * Decimal(3600))
        return {
            "remaining_usd": None,
            "hours": None,
            "metered_usd": None,
            "credit_label": "n/d",
            "hours_label": "n/d",
            "note": f"Billing Modal non disponibile. A budget pieno: circa {hours:.1f} h con il profilo worker attuale.",
        }


@app.function(
    image=web_image,
    secrets=[auth_secret],
)
@modal.concurrent(max_inputs=20)
@modal.asgi_app()
def web():
    from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
    from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

    # Fail during startup so the deployment smoke test also checks session signing.
    signing_secret()
    bootstrap_admin()
    web_app = FastAPI(title="Remove AI Watermarks")

    def auth_or_redirect(request: Request):
        user = current_user_from_request(request)
        if not user:
            return None, RedirectResponse("/login", status_code=303)
        return user, None

    def admin_or_403(request: Request):
        user = current_user_from_request(request)
        if not user:
            raise HTTPException(status_code=401, detail="Non autenticato")
        if user.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Accesso amministratore richiesto")
        return user

    @web_app.get("/login")
    def login_page(request: Request):
        if current_user_from_request(request):
            return RedirectResponse("/", status_code=303)
        return HTMLResponse(LOGIN_HTML.replace('__ERROR__', ''))

    @web_app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return Response(status_code=204)

    @web_app.post("/login")
    def login(request: Request, username: str = Form(...), password: str = Form(...)):
        if not safe_origin(request):
            raise HTTPException(status_code=403, detail="Origine richiesta non valida")
        username = normalize_username(username)
        rec = get_user(username)
        if not rec or not rec.get("enabled") or not verify_password(password, rec):
            time.sleep(0.35)
            return HTMLResponse(
                LOGIN_HTML.replace('__ERROR__', "<div class='err'>Credenziali non valide.</div>"),
                status_code=401,
            )
        rec["last_login_at"] = utc_now_iso()
        users_store[user_key(username)] = rec
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            "raiw_session",
            create_session_token(username),
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )
        return response

    @web_app.get("/logout")
    def logout():
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie("raiw_session", path="/")
        return response

    @web_app.get("/")
    def home(request: Request):
        user, redirect = auth_or_redirect(request)
        if redirect:
            return redirect
        admin_link = "<a href='/admin'>Admin</a> ·" if user.get("role") == "admin" else ""
        page = HOME_HTML.replace("__USERNAME__", html.escape(user["username"]))
        page = page.replace("__ADMIN_LINK__", admin_link)
        return HTMLResponse(page)

    @web_app.get("/api/free-status")
    def free_status(request: Request):
        user, redirect = auth_or_redirect(request)
        if redirect:
            return JSONResponse({"error": "auth"}, status_code=401)
        return JSONResponse(estimate_free_status())

    @web_app.post("/submit")
    def submit(request: Request, file: UploadFile = File(...)):
        user, redirect = auth_or_redirect(request)
        if redirect:
            return JSONResponse({"error": "auth"}, status_code=401)
        if not safe_origin(request):
            raise HTTPException(status_code=403, detail="Origine richiesta non valida")

        safe_name = sanitize_filename(file.filename or "input.jpg")
        ext = Path(safe_name).suffix.lower()
        if ext not in ALLOWED_EXTS:
            raise HTTPException(status_code=400, detail="Formato file non supportato.")

        content = file.file.read(MAX_UPLOAD_BYTES + 1)
        if not content:
            raise HTTPException(status_code=400, detail="File vuoto.")
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="File troppo grande: massimo 30 MB.")

        call = process_image.spawn(content, safe_name, user["username"])
        return JSONResponse({"job_token": create_job_token(user["username"], call.object_id)})

    @web_app.get("/result/{job_token}")
    def result(request: Request, job_token: str):
        user, redirect = auth_or_redirect(request)
        if redirect:
            return JSONResponse({"error": "auth"}, status_code=401)
        call_id = require_job_for_user(job_token, user["username"])
        if not call_id:
            raise HTTPException(status_code=404, detail="Job non valido")
        function_call = modal.FunctionCall.from_id(call_id)
        try:
            result_data = function_call.get(timeout=0)
        except modal.exception.OutputExpiredError:
            return JSONResponse({"status": "expired", "message": "Risultato scaduto."}, status_code=404)
        except TimeoutError:
            return JSONResponse({"status": "pending"}, status_code=202)

        public = {k: v for k, v in result_data.items() if k not in {"output_bytes", "log"}}
        return JSONResponse(public)

    @web_app.get("/download/{job_token}")
    def download(request: Request, job_token: str):
        user, redirect = auth_or_redirect(request)
        if redirect:
            return redirect
        call_id = require_job_for_user(job_token, user["username"])
        if not call_id:
            raise HTTPException(status_code=404, detail="Job non valido")
        result_data = modal.FunctionCall.from_id(call_id).get(timeout=0)
        if result_data.get("status") != "done" or not result_data.get("output_bytes"):
            raise HTTPException(status_code=404, detail="Output non disponibile")
        filename = sanitize_filename(result_data.get("output_name", "result.png"))
        ext = Path(filename).suffix.lower()
        media = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"
        }.get(ext, "application/octet-stream")
        return Response(
            content=result_data["output_bytes"],
            media_type=media,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @web_app.get("/logs/{job_token}")
    def logs(request: Request, job_token: str):
        user, redirect = auth_or_redirect(request)
        if redirect:
            return redirect
        call_id = require_job_for_user(job_token, user["username"])
        if not call_id:
            raise HTTPException(status_code=404, detail="Job non valido")
        result_data = modal.FunctionCall.from_id(call_id).get(timeout=0)
        return PlainTextResponse(result_data.get("log", "Nessun log disponibile."))

    @web_app.get("/admin")
    def admin_page(request: Request):
        admin = admin_or_403(request)
        rows = []
        for rec in list_users():
            username = html.escape(rec.get("username", ""))
            role = html.escape(rec.get("role", "user"))
            enabled = bool(rec.get("enabled"))
            last_login = html.escape(str(rec.get("last_login_at") or "—"))
            last_job = html.escape(str(rec.get("last_job_at") or "—"))
            jobs = f"{int(rec.get('jobs_ok', 0))} ok / {int(rec.get('jobs_failed', 0))} err"
            protected = rec.get("username") == admin.get("username")
            toggle = "" if protected else f"<form method='post' action='/admin/users/{username}/toggle'><button type='submit'>{'Disabilita' if enabled else 'Abilita'}</button></form>"
            delete = "" if protected else f"<form method='post' action='/admin/users/{username}/delete' onsubmit=\"return confirm('Eliminare {username}?')\"><button class='danger' type='submit'>Elimina</button></form>"
            reset = f"<form method='post' action='/admin/users/{username}/password' class='row'><input name='password' type='password' minlength='10' placeholder='Nuova password' required><button type='submit'>Reset password</button></form>"
            rows.append(
                f"<tr><td><b>{username}</b><div class='muted'>{role} · {'attivo' if enabled else 'disabilitato'}</div></td>"
                f"<td>{jobs}<div class='muted'>login: {last_login}<br>job: {last_job}</div></td>"
                f"<td><div class='row'>{toggle}{delete}</div><div style='margin-top:7px'>{reset}</div></td></tr>"
            )

        body = ADMIN_HTML_HEAD + """
<div class='card'><h3>Crea utente</h3><form method='post' action='/admin/users' class='row'>
<input name='username' placeholder='username' pattern='[a-z0-9._-]{3,32}' required>
<input name='password' type='password' minlength='10' placeholder='password (min 10)' required>
<select name='role'><option value='user'>user</option><option value='admin'>admin</option></select>
<button type='submit'>Crea</button></form></div>
<div class='card'><table><thead><tr><th>Utente</th><th>Attività</th><th>Azioni</th></tr></thead><tbody>""" + "".join(rows) + "</tbody></table></div></div></body></html>"
        return HTMLResponse(body)

    @web_app.post("/admin/users")
    def admin_create_user(request: Request, username: str = Form(...), password: str = Form(...), role: str = Form("user")):
        admin_or_403(request)
        if not safe_origin(request):
            raise HTTPException(status_code=403, detail="Origine richiesta non valida")
        username = normalize_username(username)
        if get_user(username):
            raise HTTPException(status_code=409, detail="Utente già esistente")
        try:
            users_store[user_key(username)] = new_user_record(username, password, role=role)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse("/admin", status_code=303)

    @web_app.post("/admin/users/{username}/toggle")
    def admin_toggle_user(request: Request, username: str):
        admin = admin_or_403(request)
        if not safe_origin(request):
            raise HTTPException(status_code=403, detail="Origine richiesta non valida")
        username = normalize_username(username)
        if username == admin["username"]:
            raise HTTPException(status_code=400, detail="Non puoi disabilitare l'account amministratore in uso")
        rec = get_user(username)
        if not rec:
            raise HTTPException(status_code=404, detail="Utente non trovato")
        rec["enabled"] = not bool(rec.get("enabled"))
        users_store[user_key(username)] = rec
        return RedirectResponse("/admin", status_code=303)

    @web_app.post("/admin/users/{username}/password")
    def admin_reset_password(request: Request, username: str, password: str = Form(...)):
        admin_or_403(request)
        if not safe_origin(request):
            raise HTTPException(status_code=403, detail="Origine richiesta non valida")
        username = normalize_username(username)
        rec = get_user(username)
        if not rec:
            raise HTTPException(status_code=404, detail="Utente non trovato")
        try:
            rec["password"] = make_password_record(password)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        users_store[user_key(username)] = rec
        return RedirectResponse("/admin", status_code=303)

    @web_app.post("/admin/users/{username}/delete")
    def admin_delete_user(request: Request, username: str):
        admin = admin_or_403(request)
        if not safe_origin(request):
            raise HTTPException(status_code=403, detail="Origine richiesta non valida")
        username = normalize_username(username)
        if username == admin["username"]:
            raise HTTPException(status_code=400, detail="Non puoi eliminare l'account amministratore in uso")
        rec = get_user(username)
        if not rec:
            raise HTTPException(status_code=404, detail="Utente non trovato")
        # Soft-delete: invalida immediatamente login e cookie senza dipendere
        # da un metodo delete della storage API.
        rec["enabled"] = False
        rec["deleted"] = True
        rec["deleted_at"] = utc_now_iso()
        users_store[user_key(username)] = rec
        return RedirectResponse("/admin", status_code=303)

    return web_app


@app.local_entrypoint()
def main():
    print("Prima configura il Secret raiw-auth con ADMIN_USER, ADMIN_PASSWORD e SESSION_SECRET.")
    print("Sviluppo: modal serve modal_app.py")
    print("Deploy:    modal deploy modal_app.py")

