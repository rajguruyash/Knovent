"""The anonymous API-level probe (scripts/probe_supabase_anon.py): what it asks, what it concludes, what it refuses, and that the key it uses never appears anywhere it writes. The Data API is
stood in for by a function: no project is contacted."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import probe_supabase_anon as P  # noqa: E402

REF, OTHER = "abcdefghijklmnopqrst", "zyxwvutsrqponmlkjihg"  # made-up refs of the right shape
URL = f"https://{REF}.supabase.co"
KEY = "sb_publishable_TEST-ONLY-KEY-never-printed-0123456789"


def api(behaviour):
    """A stand-in for the Data API: behaviour(kind, name) -> (status, body)."""
    def call(url, method, path, key, body=None, timeout=20.0):
        assert key == KEY and url.startswith(URL)
        if path.startswith("/rest/v1/rpc/"):
            return behaviour("function", path.rsplit("/", 1)[1])
        return behaviour("table", path.split("?")[0].rsplit("/", 1)[1])
    return call


REFUSE = (401, {"code": "42501", "message": "permission denied"})
ABSENT = (404, {"code": "PGRST205", "message": "not found"})


def test_the_tables_and_functions_come_from_the_migrations():
    t, f = P.tables(), P.functions()
    assert {"businesses", "faqs", "ai_inference_consents", "ai_inference_settings", "business_plans", "local_companions", "business_invitations"} <= set(t) and len(t) == 27
    assert f["has_role"] == {"b": P.DUMMY["uuid"], "roles": ["x"]} and set(f["create_invitation"]) == {"p_business", "p_email", "p_role"} and "ai_consent_allows" in f and "match_embeddings" in f
    assert not {"audit_event", "ai_consent_guard", "handle_new_user", "touch_updated_at", "ai_consent_no_truncate"} & set(f)  # trigger functions are not callable through the API


def test_a_project_where_everything_exists_and_anon_is_refused_is_clean(monkeypatch):
    monkeypatch.setattr(P, "call", api(lambda k, n: REFUSE))
    s = P.summarise(P.probe(URL, KEY))
    assert s["refused_as_expected"] == s["checked"] == len(P.tables()) + len(P.functions()) and not s["missing"] and not s["anonymous_can_reach"]


def test_missing_tables_and_reachable_ones_are_told_apart(monkeypatch):
    def behaviour(kind, name):
        if name == "ai_inference_consents":
            return ABSENT  # migration 10 not applied
        if name == "norm_text":
            return 200, {}  # a function the anonymous role can run
        if name == "faqs":
            return 200, {"rows": 0}  # readable (no row comes back, but the role holds a privilege it should not)
        return REFUSE
    monkeypatch.setattr(P, "call", api(behaviour))
    s = P.summarise(P.probe(URL, KEY))
    assert s["missing"] == ["ai_inference_consents"] and set(s["anonymous_can_reach"]) == {"norm_text", "faqs"} and s["refused_as_expected"] == s["checked"] - 3


@pytest.mark.parametrize("url,expect,forbidden,message", [
    ("http://" + REF + ".supabase.co", REF, set(), "https"),
    (f"https://{REF}.example.com", REF, set(), "https"),
    (f"https://{REF}.supabase.co/rest/v1", REF, set(), "https"),
    (URL, OTHER, set(), "not the one you said"),
    (URL, REF, {REF}, "forbidden"),
])
def test_it_refuses_an_address_that_is_not_exactly_the_expected_project(url, expect, forbidden, message):
    with pytest.raises(P.Refused, match=message):
        P.guard(url, expect, forbidden)
    assert P.guard(URL, REF, {OTHER}) == REF


def test_the_key_is_read_from_the_file_and_never_shown(tmp_path, monkeypatch, capsys):
    env = tmp_path / ".env"
    env.write_text(f"OTHER=not-this\nexport MY_KEY='{KEY}'\n")
    monkeypatch.setattr(P, "call", api(lambda k, n: REFUSE))
    out = tmp_path / "r" / "report.json"
    code = P.main(["--url", URL, "--expect-ref", REF, "--key-file", str(env), "--key-var", "MY_KEY", "--json", str(out)], env={})
    seen = capsys.readouterr()
    assert code == 0 and "every table and function exists and refuses the anonymous role" in seen.out
    assert KEY not in seen.out + seen.err + out.read_text() and "not-this" not in seen.out + seen.err
    assert "not a database validation" in seen.out


def test_the_exit_code_and_the_words_when_it_is_not_clean(tmp_path, monkeypatch, capsys):
    env = tmp_path / ".env"
    env.write_text(f"MY_KEY={KEY}\n")
    monkeypatch.setattr(P, "call", api(lambda k, n: ABSENT if n == "business_plans" else REFUSE))
    assert P.main(["--url", URL, "--expect-ref", REF, "--key-file", str(env), "--key-var", "MY_KEY"], env={}) == 1
    out = capsys.readouterr().out
    assert "business_plans" in out and "NOT clean" in out


def test_refusals_exit_with_three_and_name_no_key(tmp_path, capsys):
    env = tmp_path / ".env"
    env.write_text(f"MY_KEY={KEY}\n")
    assert P.main(["--url", URL, "--expect-ref", OTHER, "--key-file", str(env), "--key-var", "MY_KEY"], env={}) == 3
    assert P.main(["--url", URL, "--expect-ref", REF, "--key-file", str(env), "--key-var", "ABSENT_VAR"], env={}) == 3
    assert P.main(["--url", URL, "--expect-ref", REF, "--key-file", str(env), "--key-var", "MY_KEY", "--forbidden", REF], env={}) == 3
    assert P.main(["--url", URL, "--expect-ref", REF, "--key-file", str(env), "--key-var", "MY_KEY"], env={"FORBIDDEN_PROJECT_REFS": f"x, {REF}"}) == 3
    assert KEY not in capsys.readouterr().err


def test_classification():
    assert P.classify(401, {"code": "42501"}) == "refused" and P.classify(404, {"code": "PGRST205"}) == "missing" and P.classify(200, {}) == "readable" and P.classify(500, {}) == "other"
    assert P.classify(401, {"message": "permission denied for table x"}) == "refused"


def test_a_rejected_key_is_never_mistaken_for_a_refusal_by_the_database():
    for status, body in ((401, {"message": "Invalid API key", "hint": "..."}), (401, {"message": "Secret API key required"}), (403, {})):
        assert P.classify(status, body) == "rejected"
    s = P.summarise([{"name": "t", "result": "rejected"}, {"name": "u", "result": "refused"}])
    assert s["refused_as_expected"] == 1 and s["rejected_before_the_database"] == ["t"] and s["refused_as_expected"] != s["checked"]  # so the run is not clean
