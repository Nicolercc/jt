#!/usr/bin/env python3
"""Label real emails for the classifier eval in a local browser page.

Serves evals/raw/label-me.csv on 127.0.0.1 only and writes labels back to the
same file. Nothing leaves this machine.

    python3 evals/label_server.py [path/to/label-me.csv] [--port 8765]
"""

import argparse
import csv
import html
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# "" = not labeled yet (skipped by `evals.dataset build`); "none" = labeled,
# not a status change.
KINDS = ["", "none", "confirmed", "response", "screen", "tech", "onsite", "final",
         "offer", "rejected"]
LABEL_FIELDS = ("kind", "company", "role")
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


def render(rows: list[dict]) -> str:
    cards = []
    for i, r in enumerate(rows):
        opts = "".join(
            f'<option value="{k}"{" selected" if r.get("kind", "") == k else ""}>'
            f'{KIND_LABELS.get(k, k)}</option>' for k in KINDS)
        e = {k: html.escape(r.get(k, "") or "") for k in r}
        cards.append(f"""
<section class="card" data-i="{i}">
  <div class="meta">#{e['n']} · {e['date']} · {e['from']}</div>
  <h2>{e['subject']}</h2>
  <p class="preview">{e['preview']}</p>
  <div class="fields">
    <label>Kind<select name="kind">{opts}</select></label>
    <label>Company<input name="company" list="companies" value="{e.get('company', '')}"></label>
    <label>Role<input name="role" list="roles" value="{e.get('role', '')}"></label>
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
.fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px}
label{display:flex;flex-direction:column;font-size:12px;color:var(--muted);gap:3px}
select,input{font:inherit;color:var(--ink);background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:6px}
</style></head><body>
<header><strong>Label {{COUNT}} job emails</strong><span id="status">Saves to your Mac only</span><button id="save">Save</button></header>
<main>
<p>Pick what each email really means. Choose "not a status change" for job alerts, newsletters, talent-community mail, scams, or jobs you didn't apply to. Rows left on "not labeled yet" are skipped. Keep company and role names spelled exactly the same each time.</p>
{{CARDS}}
</main>
<datalist id="companies"></datalist><datalist id="roles"></datalist>
<script>
const cards=[...document.querySelectorAll('.card')];
function collect(){return cards.map(c=>({kind:c.querySelector('[name=kind]').value,company:c.querySelector('[name=company]').value.trim(),role:c.querySelector('[name=role]').value.trim()}));}
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


def make_handler(path: Path):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: str, ctype: str = "text/plain") -> None:
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            if self.path != "/":
                return self._send(404, "not found")
            _, rows = read_rows(path)
            self._send(200, render(rows), "text/html")

        def do_POST(self):  # noqa: N802
            if self.path != "/save":
                return self._send(404, "not found")
            fields, rows = read_rows(path)
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
    args = p.parse_args()
    path = Path(args.csv).resolve()
    server = HTTPServer(("127.0.0.1", args.port), make_handler(path))
    print(f"Labeling {path.name} at http://127.0.0.1:{args.port}  (Ctrl-C to stop)")
    server.serve_forever()


if __name__ == "__main__":
    main()
