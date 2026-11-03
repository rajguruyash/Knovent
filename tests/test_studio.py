"""The website's server: routes, validation and security, with no model (the model's own tests are in test_pipeline.py)."""
import io
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from faqengine.studio import feedback as fb
from faqengine.studio import server
from faqengine.studio.store import Store, write_json

CSV = "question,answer\nHow do I reset my password?,Use the reset link.\nWhere are you?,Here.\nDo you deliver?,Yes.\n"


@pytest.fixture()
def api(tmp_path):
    store = Store(tmp_path)
    server.Handler.app, server.Handler.port = server.App(store), 0
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    server.Handler.port = httpd.server_port
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"

    def call(method, path, body=None, headers=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        h = {"Content-Type": "application/json", **(headers or {})}
        req = urllib.request.Request(base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def j(method, path, body=None, **kw):
        status, data = call(method, path, body, **kw)
        return status, json.loads(data)

    yield type("Api", (), {"call": staticmethod(call), "j": staticmethod(j), "store": store, "port": httpd.server_port})
    httpd.shutdown()


def test_page_template_and_overview(api):
    status, html = api.call("GET", "/")
    assert status == 200 and b"<title>" in html
    status, tpl = api.call("GET", "/api/template.csv")
    assert status == 200 and tpl.startswith(b"id,question,answer")
    status, o = api.j("GET", "/api/overview")
    assert status == 200 and o["projects"] == [] and "cpu" in o["hardware"]


def test_upload_lists_previews_and_deletes(api):
    status, r = api.j("POST", "/api/projects", {"name": "Shop", "filename": "shop.csv", "text": CSV})
    assert status == 200
    pid = r["id"]
    status, p = api.j("GET", f"/api/projects/{pid}")
    assert p["faq"]["entries"] == 3 and p["faq"]["preview"][0]["question"] == "How do I reset my password?" and p["status"]["state"] == "new" and p["report"] is None
    assert api.j("GET", "/api/overview")[1]["projects"][0]["name"] == "Shop"
    assert api.j("GET", f"/api/projects/{pid}?light=1")[1]["status"]["state"] == "new"
    assert api.j("DELETE", f"/api/projects/{pid}")[0] == 200
    assert api.j("GET", f"/api/projects/{pid}")[0] == 404


def test_a_table_of_plain_values_is_accepted_and_read_row_by_row(api):
    status, r = api.j("POST", "/api/projects", {"filename": "x.csv", "text": "a,b\n1,2\n"})
    assert status == 200
    summary = api.j("GET", f"/api/projects/{r['id']}")[1]["faq"]
    assert summary["entries"] == 0 and summary["passages"] == 1 and summary["sources"][0]["kind"] == "records"


def test_bad_uploads_are_refused_with_a_reason_and_leave_nothing_behind(api):
    for body, words in [({"filename": "x.png", "text": CSV}, "unsupported"), ({"filename": "x.csv", "text": "a,b\n"}, "no rows"),  # a header and nothing else; a table of plain values is now readable (tested below)
                        
                        ({"filename": "x.json", "text": "{not json"}, "valid JSON"), ({"filename": "x.csv", "text": ""}, "")]:
        status, r = api.j("POST", "/api/projects", body)
        assert status == 400 and words in r["error"], (body, r)
    assert api.j("GET", "/api/overview")[1]["projects"] == []
    assert api.j("POST", "/api/projects", raw=b"{}", headers={"Content-Type": "text/plain"})[0] == 400


def test_the_example_project_is_available(api):
    status, r = api.j("POST", "/api/projects/example")
    assert status == 200
    assert api.j("GET", f"/api/projects/{r['id']}")[1]["faq"]["entries"] == 10


def test_other_websites_and_odd_paths_are_refused(api):
    assert api.call("GET", "/api/overview", headers={"Host": "evil.example.com"})[0] == 403
    assert api.call("POST", "/api/projects", {"filename": "a.csv", "text": CSV}, headers={"Origin": "https://evil.example.com"})[0] == 403
    assert api.call("GET", "/static/../store.py")[0] == 404
    assert api.call("GET", "/api/projects/..%2f..%2fetc")[0] == 404
    assert api.call("GET", "/api/projects/0123456789ab")[0] == 404
    assert api.call("POST", "/api/projects/0123456789ab/train", {})[0] == 404


def test_testing_before_training_is_refused(api):
    pid = api.j("POST", "/api/projects", {"filename": "s.csv", "text": CSV})[1]["id"]
    status, r = api.j("POST", f"/api/projects/{pid}/test", {"query": "hello"})
    assert status == 400 and "Train" in r["error"]
    assert api.j("POST", f"/api/projects/{pid}/export", {})[0] == 400


def _trained(api, pid):
    d = api.store.dir(pid)
    (d / "run").mkdir()
    write_json(d / "status.json", {"state": "trained"})
    write_json(d / "run" / "thresholds.json", {"floor": 0.3, "direct": None, "source": "self-check", "direct_reason": "x"})
    write_json(d / "run" / "thresholds.auto.json", {"floor": 0.3, "direct": None, "source": "self-check", "direct_reason": "x"})
    write_json(d / "run" / "report.json", {"verdict": [], "map": None})
    write_json(d / "meta.json", {**api.store.meta(pid), "gen": 1})


def test_judging_tests_builds_a_tally_and_proves_direct_answers_only_with_enough_evidence(api):
    pid = api.j("POST", "/api/projects", {"filename": "s.csv", "text": CSV})[1]["id"]
    _trained(api, pid)
    ids = []
    for i in range(40):  # the engine's top answer is right whenever the score is high (it takes about 32 right in a row to prove 95%)
        score = 0.95 - i * 0.02
        ids.append((api.store.add_test(pid, {"query": f"q{i}", "band": "llm", "top_entry": "faq-001", "top_score": score, "gen": 1, "hits": []}), score))
    status, r = api.j("POST", f"/api/projects/{pid}/feedback", {"test_id": ids[0][0], "verdict": "approve", "comment": "great"})
    assert status == 200 and r["feedback"]["tally"]["approved"] == 1 and r["thresholds"]["direct"] is None and "more judged tests" in r["thresholds"]["direct_reason"]
    for tid, score in ids[1:]:
        verdict = "approve" if score >= 0.3 else "reject"
        body = {"test_id": tid, "verdict": verdict, "comment": "", "correct_entry": "faq-002" if verdict == "reject" else None}
        status, r = api.j("POST", f"/api/projects/{pid}/feedback", body)
        assert status == 200
    t = r["feedback"]["tally"]
    assert t["tested"] == 40 and t["approved"] == 33 and t["rejected"] == 7 and t["right_low"] < t["right"] < t["right_high"]
    assert r["thresholds"]["labeled"] == 40 and r["thresholds"]["direct"] is not None and 0.25 < r["thresholds"]["direct"] <= 0.95  # the high scores were right
    assert api.j("POST", f"/api/projects/{pid}/feedback", {"test_id": "nope", "verdict": "approve"})[0] == 404
    assert api.j("POST", f"/api/projects/{pid}/feedback", {"test_id": ids[0][0], "verdict": "maybe"})[0] == 400
    assert api.j("POST", f"/api/projects/{pid}/feedback", {"test_id": ids[0][0], "verdict": "reject", "correct_entry": "bogus"})[0] == 400
    status, csv_bytes = api.call("GET", f"/api/projects/{pid}/feedback.csv")
    assert status == 200 and csv_bytes.count(b"\n") == 41
    status, s = api.j("POST", f"/api/projects/{pid}/settings", {"target_precision": 0.9})
    assert status == 200 and s["thresholds"]["target_precision"] == 0.9
    assert api.j("POST", f"/api/projects/{pid}/settings", {"target_precision": 0.1})[0] == 400


class _Engine:
    """Stands in for a loaded engine: remembers what it was told to search."""

    base_kb = type("KB", (), {"index": {"faq-001": 0, "faq-002": 1}})()

    def __init__(self):
        self.told = []

    def current(self):
        return True

    def learn(self, extras):
        self.told.append(extras)
        return sum(len(v) for v in extras.values())


def test_an_approved_or_corrected_question_is_searched_at_once_and_a_learned_match_is_not_evidence(api):
    pid = api.j("POST", "/api/projects", {"filename": "s.csv", "text": CSV})[1]["id"]
    _trained(api, pid)
    engine = server.Handler.app.engines[pid] = _Engine()
    add = lambda q, entry, score, **kw: api.store.add_test(pid, {"query": q, "band": "llm", "top_entry": entry, "top_score": score, "gen": 1, "hits": [], **kw})  # noqa: E731
    t1, t2, t3 = add("how do i get my money back", "faq-001", 0.8), add("refund please", "faq-001", 0.6), add("how do i get my money back", "faq-001", 1.0, learned=True)

    status, r = api.j("POST", f"/api/projects/{pid}/feedback", {"test_id": t1, "verdict": "approve"})
    assert status == 200 and r["learned"] == 1 and engine.told[-1] == {"faq-001": ["how do i get my money back"]}
    status, r = api.j("POST", f"/api/projects/{pid}/feedback", {"test_id": t2, "verdict": "reject", "correct_entry": "faq-002"})
    assert engine.told[-1] == {"faq-001": ["how do i get my money back"], "faq-002": ["refund please"]}  # the corrected question goes to the entry it belongs to
    status, r = api.j("POST", f"/api/projects/{pid}/feedback", {"test_id": t3, "verdict": "approve"})
    t = r["feedback"]["tally"]
    assert t["tested"] == 3 and t["from_learned"] == 1 and t["approved"] == 1 and t["rejected"] == 1  # the memorised match is tested, not scored
    assert sorted(fb.label_rows(api.store.history(pid))) == [(0.6, False), (0.8, True)]  # and is no evidence for answering without an LLM


def test_feedback_logic_pieces():
    h = [{"verdict": "approve", "band": "llm", "top_score": 0.8, "top_entry": "a", "query": "q1"},
         {"verdict": "approve", "band": "none", "top_score": 0.1, "top_entry": "a", "query": "q2"},
         {"verdict": "reject", "band": "none", "top_score": 0.2, "top_entry": "a", "query": "q3", "correct_entry": "b"},
         {"verdict": "reject", "band": "direct", "top_score": 0.7, "top_entry": "a", "query": "q4", "correct_entry": "none"},
         {"verdict": "reject", "band": "none", "top_score": 0.1, "top_entry": "a", "query": "q5"}]
    assert fb.label_rows(h) == [(0.8, True), (0.1, False), (0.2, False), (0.7, False)]  # the last says nothing, so it is left out
    assert fb.extra_phrasings(h, {"a", "b"}) == {"a": ["q1"], "b": ["q3"]}
    th = fb.recalibrate(h, {"floor": 0.3, "direct": None}, 0.95)
    assert th["direct"] is None and th["floor"] <= 0.3 and th["labeled"] == 4
    assert fb.tally(h)["right"] == 2 / 5


def test_a_reused_connection_survives_requests_whose_body_the_route_ignores(api):
    """Browsers keep the connection open. An unread body used to be taken for the start of the next request."""
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", api.port)
    for _ in range(3):
        conn.request("POST", "/api/projects/example", body="{}", headers={"Content-Type": "application/json"})
        r = conn.getresponse()
        assert r.status == 200 and "id" in json.loads(r.read())
        conn.request("GET", "/api/overview")
        r = conn.getresponse()
        assert r.status == 200 and len(json.loads(r.read())["projects"]) >= 1
    try:  # an oversized upload: the server answers 413 or hangs up as soon as it sees the size; either is fine, a hang is not
        conn.request("POST", "/api/projects", body=b"x" * (37 * 1024 * 1024), headers={"Content-Type": "application/json"})
        assert conn.getresponse().status == 413
    except (BrokenPipeError, ConnectionResetError):
        pass
    conn.close()


def b64(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode()


def test_a_project_can_hold_faq_files_and_documents_together(api):
    import zipfile

    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    docx = io.BytesIO()
    with zipfile.ZipFile(docx, "w") as z:
        z.writestr("word/document.xml", f'<w:document {ns}><w:body><w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Warranty</w:t></w:r></w:p>'
                                        '<w:p><w:r><w:t>Two years on the frame.</w:t></w:r></w:p></w:body></w:document>')
    status, r = api.j("POST", "/api/projects", {"name": "Mixed", "files": [{"filename": "faq.csv", "text": CSV}, {"filename": "handbook.md", "text": "# Delivery\n\nWe deliver in two days.\n"}]})
    assert status == 200
    pid = r["id"]
    p = api.j("GET", f"/api/projects/{pid}")[1]
    assert p["faq"]["entries"] == 3 and p["faq"]["passages"] == 1
    assert [(x["name"], x["kind"], x["faq_entries"], x["passages"]) for x in p["faq"]["sources"]] == [("faq.csv", "faq", 3, 0), ("handbook.md", "document", 0, 1)]
    assert {x["kind"] for x in p["faq"]["preview"]} == {"faq", "passage"} and {x["kind"] for x in p["faq"]["entry_list"]} == {"faq", "passage"}
    # add a Word file (binary, sent base64), then replace a file by sending the same name again, then remove one
    assert api.j("POST", f"/api/projects/{pid}/sources", {"files": [{"filename": "terms.docx", "base64": b64(docx.getvalue())}]})[0] == 200
    assert api.j("GET", f"/api/projects/{pid}")[1]["faq"]["passages"] == 2
    assert api.j("POST", f"/api/projects/{pid}/sources", {"files": [{"filename": "faq.csv", "text": CSV + "Can I pay by card?,Yes.\n"}]})[0] == 200
    p = api.j("GET", f"/api/projects/{pid}")[1]
    assert p["faq"]["entries"] == 4 and len(p["faq"]["sources"]) == 3
    assert api.j("POST", f"/api/projects/{pid}/sources/remove", {"name": "handbook.md"})[0] == 200
    assert api.j("GET", f"/api/projects/{pid}")[1]["faq"]["passages"] == 1
    assert api.j("POST", f"/api/projects/{pid}/sources/remove", {"name": "nope.md"})[0] == 404


def test_a_bad_file_added_later_is_refused_and_changes_nothing(api):
    pid = api.j("POST", "/api/projects", {"filename": "faq.csv", "text": CSV})[1]["id"]
    before = api.j("GET", f"/api/projects/{pid}")[1]["faq"]
    for body, words in [({"files": [{"filename": "bad.csv", "text": "a,b\n"}]}, "no rows"), ({"files": [{"filename": "pic.png", "text": "x"}]}, "unsupported"),
                        ({"files": [{"filename": "e.md", "text": "   "}]}, "empty"), ({"files": [{"filename": "x.docx", "base64": "!!!"}]}, "could not be read"),
                        ({"files": [{"filename": "x.docx", "base64": b64(b"not a zip")}]}, "not a readable")]:
        status, r = api.j("POST", f"/api/projects/{pid}/sources", body)
        assert status == 400 and words in r["error"], (body, r)
    after = api.j("GET", f"/api/projects/{pid}")[1]["faq"]
    assert [x["name"] for x in after["sources"]] == ["faq.csv"] and after["entries"] == before["entries"]
    assert api.j("POST", f"/api/projects/{pid}/sources/remove", {"name": "faq.csv"})[0] == 400  # a project needs a file


def test_changing_the_files_after_training_marks_the_model_out_of_date(api):
    pid = api.j("POST", "/api/projects", {"filename": "faq.csv", "text": CSV})[1]["id"]
    _trained(api, pid)
    api.store.set_meta(pid, trained_signature=api.store.signature(pid))
    assert api.j("GET", f"/api/projects/{pid}")[1]["stale"] is False
    api.j("POST", f"/api/projects/{pid}/sources", {"files": [{"filename": "notes.md", "text": "# Hours\n\nOpen nine to five.\n"}]})
    assert api.j("GET", f"/api/projects/{pid}")[1]["stale"] is True


def test_web_pages_are_read_into_a_project_labelled_with_their_address(api, webshop, monkeypatch):
    from faqengine import web

    monkeypatch.setattr(web, "ALLOW_PRIVATE", True)  # the test site is on this computer, which is never read otherwise
    monkeypatch.setattr(web, "DELAY", 0)
    status, r = api.j("POST", "/api/projects", {"web": {"url": webshop + "/", "pages": 3}})  # a project can start from an address alone
    assert status == 200 and len(r["notes"]) >= 1  # the pages that could not be read are explained
    pid = r["id"]
    p = api.j("GET", f"/api/projects/{pid}")[1]
    sources = p["faq"]["sources"]
    assert [s["url"].removeprefix(webshop) for s in sources] == ["/", "/faq", "/warranty"] and all(s["kind"] == "document" and s["passages"] > 0 for s in sources)
    assert p["faq"]["entries"] == 0 and p["faq"]["passages"] == sum(s["passages"] for s in sources)
    assert {row["source"] for row in p["faq"]["preview"]} == {s["url"] for s in sources}  # each passage says which address it came from

    status, r = api.j("POST", f"/api/projects/{pid}/web", {"url": webshop + "/warranty"})  # the same address again replaces the copy: a refresh
    assert status == 200 and len(api.j("GET", f"/api/projects/{pid}")[1]["faq"]["sources"]) == 3
    status, r = api.j("POST", f"/api/projects/{pid}/web", {"url": webshop + "/plain"})
    assert status == 200 and len(api.j("GET", f"/api/projects/{pid}")[1]["faq"]["sources"]) == 4
    status, r = api.j("POST", f"/api/projects/{pid}/sources", {"files": [{"filename": "faq.csv", "text": CSV}]})  # and a FAQ table can join them
    p = api.j("GET", f"/api/projects/{pid}")[1]
    assert status == 200 and p["faq"]["entries"] > 0 and p["faq"]["passages"] > 0

    before = len(p["faq"]["sources"])
    status, r = api.j("POST", f"/api/projects/{pid}/web", {"url": webshop + "/empty"})
    assert status == 400 and "JavaScript" in r["error"] and len(api.j("GET", f"/api/projects/{pid}")[1]["faq"]["sources"]) == before  # nothing changed
    name = next(s["name"] for s in p["faq"]["sources"] if s["url"].endswith("/plain"))
    assert api.j("POST", f"/api/projects/{pid}/sources/remove", {"name": name})[0] == 200
    assert len(api.j("GET", f"/api/projects/{pid}")[1]["faq"]["sources"]) == before - 1

    monkeypatch.setattr(web, "ALLOW_PRIVATE", False)
    status, r = api.j("POST", "/api/projects", {"web": {"url": webshop + "/faq"}})
    assert status == 400 and "never read" in r["error"]  # an address on this computer is refused, and no project is left behind
    assert api.j("POST", "/api/projects", {"web": {"url": "ftp://example.com/x"}})[0] == 400


def test_an_excel_workbook_and_a_semicolon_csv_in_windows_encoding_can_be_uploaded(api, tmp_path):
    import base64

    from test_sources import _xlsx

    _xlsx(tmp_path / "faq.xlsx", [("Sheet1", [["Question", "Answer"], ["How do I pay?", "By card."], ["Do you deliver?", "Yes, within ten miles."]])])
    status, r = api.j("POST", "/api/projects", {"files": [{"filename": "faq.xlsx", "base64": base64.b64encode((tmp_path / "faq.xlsx").read_bytes()).decode()}]})
    assert status == 200
    pid = r["id"]
    assert api.j("GET", f"/api/projects/{pid}")[1]["faq"]["entries"] == 2
    european = "Pregunta;Respuesta\n¿Aceptan tarjeta?;Sí, aceptamos tarjeta.\n".encode("cp1252")
    status, _ = api.j("POST", f"/api/projects/{pid}/sources", {"files": [{"filename": "es.csv", "base64": base64.b64encode(european).decode()}]})
    p = api.j("GET", f"/api/projects/{pid}")[1]
    assert status == 200 and p["faq"]["entries"] == 3 and {s["name"] for s in p["faq"]["sources"]} == {"faq.xlsx", "es.csv"}
    status, r = api.j("POST", f"/api/projects/{pid}/settings", {"base": "multilingual-e5-small"})  # the model can be chosen per project
    assert status == 200 and api.j("GET", f"/api/projects/{pid}")[1]["settings"]["base"] == "multilingual-e5-small"
    assert api.j("POST", f"/api/projects/{pid}/settings", {"base": "no-such-model"})[0] == 400


def test_changing_the_held_out_set_makes_the_report_stale(api):
    pid = api.j("POST", "/api/projects", {"filename": "faq.csv", "text": CSV})[1]["id"]
    _trained(api, pid)
    api.store.set_meta(pid, trained_signature=api.store.signature(pid))
    first = api.j("GET", f"/api/projects/{pid}")[1]["faq"]["entry_list"][0]["id"]
    api.j("POST", f"/api/projects/{pid}/heldout", {"text": f"query,entry_id,type,split\nhow can i get back into my account,{first},paraphrase,test\n"})
    assert api.j("GET", f"/api/projects/{pid}")[1]["stale"] is True  # the report was measured without it
    api.store.set_meta(pid, trained_signature=api.store.signature(pid))  # as if trained again
    assert api.j("GET", f"/api/projects/{pid}")[1]["stale"] is False
    api.j("POST", f"/api/projects/{pid}/heldout/clear", {})
    assert api.j("GET", f"/api/projects/{pid}")[1]["stale"] is True


def test_held_out_questions_and_manual_thresholds(api):
    pid = api.j("POST", "/api/projects", {"filename": "faq.csv", "text": CSV})[1]["id"]
    ids = [e["id"] for e in api.j("GET", f"/api/projects/{pid}")[1]["faq"]["entry_list"]]
    good = f"query,entry_id,type,split\nhow can i get back into my account,{ids[0]},paraphrase,test\nwhat is the weather,,out-of-scope-far,test\n"
    status, r = api.j("POST", f"/api/projects/{pid}/heldout", {"text": good})
    assert status == 200 and r["heldout"]["total"] == 2
    assert api.j("GET", f"/api/projects/{pid}")[1]["heldout"]["by_type"]["paraphrase"]["test"] == 1
    for bad, words in [("query,entry_id,type\nx,nope,paraphrase\n", "is not the id"), ("query,entry_id,type\nx,,bogus\n", "type"), ("a,b\n1,2\n", "column with the questions")]:
        status, r = api.j("POST", f"/api/projects/{pid}/heldout", {"text": bad})
        assert status == 400 and words in r["error"], (bad, r)
    assert api.j("GET", f"/api/projects/{pid}")[1]["heldout"]["total"] == 2  # the good file is still there
    assert api.j("POST", f"/api/projects/{pid}/heldout/clear", {})[0] == 200 and api.j("GET", f"/api/projects/{pid}")[1]["heldout"] is None
    _trained(api, pid)
    status, r = api.j("POST", f"/api/projects/{pid}/settings", {"floor": 0.41, "direct": 0.8})
    assert status == 200 and r["thresholds"]["floor"] == 0.41 and r["thresholds"]["direct"] == 0.8 and r["thresholds"]["source"] == "manual"
    status, r = api.j("POST", f"/api/projects/{pid}/settings", {"direct": None})
    assert r["thresholds"]["direct"] is None and r["thresholds"]["floor"] == 0.41  # one line back to automatic, the other kept
    r = api.j("POST", f"/api/projects/{pid}/settings", {"floor": None})[1]
    assert r["thresholds"]["floor"] == 0.3 and r["thresholds"]["source"] == "self-check"  # both back to what was calculated
    assert api.j("POST", f"/api/projects/{pid}/settings", {"floor": 3})[0] == 400 and api.j("POST", f"/api/projects/{pid}/settings", {})[0] == 400
