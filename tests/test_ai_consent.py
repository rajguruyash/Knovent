"""AI inference consent, in the database (migration 10), exercised as each kind of person on a real PostgreSQL: who may grant and revoke, that one business never touches another's consent,
that the history is kept, that revoking takes effect at once, that consent to one provider or one disclosure version is not consent to another, and that no credential lives here."""
import sys
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from pgdb import make_db  # noqa: E402

V1, V2 = "2026-11-02.1", "2026-12-01.1"
INSERT = "insert into public.ai_inference_consents (business_id, provider, disclosure_version, disclosure_sha256) values (%s, %s, %s, %s) returning id, granted_by"
ALLOWS = "select public.ai_consent_allows(%s, %s, %s)"


@pytest.fixture(scope="module")
def db():
    yield from make_db()


@pytest.fixture(scope="module")
def world(db):
    w = type("W", (), {})()
    w.alice, w.bob, w.erin, w.carol, w.dave, w.mallory = (db.user(n) for n in ("c-alice", "c-bob", "c-erin", "c-carol", "c-dave", "c-mallory"))
    w.A, w.B = db.business(w.alice, "consent-a"), db.business(w.bob, "consent-b")
    db.member(w.A, w.erin, "admin")
    db.member(w.A, w.carol, "editor")
    db.member(w.A, w.dave, "viewer")
    return w


def allowed(db, who, b, provider, version=V1):
    return db.run(who, ALLOWS, (b, provider, version))[0][0]


def test_an_owner_grants_and_the_database_records_who_and_under_which_disclosure(db, world):
    cid, by = db.run(world.alice, INSERT, (world.A, "cloudflare", V1, "a" * 64))[0]
    assert by == world.alice
    row = db.su("select status, disclosure_version, disclosure_sha256, revoked_at, data_categories from public.ai_inference_consents where id = %s", (cid,))[0]
    assert row[0] == "granted" and row[1] == V1 and row[2] == "a" * 64 and row[3] is None and set(row[4]) == {"question", "evidence_passages"}
    assert allowed(db, world.alice, world.A, "cloudflare") and allowed(db, world.dave, world.A, "cloudflare")  # any member can ask whether consent exists
    world.cf = cid


def test_the_person_who_granted_cannot_be_forged(db, world):
    # a request that names who granted, or sets the status or the id, is refused outright (migration 11 grants only the columns a person fills in) ...
    db.denied(world.erin, "insert into public.ai_inference_consents (business_id, provider, disclosure_version, granted_by) values (%s, 'openrouter', %s, %s)", (world.A, V1, world.bob))
    db.denied(world.erin, "insert into public.ai_inference_consents (business_id, provider, disclosure_version, status) values (%s, 'openrouter', %s, 'revoked')", (world.A, V1))
    db.denied(world.erin, "insert into public.ai_inference_consents (id, business_id, provider, disclosure_version) values (gen_random_uuid(), %s, 'openrouter', %s)", (world.A, V1))
    # ... and one that does not is recorded as the person acting
    cid, by = db.run(world.erin, INSERT, (world.A, "openrouter", V1, None))[0]
    assert by == world.erin
    world.or_ = cid
    # the server, naming who acted on someone's behalf, is the one other way in (the guard trigger keeps the name it is given)
    sid, who = db.run(world.alice, "insert into public.ai_inference_consents (business_id, provider, disclosure_version, granted_by) values (%s, 'server-made', %s, %s) returning id, granted_by",
                      (world.A, V1, world.erin), role="server")[0]
    assert who == world.erin
    db.su("update public.ai_inference_consents set status = 'revoked', revoked_at = now() where id = %s", (sid,))


def test_the_history_cannot_be_emptied_by_anyone(db, world):
    db.denied(world.alice, "truncate public.ai_inference_consents")  # not a privilege of the API roles
    db.denied(world.alice, "delete from public.ai_inference_consents where business_id = %s", (world.A,))
    with pytest.raises(psycopg.errors.CheckViolation, match="cannot be emptied"):
        db.su("truncate public.ai_inference_consents")  # nor of the database owner
    assert db.su("select count(*) from public.ai_inference_consents where business_id = %s", (world.A,))[0][0] >= 2


def test_only_an_owner_or_admin_may_grant(db, world):
    for who in (world.carol, world.dave, world.mallory):  # an editor, a viewer, someone who is a member of nothing
        db.denied(who, INSERT, (world.A, "gemini", V1, None))
    db.denied(world.bob, INSERT, (world.A, "gemini", V1, None))  # the owner of ANOTHER business
    assert db.su("select count(*) from public.ai_inference_consents where business_id = %s and provider = 'gemini'", (world.A,))[0][0] == 0


