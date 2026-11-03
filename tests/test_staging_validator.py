"""The staging validator (scripts/validate_supabase.py), tested where it can be: on a local PostgreSQL with every migration, plus deliberate breakages that it must catch.

This is a test OF THE VALIDATOR. It is not a validation of any Supabase project; the validator says so itself when it runs against a local server.
"""
import json
import sys
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "scripts"))
from pgdb import make_db  # noqa: E402

import validate_supabase as V  # noqa: E402

STAGING, OTHER, PROD = "abcdefghijklmnopqrst", "zyxwvutsrqponmlkjihg", "prodprodprodprodprod"  # made-up refs of the right shape: no real project's


@pytest.fixture()
def fresh():
    yield from make_db()


@pytest.fixture(scope="module")
def good():
    yield from make_db()


def run(db, **kw):
    return V.run(db.dsn, {}, allow_local=True, **kw)


def failed(report):
    return [x["check"] for x in report["results"] if not x["ok"]]


# ---------------------------------------------------------------- on the schema as the migrations leave it
def test_every_check_passes_on_the_migrated_schema_and_the_report_says_it_is_not_a_staging_validation(good):
    report = run(good)
    assert failed(report) == [] and report["failed"] == 0 and report["passed"] >= 35
    assert report["is_staging_validation"] is False and report["target"]["kind"] == "local"
    assert V.render(report).startswith("NOT A STAGING VALIDATION")
    assert {x["group"] for x in report["results"]} == {"catalog", "behaviour"}


def test_nothing_the_checks_make_stays_in_the_database(good):
    count = lambda: [good.su(f"select count(*) from {t}")[0][0] for t in ("auth.users", "public.businesses", "public.business_members", "public.ai_inference_consents", "public.audit_logs")]  # noqa: E731
    before = count()
    run(good)
    assert count() == before


def test_every_check_has_a_name_a_group_and_a_boolean(good):
    for x in run(good)["results"]:
        assert isinstance(x["ok"], bool) and x["check"] and x["group"] in ("catalog", "behaviour")


# ---------------------------------------------------------------- it must notice what is wrong
BREAKAGES = [
    ("grant truncate on public.faqs to authenticated", "TRUNCATE"),
    ("grant execute on function public.ai_consent_guard() to anon", "trigger function"),
    ("grant execute on function public.norm_text(text) to public", "anonymous role"),
    ("alter table public.ai_inference_consents disable row level security", "row level security is on"),
    ("alter table public.ai_inference_consents add column provider_api_key text", "credential"),
    ("grant update on public.ai_inference_consents to authenticated", "only the status"),
    ("grant delete on public.ai_inference_consents to authenticated", "only SELECT"),
    ("grant insert (granted_by) on public.ai_inference_consents to authenticated", "only the columns the product uses"),
    ("drop trigger ai_consent_no_truncate on public.ai_inference_consents", "four triggers"),
    ("drop index public.ai_consent_one_active_grant", "indexes"),
    ("grant select on public.faqs to anon", "anonymous role holds no privilege"),
    ("create or replace function public.ai_consent_allows(p_business uuid, p_provider text, p_version text) returns boolean language sql stable security definer as $$ select true $$", "SECURITY INVOKER"),
    ("create or replace function public.has_role(b uuid, roles text[]) returns boolean language sql security definer as $$ select true $$", "pins an empty search_path"),
    ("alter table public.ai_inference_settings drop constraint ai_settings_paid_needs_cap", "local first"),
    ("alter table public.ai_inference_settings alter column fallback_free set default true", "local first"),
    ("grant update (fallback_paid, paid_cap_usd) on public.ai_inference_settings to authenticated", "turn a hosted step on"),
    ("grant update (free_requests_per_month) on public.business_plans to authenticated", "turn a hosted step on"),
]


@pytest.mark.parametrize("sql,expected", BREAKAGES, ids=[b[1] + ":" + b[0][:40] for b in BREAKAGES])
def test_a_misconfiguration_is_caught_by_name(fresh, sql, expected):
    fresh.su(sql)
    report = run(fresh)
    assert any(expected in name for name in failed(report)), (sql, failed(report))


def test_a_policy_that_lets_anyone_revoke_is_caught_by_behaviour_not_only_by_inspection(fresh):
    fresh.su("drop policy ai_consent_revoke on public.ai_inference_consents")
    fresh.su("create policy ai_consent_revoke on public.ai_inference_consents for update to authenticated using (true) with check (true)")
    report = run(fresh)
    assert any("cannot revoke" in n for n in failed(report))  # the policies' names and commands are unchanged: only trying it shows the difference


def test_a_missing_guard_trigger_is_caught_by_behaviour(fresh):
    fresh.su("drop trigger ai_consent_guard on public.ai_inference_consents")
    names = failed(run(fresh))
    assert any("four triggers" in n for n in names) and any("delete nor empty" in n or "made current again" in n or "who agreed" in n for n in names)


def test_a_broken_check_that_cannot_run_is_a_failure_not_a_pass(fresh):
    fresh.su("alter table public.ai_inference_consents drop constraint ai_inference_consents_check")  # the status/revocation pairing
    assert any("constraints exist" in n for n in failed(run(fresh)))


# ---------------------------------------------------------------- what it refuses to touch
def dsn(ref, user="postgres", host=None):
    return f"postgresql://{user}:pw-never-shown@{host or f'db.{ref}.supabase.co'}:5432/postgres"


