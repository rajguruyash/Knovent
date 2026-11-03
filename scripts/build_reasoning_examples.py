"""Writes the evaluation business for structured reasoning and answer reliability ("Beacon Desk", fictional, not the Acme business of examples/unified) and its question sets.

    python scripts/build_reasoning_examples.py

The business keeps what it knows in a plans table (with a price nobody has published, "Contact us", and an empty cell), a features workbook whose "Available on" column names plans in some
rows and platforms in another, an integrations table whose "Included in" column names bundles, not plans, a limits table turned the other way round (plans are its columns), policies with
several sentences and exceptions, a FAQ that disagrees with the policy (and says the opposite about VAT), a support-hours document with dates inside it, expired and not-yet-valid material,
and a few claims that sound like the question and are not (an annual BILLING option is not an annual DISCOUNT; a free PLAN is not a free TRIAL).

The question sets are written for what Phase G adds: comparisons, price calculations, feature comparisons, semantic table headers, missing values, limit and eligibility checks,
incorrect-inference traps, multi-sentence policy answers, paraphrased conflicts, date-sensitive facts, partly answerable questions, and words that mean what the knowledge says in
other words. They were written before any engine result for them was looked at, with the knowledge. Split by a hash of the id: development (the questions debugging uses) and regression
(a gate). The Acme locked benchmark is not touched; it is used once at the end.

Columns as in examples/unified: id, category, source_types, question, expected (answered | partial | conflict | insufficient | clarification), must_contain (; separated),
must_not_contain, cite, forbid_cite, gold.
"""
from __future__ import annotations

import csv
import hashlib
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "examples" / "reasoning"
K = OUT / "knowledge"

spec = importlib.util.spec_from_file_location("bue", ROOT / "scripts" / "build_unified_examples.py")
bue = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bue)

FILES = {
    "plans.csv": '''Plan,Monthly price (USD),Agents,Storage (GB),Channels,Extra agent (USD per agent)
Free,0,1,5,Email,
Team,29,5,50,"Email, Chat",8
Growth,79,20,200,"Email, Chat and WhatsApp",6
Scale,199,Unlimited,1000,"Email, Chat, WhatsApp and Voice",5
Enterprise,Contact us,Unlimited,Unlimited,All channels,Contact us
''',
    "limits.csv": '''Limit,Free,Team,Growth,Scale,Enterprise
API requests per minute,30,120,600,3000,Unlimited
Max file upload (MB),10,25,100,500,2000
Automations,0,5,50,500,Unlimited
''',
    "integrations.csv": '''Name,Included in,Setup time (minutes)
Zapier,Starter Kit,10
Slack,Starter Kit and Pro Bundle,5
Salesforce,Pro Bundle,60
''',
    "faq.csv": '''id,question,answer,category,alt_questions
faq-001,Is there a free plan?,Yes. The Free plan costs nothing and includes 1 agent.,plans,Do you have a free tier?
faq-002,Can I upgrade my plan later?,Yes. You can upgrade at any time and the change applies immediately. Downgrades apply at the next renewal.,plans,
faq-003,Do you offer annual billing?,Annual billing is available on request. Contact sales@beacon.example.,billing,
faq-004,What is the money-back guarantee?,If you are not happy tell us within 14 days of buying and we will give you all your money back.,billing,
faq-005,Do prices include VAT?,Yes. All prices on the pricing page include VAT.,billing,
faq-006,Do you have an education programme?,Our education programme gives schools free access to the Team plan for one year.,billing,
faq-007,How do I contact support?,Email help@beacon.example or use the chat widget in the app.,support,
''',
    "policies.md": '''# Policies

## Refunds
Customers can request a full refund within 30 days of purchase. Monthly plans renew automatically and the renewal charge is not refunded once the new month has started. Refunds are not available for the Scale plan once more than 10 agents have been added. Approved refunds are paid to the original payment method within 5 business days.

## Cancellation
You can cancel from the billing page at any time. Cancelling stops the next renewal and does not delete your data. Your data is kept for 60 days and then deleted unless you export it first.

## Taxes
All prices exclude VAT. VAT is added at checkout for customers in the EU.
''',
    "support.md": '''# Support

## Hours
Until 30 June 2026 support is open Monday to Friday, 9:00 to 17:00. From 1 July 2026 support is open Monday to Saturday, 8:00 to 18:00.

## Response times
Team plan: email replies within 24 hours. Growth plan: email replies within 8 hours. Scale plan: email replies within 1 hour.

## Onboarding
Onboarding calls last 30 minutes on the Team plan and 60 minutes on the Growth plan.
''',
    "marketing.md": '''# Why Beacon Desk

## Speed
Beacon Desk answers every ticket within 1 hour on all plans.
''',
    "company.md": '''# About Beacon Desk

## Leadership
The chief executive officer is Dana Whitfield.

## People
Beacon Desk has 120 employees.

## Offices
The headquarters is in Leeds, United Kingdom.
''',
    "sla_2025.md": '''---
valid_until: 2025-12-31
---
# Service levels (2025)

## Uptime
Scale customers receive a 99.5% monthly uptime commitment.
''',
    "sla_2026.md": '''---
supersedes: sla_2025.md
effective: 2026-01-01
---
# Service levels

## Uptime
Scale customers receive a 99.9% monthly uptime commitment.
''',
    "holiday_2025.md": '''---
valid_until: 2025-12-31
---
# Holiday support

## Holiday hours
During the 2025 holidays support is closed on 25 and 26 December.
''',
    "price_update.md": '''---
effective: 2026-07-01
---
# Price update

## Growth plan
From 1 July 2026 the Growth plan costs 89 USD per month.
''',
}

