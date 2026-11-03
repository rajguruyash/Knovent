"""The Supabase schema, run for real on PostgreSQL (with pgvector): every migration in order, then what each kind of person can and cannot do.

Each check connects as a different database role and person (`authenticated` with a JWT subject, `anon`, `service_role`), because row level security only means something when it is
exercised as the person it restricts. The schemas Supabase provides (auth, storage, the API roles) are stood in for by supabase/tests/00_supabase_stand_in.sql; a live Supabase project
(JWT issuing, the sign-up flow, PostgREST) is NOT exercised here.

Skipped when no local PostgreSQL with pgvector can be reached.
"""
import sys
import uuid
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from pgdb import DENIED, SUPABASE, Db, make_db, vec  # noqa: E402,F401



@pytest.fixture(scope="module")
def db():
    yield from make_db()


@pytest.fixture(scope="module")
def world(db):
    """Two businesses with a person in each role in the first, and some knowledge in both."""
    w = type("W", (), {})()
    w.alice, w.bob, w.erin, w.carol, w.dave, w.mallory = (db.user(n) for n in ("alice", "bob", "erin", "carol", "dave", "mallory"))
    w.A, w.B = db.business(w.alice, "alpha-co"), db.business(w.bob, "beta-co")
    db.member(w.A, w.erin, "admin")
    db.member(w.A, w.carol, "editor")
    db.member(w.A, w.dave, "viewer")
    for b, tag in ((w.A, "a"), (w.B, "b")):
        src = db.su("insert into public.knowledge_sources (business_id, name, source_type, storage_path) values (%s, %s, 'faq_table', %s) returning id", (b, f"faq-{tag}.csv", f"{b}/faq-{tag}.csv"))[0][0]
        faq = db.su("insert into public.faqs (business_id, source_id, external_id, question, answer) values (%s, %s, 'f1', %s, %s) returning id", (b, src, f"Question {tag}?", f"Answer {tag}."))[0][0]
        chunk = db.su("insert into public.document_chunks (business_id, source_id, ordinal, content, content_hash) values (%s, %s, 0, %s, %s) returning id", (b, src, f"Chunk text {tag}", f"hash-{tag}"))[0][0]
        db.su("insert into public.embeddings (business_id, faq_id, view_kind, view_text, model, embedding) values (%s, %s, 'q', %s, 'minilm', %s)", (b, faq, f"Question {tag}?", vec(0 if tag == "a" else 1)))
        db.su("insert into public.embeddings (business_id, chunk_id, view_kind, view_text, model, embedding) values (%s, %s, 'chunk', %s, 'minilm', %s)", (b, chunk, f"Chunk text {tag}", vec(2 if tag == "a" else 3)))
        conv = db.su("insert into public.conversations (business_id) values (%s) returning id", (b,))[0][0]
        db.su("insert into public.messages (business_id, conversation_id, role, content) values (%s, %s, 'customer', %s)", (b, conv, f"private question {tag}"))
        db.su("insert into public.llm_provider_configs (business_id, provider, model) values (%s, 'openai', 'm')", (b,))
        db.su("insert into public.llm_provider_secrets (business_id, encrypted_api_key) values (%s, %s)", (b, b"\x00ciphertext" + tag.encode()))
        db.su("insert into public.usage_records (business_id, kind) values (%s, 'question')", (b,))
    w.src_a = db.su("select id from public.knowledge_sources where business_id = %s", (w.A,))[0][0]
    return w


# ---- the schema itself
def test_every_table_in_the_public_schema_has_row_level_security(db, world):
    off = db.su("select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace where n.nspname = 'public' and c.relkind = 'r' and not c.relrowsecurity")
    assert off == [], f"tables without row level security: {off}"
    tables = {r[0] for r in db.su("select tablename from pg_tables where schemaname = 'public'")}
    assert {"businesses", "business_members", "knowledge_sources", "faqs", "document_chunks", "embeddings", "conversations", "messages", "feedback", "evaluation_datasets",
            "evaluation_questions", "evaluation_results", "llm_provider_configs", "llm_provider_secrets", "usage_records", "audit_logs", "knowledge_versions", "profiles"} <= tables
    assert db.su("select count(*) from storage.buckets where id = 'knowledge-files' and not public")[0][0] == 1  # the bucket is private


