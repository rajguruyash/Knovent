"""Migration 12 on a real PostgreSQL, as each kind of person: the execution-mode settings, the plans and limits, the usage columns, paired companions and invitations. What is checked is who can read and
who can write, that one business never touches another's rows, and that a person joins a business only by accepting an invitation sent to their confirmed address."""
import sys
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from pgdb import make_db  # noqa: E402


@pytest.fixture(scope="module")
def db():
    yield from make_db()


@pytest.fixture(scope="module")
def world(db):
    w = type("W", (), {})()
    w.alice, w.bob, w.erin, w.carol, w.dave, w.mallory, w.zed = (db.user(n) for n in ("m-alice", "m-bob", "m-erin", "m-carol", "m-dave", "m-mallory", "m-zed"))
    w.A, w.B = db.business(w.alice, "modes-a"), db.business(w.bob, "modes-b")
    db.member(w.A, w.erin, "admin")
    db.member(w.A, w.carol, "editor")
    db.member(w.A, w.dave, "viewer")
    w.mail = lambda uid: db.su("select email from auth.users where id = %s", (uid,))[0][0]
    return w


def invite(db, who, business, email, role="viewer"):
    return db.run(who, "select public.create_invitation(%s, %s, %s)", (business, email, role))[0][0]


# ---------------------------------------------------------------- settings, plans, companions: members read, the server writes
@pytest.mark.parametrize("table,columns,values", [
    ("ai_inference_settings", "(business_id, mode)", "(%s, 'byok')"),
    ("business_plans", "(business_id, plan)", "(%s, 'paid')"),
    ("local_companions", "(business_id, label)", "(%s, 'my laptop')"),
])
def test_members_read_and_nobody_signed_in_writes(db, world, table, columns, values):
    db.su(f"insert into public.{table} {columns} values {values}", (world.A,))
    assert db.run(world.dave, f"select count(*) from public.{table} where business_id = %s", (world.A,))[0][0] == 1  # even a viewer sees where the business stands
    assert db.run(world.bob, f"select count(*) from public.{table} where business_id = %s", (world.A,))[0][0] == 0  # another business sees nothing
    assert db.run(world.mallory, f"select count(*) from public.{table}")[0][0] == 0
    for who in (world.alice, world.erin, world.carol, world.dave, world.mallory):  # owner and admin included: only the engine service (and the operator) writes
        db.denied(who, f"insert into public.{table} {columns} values {values}", (world.A,))
        db.denied(who, f"delete from public.{table} where business_id = %s", (world.A,))
    db.denied(world.alice, f"update public.{table} set business_id = business_id where business_id = %s", (world.A,))
    db.denied(world.alice, f"truncate public.{table}")
    db.denied(None, f"select count(*) from public.{table}", role="anon")


def test_the_mode_settings_obey_their_own_rules(db, world):
    b = db.business(db.user("m-rules"))
    for sql in ("insert into public.ai_inference_settings (business_id, mode) values (%s, 'cloud')",                                                               # not a mode
                "insert into public.ai_inference_settings (business_id, mode) values (%s, 'platform')",                                                              # platform needs a model
                "insert into public.ai_inference_settings (business_id, mode, platform_provider, platform_model) values (%s, 'byok', 'cloudflare', 'm')",            # only platform names one
                "insert into public.ai_inference_settings (business_id, fallback) values (%s, 'another-provider')"):                                                 # no failover to another provider exists
        with pytest.raises(psycopg.errors.CheckViolation):
            db.su(sql, (b,))
    db.su("insert into public.ai_inference_settings (business_id, mode, platform_provider, platform_model) values (%s, 'platform', 'cloudflare', '@cf/x/y')", (b,))
    assert db.su("select mode, fallback from public.ai_inference_settings where business_id = %s", (b,))[0] == ("platform", "deterministic")  # deterministic fallback unless the business chose otherwise
    act = db.su("select action, detail from public.audit_logs where business_id = %s and target_type = 'ai_inference_settings'", (b,))
    assert act and act[0][0] == "ai_inference_settings.insert" and act[0][1]["mode_after"] == "platform" and act[0][1]["model"] == "@cf/x/y"


def test_plans_have_bounds_and_an_empty_limit_is_not_a_number(db, world):
    b = db.business(db.user("m-plan"))
    for sql in ("insert into public.business_plans (business_id, inference_requests_per_month) values (%s, -1)", "insert into public.business_plans (business_id, inference_per_minute) values (%s, 0)",
                "insert into public.business_plans (business_id, inference_per_minute) values (%s, 601)", "insert into public.business_plans (business_id, plan) values (%s, 'unlimited')"):
        with pytest.raises(psycopg.errors.CheckViolation):
            db.su(sql, (b,))
    db.su("insert into public.business_plans (business_id) values (%s)", (b,))
    row = db.su("select plan, inference_requests_per_month, tokens_per_month, cost_cap_usd_per_month, inference_per_minute from public.business_plans where business_id = %s", (b,))[0]
    assert row == ("trial", None, None, None, 20)  # a plan with nothing stated allows no platform inference (the service reads None as "not allowed", never as "unlimited")


