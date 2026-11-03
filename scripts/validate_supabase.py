#!/usr/bin/env python3
"""Validates the engine's database as a real Supabase project has it: what exists, who can do what under the API roles, and that the consent rules hold. Read this before running it on anything.

    STAGING_DATABASE_URL=...  STAGING_PROJECT_REF=...  STAGING_CONFIRM_REF=...  FORBIDDEN_PROJECT_REFS=ref1,ref2  python scripts/validate_supabase.py

WHAT IT DOES NOT DO: it applies no migration and changes nothing that stays. Apply the migrations to the STAGING project yourself (`supabase link --project-ref <staging>` then `supabase db push`);
this script then reads the catalog and runs every behaviour check inside ONE transaction that is rolled back at the end (test people, businesses, consents and audit rows never persist).

WHAT IT REFUSES, before it connects to anything that matters:
  * a target without STAGING_PROJECT_REF, or whose STAGING_CONFIRM_REF does not equal it, or whose project (taken from the connection string) is not that project;
  * any project listed in FORBIDDEN_PROJECT_REFS (put production, and every other product's project, there);
  * a database that is not an engine-only database: it must already hold the engine's tables (docs/supabase-architecture.md) and must hold no table the engine does not own, so a project that
    runs some other product can never pass for the engine's staging;
  * a database that is not a Supabase project (a local PostgreSQL) unless --allow-local is given, and then the report says in its first line that it is NOT a staging validation.

CREDENTIALS: the connection string is read from the environment variable named by --dsn-env and is never printed, written to the report or logged; a failure to connect is reported by its class and SQLSTATE
only. The report names the project by its ref (not a secret) and holds no key, password or token.

HOW IT TESTS: the Data API acts for a person by switching to the role `authenticated` and setting the person's id in the request's claims; this script does exactly that, inside SQL, as each kind
of person (owner, admin, editor, viewer, a member of another business, an anonymous visitor, the server). It therefore tests the database's own rules under the real roles. It does NOT test
PostgREST, GoTrue (sign-up, sign-in, tokens), the Storage API or the pooler; those are in docs/staging-validation.md as steps for a person.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
SECRETISH = re.compile(r"key|secret|token(?!s_)|password|passwd|credential|authorization|bearer", re.I)  # (a COUNT of tokens, tokens_per_month, is not a token)
ROLES = ("anon", "authenticated", "service_role")
V1, V2 = "2026-11-02.validation1", "2026-11-02.validation2"
CONSENT = "public.ai_inference_consents"
INSERT = f"insert into {CONSENT} (business_id, provider, disclosure_version, disclosure_sha256) values (%s, %s, %s, %s) returning id, granted_by"


class Refused(Exception):
    """The target is not one this script will touch."""


def engine_tables() -> set[str]:
    """The tables the engine's migrations create in `public` (read from the migration files, so there is one list)."""
    return {m.group(1) for f in sorted(MIGRATIONS.glob("*.sql")) for m in re.finditer(r"^create table (?:if not exists )?public\.(\w+)", f.read_text(), re.M | re.I)}


def project_ref(dsn: str) -> str | None:
    """The Supabase project a connection string points at: db.<ref>.supabase.co, or the pooler's user postgres.<ref>."""
    m = re.search(r"@db\.([a-z0-9]{20})\.supabase\.(?:co|com)\b", dsn) or re.search(r"[/:]postgres\.([a-z0-9]{20})[:@]", dsn)
    return m.group(1) if m else None


def guard(dsn: str, env: dict, allow_local: bool) -> dict:
    """Refuses (raises Refused) unless the target is the declared staging project; returns what it learned about the target."""
    ref = project_ref(dsn)
    forbidden = {x.strip() for x in env.get("FORBIDDEN_PROJECT_REFS", "").split(",") if x.strip()}
    if ref:
        if ref in forbidden:
            raise Refused(f"project {ref} is listed in FORBIDDEN_PROJECT_REFS")
        declared, confirm = env.get("STAGING_PROJECT_REF", ""), env.get("STAGING_CONFIRM_REF", "")
        if not declared:
            raise Refused("STAGING_PROJECT_REF is not set: say which project is the staging project")
        if confirm != declared:
            raise Refused("STAGING_CONFIRM_REF must repeat STAGING_PROJECT_REF exactly (a second, deliberate typing of the same ref)")
        if ref != declared:
            raise Refused(f"the connection string points at project {ref}, not at the declared staging project {declared}")
    elif not allow_local:
        raise Refused("the connection string is not a Supabase project's; this would not be a staging validation (use --allow-local to run the checks on a local PostgreSQL anyway)")
    return {"kind": "supabase" if ref else "local", "ref": ref}


