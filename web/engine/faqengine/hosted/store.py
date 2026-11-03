"""The hosted service's database access (PostgreSQL via psycopg). The service connects with a server-side credential that BYPASSES row level security (it acts for many businesses), so
EVERY query here is bound to a business id, and no business id is ever taken from a request without `role_of` having confirmed that the person acting belongs to it.
tests/test_hosted.py proves the isolation: two businesses, one service, and neither can see the other."""
from __future__ import annotations

import hashlib
import json
import re
import threading
from contextlib import contextmanager

import numpy as np
import psycopg
from psycopg.types.json import Json

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
KIND_ORDER = {"q": 0, "qa": 1, "chunk": 2}


def is_uuid(value: object) -> bool:
    return isinstance(value, str) and bool(UUID.match(value.lower()))


def fresh(valid_from, valid_until, supersedes) -> dict:
    """What a source says about when it is current, in the form the runtime's validity rules read (ISO dates and the names it replaces); only what is stated."""
    out = {"valid_from": valid_from.isoformat() if valid_from else None, "valid_until": valid_until.isoformat() if valid_until else None, "supersedes": supersedes or None}
    return {k: v for k, v in out.items() if v}


def vector_literal(v) -> str:
    return "[" + ",".join(f"{float(x):.7f}" for x in v) + "]"


def text_hash(text: str) -> str:
    return hashlib.sha256(" ".join(text.lower().split()).encode()).hexdigest()


