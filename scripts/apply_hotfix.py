from pathlib import Path


app = Path("modal_app.py")
text = app.read_text(encoding="utf-8")

if "import threading\n" not in text:
    anchor = "import tempfile\nimport time\n"
    if anchor not in text:
        raise SystemExit("import anchor not found")
    text = text.replace(anchor, "import tempfile\nimport threading\nimport time\n", 1)

start = text.find("            proc = subprocess.run(\n")
end = text.find("\n            if proc.returncode != 0:", start)
if start < 0 or end < 0:
    raise SystemExit("subprocess.run block not found")

replacement = '''            print(f"[job] Avvio elaborazione: {safe_name}", flush=True)
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
'''
text = text[:start] + replacement + text[end:]

timeout_start = text.find("    except subprocess.TimeoutExpired as exc:\n")
timeout_end = text.find("    except Exception as exc:\n", timeout_start)
if timeout_start >= 0 and timeout_end >= 0:
    text = text[:timeout_start] + text[timeout_end:]

old_poll = "async function poll(token){for(;;){const r=await fetch('/result/'+encodeURIComponent(token));if(r.status===202){statusBox.textContent='⏳ Elaborazione in corso…';await new Promise(x=>setTimeout(x,4000));continue;}const d=await r.json();progress.classList.add('hidden');if(d.status==='done'){statusBox.innerHTML='<span class=\"ok\">✅ Completato</span> · '+d.output_name+' · '+d.elapsed_seconds+' s';download.href='/download/'+encodeURIComponent(token);logs.href='/logs/'+encodeURIComponent(token);actions.classList.remove('hidden');freeStatus();}else{statusBox.innerHTML='<span class=\"err\">❌ '+(d.message||'Errore')+'</span>';logs.href='/logs/'+encodeURIComponent(token);actions.classList.remove('hidden');}break;}}"
new_poll = "async function poll(token){const started=Date.now();for(;;){const r=await fetch('/result/'+encodeURIComponent(token));if(r.status===202){const elapsed=Math.floor((Date.now()-started)/1000);statusBox.textContent='⏳ Elaborazione in corso… '+elapsed+' s';await new Promise(x=>setTimeout(x,4000));continue;}const d=await r.json();progress.classList.add('hidden');if(d.status==='done'){statusBox.innerHTML='<span class=\"ok\">✅ Completato</span> · '+d.output_name+' · '+d.elapsed_seconds+' s';download.href='/download/'+encodeURIComponent(token);logs.href='/logs/'+encodeURIComponent(token);actions.classList.remove('hidden');freeStatus();}else{statusBox.innerHTML='<span class=\"err\">❌ '+(d.message||'Errore')+'</span>';logs.href='/logs/'+encodeURIComponent(token);actions.classList.remove('hidden');}break;}}"
if old_poll not in text:
    raise SystemExit("frontend polling anchor not found")
text = text.replace(old_poll, new_poll, 1)
app.write_text(text, encoding="utf-8")

agents = Path("AGENTS.md")
atext = agents.read_text(encoding="utf-8")
rule = "5. After every successfully validated change, update `AGENTS.md` when relevant, commit the coherent change, push it to `main`, and verify the resulting GitHub Actions run. Do not leave successful changes only in the local checkout.\n"
if rule not in atext:
    anchor = "4. Prefer the smallest change that fixes the root cause and verify the resulting GitHub Actions run.\n"
    if anchor not in atext:
        raise SystemExit("AGENTS rule anchor not found")
    atext = atext.replace(anchor, anchor + rule, 1)

bullet1 = "- Worker subprocess output is now streamed to Modal runtime logs while retaining the final log tail. A 30-second heartbeat makes long model downloads/inference visibly alive, and the web UI shows elapsed processing time while `/result` remains HTTP 202."
bullet2 = "- After a validated modification, keep `AGENTS.md` current, commit, push to `main`, and verify the resulting Actions run before considering the change complete."
if bullet1 not in atext:
    marker = "### 2026-09-14\n\n"
    if marker not in atext:
        raise SystemExit("AGENTS changelog anchor not found")
    atext = atext.replace(marker, marker + bullet1 + "\n" + bullet2 + "\n\n", 1)
agents.write_text(atext, encoding="utf-8")
