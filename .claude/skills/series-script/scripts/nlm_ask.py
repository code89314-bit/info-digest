"""Ask NotebookLM questions (one per block in a text file, separated by lines '---') and save answers+references as JSON.
usage: python nlm_ask.py <notebook_id> <questions.txt> <out.json>

- Questions already answered in out.json are skipped (resume after a stop). Failed ones are asked again.
- On an error, waits and retries (NotebookLM sometimes refuses when asked too fast / in parallel).
  Ask one notebook at a time; do not run two of these in parallel.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

nb, qfile, out = sys.argv[1:4]
env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
env["PATH"] += os.pathsep + os.path.expandvars(r"%APPDATA%\Python\Python310\Scripts")
qs = [q.strip() for q in open(qfile, encoding="utf-8").read().split("\n---\n") if q.strip()]
res = json.load(open(out, encoding="utf-8")) if os.path.exists(out) else []
res = [r for r in res if r.get("answer")]          # keep only answered ones; failed ones are asked again
done = {r["q"] for r in res}
WAITS = [0, 60, 180]                               # seconds before each try


def ask(q: str) -> dict:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as f:
        f.write(q)
    try:
        p = subprocess.run(["notebooklm", "ask", "--prompt-file", f.name, "-n", nb, "--json"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", env=env, timeout=900)
    finally:
        os.unlink(f.name)
    try:
        data = json.loads(p.stdout)
    except json.JSONDecodeError:
        data = {}
    if not data.get("answer"):
        data = {"error": (p.stdout + "\n" + p.stderr)[-800:]}
    return data


for q in qs:
    if q in done:
        continue
    data = {}
    for w in WAITS:
        time.sleep(w)
        data = ask(q)
        if data.get("answer"):
            break
        print(f"  retry after error: {data['error'][-200:]!r}", flush=True)
    res.append({"q": q, "answer": data.get("answer"), "references": data.get("references"), "error": data.get("error")})
    json.dump(res, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[{len(res)}/{len(qs)}] {'ok' if data.get('answer') else 'ERR'} {q[:40]}", flush=True)
    time.sleep(5)
