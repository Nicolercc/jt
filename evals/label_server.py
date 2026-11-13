#!/usr/bin/env python3
"""Label real emails for the classifier eval in a local browser page.

Serves evals/raw/label-me.csv on 127.0.0.1 only and writes labels back to the
same file. Nothing leaves this machine. Full email bodies are read (read-only)
from the jt databases so you can label from the whole message, not a preview.

    python3 evals/label_server.py [path/to/label-me.csv] [--port 8765] [--db PATH ...]

How to label: docs/EVAL_LABELING.md
"""

import argparse
import csv
import html
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals import dataset  # noqa: E402

DEFAULT_DBS = (Path.home() / ".jobtrack" / "jobs.db", ROOT / "evals/raw/pool.db")
GMAIL_URL = "https://mail.google.com/mail/u/0/#all/{}"
# "" = not labeled yet (skipped by `evals.dataset build`); "none" = labeled,
# not a status change.
KINDS = ["", "none", "confirmed", "response", "screen", "tech", "onsite", "final",
         "offer", "rejected"]
LABEL_FIELDS = ("kind", "company", "role", "note")
KIND_LABELS = {"": "— not labeled yet —", "none": "not a status change"}


def read_rows(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or []), list(reader)


def write_rows(path: Path, fields: list[str], rows: list[dict]) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)


def load_bodies(db_paths: list[Path]) -> dict[str, str]:
    existing = [p for p in db_paths if Path(p).expanduser().exists()]
    return {gid: (m["body"] or "") for gid, m in dataset._messages(existing).items()}


def render(rows: list[dict], bodies: dict[str, str] | None = None) -> str:
    bodies = bodies or {}
    cards = []
    for i, r in enumerate(rows):
        opts = "".join(
            f'<option value="{k}"{" selected" if r.get("kind", "") == k else ""}>'
            f'{KIND_LABELS.get(k, k)}</option>' for k in KINDS)
        e = {k: html.escape(r.get(k, "") or "") for k in r}
        gid = r.get("gmail_id", "") or ""
        body = bodies.get(gid)
        full = (f'<details><summary>Full email</summary><pre class="body">'
                f'{html.escape(body)}</pre></details>' if body is not None
                else '<p class="meta">Full email not found in the databases given.</p>')
        link = (f'<a href="{html.escape(GMAIL_URL.format(gid))}" target="_blank" '
                f'rel="noopener noreferrer">Open in Gmail</a>' if gid else "")
        cards.append(f"""
<section class="card" data-i="{i}">
  <div class="meta">#{e['n']} · {e['date']} · {e['from']}</div>
  <h2>{e['subject']}</h2>
  <p class="preview">{e['preview']}</p>
  {full}
  <p class="links">{link}</p>
  <div class="fields">
    <label>Kind<select name="kind">{opts}</select></label>
    <label>Company<input name="company" list="companies" value="{e.get('company', '')}"></label>
    <label>Role<input name="role" list="roles" value="{e.get('role', '')}"></label>
    <label class="wide">Note (optional: why it's tricky, or "unsure")<input name="note" value="{e.get('note', '')}"></label>
  </div>
</section>""")
    return PAGE.replace("{{CARDS}}", "".join(cards)).replace("{{COUNT}}", str(len(rows)))


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>jt eval labels</title>
<style>
:root{--bg:#f6f6f4;--card:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e2e2dc;--accent:#2f5d50}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#20201f;--ink:#ececea;--muted:#9a9a94;--line:#33332f;--accent:#7fb8a6}}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,system-ui,sans-serif}
header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);padding:12px 16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
header strong{flex:1}
#status{color:var(--muted)}
button{background:var(--accent);color:#fff;border:0;border-radius:6px;padding:8px 16px;font-size:15px;cursor:pointer}
main{max-width:760px;margin:0 auto;padding:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px 16px;margin-bottom:12px}
.card.done{border-left:4px solid var(--accent)}
.meta{color:var(--muted);font-size:13px;word-break:break-all}
h2{font-size:16px;margin:4px 0 6px}
.preview{color:var(--muted);font-size:13px;margin:0 0 10px}
details{margin:0 0 8px}summary{cursor:pointer;color:var(--accent);font-size:13px}
pre.body{white-space:pre-wrap;word-break:break-word;font:13px/1.45 ui-monospace,Menlo,monospace;background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:10px;max-height:420px;overflow:auto}
.links{margin:0 0 10px;font-size:13px}.links a{color:var(--accent)}
.wide{grid-column:1/-1}
.hide-done .card.done{display:none}
#filter{display:flex;gap:6px;align-items:center;font-size:14px;color:var(--ink)}
.fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px}
label{display:flex;flex-direction:column;font-size:12px;color:var(--muted);gap:3px}
select,input{font:inherit;color:var(--ink);background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:6px}
</style></head><body>
<header><strong>Label {{COUNT}} job emails</strong><label id="filter"><input type="checkbox" id="hide"> Hide labeled</label><span id="status">Saves to your Mac only</span><button id="save">Save</button></header>
<main>
<p>Pick what each email really means. Open "Full email" before deciding. Choose "not a status change" for job alerts, newsletters, talent-community mail, scams, or jobs you didn't apply to. Rows left on "not labeled yet" are skipped. Keep company and role names spelled exactly the same each time. Full rules: docs/EVAL_LABELING.md.</p>
{{CARDS}}
</main>
<datalist id="companies"></datalist><datalist id="roles"></datalist>
<script>
const cards=[...document.querySelectorAll('.card')];
function collect(){return cards.map(c=>({kind:c.querySelector('[name=kind]').value,company:c.querySelector('[name=company]').value.trim(),role:c.querySelector('[name=role]').value.trim(),note:c.querySelector('[name=note]').value.trim()}));}
document.getElementById('hide').onchange=e=>document.body.classList.toggle('hide-done',e.target.checked);
function refresh(){
  const d=collect(); let n=0;
  cards.forEach((c,i)=>{const ok=d[i].kind==='none'||(d[i].company&&d[i].role);const touched=!!d[i].kind;c.classList.toggle('done',!!(touched&&ok));if(touched&&ok)n++;});
  for(const [id,key] of [['companies','company'],['roles','role']]){
    document.getElementById(id).innerHTML=[...new Set(d.map(x=>x[key]).filter(Boolean))].map(v=>`<option value="${v.replace(/"/g,'&quot;')}">`).join('');}
  return n;}