def check_engine_only(conn) -> list[str]:
    """Refuses (raises Refused) a database that is not exactly the engine's: its tables must be there, and nobody else's may be."""
    have = {r[0] for r in conn.execute("select tablename from pg_tables where schemaname = 'public'")}
    expected = engine_tables()
    missing = sorted(expected - have)
    if missing:
        raise Refused(f"the engine's migrations are not all applied here (missing: {', '.join(missing[:6])}); apply them to the staging project first")
    foreign = sorted(have - expected)
    if foreign:
        raise Refused(f"this database holds tables the engine does not own ({', '.join(foreign[:6])}): it is not the engine's staging project")
    return sorted(have)


def result(group: str, name: str, ok: bool, detail: str = "") -> dict:
    return {"group": group, "check": name, "ok": bool(ok), "detail": detail}


# ---------------------------------------------------------------- the catalog: what exists and who holds which privilege
def catalog_checks(conn) -> list[dict]:
    out: list[dict] = []
    q = lambda sql, *p: conn.execute(sql, p).fetchall()  # noqa: E731
    add = lambda name, ok, detail="": out.append(result("catalog", name, ok, detail))  # noqa: E731

    owner = q("select pg_get_userbyid(relowner) from pg_class where oid = 'public.businesses'::regclass")[0][0]
    no_rls = [r[0] for r in q("select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace where n.nspname = 'public' and c.relkind = 'r' and not c.relrowsecurity")]
    add("row level security is on for every table in public", not no_rls, f"without: {', '.join(no_rls)}" if no_rls else f"{len(engine_tables())} tables")

    anon_t = q("select table_name, privilege_type from information_schema.role_table_grants where table_schema = 'public' and grantee = 'anon'")
    add("the anonymous role holds no privilege on any table", not anon_t, ", ".join(f"{t}:{p}" for t, p in anon_t[:6]))
    risky = q("select table_name, grantee, privilege_type from information_schema.role_table_grants where table_schema = 'public' and grantee in ('anon','authenticated') and privilege_type in ('TRUNCATE','TRIGGER','REFERENCES')")
    add("no API role can TRUNCATE, TRIGGER or REFERENCE a table (TRUNCATE ignores row level security)", not risky, ", ".join(f"{t}:{g}:{p}" for t, g, p in risky[:6]))

    fns = q("select p.oid, p.proname, p.prosecdef, p.proconfig, pg_get_userbyid(p.proowner), p.prorettype = 'pg_catalog.trigger'::regtype from pg_proc p join pg_namespace n on n.oid = p.pronamespace where n.nspname = 'public'")
    def public_exec(oid):  # EXECUTE held by PUBLIC (grantee 0)
        return bool(conn.execute("select 1 from pg_proc p, aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a where p.oid = %s and a.grantee = 0 and a.privilege_type = 'EXECUTE'", (oid,)).fetchone())
    def role_exec(oid, role):
        return conn.execute("select has_function_privilege(%s, %s, 'EXECUTE')", (role, oid)).fetchone()[0]
    trig = [(oid, name) for oid, name, _, _, _, is_trigger in fns if is_trigger]
    callable_ = [name for oid, name in trig if public_exec(oid) or any(role_exec(oid, r) for r in ROLES)]
    add("no trigger function can be called by PUBLIC or an API role", not callable_, ", ".join(callable_[:6]))
    sd = [(oid, name, cfg, own) for oid, name, secdef, cfg, own, _ in fns if secdef]
    bad_path = [n for _, n, cfg, _ in sd if not any(re.match(r'search_path=("")?$|search_path=\'\'$', c) for c in (cfg or []))]
    add("every SECURITY DEFINER function pins an empty search_path", not bad_path, ", ".join(bad_path))
    bad_owner = [n for _, n, _, own in sd if own != owner]
    add("every SECURITY DEFINER function is owned by the schema owner", not bad_owner, ", ".join(bad_owner) or f"owner {owner}")
    anon_exec = [n for oid, n, *_ in fns if public_exec(oid) or role_exec(oid, "anon")]
    add("no function in public can be executed by PUBLIC or the anonymous role", not anon_exec, ", ".join(anon_exec[:8]))

    # the consent table
    cols = [r[0] for r in q("select column_name from information_schema.columns where table_schema = 'public' and table_name = 'ai_inference_consents' order by ordinal_position")]
    add("the consent table has no column that could hold a credential", cols and not [c for c in cols if SECRETISH.search(c)], ", ".join(cols))
    grants = q("select privilege_type from information_schema.role_table_grants where table_schema = 'public' and table_name = 'ai_inference_consents' and grantee = 'authenticated'")
    add("a signed-in person has only SELECT on the consent table as a whole", sorted(p for (p,) in grants) == ["SELECT"], ", ".join(sorted(p for (p,) in grants)))
    colgrants: dict[str, set[str]] = {}
    for c, p in q("select column_name, privilege_type from information_schema.role_column_grants where table_schema = 'public' and table_name = 'ai_inference_consents' and grantee = 'authenticated' and privilege_type in ('INSERT','UPDATE')"):
        colgrants.setdefault(p, set()).add(c)
    add("a signed-in person can fill in only the columns the product uses when granting", colgrants.get("INSERT") == {"business_id", "provider", "disclosure_version", "disclosure_sha256", "data_categories"}, str(sorted(colgrants.get("INSERT", []))))
    add("a signed-in person can change only the status of a consent (to revoke it)", colgrants.get("UPDATE") == {"status"}, str(sorted(colgrants.get("UPDATE", []))))
    pol = q("select policyname, cmd, roles::text from pg_policies where schemaname = 'public' and tablename = 'ai_inference_consents' order by 1")
    add("the consent table has exactly its three policies, for signed-in people only", [(a, b) for a, b, _ in pol] == [("ai_consent_grant", "INSERT"), ("ai_consent_read", "SELECT"), ("ai_consent_revoke", "UPDATE")] and all(r == "{authenticated}" for *_, r in pol), str(pol))
    idx = {r[0] for r in q("select indexname from pg_indexes where schemaname = 'public' and tablename = 'ai_inference_consents'")}
    add("the consent table has its indexes (one active grant per business and provider; lookup)", {"ai_consent_one_active_grant", "ai_consent_business_idx", "ai_inference_consents_pkey"} <= idx, ", ".join(sorted(idx)))
    trg = {r[0]: r[1] for r in q("select tgname, tgenabled from pg_trigger where tgrelid = 'public.ai_inference_consents'::regclass and not tgisinternal")}
    want = {"ai_consent_touch", "ai_consent_guard", "audit_ai_consent", "ai_consent_no_truncate"}
    add("the consent table's four triggers exist and are enabled", want <= set(trg) and all(trg[t] == "O" for t in want), str(trg))
    cons = " ".join(r[0] for r in q("select pg_get_constraintdef(oid) from pg_constraint where conrelid = 'public.ai_inference_consents'::regclass"))
    add("the consent table's constraints exist (status/revocation pairing, provider key shape, business cascade)", "revoked_at IS NULL" in cons and "provider ~" in cons.replace("(provider)::text ~", "provider ~") and "ON DELETE CASCADE" in cons, "")
    f = {r[0]: r for r in q("select p.proname, p.prosecdef, p.proconfig, p.oid from pg_proc p join pg_namespace n on n.oid = p.pronamespace where n.nspname = 'public' and p.proname in ('ai_consent_allows', 'audit_event')")}
    allows = f.get("ai_consent_allows")
    ok = allows is not None and not allows[1] and any("search_path" in c for c in (allows[2] or [])) and role_exec(allows[3], "authenticated") and role_exec(allows[3], "service_role") and not role_exec(allows[3], "anon") and not public_exec(allows[3])
    add("ai_consent_allows is SECURITY INVOKER (row level security applies), pins its search_path, and only signed-in people and the server can run it", ok, "" if ok else str(allows))
    ae = f.get("audit_event")
    body = q("select prosrc from pg_proc where proname = 'audit_event' and pronamespace = 'public'::regnamespace")[0][0] if ae else ""
    add("the audit function knows the consent table and records no content", ae is not None and bool(ae[1]) and "ai_inference_consents" in body and "'provider'" in body, "")
    # migration 12: the mode settings, plans, companions and invitations
    ro = {}
    for t, p in q("select table_name, privilege_type from information_schema.role_table_grants where table_schema = 'public' and grantee = 'authenticated' and table_name in ('ai_inference_settings','business_plans','local_companions')"):
        ro.setdefault(t, set()).add(p)
    add("a signed-in person can only READ the mode settings, the plan and the paired companions (the engine service writes them)", ro == {t: {"SELECT"} for t in ("ai_inference_settings", "business_plans", "local_companions")}, str(ro))
    inv_cols = {r[0] for r in q("select column_name from information_schema.role_column_grants where table_schema = 'public' and table_name = 'business_invitations' and grantee = 'authenticated' and privilege_type = 'SELECT'")}
    inv_write = q("select privilege_type from information_schema.role_table_grants where table_schema = 'public' and table_name = 'business_invitations' and grantee = 'authenticated' and privilege_type <> 'SELECT'")
    add("invitations are readable without their secret's hash and are not writable directly", "token_hash" not in inv_cols and "email" in inv_cols and not inv_write, str(sorted(inv_cols)))
    add("nobody can insert a member directly: a person joins by accepting an invitation", not q("select 1 from information_schema.role_table_grants where table_schema = 'public' and table_name = 'business_members' and grantee in ('anon','authenticated') and privilege_type = 'INSERT'")
        and not q("select 1 from pg_policies where tablename = 'business_members' and cmd = 'INSERT'"), "")
    fns12 = {r[0]: r for r in q("select p.proname, p.prosecdef, p.proconfig, p.oid from pg_proc p where p.pronamespace = 'public'::regnamespace and p.proname in ('create_invitation','accept_invitation','revoke_invitation')")}
    add("the three invitation functions are SECURITY DEFINER with a pinned search_path, for signed-in people and the server only", len(fns12) == 3 and all(f[1] and any("search_path" in c for c in (f[2] or [])) and role_exec(f[3], "authenticated") and not role_exec(f[3], "anon") and not public_exec(f[3]) for f in fns12.values()), str(sorted(fns12)))
    cols12 = [r[0] for r in q("select column_name from information_schema.columns where table_schema = 'public' and table_name in ('local_companions','ai_inference_settings','business_plans') ")]
    add("the new tables hold no column that could carry a credential", not [c for c in cols12 if SECRETISH.search(c)], "")
    cons13 = {r[0] for r in q("select conname from pg_constraint where conrelid = 'public.ai_inference_settings'::regclass")}
    defaults13 = {r[0]: r[1] for r in q("select column_name, column_default from information_schema.columns where table_schema = 'public' and table_name = 'ai_inference_settings' and column_name in ('fallback_byok','fallback_free','fallback_paid')")}
    add("local first: every step after the first is off by default, paid hosted needs the business's own spending cap, and a deterministic business has no follower (constraints in the database)",
        {"ai_settings_paid_needs_cap", "ai_settings_deterministic_has_no_fallback"} <= cons13 and len(defaults13) == 3 and all(v == "false" for v in defaults13.values()), f"{sorted(cons13)} {defaults13}")
    return out