WORKBOOK = {
    "Features": [
        ["Feature", "Available on", "Notes"],
        ["WhatsApp inbox", "Growth, Scale and Enterprise", "Needs a WhatsApp Business number"],
        ["SSO", "Scale and Enterprise", "SAML only"],
        ["Audit log", "Enterprise", ""],
        ["API access", "All plans", ""],
        ["Custom reports", "Growth and above", ""],
        ["Mobile app", "iOS and Android", "Free to download"],
    ],
}

# id, category, source types, question, expected, must_contain, must_not_contain, cite, forbid_cite, gold
Q = [
    # plain controls: what already worked must keep working
    ("c1", "control", "csv", "How much is the Growth plan per month?", "answered", "79 USD", "89", "plans.csv", "price_update.md", "plans.csv"),
    ("c2", "control", "faq", "Is there a free plan?", "answered", "Free plan", "", "faq.csv", "", "faq.csv"),
    ("c3", "control", "faq", "Can I upgrade my plan later?", "answered", "upgrade", "", "faq.csv", "", "faq.csv"),
    ("c4", "control", "md", "How long do refunds take?", "answered", "5 business days", "", "policies.md", "", "policies.md"),
    # comparisons: sorting, minimum and maximum, counts, totals, averages
    ("k1", "comparison", "csv", "Which plan is the cheapest?", "answered", "Free;0 USD", "", "plans.csv", "", "plans.csv"),
    ("k2", "comparison", "csv", "Which plan is the most expensive?", "answered", "Scale;199 USD;Enterprise", "", "plans.csv", "", "plans.csv"),
    ("k3", "comparison", "csv", "Which plan has the highest storage limit?", "answered", "Enterprise;Unlimited", "", "plans.csv", "", "plans.csv"),
    ("k4", "comparison", "csv", "Which plan has the most agents?", "answered", "Scale;Enterprise;Unlimited", "", "plans.csv", "", "plans.csv"),
    ("k5", "comparison", "csv", "Which plan allows the largest file upload?", "answered", "Enterprise;2000", "", "limits.csv", "", "limits.csv"),
    ("k6", "comparison", "csv", "Which plan has the lowest API rate limit?", "answered", "Free;30", "", "limits.csv", "", "limits.csv"),
    ("k7", "comparison", "csv", "Which plans support more than 20 agents?", "answered", "Scale;Enterprise", "Growth", "plans.csv", "", "plans.csv"),
    ("k8", "comparison", "csv", "Which plans have at least 50 GB of storage?", "answered", "Team;Growth;Scale;Enterprise", "Free", "plans.csv", "", "plans.csv"),
    ("k9", "comparison", "csv", "How many plans are there?", "answered", "5", "", "plans.csv", "", "plans.csv"),
    ("k10", "comparison", "csv;xlsx", "How many plans include WhatsApp?", "answered", "3", "", "features.xlsx", "", "features.xlsx"),
    ("k11", "comparison", "csv", "What is the average monthly price of the plans?", "answered", "76.75;Enterprise", "", "plans.csv", "", "plans.csv"),
    ("k12", "comparison", "csv", "How much cheaper is the Team plan than the Growth plan?", "answered", "50 USD", "", "plans.csv", "", "plans.csv"),
    ("k13", "feature_compare", "csv", "What is the difference between the Team and Growth plans?", "answered", "29 USD;79 USD;5;20", "", "plans.csv", "", "plans.csv"),
    ("k14", "feature_compare", "csv", "Compare the Scale and Enterprise plans.", "answered", "Scale;Enterprise;199 USD", "", "plans.csv", "", "plans.csv"),
    ("k15", "feature_compare", "csv", "Which plan has the lowest number of automations?", "answered", "Free;0", "", "limits.csv", "", "limits.csv"),
    # price calculations
    ("p1", "price_calc", "csv", "How much would 3 Team plans cost per month?", "answered", "87 USD", "", "plans.csv", "", "plans.csv"),
    ("p2", "price_calc", "csv", "How much would 12 extra agents cost on the Growth plan?", "answered", "72 USD", "", "plans.csv", "", "plans.csv"),
    ("p3", "price_calc", "csv", "What would 10 Growth plans cost?", "answered", "790 USD", "", "plans.csv", "", "plans.csv"),
    ("p4", "price_calc", "csv", "How much do 6 months of the Team plan cost?", "answered", "174 USD", "", "plans.csv", "", "plans.csv"),
    ("p5", "missing_value", "csv", "How much would 5 Enterprise plans cost?", "insufficient", "Contact us", "= ", "plans.csv", "", "plans.csv"),
    # feature comparisons and plan membership, read from columns that are named differently
    ("f1", "feature_compare", "xlsx;csv", "Which plans include WhatsApp?", "answered", "Growth;Scale;Enterprise", "Team", "features.xlsx", "", "features.xlsx"),
    ("f2", "semantic_header", "xlsx", "Which plans have SSO?", "answered", "Scale;Enterprise", "Team;Growth", "features.xlsx", "", "features.xlsx"),
    ("f3", "feature_compare", "xlsx;csv", "Does the Team plan include WhatsApp?", "answered", "No", "", "features.xlsx|plans.csv", "", "features.xlsx;plans.csv"),
    ("f4", "semantic_header", "xlsx", "Is SSO available on the Scale plan?", "answered", "Yes;Scale", "", "features.xlsx", "", "features.xlsx"),
    ("f5", "semantic_header", "xlsx", "Is SSO available on the Growth plan?", "answered", "No;Scale and Enterprise", "", "features.xlsx", "", "features.xlsx"),
    ("f6", "semantic_header", "xlsx", "Which plans have API access?", "answered", "All plans", "", "features.xlsx", "", "features.xlsx"),
    ("f7", "semantic_header", "xlsx", "Which plans have an audit log?", "answered", "Enterprise", "Team", "features.xlsx", "", "features.xlsx"),
    ("f8", "semantic_header", "xlsx", "Which plans have custom reports?", "answered", "Growth;Scale;Enterprise", "Team", "features.xlsx", "", "features.xlsx"),
    ("f9", "semantic_header", "xlsx", "Which plans have the mobile app?", "insufficient", "does not name plans", "", "", "", "features.xlsx"),
    ("h1", "semantic_header", "csv", "Which plans include Slack?", "insufficient", "does not name plans", "", "", "", "integrations.csv"),
    ("h2", "semantic_header", "csv", "Which integration is the fastest to set up?", "answered", "Slack;5 minutes", "", "integrations.csv", "", "integrations.csv"),
    ("h3", "semantic_header", "csv", "How long does the Zapier integration take to set up?", "answered", "10 minutes", "", "integrations.csv", "", "integrations.csv"),
    ("h4", "semantic_header", "csv", "Which plans can use Zapier?", "insufficient", "does not name plans", "", "", "", "integrations.csv"),
    # limits and eligibility
    ("l1", "limit_check", "csv", "Can I have 10 agents on the Team plan?", "answered", "No;5", "", "plans.csv", "", "plans.csv"),
    ("l2", "limit_check", "csv", "Can the Growth plan have 15 agents?", "answered", "Yes;20", "", "plans.csv", "", "plans.csv"),
    ("l3", "limit_check", "csv", "Does the Scale plan support 1000 agents?", "answered", "Yes;Unlimited", "", "plans.csv", "", "plans.csv"),
    ("l4", "limit_check", "csv", "Can I upload a 50 MB file on the Team plan?", "answered", "No;25", "", "limits.csv", "", "limits.csv"),
    ("l5", "limit_check", "csv", "Which plans allow uploads of at least 100 MB?", "answered", "Growth;Scale;Enterprise", "Team", "limits.csv", "", "limits.csv"),
    ("l6", "limit_check", "csv", "How many agents does the Enterprise plan support?", "answered", "Unlimited", "", "plans.csv", "", "plans.csv"),
    ("l7", "limit_check", "csv", "How many automations does the Team plan have?", "answered", "5", "", "limits.csv", "", "limits.csv"),
    # missing values
    ("m1", "missing_value", "csv", "How much does the Enterprise plan cost per month?", "answered", "Contact us", "", "plans.csv", "", "plans.csv"),
    ("m2", "missing_value", "csv", "What is the extra agent price on the Free plan?", "insufficient", "nothing in it", "", "", "", "plans.csv"),
    # incorrect-inference traps: a statement that sounds like the question and is about something else
    ("t1", "trap", "faq", "Is there an annual discount?", "insufficient", "", "available on request", "", "", ""),
    ("t2", "trap", "faq", "Do you offer a free trial?", "insufficient", "", "forever;costs nothing", "", "", ""),
    ("t3", "trap", "faq", "Do you give discounts to students?", "insufficient", "", "schools", "", "", ""),
    ("t4", "trap", "csv", "Is there a setup fee?", "insufficient", "", "minutes", "", "", ""),
    ("t5", "trap", "md", "Do you guarantee 100% uptime?", "insufficient", "", "99.9", "", "", ""),
    ("t6", "trap", "csv", "Does the Growth plan include a dedicated account manager?", "insufficient", "", "", "", "", ""),
    ("t7", "trap", "csv", "Is there a limit on the number of customers?", "insufficient", "", "", "", "", ""),
    # multi-sentence policy answers
    ("s1", "multi_sentence", "md", "What is the refund policy?", "answered", "30 days;not refunded;Scale;5 business days", "", "policies.md", "", "policies.md"),
    ("s2", "multi_sentence", "md", "Are there any exceptions to refunds?", "answered", "Scale;10 agents", "", "policies.md", "", "policies.md"),
    ("s3", "multi_sentence", "md", "What happens to my data after I cancel?", "answered", "60 days;export", "", "policies.md", "", "policies.md"),
    ("s4", "multi_sentence", "md", "When is VAT added?", "answered", "EU", "", "policies.md", "", "policies.md"),
    ("s5", "multi_sentence", "md", "Can I cancel at any time, and what happens to my data?", "answered", "any time;60 days", "", "policies.md", "", "policies.md"),
    ("s6", "multi_sentence", "md", "How long is the onboarding call?", "answered", "30 minutes;60 minutes", "", "support.md", "", "support.md"),
    # conflicts that are worded differently, and contexts that only look like conflicts
    ("x1", "conflict", "faq;md", "How long do I have to ask for my money back?", "conflict", "", "", "faq.csv;policies.md", "", "faq.csv;policies.md"),
    ("x2", "conflict", "faq;md", "Do the prices already include VAT?", "conflict", "", "", "faq.csv;policies.md", "", "faq.csv;policies.md"),
    ("x3", "conflict", "md", "How quickly does support reply on the Team plan?", "conflict", "", "", "marketing.md;support.md", "", "marketing.md;support.md"),
    ("x4", "context", "md", "What uptime commitment do Scale customers get?", "answered", "99.9", "99.5", "sla_2026.md", "sla_2025.md", "sla_2026.md"),
    # dates
    ("d1", "date", "md", "When is support open?", "answered", "Monday to Friday;9:00", "Saturday", "support.md", "", "support.md"),
    ("d2", "date", "md", "When is support open from 1 July 2026?", "answered", "Saturday;8:00", "", "support.md", "", "support.md"),
    ("d3", "date", "md", "What are the holiday support hours?", "insufficient", "out of date", "closed on 25", "", "", "holiday_2025.md"),
    ("d4", "date", "md", "What will the Growth plan cost from 1 July 2026?", "conflict", "", "", "plans.csv;price_update.md", "", "plans.csv;price_update.md"),
    # partly answerable
    ("pa1", "partial", "csv;faq", "How much is the Growth plan and is there a free trial?", "partial", "79 USD", "", "plans.csv", "", "plans.csv"),
    ("pa2", "partial", "csv", "How many agents does the Team plan have and what is the setup fee?", "partial", "5", "", "plans.csv", "", "plans.csv"),
    ("pa3", "partial", "csv", "Which plans include SSO and do they come with a dedicated manager?", "partial", "Scale;Enterprise", "", "features.xlsx", "", "features.xlsx"),
    ("pa4", "partial", "md;csv", "Who is the finance director and how many employees does the company have?", "partial", "120", "", "company.md", "", "company.md"),
    # the same facts in other words
    ("v1", "vocabulary", "md", "Who leads the company?", "answered", "Dana Whitfield", "", "company.md", "", "company.md"),
    ("v2", "vocabulary", "md", "How many staff work at Beacon Desk?", "answered", "120", "", "company.md", "", "company.md"),
    ("v3", "vocabulary", "md", "Where is the head office?", "answered", "Leeds", "", "company.md", "", "company.md"),
    ("v4", "vocabulary", "csv", "What does it cost to use the Free plan?", "answered", "0 USD", "", "plans.csv", "", "plans.csv"),
    ("v5", "vocabulary", "md", "Can I stop my subscription whenever I want?", "answered", "cancel;at any time", "", "policies.md", "", "policies.md"),
    ("v6", "vocabulary", "csv", "How big is the file I can upload on the Growth plan?", "answered", "100", "", "limits.csv", "", "limits.csv"),
]