def test_a_companion_records_what_it_is_and_never_a_secret(db, world):
    cols = {r[0] for r in db.su("select column_name from information_schema.columns where table_schema = 'public' and table_name = 'local_companions'")}
    assert not [c for c in cols if any(w in c for w in ("secret", "key", "token", "password", "credential"))] and {"label", "platform", "runtime", "model", "revoked_at"} <= cols
    with pytest.raises(psycopg.errors.CheckViolation):
        db.su("insert into public.local_companions (business_id, label, runtime) values (%s, 'x', 'lmstudio')", (world.A,))


def test_usage_records_carry_the_mode_and_a_correlation_id(db, world):
    db.su("insert into public.usage_records (business_id, kind, mode, correlation_id, generated_by, failure_kind, provider, model, tokens_in, tokens_out, cost, status) "
          "values (%s, 'llm_call', 'platform', 'req-1', 'model', null, 'cloudflare', 'm', 10, 5, 0.0001, 'ok')", (world.A,))
    with pytest.raises(psycopg.errors.CheckViolation):
        db.su("insert into public.usage_records (business_id, kind, mode) values (%s, 'llm_call', 'elsewhere')", (world.A,))
    assert db.run(world.alice, "select count(*) from public.usage_records where business_id = %s and mode = 'platform'", (world.A,))[0][0] == 1
    assert db.run(world.dave, "select count(*) from public.usage_records where business_id = %s", (world.A,))[0][0] == 0  # usage is for owners and admins
    assert db.run(world.bob, "select count(*) from public.usage_records where business_id = %s", (world.A,))[0][0] == 0
    db.denied(world.alice, "insert into public.usage_records (business_id, kind) values (%s, 'question')", (world.A,))  # written by the service only


# ---------------------------------------------------------------- invitations
def test_nobody_is_inserted_into_a_business_by_someone_else(db, world):
    for who in (world.alice, world.erin, world.mallory):
        db.denied(who, "insert into public.business_members (business_id, user_id, role) values (%s, %s, 'viewer')", (world.A, world.zed))
    db.denied(world.alice, "insert into public.business_members (business_id, user_id, role) values (%s, %s, 'owner')", (world.A, world.alice))


def test_an_owner_or_admin_invites_and_the_secret_is_shown_once_and_kept_only_as_a_hash(db, world):
    tok = invite(db, world.alice, world.A, world.mail(world.zed).upper(), "editor")  # the address is compared without case
    assert len(tok) == 64 and all(c in "0123456789abcdef" for c in tok)
    row = db.su("select email, role, token_hash, expires_at > now() + interval '6 days' from public.business_invitations where business_id = %s", (world.A,))[0]
    assert row[0] == world.mail(world.zed) and row[1] == "editor" and row[3] is True and row[2] != tok and tok not in row[2] and len(row[2]) == 64
    assert tok != invite(db, world.erin, world.A, "someone-else@example.com", "viewer")  # an admin invites too; each secret is its own
    world.tok = tok


def test_who_may_invite_to_what_role(db, world):
    for who in (world.carol, world.dave, world.mallory, world.bob):  # an editor, a viewer, a stranger, another business's owner
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            invite(db, who, world.A, "x@example.com")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        invite(db, world.erin, world.A, "x@example.com", "admin")  # an admin cannot make an admin
    assert invite(db, world.alice, world.A, "future-admin@example.com", "admin")  # an owner can
    for bad in ("owner", "superuser", ""):
        with pytest.raises(psycopg.errors.InvalidParameterValue):
            invite(db, world.alice, world.A, "y@example.com", bad)  # nobody is invited as an owner
    for bad in ("not an address", "a@b", "@example.com", "a b@example.com"):
        with pytest.raises(psycopg.errors.InvalidParameterValue):
            invite(db, world.alice, world.A, bad)


def test_the_invitation_works_for_the_invited_person_only_and_only_once(db, world):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.run(world.mallory, "select public.accept_invitation(%s)", (world.tok,))  # someone else holding the secret
    assert db.su("select count(*) from public.business_members where business_id = %s and user_id = %s", (world.A, world.mallory))[0][0] == 0
    assert db.run(world.zed, "select public.accept_invitation(%s)", (world.tok,))[0][0] == world.A
    assert db.su("select role from public.business_members where business_id = %s and user_id = %s", (world.A, world.zed))[0][0] == "editor"
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.run(world.zed, "select public.accept_invitation(%s)", (world.tok,))  # the secret is spent
    with pytest.raises(psycopg.errors.UniqueViolation):
        invite(db, world.alice, world.A, world.mail(world.zed))  # already in


