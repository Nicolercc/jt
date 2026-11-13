"""The labeling page shows full job-search emails, so it must only answer
requests that really come from your own browser tab on 127.0.0.1."""

import csv
import http.client
import json
import sqlite3
import threading
from http.server import HTTPServer

import pytest

from evals import label_server

FIELDS = ["n", "date", "from", "subject", "preview", "kind", "company", "role", "gmail_id", "note"]


@pytest.fixture
def server(tmp_path):
    csv_path = tmp_path / "label-me.csv"
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerow({"n": "1", "date": "2026-08-01", "from": "a", "subject": "s", "preview": "p",
                    "kind": "", "company": "", "role": "", "gmail_id": "g1", "note": ""})
    db = tmp_path / "pool.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE raw_messages (gmail_msg_id TEXT PRIMARY KEY, received_on DATE, "
                 "sender TEXT, subject TEXT, body TEXT, processed INTEGER DEFAULT 0)")
    conn.execute("INSERT INTO raw_messages VALUES ('g1','2026-08-01','a','s','SECRET BODY ONE',0)")
    conn.commit()
    conn.close()
    httpd = HTTPServer(("127.0.0.1", 0), label_server.make_handler(csv_path, [db]))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield httpd.server_address[1], csv_path, db
    httpd.shutdown()
    httpd.server_close()


def request(port, method, path, *, host=None, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    h = {"Host": host or f"127.0.0.1:{port}", **(headers or {})}
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    data = r.read().decode()
    c.close()
    return r.status, data


SAVE = json.dumps([{"kind": "confirmed", "company": "Acme", "role": "Engineer", "note": ""}])


def labels(csv_path):
    with csv_path.open(newline="") as f:
        return [r["kind"] for r in csv.DictReader(f)]


# ---------------------------------------------------------------- reading

def test_own_page_loads_with_full_body(server):
    port, _, _ = server
    status, html = request(port, "GET", "/")
    assert status == 200 and "SECRET BODY ONE" in html


@pytest.mark.parametrize("host", ["localhost:{port}"])
def test_localhost_name_is_also_allowed(server, host):
    port, _, _ = server
    assert request(port, "GET", "/", host=host.format(port=port))[0] == 200


@pytest.mark.parametrize("host", ["evil.example:{port}", "evil.example", "127.0.0.1:1"])
def test_foreign_host_header_is_refused(server, host):
    """DNS rebinding: an attacker's domain resolves to 127.0.0.1, so the
    browser sends the attacker's name in Host. Refusing it stops the page
    from handing your emails to that site."""
    port, _, _ = server
    status, body = request(port, "GET", "/", host=host.format(port=port))
    assert status == 403 and "SECRET" not in body


def test_new_messages_appear_without_restarting(server):
    port, csv_path, db = server
    conn = sqlite3.connect(db)
    conn.execute("UPDATE raw_messages SET body = 'UPDATED BODY TWO' WHERE gmail_msg_id = 'g1'")
    conn.commit()
    conn.close()
    assert "UPDATED BODY TWO" in request(port, "GET", "/")[1]


# ---------------------------------------------------------------- writing

def test_own_page_can_save(server):
    port, csv_path, _ = server
    status, _ = request(port, "POST", "/save", body=SAVE,
                        headers={"Content-Type": "application/json",
                                 "Origin": f"http://127.0.0.1:{port}"})
    assert status == 200 and labels(csv_path) == ["confirmed"]


def test_cross_site_plain_text_post_is_refused(server):
    """A 'simple' cross-site POST (text/plain) skips the browser's CORS
    preflight, so the server itself must refuse it."""
    port, csv_path, _ = server
    status, _ = request(port, "POST", "/save", body=SAVE,
                        headers={"Content-Type": "text/plain",
                                 "Origin": "https://evil.example"})
    assert status in (403, 415) and labels(csv_path) == [""]


def test_json_post_from_foreign_origin_is_refused(server):
    port, csv_path, _ = server
    status, _ = request(port, "POST", "/save", body=SAVE,
                        headers={"Content-Type": "application/json",
                                 "Origin": "https://evil.example"})
    assert status == 403 and labels(csv_path) == [""]


def test_post_with_foreign_host_is_refused(server):
    port, csv_path, _ = server
    status, _ = request(port, "POST", "/save", body=SAVE, host="evil.example",
                        headers={"Content-Type": "application/json"})
    assert status == 403 and labels(csv_path) == [""]