document.addEventListener('input',()=>{refresh();document.getElementById('status').textContent='Unsaved changes';});
document.getElementById('save').onclick=async()=>{
  const r=await fetch('/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(collect())});
  document.getElementById('status').textContent=r.ok?`Saved · ${refresh()} of ${cards.length} look complete`:'Save failed: '+await r.text();};
refresh();
</script></body></html>"""


def make_handler(path: Path, db_paths: list[Path] | None = None):
    """Request handler for one label CSV. Bodies are read from `db_paths` on
    every page load, so messages exported while the server runs show up.

    The page holds full job-search emails, so it answers only requests that
    come from your own tab:
      - Host must be 127.0.0.1:<port> or localhost:<port>. This blocks DNS
        rebinding, where a website's domain is pointed at 127.0.0.1 so the
        browser treats this server as that site and lets it read the page.
      - Saves must be application/json, and any Origin must be this server.
        A cross-site text/plain POST skips the CORS preflight, so the server
        has to refuse it itself.
    """
    dbs = list(db_paths or [])

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: str, ctype: str = "text/plain") -> None:
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _own_origins(self) -> set[str]:
            port = self.server.server_address[1]  # type: ignore[index]
            return {f"127.0.0.1:{port}", f"localhost:{port}"}

        def _host_ok(self) -> bool:
            return (self.headers.get("Host") or "").strip().lower() in self._own_origins()

        def _origin_ok(self) -> bool:
            origin = self.headers.get("Origin")
            if origin is None:          # non-browser clients (curl, tests) send none
                return True
            return origin.lower() in {f"http://{h}" for h in self._own_origins()}

        def do_GET(self):  # noqa: N802
            if not self._host_ok():
                return self._send(403, "forbidden host")
            if self.path != "/":
                return self._send(404, "not found")
            _, rows = read_rows(path)
            self._send(200, render(rows, load_bodies(dbs)), "text/html")

        def do_POST(self):  # noqa: N802
            if not self._host_ok() or not self._origin_ok():
                return self._send(403, "forbidden origin")
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return self._send(415, "saves must be application/json")
            if self.path != "/save":
                return self._send(404, "not found")
            fields, rows = read_rows(path)
            if "note" not in fields:
                fields.append("note")
            try:
                labels = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if len(labels) != len(rows):
                    raise ValueError("row count changed; reload the page")
                for row, lab in zip(rows, labels):
                    if lab["kind"] not in KINDS:
                        raise ValueError(f"unknown kind {lab['kind']!r}")
                    for k in LABEL_FIELDS:
                        row[k] = str(lab.get(k, "")).strip()
            except (ValueError, KeyError, TypeError) as e:
                return self._send(400, str(e))
            write_rows(path, fields, rows)
            self._send(200, "ok")

        def log_message(self, *args):
            pass

    return Handler


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("csv", nargs="?", default=str(ROOT / "evals/raw/label-me.csv"))
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--db", action="append", type=Path,
                   help="database(s) with raw_messages for full bodies "
                        "(default: ~/.jobtrack/jobs.db and evals/raw/pool.db if present)")
    args = p.parse_args()
    path = Path(args.csv).resolve()
    dbs = args.db or list(DEFAULT_DBS)
    print(f"Full text available for {len(load_bodies(dbs))} emails")
    server = HTTPServer(("127.0.0.1", args.port), make_handler(path, dbs))
    print(f"Labeling {path.name} at http://127.0.0.1:{args.port}  (Ctrl-C to stop)")
    server.serve_forever()


if __name__ == "__main__":
    main()
