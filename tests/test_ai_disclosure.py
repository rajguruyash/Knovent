"""The text a business reads before it agrees to send data to an AI provider (hosted/disclosure.py): provider details are data that someone verified, not promises written in code; the version
follows the text, so an edit asks for a new agreement; and a provider whose terms are not verified can be neither agreed to nor used."""
import json
import subprocess
import sys

import pytest

from faqengine.hosted import disclosure as D


def entry(**kw):
    """A provider entry that is fully verified, with obviously fake values, changed by `kw`."""
    return {"label": "Test provider", "role": "test", "terms_url": "https://terms.example.invalid/x", "service_tier": "test tier", "retention": "as recorded in the test",
            "training_use": "as recorded in the test", "abuse_monitoring": "as recorded in the test", "contractual_commitments": "as recorded in the test", "verified_on": "2026-11-02", **kw}


def use(tmp_path, monkeypatch, **providers):
    f = tmp_path / "disclosure.json"
    f.write_text(json.dumps(providers))
    monkeypatch.setenv("AI_DISCLOSURE_FILE", str(f))


def test_every_provider_text_says_what_is_sent_what_is_not_and_how_to_withdraw():
    for p in D.providers():
        t = D.text(p)
        assert "question a customer asks" in t and "passages of your own knowledge" in t
        assert "never sent" in t.lower() and "API keys" in t
        assert "nothing is sent to an AI inference provider" in t  # deterministic mode
        assert "withdraw this agreement at any time" in t and "very next question" in t
        assert "separate company" in t  # a third party
        assert "not agreeing to a later one" in t or "not agreeing for another" in t


def test_no_provider_ships_verified_and_the_text_says_what_is_missing():
    for p, e in D.PROVIDERS.items():
        assert all(e[k] is None for k in (*D.REQUIRED, *D.OPTIONAL)), p  # nobody has verified anything yet
        v = D.verification(p)
        assert v["status"] == "unverified" and v["missing"] == list(D.REQUIRED) and not D.verified(p)
        t = D.text(p)
        assert "has NOT verified" in t and "Agreement is not available for this provider until its terms have been verified" in t
        assert "how long the provider keeps submitted text" in t and "whether submitted text is used to train or improve models" in t
        for promise in ("we guarantee", "is not used for training", "does not train", "never retained", "deleted immediately", "gdpr compliant", "soc 2"):
            assert promise not in t.lower(), (p, promise)
        d = D.describe(p)
        assert d["verified"] is False and d["consentable"] is False and d["verification"]["missing"]


@pytest.mark.parametrize("field", D.REQUIRED)
def test_a_missing_item_leaves_the_provider_unverified(tmp_path, monkeypatch, field):
    use(tmp_path, monkeypatch, x=entry(**{field: None}))
    v = D.verification("x")
    assert v["status"] == "unverified" and v["missing"] == [field] and not D.verified("x") and D.describe("x")["consentable"] is False


@pytest.mark.parametrize("answer", ["", "   ", "unknown", "Unknown", "not stated", "Not specified in the terms", "unclear", "TBD", "n/a", "none found", "no information", "?"])
def test_an_answer_that_says_nothing_is_not_an_answer(tmp_path, monkeypatch, answer):
    for field in ("retention", "training_use", "abuse_monitoring"):
        use(tmp_path, monkeypatch, x=entry(**{field: answer}))
        assert D.verification("x")["status"] == "unverified" and field in D.verification("x")["missing"], (field, answer)


def test_the_address_and_the_date_must_be_real(tmp_path, monkeypatch):
    for bad, why in ((entry(terms_url="http://terms.example.invalid/x"), "https"), (entry(verified_on="next week"), "date"), (entry(verified_on="2999-01-01"), "future")):
        use(tmp_path, monkeypatch, x=bad)
        v = D.verification("x")
        assert v["status"] == "unverified" and v["problems"], why
        assert any(w in " ".join(v["problems"]) for w in ("https", "date", "future")), why


def test_a_fully_verified_provider_shows_what_was_recorded_and_where_and_when(tmp_path, monkeypatch):
    use(tmp_path, monkeypatch, x=entry(retention="30 days, per section 4", regional_processing="EU only, per annex 2", verified_by="a named person"))
    v = D.verification("x")
    assert v == {"status": "verified", "missing": [], "problems": []} and D.verified("x")
    t = D.text("x")
    assert "https://terms.example.invalid/x" in t and "2026-11-02" in t and "30 days, per section 4" in t and "EU only, per annex 2" in t and "a named person" in t
    assert "has NOT verified" not in t and "Agreement is not available" not in t
    assert "Terms change" in t  # and it does not claim the terms are permanent
    assert D.describe("x")["consentable"] is True


def test_the_version_follows_the_text(tmp_path, monkeypatch):
    use(tmp_path, monkeypatch, x=entry(), y=entry(label="Another provider"))
    a = D.current_version("x")
    assert D.current_version("x") == a and D.current_version("y") != a  # stable for the same text, its own for each provider
    use(tmp_path, monkeypatch, x=entry(retention="changed"), y=entry(label="Another provider"))
    assert D.current_version("x") != a  # a changed text is a new version: earlier agreements stop applying


def test_a_changed_common_text_changes_every_version(monkeypatch):
    before = {p: D.current_version(p) for p in D.providers()}
    monkeypatch.setitem(D.COMMON, "third_party", D.COMMON["third_party"] + " (revised)")
    assert all(D.current_version(p) != before[p] for p in before)
    assert len({D.current_version(p) for p in before}) == len(before)  # each provider has its own version


def test_an_unknown_provider_has_no_disclosure_and_so_cannot_be_agreed_to():
    assert D.known("cloudflare") and not D.known("some-new-vendor") and not D.verified("some-new-vendor")
    with pytest.raises(KeyError):
        D.text("some-new-vendor")
    assert D.describe("gemini")["data_categories"] == ["question", "evidence_passages"]
    assert len(D.sha256("gemini")) == 64


def test_the_command_line_report_names_what_is_missing():
    out = subprocess.run([sys.executable, "-m", "faqengine.hosted.disclosure"], capture_output=True, text=True, timeout=60, check=True).stdout
    assert "0 of 6 providers verified" in out and "retention" in out and "cloudflare" in out and "unverified" in out
