"""A throwaway PostgreSQL database with every Supabase migration applied (and the stand-in for the schemas Supabase provides), for tests that need the real schema.

`make_db()` yields a `Db`; it skips the test when no local PostgreSQL with pgvector can be reached."""
import uuid
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

SUPABASE = Path(__file__).resolve().parents[1] / "supabase"
DENIED = (psycopg.errors.InsufficientPrivilege, psycopg.errors.CheckViolation, psycopg.errors.UniqueViolation, psycopg.errors.ForeignKeyViolation, psycopg.errors.RaiseException,
          psycopg.errors.NotNullViolation)


def vec(i: int, x: float = 1.0) -> str:
    return "[" + ",".join(str(x if k == i else 0.0) for k in range(384)) + "]"


class Db:
    def __init__(self, name: str, admin):
        self.name, self.admin = name, admin
        self.dsn = f"dbname={name}"

    def su(self, sql, params=None):
        with self.admin.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else None

    def run(self, uid, sql, params=None, role="authenticated"):
        """One statement as a person (or as anon, or as `server`: the database owner, which is what the migrations mean by the server; the cluster's own `service_role` is shared with other
        projects on this machine and is not altered). Committed, so later checks see the effect; an error rolls back and is raised."""
        with psycopg.connect(f"dbname={self.name}") as conn:
            if role != "server":
                conn.execute(f"set local role {role}")
            else:
                conn.execute("select set_config('request.jwt.claim.role', 'service_role', true)")  # what PostgREST sets for a service-role request
            if uid:
                conn.execute("select set_config('request.jwt.claim.sub', %s, true)", (str(uid),))
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall() if cur.description else None

    def denied(self, uid, sql, params=None, role="authenticated"):
        with pytest.raises(DENIED):
            self.run(uid, sql, params, role)

    def user(self, name):
        return self.su("insert into auth.users (email) values (%s) returning id", (f"{name}@example.com",))[0][0]

    def business(self, owner, slug=None):
        slug = slug or f"b-{uuid.uuid4().hex[:10]}"
        return self.run(owner, "select id from public.create_business(%s, %s)", (slug.upper(), slug))[0][0]

    def member(self, b, uid, role):
        self.su("insert into public.business_members (business_id, user_id, role) values (%s, %s, %s)", (b, uid, role))


def make_db():
    """A generator for a pytest fixture: `yield from make_db()`."""
    try:
        admin = psycopg.connect("dbname=postgres", autocommit=True)
    except psycopg.Error as e:
        pytest.skip(f"no local PostgreSQL to test on ({e})")
    name = f"faq_rls_{uuid.uuid4().hex[:8]}"
    admin.execute(f"create database {name}")
    inner = psycopg.connect(f"dbname={name}", autocommit=True)
    try:
        inner.execute((SUPABASE / "tests" / "00_supabase_stand_in.sql").read_text())
        for f in sorted((SUPABASE / "migrations").glob("*.sql")):
            try:
                inner.execute(f.read_text())
            except psycopg.errors.UndefinedFile:
                pytest.skip("the pgvector extension is not installed in the local PostgreSQL")
        yield Db(name, inner)
    finally:
        inner.close()
        admin.execute(f"drop database if exists {name} with (force)")
        admin.close()