def test_the_project_is_read_from_the_connection_string():
    assert V.project_ref(dsn(STAGING)) == STAGING
    assert V.project_ref(f"postgresql://postgres.{STAGING}:pw@aws-0-eu-west-1.pooler.supabase.com:6543/postgres") == STAGING
    assert V.project_ref("postgresql://apple@localhost/faq_rls_x") is None and V.project_ref("dbname=postgres") is None


@pytest.mark.parametrize("env,message", [
    ({}, "STAGING_PROJECT_REF is not set"),
    ({"STAGING_PROJECT_REF": STAGING}, "must repeat"),
    ({"STAGING_PROJECT_REF": STAGING, "STAGING_CONFIRM_REF": OTHER}, "must repeat"),
    ({"STAGING_PROJECT_REF": OTHER, "STAGING_CONFIRM_REF": OTHER}, "not at the declared staging project"),
    ({"STAGING_PROJECT_REF": STAGING, "STAGING_CONFIRM_REF": STAGING, "FORBIDDEN_PROJECT_REFS": f"{PROD}, {STAGING}"}, "FORBIDDEN_PROJECT_REFS"),
])
def test_it_refuses_a_target_that_is_not_the_declared_staging_project(env, message):
    with pytest.raises(V.Refused, match=message):
        V.guard(dsn(STAGING), env, allow_local=False)


def test_a_forbidden_project_is_refused_even_when_it_is_declared_and_confirmed():
    env = {"STAGING_PROJECT_REF": PROD, "STAGING_CONFIRM_REF": PROD, "FORBIDDEN_PROJECT_REFS": PROD}
    with pytest.raises(V.Refused, match="FORBIDDEN"):
        V.guard(dsn(PROD), env, allow_local=True)


def test_a_declared_and_confirmed_staging_project_is_accepted_and_named():
    assert V.guard(dsn(STAGING), {"STAGING_PROJECT_REF": STAGING, "STAGING_CONFIRM_REF": STAGING, "FORBIDDEN_PROJECT_REFS": f"{PROD},{OTHER}"}, False) == {"kind": "supabase", "ref": STAGING}


def test_a_local_server_is_refused_unless_it_is_asked_for_by_name():
    with pytest.raises(V.Refused, match="would not be a staging validation"):
        V.guard("postgresql://apple@localhost/x", {}, allow_local=False)
    assert V.guard("postgresql://apple@localhost/x", {}, allow_local=True) == {"kind": "local", "ref": None}


def test_a_database_without_the_engines_tables_is_refused_before_anything_runs(fresh):
    other = "faq_empty_" + fresh.name[-6:]
    fresh.admin.execute(f"create database {other}")
    try:
        with pytest.raises(V.Refused, match="not all applied"), psycopg.connect(f"dbname={other}") as c:
            V.check_engine_only(c)
    finally:
        fresh.admin.execute(f"drop database {other} with (force)")


def test_a_database_that_holds_another_products_tables_is_refused(fresh):
    fresh.su("create table public.washo_bookings (id uuid primary key)")
    with pytest.raises(V.Refused, match="washo_bookings"), psycopg.connect(fresh.dsn) as c:
        V.check_engine_only(c)
    with pytest.raises(V.Refused, match="not the engine's staging"):
        V.run(fresh.dsn, {}, allow_local=True)


def test_the_engine_tables_are_read_from_the_migrations():
    t = V.engine_tables()
    assert {"businesses", "faqs", "document_chunks", "ai_inference_consents", "audit_logs", "llm_provider_secrets"} <= t and len(t) == 27


# ---------------------------------------------------------------- the command
def test_the_command_needs_its_environment_and_never_prints_the_connection_string(capsys):
    assert V.main([], env={}) == 2
    secret_dsn = dsn(PROD)
    code = V.main([], env={"STAGING_DATABASE_URL": secret_dsn, "STAGING_PROJECT_REF": STAGING, "STAGING_CONFIRM_REF": STAGING, "FORBIDDEN_PROJECT_REFS": PROD})
    err = capsys.readouterr().err
    assert code == 3 and "REFUSED" in err and "pw-never-shown" not in err and secret_dsn not in err


def test_the_command_writes_its_report_and_exits_with_the_result(good, tmp_path, capsys):
    out = tmp_path / "r" / "report.json"
    assert V.main(["--allow-local", "--json", str(out)], env={"STAGING_DATABASE_URL": good.dsn}) == 0
    import json

    data = json.loads(out.read_text())
    assert data["failed"] == 0 and data["is_staging_validation"] is False and "NOT A STAGING VALIDATION" in capsys.readouterr().out


def test_a_connection_that_fails_is_reported_by_kind_and_never_quotes_the_string(capsys):
    secret = "postgresql://postgres:hunter2-must-not-appear@127.0.0.1:1/postgres?sslmode=disable"
    assert V.main(["--allow-local"], env={"STAGING_DATABASE_URL": secret}) == 4
    err = capsys.readouterr()
    assert "COULD NOT RUN" in err.err and "OperationalError" in err.err
    assert "hunter2" not in err.err + err.out and "127.0.0.1" not in err.err + err.out and "postgres:" not in err.err + err.out


def test_the_report_holds_no_connection_string_and_names_the_project_only_by_its_ref(good):
    report = V.run(good.dsn, {}, allow_local=True)
    blob = json.dumps(report, default=str) + V.render(report)
    assert good.dsn not in blob and "dbname=" not in blob and "postgresql://" not in blob
    assert set(report["target"]) == {"kind", "project_ref", "server_version", "current_user", "tables"}