def test_every_table_that_belongs_to_a_business_carries_its_id(db, world):
    missing = db.su("""select t.tablename from pg_tables t where t.schemaname = 'public' and t.tablename not in ('profiles', 'businesses', 'deletion_receipts')
                       and not exists (select 1 from information_schema.columns c where c.table_schema = 'public' and c.table_name = t.tablename and c.column_name = 'business_id')""")
    assert missing == [], f"no business_id on: {missing}"


# ---- isolation between businesses
@pytest.mark.parametrize("table", ["businesses", "business_members", "business_settings", "knowledge_sources", "faqs", "document_chunks", "embeddings", "conversations", "messages",
                                   "llm_provider_configs", "usage_records"])
def test_a_person_sees_their_own_business_and_never_the_other(db, world, table):
    key = "id" if table == "businesses" else "business_id"
    mine = {r[0] for r in db.run(world.alice, f"select {key} from public.{table}")}
    assert mine and mine == {world.A}, (table, mine)
    theirs = {r[0] for r in db.run(world.bob, f"select {key} from public.{table}")}
    assert theirs == {world.B}
    assert db.run(world.mallory, f"select count(*) from public.{table}")[0][0] == 0  # signed in, a member of nothing


def test_a_signed_out_visitor_gets_nothing(db, world):
    for table in ("businesses", "knowledge_sources", "faqs", "document_chunks", "messages", "llm_provider_configs"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.run(None, f"select * from public.{table}", role="anon")


def test_writing_into_another_business_is_refused(db, world):
    db.denied(world.alice, "insert into public.knowledge_sources (business_id, name, source_type) values (%s, 'sneaky', 'text')", (world.B,))
    db.denied(world.alice, "insert into public.faqs (business_id, external_id, question, answer) values (%s, 'x', 'q', 'a')", (world.B,))
    assert db.run(world.alice, "update public.faqs set answer = 'hijacked' where business_id = %s returning id", (world.B,)) == []  # not visible, so nothing is changed
    assert db.run(world.alice, "delete from public.knowledge_sources where business_id = %s returning id", (world.B,)) == []
    assert db.su("select answer from public.faqs where business_id = %s", (world.B,))[0][0] == "Answer b."
    db.denied(world.alice, "update public.knowledge_sources set business_id = %s where id = %s", (world.B, world.src_a))  # nor can a row be moved across


def test_a_row_cannot_point_at_a_parent_of_another_business(db, world):
    src_b = db.su("select id from public.knowledge_sources where business_id = %s", (world.B,))[0][0]
    with pytest.raises(psycopg.errors.ForeignKeyViolation):  # even the server cannot attach A's FAQ to B's source
        db.su("insert into public.faqs (business_id, source_id, external_id, question, answer) values (%s, %s, 'cross', 'q', 'a')", (world.A, src_b))
    faq_a = db.su("select id from public.faqs where business_id = %s", (world.A,))[0][0]
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db.su("insert into public.embeddings (business_id, faq_id, view_kind, view_text, model, embedding) values (%s, %s, 'q', 'x', 'm', %s)", (world.B, faq_a, vec(5)))


def test_business_ids_chosen_by_the_client_are_not_trusted(db, world):
    db.denied(world.mallory, "insert into public.business_members (business_id, user_id, role) values (%s, %s, 'owner')", (world.A, world.mallory))
    db.denied(world.mallory, "insert into public.business_members (business_id, user_id, role) values (%s, %s, 'viewer')", (world.A, world.mallory))
    db.denied(world.alice, "insert into public.businesses (name, slug, owner_id) values ('x', 'direct-insert', %s)", (world.alice,))  # only create_business() makes a business
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.run(None, "select * from public.create_business('x', 'anon-made')", role="anon")


# ---- roles inside a business
def test_viewers_read_editors_write_admins_manage_owners_own(db, world):
    assert db.run(world.dave, "select count(*) from public.faqs")[0][0] == 1  # a viewer reads
    db.denied(world.dave, "insert into public.knowledge_sources (business_id, name, source_type) values (%s, 'v', 'text')", (world.A,))
    assert db.run(world.carol, "insert into public.knowledge_sources (business_id, name, source_type) values (%s, 'editor-note', 'text') returning id", (world.A,))  # an editor writes
    db.denied(world.carol, "insert into public.business_members (business_id, user_id, role) values (%s, %s, 'viewer')", (world.A, world.mallory))  # but does not manage people
    db.denied(world.erin, "insert into public.business_members (business_id, user_id, role) values (%s, %s, 'viewer')", (world.A, world.mallory))  # nobody is inserted into a business by someone else (migration 12): an admin invites
    mail = db.su("select email from auth.users where id = %s", (world.mallory,))[0][0]
    token = db.run(world.erin, "select public.create_invitation(%s, %s, 'viewer')", (world.A, mail))[0][0]
    assert db.run(world.mallory, "select public.accept_invitation(%s)", (token,))[0][0] == world.A  # an admin manages people by inviting, and the person accepts
    db.run(world.erin, "delete from public.business_members where business_id = %s and user_id = %s", (world.A, world.mallory))
    db.denied(world.erin, "insert into public.business_members (business_id, user_id, role) values (%s, %s, 'owner')", (world.A, world.mallory))  # but cannot make an owner
    db.denied(world.erin, "update public.business_members set role = 'viewer' where business_id = %s and user_id = %s", (world.A, world.alice))  # nor change one
    db.denied(world.carol, "update public.business_settings set hybrid_alpha = 0.9 where business_id = %s", (world.A,)) if False else None
    assert db.run(world.carol, "update public.business_settings set hybrid_alpha = 0.9 where business_id = %s returning hybrid_alpha", (world.A,)) == []  # settings: admin and owner only


def test_a_business_always_keeps_an_owner(db, world):
    b = db.business(db.user("solo"))
    solo = db.su("select user_id from public.business_members where business_id = %s", (b,))[0][0]
    with pytest.raises(DENIED):
        db.run(solo, "delete from public.business_members where business_id = %s and user_id = %s", (b, solo))
    with pytest.raises(DENIED):
        db.run(solo, "update public.business_members set role = 'admin' where business_id = %s and user_id = %s", (b, solo))
    second = db.user("second")
    db.member(b, second, "owner")  # (made an owner by the server: who may do that is the guard's rule, tested above)
    db.run(solo, "delete from public.business_members where business_id = %s and user_id = %s", (b, solo))  # fine now: another owner remains


def test_the_columns_the_server_owns_cannot_be_changed_by_a_person(db, world):
    db.denied(world.alice, "update public.businesses set owner_id = %s where id = %s", (world.mallory, world.A))
    db.denied(world.alice, "update public.businesses set deleted_at = now() where id = %s", (world.A,))
    db.denied(world.carol, "update public.knowledge_sources set version = 99, checksum = 'x' where id = %s", (world.src_a,))
    db.denied(world.alice, "insert into public.document_chunks (business_id, source_id, ordinal, content, content_hash) values (%s, %s, 5, 'x', 'h')", (world.A, world.src_a))
    db.denied(world.alice, "insert into public.embeddings (business_id, faq_id, view_kind, view_text, model, embedding) select %s, id, 'q', 'x', 'm', %s from public.faqs where business_id = %s limit 1", (world.A, vec(9), world.A))
    db.denied(world.alice, "insert into public.messages (business_id, conversation_id, role, content) select %s, id, 'customer', 'forged' from public.conversations where business_id = %s limit 1", (world.A, world.A))
    assert db.run(world.alice, "update public.businesses set name = 'Alpha Co Ltd' where id = %s returning name", (world.A,))[0][0] == "Alpha Co Ltd"


# ---- the key and the model settings
def test_the_api_key_is_unreachable_from_any_client_role(db, world):
    for who, role in ((world.alice, "authenticated"), (world.erin, "authenticated"), (None, "anon")):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.run(who, "select encrypted_api_key from public.llm_provider_secrets", role=role)
    db.denied(world.alice, "insert into public.llm_provider_secrets (business_id, encrypted_api_key) values (%s, 'x')", (world.A,))
    assert db.run(None, "select count(*) from public.llm_provider_secrets", role="server")[0][0] >= 2  # the server reads it
    can = lambda role: db.su("select has_table_privilege(%s, 'public.llm_provider_secrets', 'SELECT')", (role,))[0][0]  # noqa: E731
    assert can("service_role") and not can("authenticated") and not can("anon")  # and the privileges say the same
    assert "encrypted_api_key" not in {c[0] for c in db.su("select column_name from information_schema.columns where table_name = 'llm_provider_configs'")}


def test_model_settings_are_per_business_and_a_person_cannot_claim_a_key_exists(db, world):
    assert db.run(world.erin, "update public.llm_provider_configs set model = 'gpt-x', enabled = true where business_id = %s returning model", (world.A,))[0][0] == "gpt-x"
    db.denied(world.erin, "update public.llm_provider_configs set has_key = true, key_status = 'valid' where business_id = %s", (world.A,))
    assert db.run(world.carol, "update public.llm_provider_configs set model = 'editor-change' where business_id = %s returning model", (world.A,)) == []  # an editor cannot
    assert db.run(world.dave, "select model from public.llm_provider_configs")[0][0] == "gpt-x"  # everyone in the business can see which model is used...
    assert db.run(world.bob, "select model from public.llm_provider_configs")[0][0] == "m"  # ...and the other business has its own
    audit = db.run(world.alice, "select detail::text from public.audit_logs where action like 'llm_provider_configs.%%'")
    assert audit and all("sk-" not in a[0] and "api_key" not in a[0] for a in audit)  # changes are audited without any secret in them


# ---- retrieval stays inside one business
def test_vector_search_returns_only_the_callers_business(db, world):
    rows = db.run(world.alice, "select view_text, similarity from public.match_embeddings(%s, %s::extensions.vector, 10)", (world.A, vec(0)))
    assert [r[0] for r in rows][0] == "Question a?" and rows[0][1] == pytest.approx(1.0)
    assert all("b" not in r[0].split()[-1] for r in rows) and len(rows) == 2
    assert db.run(world.alice, "select * from public.match_embeddings(%s, %s::extensions.vector, 10)", (world.B, vec(1))) == []  # asking about B's id returns nothing
    assert db.run(world.mallory, "select * from public.match_embeddings(%s, %s::extensions.vector, 10)", (world.A, vec(0))) == []


def test_replacing_a_source_stops_the_old_content_being_searched_and_keeps_no_duplicates(db, world):
    b = db.business(db.user("crawler"))
    src = db.su("insert into public.knowledge_sources (business_id, name, source_type, url) values (%s, 'https://x.example/', 'web_page', 'https://x.example/') returning id", (b,))[0][0]
    old = db.su("insert into public.document_chunks (business_id, source_id, ordinal, content, content_hash) values (%s, %s, 0, 'Open 9 to 5', 'h-old') returning id", (b, src))[0][0]
    with pytest.raises(psycopg.errors.UniqueViolation):  # the same text twice for one business is refused
        db.su("insert into public.document_chunks (business_id, source_id, ordinal, content, content_hash) values (%s, %s, 1, 'Open 9 to 5', 'h-old')", (b, src))
    db.run(None, "select public.supersede_source_content(%s, 2)", (src,), role="server")
    assert db.su("select active from public.document_chunks where id = %s", (old,))[0][0] is False  # the old reading is out of the search...
    db.su("insert into public.document_chunks (business_id, source_id, ordinal, content, content_hash, source_version) values (%s, %s, 0, 'Open 9 to 5', 'h-old', 2)", (b, src))  # ...so unchanged text can come back
    assert db.su("select count(*) from public.document_chunks where source_id = %s and active", (src,))[0][0] == 1
    db.denied(world.alice, "select public.supersede_source_content(%s, 3)", (src,))  # only the server may do this


# ---- evaluation integrity
def dataset(db, owner, b, role, name=None):
    return db.run(owner, "insert into public.evaluation_datasets (business_id, name, role) values (%s, %s, %s) returning id", (b, name or f"{role}-{uuid.uuid4().hex[:6]}", role))[0][0]


def question(db, who, b, ds, text, group):
    return db.run(who, "insert into public.evaluation_questions (business_id, dataset_id, question, group_key, expected_outcome) values (%s, %s, %s, %s, 'faq') returning id", (b, ds, text, group))


def test_evaluation_questions_cannot_be_duplicated_split_across_sets_or_taken_from_the_knowledge(db, world):
    b = db.business(db.user("evaluator"))
    owner = db.su("select user_id from public.business_members where business_id = %s", (b,))[0][0]
    dev, reg = dataset(db, owner, b, "development"), dataset(db, owner, b, "regression")
    assert question(db, owner, b, dev, "What time do you open?", "hours")
    db.denied(owner, "insert into public.evaluation_questions (business_id, dataset_id, question, group_key, expected_outcome) values (%s, %s, 'what time do you OPEN', 'other', 'faq')", (b, dev))  # a duplicate, however it is written
    db.denied(owner, "insert into public.evaluation_questions (business_id, dataset_id, question, group_key, expected_outcome) values (%s, %s, 'What time do you open?', 'x', 'insufficient')", (b, reg))  # in another set
    db.denied(owner, "insert into public.evaluation_questions (business_id, dataset_id, question, group_key, expected_outcome) values (%s, %s, 'When are you open?', 'hours', 'faq')", (b, reg))  # a paraphrase of the same fact in another set
    assert question(db, owner, b, dev, "When are you open?", "hours")  # the same group in the same set is fine
    db.su("insert into public.faqs (business_id, external_id, question, answer, alt_questions) values (%s, 'h', 'Opening hours?', 'Nine to five.', array['Are you open on Sunday?'])", (b,))
    db.denied(owner, "insert into public.evaluation_questions (business_id, dataset_id, question, group_key, expected_outcome) values (%s, %s, 'opening hours', 'g2', 'faq')", (b, dev))  # already an FAQ question
    db.denied(owner, "insert into public.evaluation_questions (business_id, dataset_id, question, group_key, expected_outcome) values (%s, %s, 'Are you open on Sunday', 'g3', 'faq')", (b, dev))  # ...or an alternative of one
    with pytest.raises(psycopg.errors.CheckViolation):  # and the other way round: a test question never becomes knowledge
        db.su("insert into public.faqs (business_id, external_id, question, answer) values (%s, 'leak', 'what time do you open', 'Nine.')", (b,))
    faq_id = db.su("select id from public.faqs where business_id = %s", (b,))[0][0]
    with pytest.raises(psycopg.errors.CheckViolation):
        db.su("insert into public.learned_questions (business_id, faq_id, question) values (%s, %s, 'When are you open?')", (b, faq_id))  # nor does feedback make one


def test_the_locked_set_is_sealed_hidden_and_used_for_the_final_report_only(db, world):
    b = db.business(db.user("sealer"))
    owner = db.su("select user_id from public.business_members where business_id = %s", (b,))[0][0]
    editor = db.user("sealer-editor")
    db.member(b, editor, "editor")
    locked, reg, dev = dataset(db, owner, b, "locked"), dataset(db, owner, b, "regression"), dataset(db, owner, b, "development")
    question(db, owner, b, locked, "Final exam question one?", "final-1")
    db.denied(editor, "insert into public.evaluation_datasets (business_id, name, role) values (%s, 'e', 'development')", (b,))  # editors do not create sets
    assert db.run(editor, "select count(*) from public.evaluation_questions")[0][0] == 0  # an editor cannot read the locked questions
    assert db.run(owner, "select count(*) from public.evaluation_questions")[0][0] == 1
    # before sealing, a locked set cannot yet be reported on; and it is never used for anything else
    for purpose in ("development", "promotion_gate"):
        db.denied(owner, "insert into public.evaluation_results (business_id, dataset_id, purpose, run_by) values (%s, %s, %s, %s)", (b, locked, purpose, owner))
    db.denied(owner, "insert into public.evaluation_results (business_id, dataset_id, purpose, run_by) values (%s, %s, 'final_report', %s)", (b, locked, owner))  # not yet sealed
    db.run(owner, "update public.evaluation_datasets set sealed_at = now() where id = %s", (locked,))
    db.denied(owner, "update public.evaluation_datasets set sealed_at = null where id = %s", (locked,))
    db.denied(owner, "insert into public.evaluation_questions (business_id, dataset_id, question, group_key, expected_outcome) values (%s, %s, 'One more?', 'final-2', 'faq')", (b, locked))
    db.denied(owner, "update public.evaluation_questions set question = 'Changed?' where dataset_id = %s", (locked,))
    db.denied(owner, "delete from public.evaluation_questions where dataset_id = %s", (locked,))
    rid = db.run(owner, "insert into public.evaluation_results (business_id, dataset_id, purpose, run_by, metrics) values (%s, %s, 'final_report', %s, '{}') returning id", (b, locked, owner))[0][0]
    assert db.run(owner, "update public.evaluation_results set metrics = '{\"cherry\": \"picked\"}' where id = %s returning id", (rid,)) == []  # no policy lets anyone edit a result...
    db.denied(owner, "delete from public.evaluation_results where id = %s", (rid,))  # ...or delete one
    with pytest.raises(psycopg.errors.CheckViolation):  # and not even the server can rewrite the record of the locked set
        db.su("update public.evaluation_results set metrics = '{\"cherry\": \"picked\"}' where id = %s", (rid,))
    assert db.su("select metrics::text from public.evaluation_results where id = %s", (rid,))[0][0] == "{}"
    # the other sets keep to their own purposes
    db.denied(owner, "insert into public.evaluation_results (business_id, dataset_id, purpose, run_by) values (%s, %s, 'final_report', %s)", (b, reg, owner))
    db.denied(owner, "insert into public.evaluation_results (business_id, dataset_id, purpose, run_by) values (%s, %s, 'promotion_gate', %s)", (b, dev, owner))
    assert db.run(owner, "insert into public.evaluation_results (business_id, dataset_id, purpose, passed, run_by) values (%s, %s, 'promotion_gate', true, %s) returning id", (b, reg, owner))
    assert db.run(owner, "insert into public.evaluation_results (business_id, dataset_id, purpose, run_by) values (%s, %s, 'development', %s) returning id", (b, dev, owner))


def test_a_new_version_goes_live_only_after_a_passed_gate_and_can_be_rolled_back(db, world):
    b = db.business(db.user("releaser"))
    owner = db.su("select user_id from public.business_members where business_id = %s", (b,))[0][0]
    reg = dataset(db, owner, b, "regression")
    gate = lambda ok: db.run(owner, "insert into public.evaluation_results (business_id, dataset_id, purpose, passed, run_by) values (%s, %s, 'promotion_gate', %s, %s) returning id", (b, reg, ok, owner))[0][0]  # noqa: E731
    v = lambda n, status, rid=None: db.run(owner, "insert into public.knowledge_versions (business_id, version, status, regression_result_id) values (%s, %s, %s, %s) returning id", (b, n, status, rid))  # noqa: E731
    v(1, "candidate")
    db.denied(owner, "update public.knowledge_versions set status = 'active' where business_id = %s and version = 1", (b,))  # no gate at all
    failed = gate(False)
    db.denied(owner, "update public.knowledge_versions set status = 'active', regression_result_id = %s where business_id = %s and version = 1", (failed, b))  # a failed gate
    ok1 = gate(True)
    db.run(owner, "update public.knowledge_versions set status = 'active', regression_result_id = %s where business_id = %s and version = 1", (ok1, b))
    assert db.su("select promoted_at is not null from public.knowledge_versions where business_id = %s and version = 1", (b,))[0][0] is True
    v(2, "candidate")
    ok2 = gate(True)
    db.run(owner, "update public.knowledge_versions set status = 'retired' where business_id = %s and version = 1", (b,))
    db.run(owner, "update public.knowledge_versions set status = 'active', regression_result_id = %s where business_id = %s and version = 2", (ok2, b))
    with pytest.raises(psycopg.errors.UniqueViolation):  # never two live versions
        db.su("update public.knowledge_versions set status = 'active' where business_id = %s and version = 1", (b,))
    db.run(owner, "select public.rollback_to_version(%s, 1)", (b,))
    assert dict(db.su("select version, status from public.knowledge_versions where business_id = %s", (b,))) == {1: "active", 2: "retired"}
    v(3, "candidate")
    db.denied(owner, "select public.rollback_to_version(%s, 3)", (b,))  # a version that was never live cannot be rolled back to
    db.denied(world.mallory, "select public.rollback_to_version(%s, 1)", (b,))


# ---- audit, deletion, storage
def test_the_audit_trail_records_sensitive_changes_and_cannot_be_edited(db, world):
    b = db.business(db.user("auditor"))
    owner = db.su("select user_id from public.business_members where business_id = %s", (b,))[0][0]
    newbie = db.user("newbie")
    mail = db.su("select email from auth.users where id = %s", (newbie,))[0][0]
    token = db.run(owner, "select public.create_invitation(%s, %s, 'viewer')", (b, mail))[0][0]
    db.run(newbie, "select public.accept_invitation(%s)", (token,))
    rows = db.run(owner, "select action, actor_id, detail ->> 'role_after' from public.audit_logs where business_id = %s order by created_at", (b,))
    assert ("business_members.insert", newbie, "viewer") in rows and any(r[0] == "business_invitations.insert" and r[1] == owner for r in rows)  # who invited, and who accepted
    assert db.run(newbie, "select count(*) from public.audit_logs")[0][0] == 0  # a viewer does not read it
    db.denied(owner, "update public.audit_logs set action = 'nothing happened' where business_id = %s", (b,))
    db.denied(owner, "delete from public.audit_logs where business_id = %s", (b,))
    with pytest.raises(psycopg.errors.CheckViolation):
        db.su("delete from public.audit_logs where business_id = %s", (b,))  # not even the database owner can


def test_deleting_a_business_removes_everything_it_owned_and_leaves_a_receipt(db, world):
    owner = db.user("leaver")
    b = db.business(owner)
    src = db.su("insert into public.knowledge_sources (business_id, name, source_type) values (%s, 's', 'text') returning id", (b,))[0][0]
    faq = db.su("insert into public.faqs (business_id, source_id, external_id, question, answer) values (%s, %s, 'f', 'q', 'a') returning id", (b, src))[0][0]
    db.su("insert into public.embeddings (business_id, faq_id, view_kind, view_text, model, embedding) values (%s, %s, 'q', 'q', 'm', %s)", (b, faq, vec(7)))
    db.su("insert into public.llm_provider_secrets (business_id, encrypted_api_key) values (%s, 'k')", (b,))
    ds = dataset(db, owner, b, "locked")
    question(db, owner, b, ds, "A sealed question?", "g")
    db.run(owner, "update public.evaluation_datasets set sealed_at = now() where id = %s", (ds,))
    db.denied(world.alice, "select public.delete_business_data(%s)", (b,))  # another business's owner cannot
    counts = db.run(owner, "select public.delete_business_data(%s)", (b,))[0][0]
    assert counts["faqs"] == 1 and counts["embeddings"] == 1 and counts["evaluation_questions"] == 1
    for table in ("knowledge_sources", "faqs", "embeddings", "llm_provider_secrets", "evaluation_datasets", "evaluation_questions", "audit_logs", "business_members", "business_settings"):
        assert db.su(f"select count(*) from public.{table} where business_id = %s", (b,))[0][0] == 0, table
    receipt = db.su("select business_name, counts from public.deletion_receipts where business_id = %s", (b,))
    assert receipt and "A sealed question" not in str(receipt)  # the receipt says what was removed, and holds none of it


def test_storage_files_are_reachable_only_through_their_business_folder(db, world):
    put = lambda who, path: db.run(who, "insert into storage.objects (bucket_id, name, owner) values ('knowledge-files', %s, %s) returning id", (path, who))  # noqa: E731
    assert put(world.carol, f"{world.A}/{world.src_a}/faq.csv")  # an editor uploads into their own business's folder
    db.denied(world.carol, "insert into storage.objects (bucket_id, name, owner) values ('knowledge-files', %s, %s)", (f"{world.B}/x/faq.csv", world.carol))  # not into another's
    db.denied(world.dave, "insert into storage.objects (bucket_id, name, owner) values ('knowledge-files', %s, %s)", (f"{world.A}/x/viewer.csv", world.dave))  # a viewer cannot upload
    db.denied(world.alice, "insert into storage.objects (bucket_id, name, owner) values ('knowledge-files', %s, %s)", ("not-a-business-id/faq.csv", world.alice))
    db.denied(world.alice, "insert into storage.objects (bucket_id, name, owner) values ('other-bucket', %s, %s)", (f"{world.A}/x.csv", world.alice))
    db.su("insert into storage.objects (bucket_id, name) values ('knowledge-files', %s)", (f"{world.B}/secret.csv",))
    seen = {r[0] for r in db.run(world.alice, "select name from storage.objects")}
    assert f"{world.A}/{world.src_a}/faq.csv" in seen and f"{world.B}/secret.csv" not in seen  # A's files are visible to A's members only
    assert db.run(world.dave, "select count(*) from storage.objects where name like %s", (f"{world.A}/%",))[0][0] >= 1  # a viewer may read
    assert db.run(world.mallory, "select count(*) from storage.objects")[0][0] == 0
    assert db.run(world.carol, "delete from storage.objects where name = %s returning id", (f"{world.A}/{world.src_a}/faq.csv",)) == []  # deleting needs an admin
    assert db.run(world.erin, "delete from storage.objects where name = %s returning id", (f"{world.A}/{world.src_a}/faq.csv",))
    db.denied(world.alice, "update public.knowledge_sources set storage_path = %s where id = %s", (f"{world.B}/steal.csv", world.src_a))  # a source cannot point into another business's folder


def test_a_new_user_gets_a_profile_and_sees_only_their_own_and_their_colleagues(db, world):
    assert db.run(world.alice, "select count(*) from public.profiles where id = %s", (world.alice,))[0][0] == 1
    assert db.run(world.alice, "select count(*) from public.profiles where id = %s", (world.bob,))[0][0] == 0  # Bob is in another business
    assert db.run(world.alice, "select count(*) from public.profiles where id = %s", (world.erin,))[0][0] == 1  # Erin is a colleague
    db.denied(world.alice, "update public.profiles set display_name = 'x' where id = %s", (world.erin,)) if False else None
    assert db.run(world.alice, "update public.profiles set display_name = 'x' where id = %s returning id", (world.erin,)) == []


def test_migration_order_is_documented_by_the_file_names(db, world):
    names = [p.name for p in sorted((SUPABASE / "migrations").glob("*.sql"))]
    assert names == sorted(names) and len(names) == 13 and names[0].startswith("20261101000001_") and names[11].startswith("20261101000012_") and names[-1].startswith("20261102000013_")


# ---- 09 · one read model over every kind of knowledge, and freshness
def test_the_knowledge_view_shows_faq_entries_and_passages_alike_to_members_only(db, world):
    rows = db.run(world.alice, "select unit_kind, source_name, source_type, heading, is_current, superseded_by from public.knowledge_units order by unit_kind")
    assert [(r[0], r[1]) for r in rows] == [("faq", "faq-a.csv"), ("passage", "faq-a.csv")] and all(r[4] is True and r[5] is None for r in rows)
    assert {r[0] for r in db.run(world.bob, "select business_id from public.knowledge_units")} == {world.B}  # the view runs with the caller's rights: no leak across businesses
    assert db.run(world.mallory, "select count(*) from public.knowledge_units")[0][0] == 0
    db.denied(None, "select count(*) from public.knowledge_units", role="anon")


def test_a_source_that_has_expired_or_not_started_or_was_replaced_is_not_current(db, world):
    b = db.business(db.user("fresh-owner"), "fresh-co")

    def add(name, **cols):
        keys = ", ".join(["business_id", "name", "source_type", "status", *cols])
        marks = ", ".join(["%s"] * (4 + len(cols)))
        sid = db.su(f"insert into public.knowledge_sources ({keys}) values ({marks}) returning id", (b, name, "document", "ready", *cols.values()))[0][0]
        db.su("insert into public.document_chunks (business_id, source_id, ordinal, content, content_hash) values (%s, %s, 0, %s, %s)", (b, sid, f"text of {name}", f"h-{name}"))
        return sid

    add("terms-2025.md", valid_until="2025-12-31")
    add("terms-2099.md", valid_from="2099-01-01")
    add("evergreen.md")
    add("old-prices.md")
    add("new-prices.md", supersedes="Old-Prices.md, other.md")
    add("next-year-prices.md", valid_from="2099-01-01", supersedes="evergreen.md")  # announced, not in force: it retires nothing yet
    state = {r[0]: (r[1], r[2]) for r in db.su("select source_name, is_current, superseded_by from public.knowledge_units where business_id = %s", (b,))}
    assert state["terms-2025.md"][0] is False and state["terms-2099.md"][0] is False
    assert state["old-prices.md"] == (False, "new-prices.md") and state["new-prices.md"][0] is True
    assert state["evergreen.md"] == (True, None)


def test_dates_must_be_in_order_and_only_editors_may_state_them(db, world):
    db.denied(world.alice, "update public.knowledge_sources set valid_from = '2030-01-01', valid_until = '2029-01-01' where id = %s", (world.src_a,))
    assert db.run(world.carol, "update public.knowledge_sources set valid_until = '2099-01-01' where id = %s returning id", (world.src_a,))  # an editor may state when it ends
    assert db.run(world.dave, "update public.knowledge_sources set valid_until = '2001-01-01' where id = %s returning id", (world.src_a,)) == []  # a viewer may not
    assert str(db.su("select valid_until from public.knowledge_sources where id = %s", (world.src_a,))[0][0]) == "2099-01-01"
    db.run(world.carol, "update public.knowledge_sources set valid_until = null where id = %s", (world.src_a,))
