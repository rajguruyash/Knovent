"""Writes the FRESH question sets used to compare language models (examples/unified/fresh.csv for Acme, examples/reasoning/fresh.csv for Beacon): 40 questions about the two fictional
businesses, written before any model or engine result for them was seen, and NOT used for any tuning. Not the verify set (examples/reasoning/verify.csv stays untouched and unused).
Covers multi-condition reasoning, structured comparisons and calculations, scope and qualifier mismatches, conflicting evidence, missing information, multi-source questions and questions
whose words resemble another answer's. Columns as in examples/unified."""
import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COLS = ["id", "category", "source_types", "question", "expected", "must_contain", "must_not_contain", "cite", "forbid_cite", "gold"]
ACME = [
    ("fa1", "multi_condition", "csv", "Which plans include API access and have at least 5 seats?", "answered", "Pro", "Starter", "pricing_plans.csv", "", "pricing_plans.csv"),
    ("fa2", "structured", "csv", "What is the total number of seats across the Starter and Pro plans?", "answered", "6", "", "pricing_plans.csv", "", "pricing_plans.csv"),
    ("fa3", "structured", "csv", "How much more does the Enterprise plan cost than the Business plan each month?", "answered", "54", "", "pricing_plans.csv", "", "pricing_plans.csv"),
    ("fa4", "structured", "csv", "Which plan comes with the most seats?", "answered", "Enterprise;100", "", "pricing_plans.csv", "", "pricing_plans.csv"),
    ("fa5", "structured", "xlsx", "How many API requests per minute can the Business plan make?", "answered", "1200", "", "integrations.xlsx", "", "integrations.xlsx"),
    ("fa6", "scope", "md", "Do Starter customers get a 99.9% uptime commitment?", "answered", "Business and Enterprise", "", "support_sla.md", "sla_2024.md", "support_sla.md"),
    ("fa7", "scope", "xlsx", "Is the Salesforce integration available on the Pro plan?", "answered", "Enterprise", "", "integrations.xlsx", "", "integrations.xlsx"),
    ("fa8", "conflict", "md", "What uptime percentage does Acme promise?", "conflict", "", "", "support_sla.md;marketing_overview.md", "", "support_sla.md;marketing_overview.md"),
    ("fa9", "conflict", "faq;md", "Within how many days must an invoice be paid?", "conflict", "", "", "faq.csv;billing_terms.md", "", "faq.csv;billing_terms.md"),
    ("fa10", "missing", "csv", "What would 200 seats cost on the Enterprise plan?", "insufficient", "", "", "", "", "pricing_plans.csv"),
    ("fa11", "missing", "faq", "Does Acme offer a student discount?", "insufficient", "", "25%", "", "", ""),
    ("fa12", "missing", "md", "Who is the chief financial officer?", "insufficient", "", "Priya;Daniel", "", "", ""),
    ("fa13", "multi_source", "md", "Who runs Acme and where is its headquarters?", "answered", "Priya Raman;Pune", "", "company_profile.md", "", "company_profile.md"),
    ("fa14", "multi_source", "xlsx", "How long does the Slack integration take to set up and which plans have it?", "answered", "5 minutes;Pro;Business;Enterprise", "", "integrations.xlsx", "", "integrations.xlsx"),
    ("fa15", "similar_terms", "md", "How long do I have to request a refund?", "answered", "14 days", "30 days", "billing_terms.md", "refund_policy_2025.md", "billing_terms.md"),
    ("fa16", "similar_terms", "md", "How long is my data kept after I cancel?", "answered", "30 days", "365", "billing_terms.md", "", "billing_terms.md"),
    ("fa17", "similar_terms", "json", "For how many days are audit logs kept?", "answered", "365", "30 days", "product_profile.json", "", "product_profile.json"),
    ("fa18", "multi_condition", "html", "Are the internships paid, and how long do they last?", "answered", "paid;6 months", "", "acme.example/careers", "", "acme.example/careers"),
    ("fa19", "multi_condition", "html", "When is the scheduled maintenance, and at what time of day?", "answered", "first Sunday;02:00", "", "acme.example/status", "", "acme.example/status"),
    ("fa20", "multi_condition", "faq", "Do non-profits get a discount, and how do they ask for it?", "answered", "25%;sales@acme.example", "", "faq.csv", "", "faq.csv"),
]
BEACON = [
    ("fb1", "multi_condition", "csv;xlsx", "Which plans have more than 5 agents and include WhatsApp?", "answered", "Growth;Scale;Enterprise", "Team", "plans.csv", "", "plans.csv;features.xlsx"),
    ("fb2", "structured", "csv", "What would 15 extra agents cost on the Scale plan?", "answered", "75 USD", "", "plans.csv", "", "plans.csv"),
    ("fb3", "structured", "csv", "Which plan has the least storage that is still above 100 GB?", "answered", "Growth;200", "", "plans.csv", "", "plans.csv"),
    ("fb4", "structured", "csv", "What is the cheapest plan that includes Chat?", "answered", "Team;29 USD", "", "plans.csv", "", "plans.csv"),
    ("fb5", "structured", "csv", "How many plans have unlimited agents?", "answered", "2;Scale;Enterprise", "", "plans.csv", "", "plans.csv"),
    ("fb6", "scope", "md", "Do Team customers get an uptime commitment?", "answered", "Scale", "", "sla_2026.md", "sla_2025.md", "sla_2026.md"),
    ("fb7", "scope", "xlsx", "Is SSO available on Team?", "answered", "Scale and Enterprise", "", "features.xlsx", "", "features.xlsx"),
    ("fb8", "conflict", "md", "How soon will support answer a ticket on the Team plan?", "conflict", "", "", "marketing.md;support.md", "", "marketing.md;support.md"),
    ("fb9", "conflict", "faq;md", "Are the prices quoted with VAT included?", "conflict", "", "", "faq.csv;policies.md", "", "faq.csv;policies.md"),
    ("fb10", "missing", "faq", "Does Beacon Desk offer on-premises hosting?", "insufficient", "", "", "", "", ""),
    ("fb11", "missing", "md", "Who is the chief financial officer of Beacon Desk?", "insufficient", "", "Dana", "", "", ""),
    ("fb12", "multi_source", "md", "Who is the CEO and how many employees are there?", "answered", "Dana Whitfield;120", "", "company.md", "", "company.md"),
    ("fb13", "conflict", "faq;md", "How many days do I have to ask for a refund, and how long until I am paid?", "conflict", "", "", "faq.csv;policies.md", "", "faq.csv;policies.md"),
    ("fb14", "similar_terms", "md", "How long does onboarding take for Growth customers?", "answered", "60 minutes", "8 hours", "support.md", "", "support.md"),
    ("fb15", "similar_terms", "md", "For how many days is my data kept after I cancel?", "answered", "60 days", "30 days", "policies.md", "", "policies.md"),
    ("fb16", "similar_terms", "csv", "What is the largest file I can upload on the Growth plan?", "answered", "100", "200", "limits.csv", "", "limits.csv"),
    ("fb17", "multi_condition", "md", "Can I cancel at any time, and is my data deleted straight away?", "answered", "any time;60 days", "", "policies.md", "", "policies.md"),
    ("fb18", "date", "md", "Is support open on Saturdays at the moment?", "answered", "Monday to Friday", "Saturday", "support.md", "", "support.md"),
    ("fb19", "missing", "csv", "What is the annual price of the Team plan?", "insufficient", "", "348", "", "", "plans.csv"),
    ("fb20", "structured", "csv", "What is the difference in storage between the Team and Scale plans?", "answered", "950", "", "plans.csv", "", "plans.csv"),
]
for rows, where in ((ACME, "unified"), (BEACON, "reasoning")):
    with (ROOT / "examples" / where / "fresh.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(COLS)
        w.writerows(rows)
print(len(ACME) + len(BEACON), "fresh questions")
