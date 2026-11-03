"""Hardening found by the security review: what happens to uploaded files (a malware scan, a total unpacking budget), how long exchanges are kept (retention, enforced), and how many database
connections a burst may take. Real schema and embedding model where they matter; a stand-in clamd over a real socket."""
import socket
import struct
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("cryptography")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from pgdb import make_db  # noqa: E402

from faqengine import sources  # noqa: E402
from faqengine.hosted import scanner as sc  # noqa: E402
from faqengine.hosted import service as svc  # noqa: E402
from faqengine.hosted.secrets import SecretBox  # noqa: E402
from faqengine.hosted.store import Store  # noqa: E402
from faqengine.runtime.embedder import Embedder  # noqa: E402

MODEL = ROOT / "dist" / "bike-shop" / "model"
EICAR = rb"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


class FakeClamd:
    """The clamd INSTREAM protocol: zINSTREAM, length-prefixed chunks, a zero length, a one-line verdict."""

    def __init__(self, verdict=None):
        self.verdict, self.seen, self.chunks = verdict, [], 0
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(8)
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(conn,), daemon=True).start()

    def _one(self, c):
        with c:
            buf = b""
            while len(buf) < 10:
                buf += c.recv(10 - len(buf))
            assert buf == b"zINSTREAM\0"
            data = b""
            while True:
                n = struct.unpack("!I", self._read(c, 4))[0]
                if n == 0:
                    break
                self.chunks += 1
                data += self._read(c, n)
            self.seen.append(data)
            c.sendall(((self.verdict(data) if self.verdict else ("stream: Eicar-Test-Signature FOUND" if b"EICAR" in data else "stream: OK")) + "\0").encode())

    @staticmethod
    def _read(c, n):
        out = b""
        while len(out) < n:
            got = c.recv(n - len(out))
            if not got:
                raise ConnectionError
            out += got
        return out

    def close(self):
        self.srv.close()


# ---------------------------------------------------------------- the scanner client
def test_a_clean_file_passes_and_an_infected_one_is_named():
    d = FakeClamd()
    try:
        s = sc.ClamdScanner("127.0.0.1", d.port)
        assert s.scan("a.txt", b"hello") is None
        with pytest.raises(sc.Infected) as e:
            s.scan("a.txt", EICAR)
        assert e.value.signature == "Eicar-Test-Signature"
        big = b"x" * (3 * sc.CHUNK + 5)
        assert s.scan("big.bin", big) is None and d.seen[-1] == big and d.chunks >= 4  # streamed in chunks, whole
    finally:
        d.close()


def test_a_scanner_that_cannot_be_reached_or_says_nothing_usable_is_a_scan_error_not_a_pass():
    with pytest.raises(sc.ScanError):
        sc.ClamdScanner("127.0.0.1", 1, timeout=1).scan("a", b"x")
    d = FakeClamd(verdict=lambda data: "INSTREAM size limit exceeded. ERROR")
    try:
        with pytest.raises(sc.ScanError):
            sc.ClamdScanner("127.0.0.1", d.port).scan("a", b"x")
    finally:
        d.close()
    assert sc.NullScanner().scan("a", b"x") is None


# ---------------------------------------------------------------- an office file's total unpacking budget
def make_xlsx(path, sheets, payload=2000):
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook {ns}><sheets>' + "".join(f'<sheet name="S{i}" sheetId="{i}" r:id="rId{i}"/>' for i in range(1, sheets + 1)) + "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + "".join(f'<Relationship Id="rId{i}" Target="worksheets/sheet{i}.xml"/>' for i in range(1, sheets + 1)) + "</Relationships>")
        for i in range(1, sheets + 1):
            rows = '<row r="1"><c r="A1" t="inlineStr"><is><t>question</t></is></c><c r="B1" t="inlineStr"><is><t>answer</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>q</t></is></c><c r="B2" t="inlineStr"><is><t>' + "a" * payload + "</t></is></c></row>"
            z.writestr(f"xl/worksheets/sheet{i}.xml", f"<worksheet {ns}><sheetData>{rows}</sheetData></worksheet>")


def test_many_sheets_cannot_add_up_past_the_total_budget(tmp_path, monkeypatch):
    f = tmp_path / "w.xlsx"
    make_xlsx(f, 6)
    assert len(sources._xlsx_sheets(f)) == 6  # a normal workbook reads
    monkeypatch.setattr(sources, "MAX_TOTAL_BYTES", 6000)  # each sheet is far under the per-part limit; together they are not
    with pytest.raises(ValueError, match="too large to read"):
        sources._xlsx_sheets(f)


# ---------------------------------------------------------------- the service: scanning in ingest, retention, connection cap
@pytest.fixture(scope="module")
def db():
    yield from make_db()


class Files(dict):
    def __call__(self, path):
        return self[path]


@pytest.fixture(scope="module")
def world(db):
    w = type("W", (), {})()
    w.owner, w.other = str(db.user("h-owner")), str(db.user("h-other"))
    w.A, w.B = str(db.business(w.owner, "hard-a")), str(db.business(w.other, "hard-b"))
    return w