class Store:
    def __init__(self, dsn: str, max_connections: int = 20):
        self.dsn = dsn
        self._slots = threading.BoundedSemaphore(max_connections)  # a burst of requests waits for a free slot instead of opening more connections than the database (or its pooler) allows

    @contextmanager
    def conn(self):
        with self._slots, psycopg.connect(self.dsn, prepare_threshold=None) as c:  # (leaving the block commits, or rolls back on an error, and closes; no server-side prepared statements, which a pooled connection string (pgbouncer, transaction mode) does not allow)
            yield c

    def _one(self, sql, params=()):
        with self.conn() as c:
            return c.execute(sql, params).fetchone()

    def _all(self, sql, params=()):
        with self.conn() as c:
            return c.execute(sql, params).fetchall()

    # ---- who may do what
    def role_of(self, user_id: str, business_id: str) -> str | None:
        row = self._one("select m.role from public.business_members m join public.businesses b on b.id = m.business_id where m.business_id = %s and m.user_id = %s and b.deleted_at is null",
                        (business_id, user_id))
        return row[0] if row else None

    def settings(self, business_id: str) -> dict:
        row = self._one("select retrieval_method, hybrid_alpha, thresholds, privacy from public.business_settings where business_id = %s", (business_id,))
        if not row:
            return {"retrieval_method": "hybrid", "hybrid_alpha": 0.3, "thresholds": {}, "privacy": {"log_questions": True}}
        return {"retrieval_method": row[0], "hybrid_alpha": float(row[1]), "thresholds": row[2] or {}, "privacy": row[3] or {}}

    # ---- the index
    def index_token(self, business_id: str) -> tuple:
        """Changes whenever what is searchable changes (so a cached index is dropped), without loading it."""
        b = (business_id,)
        with self.conn() as c:
            q = lambda sql: c.execute(sql, b).fetchone()[0]  # noqa: E731
            return (q("select count(*) from public.faqs where business_id = %s and active"), q("select coalesce(max(updated_at), 'epoch') from public.faqs where business_id = %s"),
                    q("select count(*) from public.document_chunks where business_id = %s and active"), q("select coalesce(max(created_at), 'epoch') from public.document_chunks where business_id = %s"),
                    q("select count(*) from public.embeddings where business_id = %s"), q("select count(*) from public.learned_questions where business_id = %s and active"),
                    q("select coalesce(max(updated_at), 'epoch') from public.business_settings where business_id = %s"),
                    q("select coalesce(max(updated_at), 'epoch') from public.knowledge_sources where business_id = %s"))  # a source's dates or the name it replaces

    def load_index(self, business_id: str) -> dict:
        """Every active entry of one business with its views and vectors, in the form retrieval.HybridIndex takes."""
        with self.conn() as c:
            faqs = c.execute("select f.id, f.external_id, f.question, f.answer, f.category, coalesce(ks.name, ''), ks.valid_from, ks.valid_until, ks.supersedes, ks.source_type, coalesce(ks.url, ks.storage_path), f.source_version, f.created_at "
                             "from public.faqs f left join public.knowledge_sources ks on ks.id = f.source_id "
                             "where f.business_id = %s and f.active order by f.created_at, f.external_id", (business_id,)).fetchall()
            chunks = c.execute("select c.id, c.ordinal, c.heading, c.content, ks.name, ks.valid_from, ks.valid_until, ks.supersedes, ks.source_type, coalesce(ks.url, ks.storage_path), c.source_version, c.created_at, "
                               "coalesce(nullif(c.location ->> 'source', ''), ks.name), c.location -> 'record' "
                               "from public.document_chunks c join public.knowledge_sources ks on ks.id = c.source_id "
                               "where c.business_id = %s and c.active order by ks.name, c.ordinal", (business_id,)).fetchall()
            rows = c.execute("select e.faq_id, e.chunk_id, e.view_kind, e.view_text, e.embedding::real[] from public.embeddings e "
                             "left join public.faqs f on f.id = e.faq_id left join public.document_chunks k on k.id = e.chunk_id "
                             "where e.business_id = %s and coalesce(f.active, k.active)", (business_id,)).fetchall()
            learned = c.execute("select q.faq_id, q.question from public.learned_questions q join public.faqs f on f.id = q.faq_id and f.active where q.business_id = %s and q.active", (business_id,)).fetchall()
        entries, owner, db_ids = [], {}, {}
        for fid, ext, question, answer, category, source, v_from, v_until, supersedes, stype, location, version, ingested in faqs:
            owner[("f", fid)] = len(entries)
            db_ids[ext] = str(fid)
            entries.append({"id": ext, "question": question, "answer": answer, "kind": "faq", "source": source, "source_name": source, "category": category, **fresh(v_from, v_until, supersedes),
                            "source_type": stype, "location": location, "version": version, "ingested_at": ingested.isoformat() if ingested else None})
        for cid, ordinal, heading, content, source, v_from, v_until, supersedes, stype, location, version, ingested, shown, record in chunks:
            slug = re.sub(r"[^a-z0-9]+", "-", source.lower()).strip("-")[:60] or "document"
            eid = f"{slug}-{ordinal:03d}"
            owner[("c", cid)] = len(entries)
            db_ids[eid] = str(cid)
            # `source` is what a person sees (the page's own address for a web page, the file's name otherwise); `source_name` is the source it was added as, which freshness rules refer to
            entries.append({"id": eid, "question": heading or source, "answer": content, "kind": "passage", "source": shown, "source_name": source, "category": None, **fresh(v_from, v_until, supersedes),
                            "source_type": stype, "location": location, "version": version, "ingested_at": ingested.isoformat() if ingested else None, **({"record": record} if record else {})})
        grouped: dict[int, list] = {}
        for faq_id, chunk_id, kind, text, vec in rows:
            i = owner.get(("f", faq_id)) if faq_id else owner.get(("c", chunk_id))
            if i is not None:
                grouped.setdefault(i, []).append((KIND_ORDER.get(kind, 3), text, kind, vec))
        keep = sorted(grouped)  # an entry nobody has embedded yet is not searchable yet
        remap = {old: new for new, old in enumerate(keep)}
        views, vectors = [], []
        for old in keep:
            for _, text, kind, vec in sorted(grouped[old], key=lambda x: (x[0], x[1])):
                views.append({"text": text, "entry": remap[old], "kind": "qa" if kind == "qa" else "q"})
                vectors.append(vec)
        extra = [(remap[owner[("f", fid)]], q) for fid, q in learned if ("f", fid) in owner and owner[("f", fid)] in remap]
        return {"entries": [entries[i] for i in keep], "views": views, "vectors": np.array(vectors, dtype=np.float32) if vectors else np.zeros((0, 384), np.float32),
                "db_ids": db_ids, "learned": extra}

    # ---- the model configuration and its key
    def llm_config(self, business_id: str) -> dict | None:
        row = self._one("select provider, model, base_url, enabled, capabilities, has_key, key_status from public.llm_provider_configs where business_id = %s", (business_id,))
        if not row:
            return None
        return {"provider": row[0], "model": row[1], "base_url": row[2] or "", "enabled": row[3], "capabilities": row[4] or {}, "has_key": row[5], "key_status": row[6]}

    def llm_secret(self, business_id: str) -> tuple[bytes, int] | None:
        row = self._one("select encrypted_api_key, key_version from public.llm_provider_secrets where business_id = %s", (business_id,))
        return (bytes(row[0]), row[1]) if row else None

    def save_llm(self, business_id: str, user_id: str, fields: dict, encrypted: tuple[bytes, int] | None, key_status: str | None) -> None:
        with self.conn() as c:
            c.execute("insert into public.llm_provider_configs (business_id, provider, model, base_url, enabled, capabilities, updated_by) values (%s, %s, %s, %s, %s, %s, %s) "
                      "on conflict (business_id) do update set provider = excluded.provider, model = excluded.model, base_url = excluded.base_url, enabled = excluded.enabled, "
                      "capabilities = excluded.capabilities, updated_by = excluded.updated_by",
                      (business_id, fields["provider"], fields["model"], fields.get("base_url") or None, fields["enabled"], Json(fields["capabilities"]), user_id))
            if encrypted is not None:
                c.execute("insert into public.llm_provider_secrets (business_id, encrypted_api_key, key_version) values (%s, %s, %s) "
                          "on conflict (business_id) do update set encrypted_api_key = excluded.encrypted_api_key, key_version = excluded.key_version, updated_at = now()", (business_id, encrypted[0], encrypted[1]))
                c.execute("update public.llm_provider_configs set has_key = true, key_status = coalesce(%s, 'unchecked'), key_validated_at = case when %s = 'valid' then now() else key_validated_at end "
                          "where business_id = %s", (key_status, key_status, business_id))
            elif key_status:
                c.execute("update public.llm_provider_configs set key_status = %s, key_validated_at = case when %s = 'valid' then now() else key_validated_at end where business_id = %s",
                          (key_status, key_status, business_id))

    def drop_llm(self, business_id: str, user_id: str) -> None:
        with self.conn() as c:
            c.execute("delete from public.llm_provider_secrets where business_id = %s", (business_id,))
            c.execute("update public.llm_provider_configs set has_key = false, enabled = false, key_status = 'unchecked', updated_by = %s where business_id = %s", (user_id, business_id))

    # ---- agreements to send data to a third-party AI provider (public.ai_inference_consents; the database enforces who may grant or revoke and keeps the history)
    def consent_allows(self, business_id: str, provider: str, version: str) -> bool:
        """The question asked before every external call: an active grant for this provider under this disclosure version. Any error means no: a consent that cannot be read is not given."""
        try:
            row = self._one("select public.ai_consent_allows(%s, %s, %s)", (business_id, provider, version))
        except psycopg.Error:
            return False
        return bool(row and row[0])

    def consents(self, business_id: str) -> list[dict]:
        rows = self._all("select id, provider, status, disclosure_version, data_categories, granted_by, granted_at, revoked_by, revoked_at from public.ai_inference_consents "
                         "where business_id = %s order by granted_at desc, id", (business_id,))
        return [{"id": str(r[0]), "provider": r[1], "status": r[2], "disclosure_version": r[3], "data_categories": list(r[4]), "granted_by": str(r[5]) if r[5] else None,
                 "granted_at": r[6].isoformat(), "revoked_by": str(r[7]) if r[7] else None, "revoked_at": r[8].isoformat() if r[8] else None} for r in rows]

    def _acting(self, conn, user_id: str) -> None:
        """The person on whose behalf this transaction acts, as the database's own audit trail reads it (auth.uid())."""
        conn.execute("select set_config('request.jwt.claim.sub', %s, true), set_config('request.jwt.claims', %s, true)", (user_id, json.dumps({"sub": user_id})))

    def grant_consent(self, business_id: str, provider: str, version: str, sha256: str, granted_by: str, categories: list[str]) -> str:
        with self.conn() as c, c.transaction():
            self._acting(c, granted_by)
            c.execute("update public.ai_inference_consents set status = 'revoked', revoked_by = %s where business_id = %s and provider = %s and status = 'granted' and disclosure_version <> %s",
                      (granted_by, business_id, provider, version))  # an agreement to an older text ends when the new one is agreed to; one active grant per provider
            row = c.execute("select id from public.ai_inference_consents where business_id = %s and provider = %s and status = 'granted'", (business_id, provider)).fetchone()
            if row:
                return str(row[0])  # already agreed to this very text: nothing new is recorded
            return str(c.execute("insert into public.ai_inference_consents (business_id, provider, disclosure_version, disclosure_sha256, data_categories, granted_by) values (%s, %s, %s, %s, %s, %s) returning id",
                                 (business_id, provider, version, sha256, categories, granted_by)).fetchone()[0])

    def revoke_consent(self, business_id: str, provider: str, revoked_by: str) -> int:
        with self.conn() as c, c.transaction():
            self._acting(c, revoked_by)
            return c.execute("update public.ai_inference_consents set status = 'revoked', revoked_by = %s where business_id = %s and provider = %s and status = 'granted'",
                             (revoked_by, business_id, provider)).rowcount

    def record_audit(self, business_id: str, actor_id: str | None, action: str, target_type: str, target_id: str | None, detail: dict) -> None:
        """One line in the append-only audit trail from the service (what the database's own triggers cannot see, such as a shadow comparison). `detail` holds ids, counts and short phrases: never text."""
        with self.conn() as c:
            c.execute("insert into public.audit_logs (business_id, actor_id, action, target_type, target_id, detail) values (%s, %s, %s, %s, %s, %s)",
                      (business_id, actor_id, action, target_type, target_id, Json(detail, dumps=_dumps)))

    # ---- what was asked and answered
    def record_exchange(self, business_id: str, user_id: str, conversation_id: str | None, query: str, result: dict, channel: str = "playground") -> tuple[str, str]:
        answer = result.get("answer") or result.get("partial_answer") or result.get("message") or ""
        with self.conn() as c:
            if conversation_id and not c.execute("select 1 from public.conversations where id = %s and business_id = %s", (conversation_id, business_id)).fetchone():
                conversation_id = None  # an id from another business is not honoured
            if not conversation_id:
                conversation_id = str(c.execute("insert into public.conversations (business_id, channel, created_by) values (%s, %s, %s) returning id", (business_id, channel, user_id)).fetchone()[0])
            c.execute("insert into public.messages (business_id, conversation_id, role, content) values (%s, %s, 'customer', %s)", (business_id, conversation_id, query[:20000]))
            llm = {k: result.get(k) for k in ("llm", "verification", "fallback", "execution")}
            message_id = str(c.execute("insert into public.messages (business_id, conversation_id, role, content, outcome, evidence, retrieval, llm, latency_ms) "
                                       "values (%s, %s, 'assistant', %s, %s, %s, %s, %s, %s) returning id",
                                       (business_id, conversation_id, answer[:20000], result["outcome"], Json({k: result.get(k) for k in ("parts", "unanswered_parts", "evidence_class", "sources", "conflicts", "part_assessments")}
                                                     | {k: result[k] for k in ("query_plan", "evidence", "sufficiency", "claim_verification", "claims") if result.get(k) is not None}, dumps=_dumps),
                                        Json(result.get("retrieval"), dumps=_dumps), Json(llm, dumps=_dumps), int(result.get("latency_ms") or 0))).fetchone()[0])
        return conversation_id, message_id

    def record_usage(self, business_id: str, kind: str, message_id: str | None = None, provider: str | None = None, model: str | None = None, tokens_in: int | None = None,
                     tokens_out: int | None = None, cost: float | None = None, latency_ms: int | None = None, status: str = "ok", mode: str | None = None, correlation_id: str | None = None,
                     generated_by: str | None = None, failure_kind: str | None = None, tier: str | None = None) -> None:
        with self.conn() as c:
            c.execute("insert into public.usage_records (business_id, kind, message_id, provider, model, tokens_in, tokens_out, cost, latency_ms, status, mode, correlation_id, generated_by, failure_kind, tier) "
                      "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                      (business_id, kind, message_id, provider, model, tokens_in, tokens_out, cost, latency_ms, status, mode, correlation_id, generated_by, failure_kind, tier))

    # ---- how a business answers, what it is entitled to, what it has used
    DEFAULT_AI = {"mode": "deterministic", "platform_provider": None, "platform_model": None, "fallback": "deterministic", "fallback_byok": False, "fallback_free": False, "fallback_paid": False, "paid_cap_usd": None}

    def ai_settings(self, business_id: str) -> dict:
        row = self._one("select mode, platform_provider, platform_model, fallback, fallback_byok, fallback_free, fallback_paid, paid_cap_usd from public.ai_inference_settings where business_id = %s", (business_id,))
        if not row:
            return dict(self.DEFAULT_AI)
        return {"mode": row[0], "platform_provider": row[1], "platform_model": row[2], "fallback": row[3], "fallback_byok": row[4], "fallback_free": row[5], "fallback_paid": row[6],
                "paid_cap_usd": float(row[7]) if row[7] is not None else None}

    def save_ai_settings(self, business_id: str, user_id: str, mode: str, platform_provider: str | None, platform_model: str | None, fallback: str,
                         fallback_byok: bool = False, fallback_free: bool = False, fallback_paid: bool = False, paid_cap_usd: float | None = None) -> None:
        with self.conn() as c, c.transaction():
            self._acting(c, user_id)
            c.execute("insert into public.ai_inference_settings (business_id, mode, platform_provider, platform_model, fallback, fallback_byok, fallback_free, fallback_paid, paid_cap_usd, updated_by) "
                      "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                      "on conflict (business_id) do update set mode = excluded.mode, platform_provider = excluded.platform_provider, platform_model = excluded.platform_model, fallback = excluded.fallback, "
                      "fallback_byok = excluded.fallback_byok, fallback_free = excluded.fallback_free, fallback_paid = excluded.fallback_paid, paid_cap_usd = excluded.paid_cap_usd, updated_by = excluded.updated_by",
                      (business_id, mode, platform_provider, platform_model, fallback, fallback_byok, fallback_free, fallback_paid, paid_cap_usd, user_id))

    def plan(self, business_id: str) -> dict | None:
        row = self._one("select plan, inference_requests_per_month, tokens_per_month, cost_cap_usd_per_month, inference_per_minute, trial_ends_at, free_requests_per_month, free_tokens_per_month "
                        "from public.business_plans where business_id = %s", (business_id,))
        if not row:
            return None
        return {"plan": row[0], "requests_per_month": row[1], "tokens_per_month": row[2], "cost_cap_usd_per_month": float(row[3]) if row[3] is not None else None, "per_minute": row[4],
                "trial_ends_at": row[5], "free_requests_per_month": row[6], "free_tokens_per_month": row[7]}

    def usage_period(self, business_id: str, mode: str, since, tier: str | None = None) -> dict:
        """What the business has used from `since` in one mode (and, if given, one tier of it): requests that reached a provider, tokens and the estimated cost (None when no call carried a price)."""
        row = self._one("select count(*), coalesce(sum(tokens_in), 0), coalesce(sum(tokens_out), 0), sum(cost), count(*) filter (where status = 'failed') from public.usage_records "
                        "where business_id = %s and kind = 'llm_call' and mode = %s and created_at >= %s "
                        "and (%s::text is null or tier = %s or (%s = 'paid' and tier is null))", (business_id, mode, since, tier, tier, tier))  # (calls from before tiers existed count as paid)
        return {"requests": row[0], "tokens_in": int(row[1]), "tokens_out": int(row[2]), "cost": float(row[3]) if row[3] is not None else None, "failed": row[4]}

    def inference_calls_since(self, business_id: str, seconds: int) -> int:
        return self._one("select count(*) from public.usage_records where business_id = %s and kind = 'llm_call' and created_at >= now() - make_interval(secs => %s)", (business_id, seconds))[0]

    def usage_by_day(self, business_id: str, since) -> list[dict]:
        rows = self._all("select date_trunc('day', created_at)::date, kind, coalesce(mode, ''), status, count(*), coalesce(sum(tokens_in), 0), coalesce(sum(tokens_out), 0), sum(cost) "
                         "from public.usage_records where business_id = %s and created_at >= %s group by 1, 2, 3, 4 order by 1 desc, 2, 3", (business_id, since))
        return [{"day": r[0].isoformat(), "kind": r[1], "mode": r[2] or None, "status": r[3], "count": r[4], "tokens_in": int(r[5]), "tokens_out": int(r[6]), "cost": float(r[7]) if r[7] is not None else None} for r in rows]

    def history(self, business_id: str, limit: int = 50, before=None) -> list[dict]:
        """The latest answers of a business, newest first, with who generated each (from the stored execution) and the question that was asked when the business keeps questions."""
        rows = self._all("select a.id, a.created_at, a.outcome, a.latency_ms, a.llm -> 'execution', (select c.content from public.messages c where c.conversation_id = a.conversation_id and c.role = 'customer' and c.created_at <= a.created_at order by c.created_at desc limit 1) "
                         "from public.messages a where a.business_id = %s and a.role = 'assistant' and (%s::timestamptz is null or a.created_at < %s) order by a.created_at desc limit %s",
                         (business_id, before, before, max(1, min(limit, 200))))
        return [{"id": str(r[0]), "at": r[1].isoformat(), "outcome": r[2], "latency_ms": r[3], "execution": r[4] or {}, "question": r[5]} for r in rows]

    # ---- companions on the business's own machines (no secret is ever stored)
    def companions(self, business_id: str) -> list[dict]:
        rows = self._all("select id, label, platform, runtime, model, paired_at, last_seen_at, revoked_at, fit from public.local_companions where business_id = %s order by paired_at desc", (business_id,))
        return [{"id": str(r[0]), "label": r[1], "platform": r[2], "runtime": r[3], "model": r[4], "paired_at": r[5].isoformat(), "last_seen_at": r[6].isoformat() if r[6] else None, "revoked": r[7] is not None,
                 "fit": r[8]} for r in rows]

    def register_companion(self, business_id: str, user_id: str, label: str, platform: str | None, runtime: str | None, model: str | None, fit: str | None = None) -> str:
        with self.conn() as c, c.transaction():
            self._acting(c, user_id)
            return str(c.execute("insert into public.local_companions (business_id, label, platform, runtime, model, paired_by, fit) values (%s, %s, %s, %s, %s, %s, %s) returning id",
                                 (business_id, label, platform, runtime, model, user_id, fit)).fetchone()[0])

    def revoke_companion(self, business_id: str, user_id: str, companion_id: str) -> int:
        with self.conn() as c, c.transaction():
            self._acting(c, user_id)
            return c.execute("update public.local_companions set revoked_at = now(), revoked_by = %s where business_id = %s and id = %s and revoked_at is null", (user_id, business_id, companion_id)).rowcount

    def active_companion(self, business_id: str) -> dict | None:
        row = self._one("select id, label, runtime, model, fit from public.local_companions where business_id = %s and revoked_at is null order by paired_at desc limit 1", (business_id,))
        return {"id": str(row[0]), "label": row[1], "runtime": row[2], "model": row[3], "fit": row[4]} if row else None

    # ---- retention
    def purge_expired(self, limit: int = 50_000) -> dict:
        """Deletes the exchanges older than each business's own `retention_days` (365 when it has none or the value is not a number; never less than 1 day or more than 10 years), the feedback that hung
        on them (it cascades), and conversations left empty. Audit rows are append-only and are not touched; usage records carry no text and are kept for billing. Returns counts only."""
        with self.conn() as c:
            n = c.execute("""
                with expired as (
                  select m.id from public.messages m left join public.business_settings s on s.business_id = m.business_id
                  where m.created_at < now() - make_interval(days => case when (s.privacy ->> 'retention_days') ~ '^[0-9]{1,4}$' then greatest(1, least((s.privacy ->> 'retention_days')::int, 3650)) else 365 end)
                  order by m.created_at limit %s)
                delete from public.messages m using expired e where m.id = e.id""", (limit,)).rowcount
            convs = c.execute("delete from public.conversations c where c.created_at < now() - interval '1 day' and not exists (select 1 from public.messages m where m.conversation_id = c.id)").rowcount
        return {"messages_deleted": n, "conversations_deleted": convs}

    # ---- sources
    def source(self, business_id: str, source_id: str) -> dict | None:
        row = self._one("select id, name, source_type, url, storage_path, status, version, checksum, (select count(*) from public.document_chunks c where c.source_id = s.id and c.active) + "
                        "(select count(*) from public.faqs f where f.source_id = s.id and f.active) from public.knowledge_sources s where business_id = %s and id = %s", (business_id, source_id))
        if not row:
            return None
        return {"id": str(row[0]), "name": row[1], "type": row[2], "url": row[3], "storage_path": row[4], "status": row[5], "version": row[6], "checksum": row[7], "active_rows": row[8]}

    def set_source_state(self, business_id: str, source_id: str, status: str, parse_status: str, error: str | None) -> None:
        with self.conn() as c:
            c.execute("update public.knowledge_sources set status = %s, parse_status = %s, error = %s where business_id = %s and id = %s", (status, parse_status, error, business_id, source_id))

    def replace_content(self, business_id: str, source: dict, faqs: list[dict], chunks: list[dict], model: str, checksum: str, parse_status: str, warnings: list[str],
                        freshness: dict | None = None) -> dict:
        """Swap a source's searchable content for a new reading, all or nothing. `faqs` / `chunks` carry their views and vectors. Returns what changed."""
        sid = source["id"]
        with self.conn() as conn, conn.transaction():
            old = {r[0] for r in conn.execute("select content_hash from public.document_chunks where source_id = %s and active", (sid,)).fetchall()}
            old_faq = {r[0] for r in conn.execute("select external_id from public.faqs where source_id = %s and active", (sid,)).fetchall()}
            version = source["version"]
            if old or old_faq:  # an earlier reading exists: it stops being searched in the same transaction in which the new one starts
                version += 1
                conn.execute("select public.supersede_source_content(%s, %s)", (sid, version))
            taken = {r[0] for r in conn.execute("select content_hash from public.document_chunks where business_id = %s and active", (business_id,)).fetchall()}
            skipped = 0
            for f in faqs:
                fid = conn.execute("insert into public.faqs (business_id, source_id, external_id, question, answer, category, alt_questions, source_version) values (%s, %s, %s, %s, %s, %s, %s, %s) returning id",
                                   (business_id, sid, f["external_id"], f["question"], f["answer"], f["category"], f["alt_questions"], version)).fetchone()[0]
                conn.cursor().executemany("insert into public.embeddings (business_id, faq_id, view_kind, view_text, model, embedding) values (%s, %s, %s, %s, %s, %s::extensions.vector)",
                                          [(business_id, fid, kind, text, model, vector_literal(vec)) for kind, text, vec in f["views"]])
            kept = set()
            for ch in chunks:
                if ch["hash"] in taken or ch["hash"] in kept:  # the same text is already searchable (another page, an earlier paragraph): not stored twice
                    skipped += 1
                    continue
                kept.add(ch["hash"])
                cid = conn.execute("insert into public.document_chunks (business_id, source_id, ordinal, heading, content, content_hash, location, injection_flags, source_version) values (%s, %s, %s, %s, %s, %s, %s, %s, %s) returning id",
                                   (business_id, sid, ch["ordinal"], ch["heading"], ch["content"], ch["hash"], Json(ch["location"]), ch["flags"], version)).fetchone()[0]
                conn.cursor().executemany("insert into public.embeddings (business_id, chunk_id, view_kind, view_text, model, embedding) values (%s, %s, %s, %s, %s, %s::extensions.vector)",
                                          [(business_id, cid, kind, text, model, vector_literal(vec)) for kind, text, vec in ch["views"]])
            summary = {"chunks_added": len(kept - old), "chunks_removed": len(old - kept), "chunks_unchanged": len(kept & old), "duplicates_skipped": skipped,
                       "faqs_added": len({f["external_id"] for f in faqs} - old_faq), "faqs_removed": len(old_faq - {f["external_id"] for f in faqs}), "warnings": warnings[:20]}
            conn.execute("insert into public.knowledge_source_versions (business_id, source_id, version, checksum, change_summary) values (%s, %s, %s, %s, %s) on conflict (source_id, version) do nothing",
                         (business_id, sid, version, checksum, Json(summary)))
            fr = freshness or {}  # what the file says about itself replaces what was stated before; a file that says nothing leaves what a person stated in place
            conn.execute("update public.knowledge_sources set status = 'ready', parse_status = %s, error = null, version = %s, checksum = %s, entry_count = %s, chunk_count = %s, "
                         "valid_from = coalesce(%s::date, valid_from), valid_until = coalesce(%s::date, valid_until), supersedes = coalesce(%s, supersedes), "
                         "last_crawled_at = case when source_type in ('web_page','website') then now() else last_crawled_at end where id = %s",
                         (parse_status, version, checksum, len(faqs), len(kept), fr.get("valid_from"), fr.get("valid_until"), fr.get("supersedes"), sid))
            return {"version": version, **summary}


def _dumps(obj) -> str:
    return json.dumps(obj, default=lambda o: float(o) if isinstance(o, (np.floating,)) else int(o) if isinstance(o, np.integer) else list(o) if isinstance(o, (set, tuple)) else str(o))