def test_another_business_cannot_see_or_change_this_ones_consent(db, world):
    assert db.run(world.bob, "select count(*) from public.ai_inference_consents where business_id = %s", (world.A,))[0][0] == 0
    assert db.run(world.mallory, "select count(*) from public.ai_inference_consents")[0][0] == 0
    assert db.run(world.bob, "update public.ai_inference_consents set status = 'revoked' where id = %s returning id", (world.cf,)) == []  # no row is visible to change
    assert not allowed(db, world.bob, world.A, "cloudflare")  # and the question, asked from B, is answered for B's rows only
    assert db.su("select status from public.ai_inference_consents where id = %s", (world.cf,))[0][0] == "granted"


def test_consent_to_one_provider_is_not_consent_to_another_and_not_to_another_disclosure(db, world):
    assert allowed(db, world.alice, world.A, "cloudflare") and allowed(db, world.alice, world.A, "openrouter")
    assert not allowed(db, world.alice, world.A, "gemini")
    assert not allowed(db, world.alice, world.A, "cloudflare", V2)  # a newer disclosure has not been agreed to
    assert not allowed(db, world.bob, world.B, "cloudflare")  # Business B has agreed to nothing


def test_one_active_grant_per_provider(db, world):
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.run(world.alice, INSERT, (world.A, "cloudflare", V1, None))


def test_a_grant_cannot_be_edited_only_revoked_and_not_by_a_member_who_may_not(db, world):
    for who in (world.carol, world.dave):
        assert db.run(who, "update public.ai_inference_consents set status = 'revoked' where id = %s returning id", (world.or_,)) == []  # (not visible to change: no policy lets them)
    db.denied(world.alice, "update public.ai_inference_consents set provider = 'gemini' where id = %s", (world.cf,))  # not a column a person may change
    db.denied(world.alice, "update public.ai_inference_consents set disclosure_version = %s where id = %s", (V2, world.cf))
    db.denied(world.alice, "delete from public.ai_inference_consents where id = %s", (world.cf,))
    with pytest.raises(psycopg.errors.CheckViolation):  # not even the database owner can delete the history
        db.su("delete from public.ai_inference_consents where id = %s", (world.cf,))


def test_revoking_takes_effect_at_once_and_records_who_and_when(db, world):
    assert allowed(db, world.alice, world.A, "openrouter")
    db.run(world.erin, "update public.ai_inference_consents set status = 'revoked' where id = %s", (world.or_,))
    assert not allowed(db, world.alice, world.A, "openrouter") and allowed(db, world.alice, world.A, "cloudflare")  # only that provider
    status, by, at = db.su("select status, revoked_by, revoked_at from public.ai_inference_consents where id = %s", (world.or_,))[0]
    assert status == "revoked" and by == world.erin and at is not None


def test_a_revoked_grant_stays_revoked_and_a_new_grant_is_a_new_row_with_the_history_kept(db, world):
    with pytest.raises(psycopg.errors.CheckViolation):
        db.su("update public.ai_inference_consents set status = 'granted', revoked_at = null where id = %s", (world.or_,))
    new, _ = db.run(world.alice, INSERT, (world.A, "openrouter", V2, None))[0]
    assert allowed(db, world.alice, world.A, "openrouter", V2) and not allowed(db, world.alice, world.A, "openrouter", V1)
    rows = db.su("select status, disclosure_version from public.ai_inference_consents where business_id = %s and provider = 'openrouter' order by granted_at", (world.A,))
    assert rows == [("revoked", V1), ("granted", V2)] and new != world.or_


def test_no_credential_is_stored_here(db, world):
    cols = {r[0] for r in db.su("select column_name from information_schema.columns where table_schema = 'public' and table_name = 'ai_inference_consents'")}
    assert not {c for c in cols if any(w in c for w in ("key", "secret", "token", "password", "credential", "url"))}


def test_every_grant_and_revocation_is_in_the_audit_trail_without_content(db, world):
    rows = db.run(world.alice, "select action, detail::text from public.audit_logs where business_id = %s and target_type = 'ai_inference_consents' order by created_at", (world.A,))
    actions = [r[0] for r in rows]
    assert actions.count("ai_inference_consents.insert") >= 3 and "ai_inference_consents.update" in actions
    assert all("evidence" not in r[1] and "question" not in r[1] for r in rows)
    assert db.run(world.dave, "select count(*) from public.audit_logs where business_id = %s", (world.A,))[0][0] == 0  # only owners and admins read it