def engine_with(db, scanner=None, required=False):
    box = SecretBox({1: SecretBox.generate() and __import__("base64").b64decode(SecretBox.generate())})
    return svc.HostedEngine(Store(db.dsn), Embedder(MODEL), box, Files(), rate_per_minute=10_000, scanner=scanner, scan_required=required)


def upload(db, engine, biz, owner, name, data):
    engine.fetch_object[f"{biz}/{name}"] = data
    sid = str(db.su("insert into public.knowledge_sources (business_id, name, source_type, storage_path) values (%s, %s, 'document', %s) returning id", (biz, name, f"{biz}/{name}"))[0][0])
    return sid


@pytest.mark.skipif(not MODEL.is_dir(), reason="needs an exported embedding model (dist/bike-shop/model)")
def test_a_scanned_file_is_read_an_infected_one_is_refused_and_the_match_is_recorded_without_the_file(db, world):
    d = FakeClamd()
    try:
        e = engine_with(db, sc.ClamdScanner("127.0.0.1", d.port))
        ok = e.ingest(world.A, world.owner, upload(db, e, world.A, world.owner, "faq.md", b"# Shop\n\n## Hours\n\nWe are open at nine.\n"))
        assert ok["scanned"] is True and ok["chunks"] == 1
        sid = upload(db, e, world.A, world.owner, "bad.md", b"# Bad\n\n## Note\n\n" + EICAR + b"\n")
        with pytest.raises(svc.ServiceError) as err:
            e.ingest(world.A, world.owner, sid)
        assert err.value.status == 422 and "malware signature" in err.value.message
        assert db.su("select status from public.knowledge_sources where id = %s", (sid,))[0][0] == "failed"
        assert db.su("select count(*) from public.document_chunks where source_id = %s", (sid,))[0][0] == 0  # nothing of it was stored
        rec = db.su("select detail::text from public.audit_logs where business_id = %s and action = 'source.quarantined'", (world.A,))[0][0]
        assert "Eicar-Test-Signature" in rec and "EICAR-STANDARD" not in rec
        assert e.tele.counters["upload_quarantined"] == 1
    finally:
        d.close()


@pytest.mark.skipif(not MODEL.is_dir(), reason="needs an exported embedding model (dist/bike-shop/model)")
def test_when_the_scanner_is_down_the_file_is_read_unscanned_and_said_so_unless_scanning_is_required(db, world):
    optional = engine_with(db, sc.ClamdScanner("127.0.0.1", 1, timeout=1))
    done = optional.ingest(world.B, world.other, upload(db, optional, world.B, world.other, "a.md", b"# A\n\n## B\n\nWe sell bikes.\n"))
    assert done["scanned"] is False and done["chunks"] == 1  # read, and the result says it was not scanned
    strict = engine_with(db, sc.ClamdScanner("127.0.0.1", 1, timeout=1), required=True)
    sid = upload(db, strict, world.B, world.other, "b.md", b"# A\n\n## B\n\nWe sell helmets.\n")
    with pytest.raises(svc.ServiceError) as e:
        strict.ingest(world.B, world.other, sid)
    assert e.value.status == 503 and db.su("select count(*) from public.document_chunks where source_id = %s", (sid,))[0][0] == 0
    none_but_required = engine_with(db, None, required=True)
    with pytest.raises(svc.ServiceError) as e:
        none_but_required.ingest(world.B, world.other, upload(db, none_but_required, world.B, world.other, "c.md", b"# A\n\n## B\n\nx\n"))
    assert e.value.status == 503 and "no scanner is configured" in e.value.message


def message(db, biz, user, text, days_ago, conv=None):
    cid = conv or str(db.su("insert into public.conversations (business_id, created_by, created_at) values (%s, %s, now() - make_interval(days => %s)) returning id", (biz, user, days_ago))[0][0])
    mid = str(db.su("insert into public.messages (business_id, conversation_id, role, content, created_at) values (%s, %s, 'customer', %s, now() - make_interval(days => %s)) returning id", (biz, cid, text, days_ago))[0][0])
    return cid, mid