# A third file, written AFTER the development work had made both sets pass: the same knowledge asked in other words and shapes (and a few shapes nothing was built for, kept on purpose). It is run
# once, at the end, and reported as aggregate numbers only; nothing is changed because of it.
QV = [
    ("w1", "comparison", "csv", "Which tier is the least expensive?", "answered", "Free", "", "plans.csv", "", "plans.csv"),
    ("w2", "comparison", "csv", "What is the priciest plan?", "answered", "Scale;199 USD", "", "plans.csv", "", "plans.csv"),
    ("w3", "comparison", "csv", "Which plan comes with the most storage?", "answered", "Enterprise;Unlimited", "", "plans.csv", "", "plans.csv"),
    ("w4", "comparison", "csv", "Which plan has the fewest automations?", "answered", "Free;0", "", "limits.csv", "", "limits.csv"),
    ("w5", "semantic_header", "xlsx", "Does Growth come with SSO?", "answered", "No;Scale and Enterprise", "", "features.xlsx", "", "features.xlsx"),
    ("w6", "semantic_header", "xlsx", "Is the audit log part of the Enterprise plan?", "answered", "Yes;Enterprise", "", "features.xlsx", "", "features.xlsx"),
    ("w7", "feature_compare", "csv", "Which plans give me Voice support?", "answered", "Scale", "Team", "plans.csv", "", "plans.csv"),
    ("w8", "feature_compare", "csv", "What is the difference in price between Team and Scale?", "answered", "29 USD;199 USD;170 USD", "", "plans.csv", "", "plans.csv"),
    ("w9", "price_calc", "csv", "How much would 4 Growth plans cost?", "answered", "316 USD", "", "plans.csv", "", "plans.csv"),
    ("w10", "price_calc", "csv", "What do 20 extra agents cost on Team?", "answered", "160 USD", "", "plans.csv", "", "plans.csv"),
    ("w11", "limit_check", "csv", "Can the Free plan have 3 agents?", "answered", "No;1", "", "plans.csv", "", "plans.csv"),
    ("w12", "limit_check", "csv", "Is a 200 MB upload possible on the Scale plan?", "answered", "Yes;500", "", "limits.csv", "", "limits.csv"),
    ("w13", "comparison", "csv", "Which plans offer at least 100 GB of storage?", "answered", "Growth;Scale;Enterprise", "Team", "plans.csv", "", "plans.csv"),
    ("w14", "comparison", "csv", "What is the total number of automations across the Free and Team plans?", "answered", "5", "", "limits.csv", "", "limits.csv"),
    ("w15", "conflict", "faq;md", "What is the refund window?", "conflict", "", "", "faq.csv;policies.md", "", "faq.csv;policies.md"),
    ("w16", "multi_sentence", "md", "Are refunds possible on the Scale plan?", "answered", "10 agents", "", "policies.md", "", "policies.md"),
    ("w17", "control", "faq", "What does the Free plan include?", "answered", "1 agent", "", "faq.csv", "", "faq.csv"),
    ("w19", "date", "md", "What are the support hours on 15 July 2026?", "answered", "Saturday;8:00", "", "support.md", "", "support.md"),
    ("w20", "date", "md", "Are there holiday hours for 2025?", "insufficient", "out of date", "closed on 25", "", "", "holiday_2025.md"),
    ("w22", "trap", "faq", "Do you offer a student discount?", "insufficient", "", "schools", "", "", ""),
    ("w23", "trap", "faq", "Is there a discount for paying annually?", "insufficient", "", "available on request", "", "", ""),
    ("w24", "trap", "md", "Is there a trial of the Scale plan?", "insufficient", "", "", "", "", ""),
    ("w25", "context", "md", "What uptime does the Scale plan get?", "answered", "99.9", "99.5", "sla_2026.md", "sla_2025.md", "sla_2026.md"),
    ("w26", "trap", "md", "Do Scale customers get an uptime guarantee?", "answered", "99.9", "99.5", "sla_2026.md", "sla_2025.md", "sla_2026.md"),
    ("w27", "conflict", "md", "How quickly are tickets answered on the Growth plan?", "conflict", "", "", "marketing.md;support.md", "", "marketing.md;support.md"),
    ("w28", "vocabulary", "md", "Who is the CEO?", "answered", "Dana Whitfield", "", "company.md", "", "company.md"),
    ("w29", "vocabulary", "md", "How many people work for Beacon Desk?", "answered", "120", "", "company.md", "", "company.md"),
    ("w30", "vocabulary", "md", "Where are you based?", "answered", "Leeds", "", "company.md", "", "company.md"),
    ("w31", "comparison", "csv", "Which integration takes the longest to set up?", "answered", "Salesforce;60 minutes", "", "integrations.csv", "", "integrations.csv"),
    ("w32", "comparison", "csv", "How many integrations are there?", "answered", "3", "", "integrations.csv", "", "integrations.csv"),
    ("w33", "semantic_header", "csv", "Which plans include Salesforce?", "insufficient", "does not name plans", "", "", "", "integrations.csv"),
    ("w34", "missing_value", "csv", "What is the price of the Enterprise plan?", "answered", "Contact us", "", "plans.csv", "", "plans.csv"),
    ("w35", "feature_compare", "csv", "How much are the Free plan and the Scale plan?", "answered", "0 USD;199 USD", "", "plans.csv", "", "plans.csv"),
    ("w36", "feature_compare", "xlsx", "Which plans have WhatsApp and SSO?", "answered", "Scale;Enterprise", "Team", "features.xlsx", "", "features.xlsx"),
    ("w37", "feature_compare", "csv", "Compare the Free and Growth plans on price.", "answered", "0 USD;79 USD", "", "plans.csv", "", "plans.csv"),
    ("w38", "comparison", "csv", "How much storage do the Team and Growth plans have in total?", "answered", "250", "", "plans.csv", "", "plans.csv"),
    ("w39", "semantic_header", "xlsx", "Is the mobile app available on the Growth plan?", "insufficient", "does not name plans", "", "", "", "features.xlsx"),
    ("w40", "conflict", "faq;md", "What is the VAT policy?", "conflict", "", "", "faq.csv;policies.md", "", "faq.csv;policies.md"),
]


def split_of(qid: str) -> str:
    return "regression" if int(hashlib.sha1(qid.encode()).hexdigest(), 16) % 100 < 22 else "dev"


def main() -> None:
    K.mkdir(parents=True, exist_ok=True)
    for name, text in FILES.items():
        (K / name).write_text(text, encoding="utf-8")
    (K / "features.xlsx").write_bytes(bue.xlsx(WORKBOOK))
    cols = ["id", "category", "source_types", "question", "expected", "must_contain", "must_not_contain", "cite", "forbid_cite", "gold"]
    sets: dict[str, list] = {"dev": [], "regression": []}
    for row in Q:
        sets[split_of(row[0])].append(row)
    for name, rows in sets.items():
        with (OUT / f"{name}.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows(rows)
    with (OUT / "verify.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(QV)
    print({k: len(v) for k, v in sets.items()}, "verify:", len(QV), "files:", len(list(K.iterdir())))


if __name__ == "__main__":
    main()