# ---------------------------------------------------------------- behaviour: each kind of person, inside one transaction that is rolled back
class Run:
    def __init__(self, conn):
        self.c = conn
        self.n = 0
        cols = {r[0]: r[1] for r in conn.execute("select column_name, is_nullable from information_schema.columns where table_schema = 'auth' and table_name = 'users'")}
        self.user_cols = [c for c in ("aud", "role") if c in cols]

    def become(self, who: str | None, role: str = "authenticated") -> None:
        self.c.execute("reset role")
        claims = {"role": role, **({"sub": who} if who else {})}
        self.c.execute("select set_config('request.jwt.claims', %s, true), set_config('request.jwt.claim.sub', %s, true), set_config('request.jwt.claim.role', %s, true)", (json.dumps(claims), who or "", role))
        if role != "server":
            self.c.execute(f"set local role {role}")
        else:  # the engine's own connection: the database owner, naming the service role in its claims
            self.c.execute("select set_config('request.jwt.claim.role', 'service_role', true)")

    def attempt(self, who, sql, params=(), role="authenticated"):
        """('ok', rows) or ('denied', sqlstate), with the effects of a success kept and those of a failure undone."""
        self.n += 1
        sp = f"s{self.n}"
        self.c.execute(f"savepoint {sp}")
        try:
            self.become(who, role)
            cur = self.c.execute(sql, params)
            res = ("ok", cur.fetchall() if cur.description else [])
            self.c.execute("reset role")
            self.c.execute(f"release savepoint {sp}")
        except psycopg.Error as e:
            self.c.execute(f"rollback to savepoint {sp}")
            self.c.execute("reset role")
            res = ("denied", e.sqlstate or "?")
        return res

    def su(self, sql, params=()):
        self.c.execute("reset role")
        cur = self.c.execute(sql, params)
        return cur.fetchall() if cur.description else []

    def user(self, name: str) -> str:
        uid = str(uuid.uuid4())
        cols, vals = ["id", "email"], [uid, f"{name}-{uid[:8]}@validation.invalid"]
        for c in self.user_cols:
            cols.append(c)
            vals.append("authenticated")
        self.su(f"insert into auth.users ({', '.join(cols)}) values ({', '.join(['%s'] * len(vals))})", vals)
        return uid