def test_retention_deletes_what_each_business_chose_and_nothing_else(db, world):
    st = Store(db.dsn)
    db.su("update public.business_settings set privacy = '{\"log_questions\": true, \"retention_days\": 30}' where business_id = %s", (world.A,))
    db.su("update public.business_settings set privacy = '{\"retention_days\": \"banana\"}' where business_id = %s", (world.B,))  # not a number: the default of 365 applies
    old_c, old_m = message(db, world.A, world.owner, "old question A", 45)
    new_c, new_m = message(db, world.A, world.owner, "new question A", 5)
    b_old = message(db, world.B, world.other, "B 100 days old", 100)
    b_ancient = message(db, world.B, world.other, "B 400 days old", 400)
    db.su("insert into public.feedback (business_id, message_id, verdict, created_by) values (%s, %s, 'approve', %s)", (world.A, old_m, world.owner))
    counts = st.purge_expired()
    assert counts["messages_deleted"] == 2 and counts["conversations_deleted"] >= 2
    have = {r[0] for r in db.su("select content from public.messages")}
    assert "old question A" not in have and "B 400 days old" not in have and {"new question A", "B 100 days old"} <= have  # each business by its own setting
    assert db.su("select count(*) from public.feedback where message_id = %s", (old_m,))[0][0] == 0  # the feedback on a deleted exchange goes with it
    assert db.su("select count(*) from public.conversations where id = %s", (old_c,))[0][0] == 0 and db.su("select count(*) from public.conversations where id = %s", (new_c,))[0][0] == 1
    assert db.su("select count(*) from public.audit_logs where business_id = %s", (world.A,))[0][0] > 0  # the audit trail is append-only and stays
    assert st.purge_expired() == {"messages_deleted": 0, "conversations_deleted": 0} and b_old and b_ancient


def test_retention_is_bounded_to_a_day_at_least_and_ten_years_at_most(db, world):
    st = Store(db.dsn)
    db.su("update public.business_settings set privacy = '{\"retention_days\": 0}' where business_id = %s", (world.A,))
    message(db, world.A, world.owner, "two days old, retention 0", 2)
    message(db, world.A, world.owner, "today", 0)
    st.purge_expired()
    have = {r[0] for r in db.su("select content from public.messages where business_id = %s", (world.A,))}
    assert "two days old, retention 0" not in have and "today" in have  # 0 means the minimum, one day: never "delete everything now"
    db.su("update public.business_settings set privacy = '{\"retention_days\": 99999}' where business_id = %s", (world.A,))
    message(db, world.A, world.owner, "eleven years old", 4000)
    st.purge_expired()
    assert "eleven years old" not in {r[0] for r in db.su("select content from public.messages where business_id = %s", (world.A,))}  # the cap is ten years
    db.su("update public.business_settings set privacy = '{\"log_questions\": true, \"retention_days\": 365}' where business_id in (%s, %s)", (world.A, world.B))


@pytest.mark.skipif(not MODEL.is_dir(), reason="needs an exported embedding model (dist/bike-shop/model)")
def test_the_purge_route_is_behind_the_shared_secret_and_reports_counts_only(db, world):
    import json
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    e = engine_with(db)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), svc.make_handler(e, "shared-secret-for-tests-0123456789"))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        def post(secret):
            req = urllib.request.Request(f"http://127.0.0.1:{httpd.server_port}/v1/maintenance/purge", data=b"{}", method="POST", headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {secret}"} if secret else {})})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as err:
                return err.code, json.loads(err.read())
        assert post(None)[0] == 401 and post("wrong")[0] == 401
        status, body = post("shared-secret-for-tests-0123456789")
        assert status == 200 and set(body) == {"messages_deleted", "conversations_deleted"}
    finally:
        httpd.shutdown()


def test_a_burst_of_requests_cannot_open_more_connections_than_the_cap(db, monkeypatch):
    live, peak = [0], [0]
    lock = threading.Lock()
    real = psycopg.connect

    def counting(*a, **k):
        c = real(*a, **k)
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        orig_close = c.close

        def close():
            with lock:
                live[0] -= 1
            orig_close()
        c.close = close
        return c

    monkeypatch.setattr(psycopg, "connect", counting)
    st = Store(db.dsn, max_connections=2)

    def work():
        st._one("select pg_sleep(0.05)")

    threads = [threading.Thread(target=work) for _ in range(12)]
    started = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert peak[0] <= 2 and time.monotonic() - started >= 0.25  # they took turns: 12 sleeps of 50 ms through 2 connections
    assert st._one("select 1")[0] == 1 and live[0] == 0


@pytest.mark.skipif(not MODEL.is_dir(), reason="needs an exported embedding model (dist/bike-shop/model)")
@pytest.mark.parametrize("name", [".env", "prod.env", ".env.local", "server.pem", "id_rsa", "credentials", "secrets.json", "deploy.key"])
def test_a_file_of_secrets_is_refused_as_knowledge_and_nothing_of_it_is_stored(db, world, name):
    e = engine_with(db)
    sid = upload(db, e, world.A, world.owner, name, b"OPENAI_API_KEY=sk-proj-AAAAAAAAAAAAAAAA1234\nDATABASE_URL=postgres://u:hunter2@db/app\n")
    with pytest.raises(svc.ServiceError) as err:
        e.ingest(world.A, world.owner, sid)
    assert err.value.status == 422 and "file of secrets" in err.value.message and "hunter2" not in err.value.message and "sk-proj" not in err.value.message
    assert db.su("select status from public.knowledge_sources where id = %s", (sid,))[0][0] == "failed"
    assert db.su("select count(*) from public.document_chunks where source_id = %s", (sid,))[0][0] == 0 and db.su("select count(*) from public.faqs where source_id = %s", (sid,))[0][0] == 0
