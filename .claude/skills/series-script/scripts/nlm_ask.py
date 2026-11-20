"""Ask NotebookLM questions (one per block in a text file, separated by lines '---') and save answers+references as JSON.
usage: python nlm_ask.py <notebook_id> <questions.txt> <out.json>"""
import json
import os
import subprocess
import sys
import tempfile

nb, qfile, out = sys.argv[1:4]
env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
env["PATH"] += os.pathsep + os.path.expandvars(r"%APPDATA%\Python\Python310\Scripts")
qs = [q.strip() for q in open(qfile, encoding="utf-8").read().split("\n---\n") if q.strip()]
res = json.load(open(out, encoding="utf-8")) if os.path.exists(out) else []
done = {r["q"] for r in res}
for q in qs:
    if q in done:
        continue
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as f:
        f.write(q)
    p = subprocess.run(["notebooklm", "ask", "--prompt-file", f.name, "-n", nb, "--json"], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", env=env, timeout=900)
    os.unlink(f.name)
    try:
        data = json.loads(p.stdout)
    except json.JSONDecodeError:
        data = {"error": (p.stdout + p.stderr)[-500:]}
    res.append({"q": q, "answer": data.get("answer"), "references": data.get("references"), "error": data.get("error")})
    json.dump(res, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[{len(res)}] {'ok' if data.get('answer') else 'ERR'} {q[:40]}", flush=True)