def behaviour_checks(conn) -> list[dict]:
    out: list[dict] = []
    add = lambda name, ok, detail="": out.append(result("behaviour", name, ok, detail))  # noqa: E731
    r = Run(conn)
    alice, bob, erin, carol, dave, mallory = (r.user(n) for n in ("owner", "other-owner", "admin", "editor", "viewer", "stranger"))
    st, row = r.attempt(alice, "select id from public.create_business(%s, %s)", ("Validation A", f"validation-a-{uuid.uuid4().hex[:8]}"))
    A = row[0][0] if st == "ok" else None
    st2, row2 = r.attempt(bob, "select id from public.create_business(%s, %s)", ("Validation B", f"validation-b-{uuid.uuid4().hex[:8]}"))
    B = row2[0][0] if st2 == "ok" else None
    add("people can create their own businesses through the application's function", A is not None and B is not None, f"{st}/{st2}")
    if A is None or B is None:
        return out
    for who, role in ((erin, "admin"), (carol, "editor"), (dave, "viewer")):
        r.su("insert into public.business_members (business_id, user_id, role) values (%s, %s, %s)", (A, who, role))

    st, rows = r.attempt(alice, INSERT, (A, "cloudflare", V1, "a" * 64))
    cid = rows[0][0] if st == "ok" else None
    add("an owner can record an agreement, and the database records them as the person who agreed", st == "ok" and rows[0][1] == uuid.UUID(alice), f"{st}")
    st, rows = r.attempt(erin, INSERT, (A, "openrouter", V1, None))
    add("an admin can record an agreement", st == "ok" and rows[0][1] == uuid.UUID(erin), st)
    stored = r.su("select status, disclosure_version, disclosure_sha256, revoked_at, data_categories from public.ai_inference_consents where id = %s", (cid,))
    add("the agreement keeps the disclosure version, the fingerprint of the text shown and the data categories", bool(stored) and stored[0][:4] == ("granted", V1, "a" * 64, None) and set(stored[0][4]) == {"question", "evidence_passages"}, str(stored[:1]))

    denied = {n: r.attempt(w, INSERT, (A, "gemini", V1, None))[0] == "denied" for n, w in (("an editor", carol), ("a viewer", dave), ("a stranger", mallory), ("another business's owner", bob))}
    add("an editor, a viewer, a stranger and another business's owner cannot record an agreement", all(denied.values()), str(denied))
    forged = [r.attempt(erin, s, p)[0] for s, p in (
        (f"insert into {CONSENT} (business_id, provider, disclosure_version, granted_by) values (%s, 'x-forged', %s, %s)", (A, V1, bob)),
        (f"insert into {CONSENT} (business_id, provider, disclosure_version, status) values (%s, 'x-forged', %s, 'revoked')", (A, V1)),
        (f"insert into {CONSENT} (id, business_id, provider, disclosure_version) values (gen_random_uuid(), %s, 'x-forged', %s)", (A, V1)))]
    add("a request cannot name who agreed, set the status, or choose the id", forged == ["denied"] * 3, str(forged))
    add("an agreement for a business the person is not in is refused even by an owner elsewhere", r.attempt(bob, INSERT, (A, "x-cross", V1, None))[0] == "denied")

    seen = {n: r.attempt(w, f"select count(*) from {CONSENT} where business_id = %s", (A,)) for n, w in (("owner", alice), ("viewer", dave), ("other", bob), ("stranger", mallory))}
    add("every member of the business can read its agreements; people outside it see none", seen["owner"][1][0][0] >= 2 and seen["viewer"][1][0][0] >= 2 and seen["other"][1][0][0] == 0 and seen["stranger"][1][0][0] == 0, str({k: v[1][0][0] if v[0] == "ok" else v for k, v in seen.items()}))
    add("an anonymous visitor can neither read the agreements nor ask the question", r.attempt(None, f"select count(*) from {CONSENT}", role="anon")[0] == "denied" and r.attempt(None, "select public.ai_consent_allows(%s, 'cloudflare', %s)", (A, V1), role="anon")[0] == "denied")

    allows = lambda who, b, p, v: r.attempt(who, "select public.ai_consent_allows(%s, %s, %s)", (b, p, v))[1][0][0]  # noqa: E731
    add("the question is answered yes for the exact provider and disclosure version", allows(alice, A, "cloudflare", V1) is True and allows(dave, A, "cloudflare", V1) is True)
    add("it is answered no for another provider, another disclosure version, and another business's question", allows(alice, A, "gemini", V1) is False and allows(alice, A, "cloudflare", V2) is False and allows(bob, A, "cloudflare", V1) is False)

    st, rows = r.attempt(carol, f"update {CONSENT} set status = 'revoked' where id = %s returning id", (cid,))
    st_v, rows_v = r.attempt(dave, f"update {CONSENT} set status = 'revoked' where id = %s returning id", (cid,))
    st_o, rows_o = r.attempt(bob, f"update {CONSENT} set status = 'revoked' where id = %s returning id", (cid,))
    add("an editor, a viewer and another business's owner cannot revoke", all(s == "denied" or x == [] for s, x in ((st, rows), (st_v, rows_v), (st_o, rows_o))) and allows(alice, A, "cloudflare", V1) is True, f"{st}/{st_v}/{st_o}")
    other_cols = [r.attempt(alice, f"update {CONSENT} set {c} = {v} where id = %s", (cid,))[0] for c, v in (("provider", "'gemini'"), ("disclosure_version", "'x'"), ("granted_by", "null"), ("business_id", f"'{B}'"))]
    add("the only thing that can change in an agreement is its status", other_cols == ["denied"] * 4, str(other_cols))

    st, rows = r.attempt(erin, f"update {CONSENT} set status = 'revoked' where id = %s returning id", (cid,))
    add("an admin can revoke, and the very next question is answered no", st == "ok" and len(rows) == 1 and allows(alice, A, "cloudflare", V1) is False and allows(alice, A, "openrouter", V1) is True)
    rev = r.su(f"select status, revoked_by, revoked_at is not null from {CONSENT} where id = %s", (cid,))
    add("a revocation records who and when, and the row stays", rev == [("revoked", uuid.UUID(erin), True)], str(rev))
    add("a revoked agreement cannot be made current again, and a new agreement is a new row", r.attempt(alice, f"update {CONSENT} set status = 'granted' where id = %s", (cid,))[0] == "denied"
        and r.attempt(alice, INSERT, (A, "cloudflare", V2, None))[0] == "ok" and r.su(f"select count(*) from {CONSENT} where business_id = %s and provider = 'cloudflare'", (A,))[0][0] == 2)

    d1 = r.attempt(alice, f"delete from {CONSENT} where business_id = %s", (A,))
    t1 = r.attempt(alice, f"truncate {CONSENT}")
    add("a signed-in owner can neither delete nor empty the history", d1[0] == "denied" and t1[0] == "denied")
    d2 = r.attempt(None, f"delete from {CONSENT} where business_id = %s", (A,), role="server")
    t2 = r.attempt(None, f"truncate {CONSENT}", role="server")
    add("not even the server's own connection can delete or empty the history", d2[0] == "denied" and t2[0] == "denied", f"{d2[0]}/{t2[0]}")

    st, rows = r.attempt(erin, INSERT, (A, "server-named", V1, None), role="server")
    add("the server can record an agreement naming who agreed (the engine acts for a person)", st == "ok", st)

    audit = r.su("select actor_id, action, detail from public.audit_logs where business_id = %s and target_type = 'ai_inference_consents' order by created_at", (A,))
    acts = [a[1] for a in audit]
    keys = {frozenset(a[2]) for a in audit}
    add("every grant and revocation is in the audit trail, with who did it", acts.count("ai_inference_consents.insert") >= 3 and "ai_inference_consents.update" in acts and uuid.UUID(alice) in {a[0] for a in audit} and uuid.UUID(erin) in {a[0] for a in audit}, f"{len(audit)} rows")
    add("the audit rows say which provider and which versions, and hold nothing else", keys <= {frozenset({"op", "provider", "status_before", "status_after", "disclosure_version"})}, str(keys))
    add("only an owner or admin can read the audit trail of the business", r.attempt(dave, "select count(*) from public.audit_logs where business_id = %s", (A,))[1][0][0] == 0 and r.attempt(alice, "select count(*) from public.audit_logs where business_id = %s", (A,))[1][0][0] > 0)

    # migration 12: invitations, and the settings only the service writes
    add("nobody, an owner included, can insert a member directly", all(r.attempt(w, "insert into public.business_members (business_id, user_id, role) values (%s, %s, 'viewer')", (A, mallory))[0] == "denied" for w in (alice, erin)))
    invitee = r.user("invitee")
    mail = r.su("select email from auth.users where id = %s", (invitee,))[0][0]
    st, tok = r.attempt(erin, "select public.create_invitation(%s, %s, 'viewer')", (A, mail))
    tok = tok[0][0] if st == "ok" else ""
    add("an admin can invite, and the secret comes back once as 64 hex characters", st == "ok" and bool(re.fullmatch(r"[0-9a-f]{64}", tok)), st)
    add("an editor, a viewer, a stranger and another business's owner cannot invite; an admin cannot invite an admin", all(r.attempt(w, "select public.create_invitation(%s, %s, 'viewer')", (A, "x@validation.invalid"))[0] == "denied" for w in (carol, dave, mallory, bob))
        and r.attempt(erin, "select public.create_invitation(%s, %s, 'admin')", (A, "y@validation.invalid"))[0] == "denied")
    add("the secret works for nobody but the invited person, and the stored copy is a hash", r.attempt(mallory, "select public.accept_invitation(%s)", (tok,))[0] == "denied"
        and r.su("select token_hash <> %s and length(token_hash) = 64 from public.business_invitations where email = %s", (tok, mail))[0][0] is True)
    st, joined = r.attempt(invitee, "select public.accept_invitation(%s)", (tok,))
    add("the invited person joins with the invited role, once", st == "ok" and r.su("select role from public.business_members where business_id = %s and user_id = %s", (A, invitee)) == [("viewer",)]
        and r.attempt(invitee, "select public.accept_invitation(%s)", (tok,))[0] == "denied", st)
    add("invitations are readable by owners and admins only, and never their hash", r.attempt(alice, "select count(*) from public.business_invitations where business_id = %s", (A,))[1][0][0] >= 1
        and r.attempt(dave, "select count(*) from public.business_invitations where business_id = %s", (A,))[1][0][0] == 0 and r.attempt(alice, "select token_hash from public.business_invitations")[0] == "denied")
    add("no signed-in person can turn a hosted step on, set the spending cap or give itself a free allowance", all(r.attempt(alice, sql, (A,))[0] == "denied" for sql in (
        "update public.ai_inference_settings set fallback_paid = true, paid_cap_usd = 1000 where business_id = %s", "update public.ai_inference_settings set fallback_free = true where business_id = %s",
        "update public.business_plans set free_requests_per_month = 99999999 where business_id = %s", "update public.usage_records set tier = 'local' where business_id = %s")))
    add("no signed-in person can write the mode settings, the plan or the companions", all(r.attempt(alice, sql, (A,))[0] == "denied" for sql in (
        "insert into public.ai_inference_settings (business_id, mode) values (%s, 'byok')", "insert into public.business_plans (business_id) values (%s)", "insert into public.local_companions (business_id, label) values (%s, 'x')")))
    return out


