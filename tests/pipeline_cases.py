"""Labelled cases for the layers of the reasoning pipeline, written for focused tests of each (not taken from any evaluation set). Used by tests/test_pipeline.py and scripts/pipeline_report.py."""

KNOWN = {"free", "team", "growth", "scale", "enterprise", "sso", "whatsapp inbox"}

# question -> what the query plan must hold
QUERY_CASES = [
    ("Which plans support both WhatsApp and SSO?", {"conditions": {"whatsapp", "sso"}, "qualifiers": {"both"}, "intent": "filter"}),
    ("Is the annual plan discounted?", {"claim_types": {"discount"}, "qualifier_on_condition": "annual", "intent": "yes_no"}),
    ("Is there an annual discount?", {"claim_types": {"discount"}, "qualifier_on_condition": "annual"}),
    ("Do you offer annual billing?", {"claim_types": set(), "intent": "yes_no"}),
    ("Which plan is cheapest?", {"intent": "superlative", "comparison": "min", "needs_structured": True}),
    ("Which plan has the highest storage limit?", {"intent": "superlative", "comparison": "max", "needs_structured": True}),
    ("What is the difference between the Team and Growth plans?", {"intent": "difference", "entities": {"team", "growth"}, "needs_structured": True}),
    ("How much would 3 Team plans cost per month?", {"intent": "calculation", "needs_structured": True}),
    ("Can I have 10 agents on the Team plan?", {"intent": "limit_check", "needs_structured": True}),
    ("How many plans are there?", {"intent": "count", "needs_structured": True}),
    ("Which plans have more than 5 agents and include WhatsApp?", {"conditions": {"more than 5 agents", "include whatsapp"}, "intent": "filter"}),
    ("When is support open from 1 July 2026?", {"day": "2026-07-01", "conditions": set()}),
    ("What is the refund policy?", {"intent": "policy", "claim_types": {"refund"}}),
    ("Are students eligible for the Free plan?", {"claim_types": {"eligibility"}}),
    ("Do you guarantee 99.9% uptime?", {"claim_types": {"guarantee"}, "intent": "yes_no"}),
    ("How long is the free trial and does it need a card?", {"parts": 2, "claim_types": {"trial"}}),
]

# (what retrieval returned, the question, expected status) for evidence validation
def hit(i, heading, text, source="plans.csv", kind="passage", **kw):
    return {"entry_id": i, "question": heading, "answer": text, "kind": kind, "source": source, "score": 0.8, "combined_score": 0.8, "rank": 1, **kw}


EVIDENCE_CASES = [
    ("accepted: states the claim", "Is there an annual discount?", hit("a", "Billing", "Annual plans save 20 percent.", "billing.md"), "accepted"),
    ("context: annual billing is not an annual discount", "Is there an annual discount?", hit("b", "Billing", "Annual billing is available on request.", "billing.md"), "context"),
    ("rejected: another business's", "Is there an annual discount?", hit("c", "Billing", "Annual plans save 20 percent.", "billing.md", tenant_id="other"), "rejected"),
    ("rejected: expired", "How long is the refund window?", hit("d", "Refunds", "Refunds are available within 30 days.", "refund_2025.md", inactive="expired on 2025-12-31"), "rejected"),
    ("rejected: not yet valid", "How long is the refund window?", hit("e", "Refunds", "Refunds are available within 14 days.", "refund_2027.md", valid_from="2099-01-01"), "rejected"),
    ("rejected: superseded", "How long is the refund window?", hit("f", "Refunds", "Refunds are available within 30 days.", "old.md", inactive="superseded by new.md"), "rejected"),
    ("rejected: irrelevant", "Is there an annual discount?", hit("g", "Offices", "The headquarters is in Leeds.", "company.md"), "rejected"),
    ("accepted: a plain statement of the fact", "How long is the refund window?", hit("h", "Refunds", "Customers can request a full refund within 30 days of purchase.", "policies.md"), "accepted"),
    ("context: another plan's statement", "How quickly does support reply on the Growth plan?", hit("i", "Response times", "Team plan: email replies within 24 hours.", "support.md"), "context"),
    ("context: a guarantee is not an aim", "Do you guarantee 99.9% uptime?", hit("j", "Uptime", "We aim for 99.9% uptime on the Scale plan.", "sla.md"), "context"),
]


class Sim:
    """Closeness in meaning, stood in for: sentences about other things are far, the rest are near."""

    FAR = ("headquarters", "leeds")

    def __call__(self, part, texts):
        return [0.15 if any(w in t.lower() for w in self.FAR) else 0.65 for t in texts]