def test_every_refusal_reads_the_same_so_the_function_tells_nothing(db, world):
    u = db.user("m-late")
    mail = world.mail(u)
    messages = set()
    cases = {}
    cases["unknown secret"] = "0" * 64
    cases["empty"] = ""
    t_expired = invite(db, world.alice, world.A, mail)
    db.su("update public.business_invitations set expires_at = now() - interval '1 minute' where email = %s", (mail,))
    cases["expired"] = t_expired
    t_revoked = invite(db, world.alice, world.A, mail)
    db.su("update public.business_invitations set revoked_at = now() where email = %s and expires_at > now()", (mail,))
    cases["revoked"] = t_revoked
    t_unconfirmed = invite(db, world.alice, world.A, mail)
    db.su("update auth.users set email_confirmed_at = null where id = %s", (u,))
    cases["address not confirmed"] = t_unconfirmed
    for why, tok in cases.items():
        with pytest.raises(psycopg.errors.InsufficientPrivilege) as e:
            db.run(u, "select public.accept_invitation(%s)", (tok,))
        messages.add(str(e.value).split("\n")[0])
    assert messages == {"this invitation cannot be used"}, messages
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.run(None, "select public.accept_invitation(%s)", (t_unconfirmed,))  # not signed in
    db.denied(None, "select public.accept_invitation(%s)", (t_unconfirmed,), role="anon")
    db.su("update auth.users set email_confirmed_at = now() where id = %s", (u,))
    assert db.run(u, "select public.accept_invitation(%s)", (t_unconfirmed,))[0][0] == world.A  # once the address is confirmed, the same secret works


def test_a_newer_invitation_replaces_the_older_one_for_the_same_address(db, world):
    mail = "twice@example.com"
    first = invite(db, world.alice, world.A, mail)
    second = invite(db, world.alice, world.A, mail)
    assert db.su("select count(*) from public.business_invitations where business_id = %s and email = %s and revoked_at is null and accepted_at is null", (world.A, mail))[0][0] == 1
    u = db.user("m-twice")
    db.su("update auth.users set email = %s where id = %s", (mail, u))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.run(u, "select public.accept_invitation(%s)", (first,))
    assert db.run(u, "select public.accept_invitation(%s)", (second,))[0][0] == world.A


def test_invitations_are_visible_only_to_owners_and_admins_of_that_business_and_never_their_secret(db, world):
    assert db.run(world.alice, "select count(*) from public.business_invitations where business_id = %s", (world.A,))[0][0] >= 3
    assert db.run(world.erin, "select count(*) from public.business_invitations where business_id = %s", (world.A,))[0][0] >= 3
    for who in (world.carol, world.dave, world.mallory, world.bob):
        assert db.run(who, "select count(*) from public.business_invitations where business_id = %s", (world.A,))[0][0] == 0
    db.denied(world.alice, "select token_hash from public.business_invitations")  # not even the hash
    db.denied(world.alice, "select * from public.business_invitations")
    for who in (world.alice, world.erin):
        db.denied(who, "insert into public.business_invitations (business_id, email, role, token_hash, expires_at) values (%s, 'a@example.com', 'owner', %s, now())", (world.A, "0" * 64))
        db.denied(who, "update public.business_invitations set role = 'admin' where business_id = %s", (world.A,))
        db.denied(who, "delete from public.business_invitations where business_id = %s", (world.A,))


def test_withdrawing_an_invitation_is_for_the_business_that_made_it(db, world):
    tok = invite(db, world.alice, world.A, "withdraw@example.com")
    inv = db.su("select id from public.business_invitations where email = 'withdraw@example.com'")[0][0]
    for who in (world.carol, world.dave, world.mallory, world.bob):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.run(who, "select public.revoke_invitation(%s)", (inv,))  # another business's owner included
    db.run(world.erin, "select public.revoke_invitation(%s)", (inv,))
    u = db.user("m-withdrawn")
    db.su("update auth.users set email = 'withdraw@example.com' where id = %s", (u,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.run(u, "select public.accept_invitation(%s)", (tok,))


def test_the_open_invitations_of_one_business_are_limited(db, world):
    b = db.business(db.user("m-limit"))
    owner = db.su("select user_id from public.business_members where business_id = %s", (b,))[0][0]
    for i in range(50):
        invite(db, owner, b, f"person{i}@example.com")
    with pytest.raises(psycopg.errors.ConfigurationLimitExceeded):
        invite(db, owner, b, "one-too-many@example.com")


def test_the_audit_trail_records_who_invited_and_holds_no_address_or_secret(db, world):
    rows = db.su("select actor_id, action, detail::text from public.audit_logs where business_id = %s and target_type = 'business_invitations'", (world.A,))
    assert rows and {r[0] for r in rows} >= {world.alice, world.erin}
    blob = " ".join(r[2] for r in rows)
    assert "@" not in blob and "token" not in blob.lower() and world.tok not in blob