def run(dsn: str, env: dict, allow_local: bool = False) -> dict:
    target = guard(dsn, env, allow_local)
    with psycopg.connect(dsn, autocommit=False) as conn:
        info = {"server_version": conn.execute("show server_version").fetchone()[0], "current_user": conn.execute("select current_user").fetchone()[0]}
        tables = check_engine_only(conn)
        results = catalog_checks(conn)
        conn.rollback()
        results += behaviour_checks(conn)
        conn.rollback()  # nothing the checks made stays: test people, businesses, agreements and audit rows are gone
    kind = target["kind"]
    return {"started": datetime.now(timezone.utc).isoformat(timespec="seconds"), "target": {"kind": kind, "project_ref": target["ref"], **info, "tables": len(tables)},
            "is_staging_validation": kind == "supabase", "results": results, "passed": sum(x["ok"] for x in results), "failed": sum(not x["ok"] for x in results)}


def render(report: dict) -> str:
    t = report["target"]
    head = ("STAGING VALIDATION of Supabase project " + str(t["project_ref"])) if report["is_staging_validation"] else "NOT A STAGING VALIDATION: the target is a local PostgreSQL, not a Supabase project. These results say the checks work, not that a project is right."
    lines = [head, f"server {t['server_version']} · connected as {t['current_user']} · {t['tables']} tables · {report['started']}", ""]
    for x in report["results"]:
        lines.append(f"{'PASS' if x['ok'] else 'FAIL'}  [{x['group']}] {x['check']}" + ("" if x["ok"] or not x["detail"] else f"\n        {x['detail']}"))
    lines += ["", f"{report['passed']} passed, {report['failed']} failed"]
    return "\n".join(lines)


def main(argv=None, env=os.environ) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dsn-env", default="STAGING_DATABASE_URL", help="the environment variable that holds the connection string (never pass the string itself)")
    ap.add_argument("--allow-local", action="store_true", help="also run against a local PostgreSQL (the report then says it is not a staging validation)")
    ap.add_argument("--json", help="write the report here")
    args = ap.parse_args(argv)
    dsn = env.get(args.dsn_env)
    if not dsn:
        print(f"set {args.dsn_env} (in your protected environment file, not on the command line)", file=sys.stderr)
        return 2
    try:
        report = run(dsn, dict(env), args.allow_local)
    except Refused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 3
    except psycopg.Error as e:  # the server's own message can quote the connection (host, user, sometimes a malformed string): say what kind of failure it was and nothing it said
        print(f"COULD NOT RUN: the database reported a {type(e).__name__} (SQLSTATE {e.sqlstate or 'none'}). Check the connection string and the project, without pasting either here.", file=sys.stderr)
        return 4
    print(render(report))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(report, indent=2, default=str))
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
