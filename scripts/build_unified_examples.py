"""Writes the unified-knowledge evaluation business ("Acme Cloud", fictional) and its question sets.

    python scripts/build_unified_examples.py

The business keeps what it knows in every kind of source at once: a FAQ table, Markdown documents, a pricing table, a two-sheet Excel workbook, a list of offices (JSON), a nested
product profile (JSON), two stored web pages, and material that is out of date or disagrees with other material on purpose:

  refund_policy_2025.md   expired (valid_until 2025-12-31)         -> must never be used as the current refund policy
  sla_2024.md             superseded by support_sla.md             -> must never be used as the current service levels
  marketing_overview.md   claims 99.99% uptime; the SLA says 99.9%  -> a conflict that must be reported
  faq.csv vs billing_terms.md  invoice payable in 30 / 15 days     -> a conflict that must be reported

examples/unified/{dev,regression,locked}.csv hold the questions, split by a hash of the id (dev 35%, regression 15%, locked 50%). The questions were written by one author, together
with the knowledge, before any engine result for them was looked at. The locked file's sha1 is recorded; scripts/unified_benchmark.py reports aggregate numbers only for it.
Columns: id, category, source_types, question, expected (answered | partial | conflict | insufficient | clarification), must_contain (; separated), must_not_contain,
cite (; separates facts that all need a citation, | separates alternatives), forbid_cite, gold (the files that hold the evidence, for retrieval).
"""
from __future__ import annotations

import csv
import hashlib
import json
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "examples" / "unified"
K = OUT / "knowledge"

FILES = {
    "faq.csv": '''id,question,answer,category,alt_questions
faq-001,Do you offer a free trial?,Yes. Every plan has a 14 day free trial and no card is needed to start.,billing,Is there a trial period?
faq-002,How do I contact support?,Email support@acme.example. Pro and Enterprise customers also get a phone line.,support,
faq-003,Can I change my plan later?,Yes. You can upgrade at any time and the change applies immediately. Downgrades apply at the next renewal.,billing,
faq-004,Which payment methods do you accept?,We accept credit cards and bank transfer. Invoices are payable within 30 days.,billing,
faq-005,Where is my data stored?,Customer data is stored in Frankfurt and Mumbai data centres.,security,
faq-006,Do you have a mobile app?,Yes. There are apps for iOS and Android and both are free to download.,product,
faq-007,Can I export my data?,Yes. Export everything as CSV or JSON from Settings at any time.,product,
faq-008,Do you offer discounts for non-profits?,Yes. Registered non-profits get 25% off any paid plan. Email sales@acme.example with your registration number.,billing,
faq-009,What browsers do you support?,The latest two versions of Chrome and Firefox and Safari and Edge.,product,
faq-010,How do I reset my password?,Use the Forgot password link on the sign-in page. The reset link expires after 1 hour.,account,
''',
    "company_profile.md": '''# Acme Cloud company profile

## About Acme
Acme Cloud was founded in 2014 and builds cloud software for customer support teams. The company has 120 employees.

## Leadership
The chief executive officer is Priya Raman. The chief technology officer is Daniel Okafor.

## Offices
The headquarters is in Pune, India. Acme also has an office in Leeds, United Kingdom.
''',
    "billing_terms.md": '''# Billing terms

## Billing cycle
Plans are billed monthly in advance. Annual billing is available on request.

## Cancelling a subscription
You can cancel at any time from the Billing page. When you cancel, the plan stays active until the end of the period you have paid for. After that your data is kept for 30 days and then deleted.

## Refunds
Refunds are available only if requested within 14 days of the first payment. Approved refunds are processed within 7 business days.

## Invoices
Invoices are payable within 15 days of the invoice date. Late invoices may be charged interest.
''',
    "security_privacy.md": '''# Security and privacy

## Encryption
Customer data is encrypted with AES-256 at rest and with TLS 1.2 or higher in transit.

## Compliance
Acme Cloud holds a SOC 2 Type II report, which is available to customers under NDA.

## Incident response
Customers are notified of a confirmed data breach within 72 hours.

## Single sign-on
Single sign-on with SAML is available on the Enterprise plan.
''',
    "support_sla.md": '''---
supersedes: sla_2024.md
effective: 2025-01-01
---
# Support and service levels

## Support hours
Support is available Monday to Friday, 9am to 6pm India time.

## Response times
Priority support answers within 4 hours on business days. Enterprise customers are answered within 1 hour.

## Uptime
Business and Enterprise customers receive a 99.9% monthly uptime commitment. Service credits apply if it is missed.
''',
    "sla_2024.md": '''# Service levels (2024)

## Uptime
Business customers receive a 99.5% monthly uptime commitment.

## Response times
Priority support answers within 24 hours.
''',
    "refund_policy_2025.md": '''---
valid_until: 2025-12-31
---
# Refund policy (2025)

## Refunds
Refunds are available within 30 days of the first payment.
''',
    "marketing_overview.md": '''# Why Acme Cloud

## Reliability
Acme Cloud is built for reliability. We offer 99.99% uptime across all plans.
''',
    "pricing_plans.csv": '''Plan,Monthly price (USD),Seats,Includes
Starter,9,1,Email support
Pro,20,5,API access and priority support
Business,45,25,Advanced reports and a 99.9% uptime commitment
Enterprise,99,100,"SSO, audit logs and a dedicated manager"
''',
    "offices.json": json.dumps([{"city": "Pune", "staff": 80, "address": "12 Baner Road, Pune", "phone": "+91 20 5550 0101"},
                                {"city": "Leeds", "staff": 40, "address": "4 Park Row, Leeds", "phone": "+44 113 555 0102"}], indent=1),
    "product_profile.json": json.dumps({"product": {"name": "Acme Cloud", "version": "4.2", "released": "2026-03-01",
                                                      "modules": [{"name": "Reports", "included_in": "all plans"}, {"name": "Automation", "included_in": "Pro and above"}]},
                                        "limits": {"max_file_size_mb": 100, "audit_log_retention_days": 365}}, indent=1),
    "web/acme-status.html": '''<!doctype html><html><head><meta charset="utf-8"><meta name="source-url" content="https://acme.example/status"><title>Acme Cloud status</title></head><body><h1>Acme Cloud status</h1><h2>Current status</h2><p>All systems are operational.</p><h2>Scheduled maintenance</h2><p>Scheduled maintenance takes place on the first Sunday of every month from 02:00 to 04:00 UTC.</p></body></html>
''',
    "web/acme-careers.html": '''<!doctype html><html><head><meta charset="utf-8"><meta name="source-url" content="https://acme.example/careers"><title>Careers at Acme</title></head><body><h1>Careers at Acme</h1><h2>Open roles</h2><p>Acme is hiring engineers and support specialists in Pune and Leeds.</p><h2>Internships</h2><p>Internships last 6 months and are paid.</p></body></html>
''',
}


def xlsx(sheets: dict[str, list[list[str]]]) -> bytes:
    """A small valid workbook (inline strings) with one sheet per entry."""
    def cell(ref: str, v: str) -> str:
        return f'<c r="{ref}" t="inlineStr"><is><t>{escape(str(v))}</t></is></c>'

    def col(i: int) -> str:
        return "ABCDEFGHIJ"[i]

    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   + "".join(f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(len(sheets))) + "</Types>")
        z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr("xl/workbook.xml", '<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                   + "".join(f'<sheet name="{escape(n)}" sheetId="{i + 1}" r:id="rId{i + 1}"/>' for i, n in enumerate(sheets)) + "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   + "".join(f'<Relationship Id="rId{i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i + 1}.xml"/>' for i in range(len(sheets))) + "</Relationships>")
        for i, rows in enumerate(sheets.values()):
            body = "".join(f'<row r="{r + 1}">' + "".join(cell(f"{col(c)}{r + 1}", v) for c, v in enumerate(row)) + "</row>" for r, row in enumerate(rows))
            z.writestr(f"xl/worksheets/sheet{i + 1}.xml", f'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{body}</sheetData></worksheet>')
    return buf.getvalue()


WORKBOOK = {
    "Integrations": [["Name", "Category", "Available on", "Setup time minutes"], ["Slack", "Messaging", "Pro, Business and Enterprise", "5"], ["Salesforce", "CRM", "Enterprise", "60"], ["Zapier", "Automation", "All plans", "10"]],
    "Limits": [["Resource", "Starter", "Pro", "Business", "Enterprise"], ["API requests per minute", "60", "600", "1200", "6000"], ["Storage GB", "10", "100", "500", "5000"]],
}

# id, category, source types, question, expected, must_contain, must_not_contain, cite, forbid_cite, gold
Q = [
    ("f1", "faq", "faq", "Do you offer a free trial?", "answered", "14 day", "", "faq.csv", "", "faq.csv"),
    ("f2", "faq", "faq", "Can I try the product before paying, without entering a card?", "answered", "no card", "", "faq.csv", "", "faq.csv"),
    ("f3", "faq", "faq", "Which web browsers does the app work in?", "answered", "chrome", "", "faq.csv", "", "faq.csv"),
    ("f4", "faq", "faq", "Is there an Android app?", "answered", "android", "", "faq.csv", "", "faq.csv"),
    ("f5", "faq", "faq", "How can I get my data out of the system?", "answered", "csv", "", "faq.csv", "", "faq.csv"),
    ("f6", "faq", "faq", "How long is the password reset link valid?", "answered", "1 hour", "", "faq.csv", "", "faq.csv"),
    ("f7", "faq", "faq", "Do charities get a discount?", "answered", "25%", "", "faq.csv", "", "faq.csv"),
    ("f8", "faq", "faq", "Where do you keep customer data?", "answered", "frankfurt", "", "faq.csv", "", "faq.csv"),
    ("d1", "document", "md", "When was Acme Cloud founded?", "answered", "2014", "", "company_profile.md", "", "company_profile.md"),
    ("d2", "document", "md", "Who is the chief executive officer?", "answered", "priya raman", "", "company_profile.md", "", "company_profile.md"),
    ("d3", "document", "md", "How is customer data encrypted at rest?", "answered", "aes-256", "", "security_privacy.md", "", "security_privacy.md"),
    ("d4", "document", "md", "How soon are customers told about a data breach?", "answered", "72 hours", "", "security_privacy.md", "", "security_privacy.md"),
    ("d5", "document", "md", "Does Acme Cloud have a SOC 2 report?", "answered", "soc 2", "", "security_privacy.md", "", "security_privacy.md"),
    ("d6", "document", "md", "What are the support hours?", "answered", "monday to friday", "", "support_sla.md", "", "support_sla.md"),
    ("d7", "document", "md", "What happens to my data after I cancel?", "answered", "30 days", "", "billing_terms.md", "", "billing_terms.md"),
    ("d8", "document", "md", "How long does it take to process an approved refund?", "answered", "7 business days", "", "billing_terms.md", "", "billing_terms.md"),
    ("d9", "document", "md", "Is the company headquartered in Pune?", "answered", "pune", "", "company_profile.md", "", "company_profile.md"),
    ("d10", "document", "md", "How many employees does Acme Cloud have?", "answered", "120", "", "company_profile.md", "", "company_profile.md"),
    ("w1", "website", "html", "When does scheduled maintenance happen?", "answered", "first sunday", "", "acme.example/status", "", "acme.example/status"),
    ("w2", "website", "html", "What time is the maintenance window?", "answered", "02:00", "", "acme.example/status", "", "acme.example/status"),
    ("w3", "website", "html", "Is Acme hiring in Leeds?", "answered", "leeds", "", "acme.example/careers", "", "acme.example/careers"),
    ("w4", "website", "html", "How long do internships last?", "answered", "6 months", "", "acme.example/careers", "", "acme.example/careers"),
    ("c1", "csv", "csv", "How much does the Pro plan cost per month?", "answered", "20 usd", "", "pricing_plans.csv", "", "pricing_plans.csv"),
    ("c2", "csv", "csv", "How many seats come with the Business plan?", "answered", "25", "", "pricing_plans.csv", "", "pricing_plans.csv"),
    ("c3", "csv", "csv;md", "Which plan includes SSO?", "answered", "enterprise", "", "pricing_plans.csv|security_privacy.md", "", "pricing_plans.csv"),
    ("c4", "csv", "csv", "What is included in the Starter plan?", "answered", "email support", "", "pricing_plans.csv", "", "pricing_plans.csv"),
    ("c5", "csv", "csv", "What is the cheapest plan?", "answered", "starter", "", "pricing_plans.csv", "", "pricing_plans.csv"),
    ("x1", "excel", "xlsx", "Is there a Slack integration?", "answered", "slack", "", "integrations.xlsx", "", "integrations.xlsx"),
    ("x2", "excel", "xlsx", "How long does the Salesforce integration take to set up?", "answered", "60", "", "integrations.xlsx", "", "integrations.xlsx"),
    ("x3", "excel", "xlsx", "What is the API rate limit on the Pro plan?", "answered", "600", "", "integrations.xlsx", "", "integrations.xlsx"),
    ("x4", "excel", "xlsx", "Which plans can use Zapier?", "answered", "all plans", "", "integrations.xlsx", "", "integrations.xlsx"),
    ("x5", "excel", "xlsx", "How many API requests per minute does the Starter plan allow?", "answered", "60", "", "integrations.xlsx", "", "integrations.xlsx"),
    ("j1", "json", "json", "How many people work in the Leeds office?", "answered", "40", "", "offices.json", "", "offices.json"),
    ("j2", "json", "json", "What is the address of the Pune office?", "answered", "baner road", "", "offices.json", "", "offices.json"),
    ("j3", "json", "json", "Which version of Acme Cloud is current?", "answered", "4.2", "", "product_profile.json", "", "product_profile.json"),
    ("j4", "json", "json", "Which plans include the Automation module?", "answered", "pro and above", "", "product_profile.json", "", "product_profile.json"),
    ("j5", "json", "json", "What is the maximum file size?", "answered", "100", "", "product_profile.json", "", "product_profile.json"),
    ("m1", "multi_source", "csv;md", "Does your Enterprise plan include SSO, and what happens if I cancel?", "answered", "sso;end of the period", "", "pricing_plans.csv|security_privacy.md;billing_terms.md", "", "pricing_plans.csv;billing_terms.md"),
    ("m2", "multi_source", "faq;csv", "Is there a free trial, and how much is the Pro plan?", "answered", "14 day;20 usd", "", "faq.csv;pricing_plans.csv", "", "faq.csv;pricing_plans.csv"),
    ("m3", "multi_source", "md", "Who is the chief executive officer and where is the headquarters?", "answered", "priya raman;pune", "", "company_profile.md", "", "company_profile.md"),
    ("m4", "multi_source", "faq;xlsx", "Can I export my data, and is there a Zapier integration?", "answered", "csv;zapier", "", "faq.csv;integrations.xlsx", "", "faq.csv;integrations.xlsx"),
    ("m5", "multi_source", "faq;md", "How do I contact support, and what are the support hours?", "answered", "support@acme.example;monday to friday", "", "faq.csv;support_sla.md", "", "faq.csv;support_sla.md"),
    ("m6", "multi_source", "md;json", "How many employees are there, and how many work in Leeds?", "answered", "120;40", "", "company_profile.md;offices.json", "", "company_profile.md;offices.json"),
    ("m7", "multi_source", "csv;xlsx", "How much is the Business plan, and what is its API request limit?", "answered", "45 usd;1200", "", "pricing_plans.csv;integrations.xlsx", "", "pricing_plans.csv;integrations.xlsx"),
    ("m8", "multi_source", "html;md", "Is Acme hiring in Pune, and what are the support hours?", "answered", "pune;monday to friday", "", "acme.example/careers;support_sla.md", "", "acme.example/careers;support_sla.md"),
    ("p1", "multi_part", "md", "What is the refund window and how long do refunds take?", "answered", "14 days;7 business days", "30 days", "billing_terms.md", "refund_policy_2025.md", "billing_terms.md"),
    ("p2", "multi_part", "csv", "What is the price of the Pro plan and how many seats does it have?", "answered", "20 usd;5", "", "pricing_plans.csv", "", "pricing_plans.csv"),
    ("p3", "multi_part", "faq", "Can I export my data, and where is it stored?", "answered", "csv;frankfurt", "", "faq.csv", "", "faq.csv"),
    ("p4", "multi_part", "xlsx", "How long does the Slack integration take to set up, and which plans have it?", "answered", "5 minutes;pro, business and enterprise", "", "integrations.xlsx", "", "integrations.xlsx"),
    ("pa1", "partial", "md", "What are the support hours and do you offer support in Hindi?", "partial", "monday to friday", "", "support_sla.md", "", "support_sla.md"),
    ("pa2", "partial", "csv", "How much is the Pro plan and is there an annual discount?", "partial", "20 usd", "", "pricing_plans.csv", "", "pricing_plans.csv"),
    ("pa3", "partial", "json", "Where is the Pune office and how many parking spaces does it have?", "partial", "baner road", "", "offices.json", "", "offices.json"),
    ("pa4", "partial", "html", "When is scheduled maintenance and which regions are affected?", "partial", "first sunday", "", "acme.example/status", "", "acme.example/status"),
    ("pa5", "partial", "xlsx", "Does Acme have a Slack integration and a Microsoft Teams integration?", "partial", "slack", "", "integrations.xlsx", "", "integrations.xlsx"),
    ("pa6", "partial", "faq", "How long is the free trial and does it include Enterprise features?", "partial", "14 day", "", "faq.csv", "", "faq.csv"),
    ("cf1", "conflict", "faq;md", "How many days do I have to pay an invoice?", "conflict", "", "", "faq.csv;billing_terms.md", "", "faq.csv;billing_terms.md"),
    ("cf2", "conflict", "faq;md", "When are invoices due?", "conflict", "", "", "faq.csv;billing_terms.md", "", "faq.csv;billing_terms.md"),
    ("cf3", "conflict", "md", "What uptime does Acme Cloud guarantee?", "conflict", "", "", "support_sla.md;marketing_overview.md", "", "support_sla.md;marketing_overview.md"),
    ("cf4", "conflict", "md", "What is the uptime commitment?", "conflict", "", "", "support_sla.md;marketing_overview.md", "", "support_sla.md;marketing_overview.md"),
    ("o1", "outdated", "md", "What is the refund window?", "answered", "14 days", "30 days", "billing_terms.md", "refund_policy_2025.md", "billing_terms.md"),
    ("o2", "outdated", "md", "Within how many days of payment can I ask for a refund?", "answered", "14 days", "30 days", "billing_terms.md", "refund_policy_2025.md", "billing_terms.md"),
    ("o3", "outdated", "md", "What uptime did the 2024 service level agreement promise?", "insufficient", "", "99.5", "", "sla_2024.md", ""),
    ("o4", "outdated", "md", "How quickly does priority support respond?", "answered", "4 hours", "24 hours", "support_sla.md", "sla_2024.md", "support_sla.md"),
    ("u1", "unsupported", "", "Do you support single sign-on with Okta?", "insufficient", "", "", "", "", ""),
    ("u2", "unsupported", "", "What is the capital of France?", "insufficient", "", "", "", "", ""),
    ("u3", "unsupported", "", "Do you offer on-premise installation?", "insufficient", "", "", "", "", ""),
    ("u4", "unsupported", "", "Who are your investors?", "insufficient", "", "", "", "", ""),
    ("u5", "unsupported", "", "What is the chief executive's phone number?", "insufficient", "", "", "", "", ""),
    ("u6", "unsupported", "", "Is Acme Cloud HIPAA compliant?", "insufficient", "", "", "", "", ""),
    ("u7", "unsupported", "", "How much is the Platinum plan?", "insufficient", "", "", "", "", ""),
    ("u8", "unsupported", "", "Do you have an office in Singapore?", "insufficient", "", "", "", "", ""),
    ("v1", "vague", "", "What about that?", "clarification", "", "", "", "", ""),
    ("v2", "vague", "", "and it?", "clarification", "", "", "", "", ""),
]

# More development questions (always dev): the hash split leaves too few to work with, and the locked questions must never be what the work is steered by.
EXTRA_DEV = [
    ("e1", "faq", "faq", "Is the mobile app free?", "answered", "free to download", "", "faq.csv", "", "faq.csv"),
    ("e2", "faq", "faq", "What happens if I downgrade my plan?", "answered", "next renewal", "", "faq.csv", "", "faq.csv"),
    ("e3", "document", "md", "Who is the chief technology officer?", "answered", "daniel okafor", "", "company_profile.md", "", "company_profile.md"),
    ("e4", "document", "md", "Which TLS version is used in transit?", "answered", "tls 1.2", "", "security_privacy.md", "", "security_privacy.md"),
    ("e5", "document", "md", "How quickly does Enterprise support answer?", "answered", "1 hour", "", "support_sla.md", "sla_2024.md", "support_sla.md"),
    ("e6", "document", "md", "Is annual billing available?", "answered", "annual billing", "", "billing_terms.md", "", "billing_terms.md"),
    ("e7", "website", "html", "Is the service operational at the moment?", "answered", "operational", "", "acme.example/status", "", "acme.example/status"),
    ("e8", "website", "html", "Are the internships paid?", "answered", "paid", "", "acme.example/careers", "", "acme.example/careers"),
    ("e9", "csv", "csv", "How many seats does the Enterprise plan include?", "answered", "100", "", "pricing_plans.csv", "", "pricing_plans.csv"),
    ("e10", "csv", "csv", "What does the Business plan include?", "answered", "advanced reports", "", "pricing_plans.csv", "", "pricing_plans.csv"),
    ("e11", "excel", "xlsx", "Which category is the Salesforce integration in?", "answered", "crm", "", "integrations.xlsx", "", "integrations.xlsx"),
    ("e12", "excel", "xlsx", "How much storage does the Business plan have?", "answered", "500", "", "integrations.xlsx", "", "integrations.xlsx"),
    ("e13", "json", "json", "What is the phone number of the Leeds office?", "answered", "113", "", "offices.json", "", "offices.json"),
    ("e14", "json", "json", "When was the current version released?", "answered", "2026-03-01", "", "product_profile.json", "", "product_profile.json"),
    ("e15", "json", "json", "How many days are audit logs kept?", "answered", "365", "", "product_profile.json", "", "product_profile.json"),
    ("e16", "multi_source", "csv;xlsx", "What is the Pro plan's API limit and how much does it cost?", "answered", "600;20 usd", "", "pricing_plans.csv;integrations.xlsx", "", "pricing_plans.csv;integrations.xlsx"),
    ("e17", "multi_source", "faq;md", "Where is data stored, and is it encrypted at rest?", "answered", "frankfurt;aes-256", "", "faq.csv;security_privacy.md", "", "faq.csv;security_privacy.md"),
    ("e18", "multi_source", "md;json", "Who leads the company and how many staff work in Pune?", "answered", "priya raman;80", "", "company_profile.md;offices.json", "", "company_profile.md;offices.json"),
    ("e19", "partial", "xlsx", "How long does the Salesforce integration take and does it support custom objects?", "partial", "60", "", "integrations.xlsx", "", "integrations.xlsx"),
    ("e20", "partial", "md", "What is the breach notification time and is there a bug bounty programme?", "partial", "72 hours", "", "security_privacy.md", "", "security_privacy.md"),
    ("e21", "conflict", "faq;md", "What is the payment term for invoices?", "conflict", "", "", "faq.csv;billing_terms.md", "", "faq.csv;billing_terms.md"),
    ("e22", "outdated", "md", "How fast does priority support answer on business days?", "answered", "4 hours", "24 hours", "support_sla.md", "sla_2024.md", "support_sla.md"),
    ("e23", "unsupported", "", "Do you offer a reseller programme?", "insufficient", "", "", "", "", ""),
    ("e24", "unsupported", "", "Which cloud provider hosts the application?", "insufficient", "", "", "", "", ""),
    ("e25", "unsupported", "", "Is there a student discount?", "insufficient", "", "", "", "", ""),
]
HEADER = ["id", "category", "source_types", "question", "expected", "must_contain", "must_not_contain", "cite", "forbid_cite", "gold"]


def split_of(qid: str) -> str:
    h = int(hashlib.sha1(qid.encode()).hexdigest()[:8], 16) % 100
    return "dev" if h < 35 else "regression" if h < 50 else "locked"


def main() -> None:
    for name, text in FILES.items():
        p = K / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    (K / "integrations.xlsx").write_bytes(xlsx(WORKBOOK))
    sets: dict[str, list] = {"dev": [], "regression": [], "locked": []}
    for row in Q:
        sets[split_of(row[0])].append(row)
    sets["dev"] += EXTRA_DEV
    for name, rows in sets.items():
        with (OUT / f"{name}.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(HEADER)
            w.writerows(rows)
    (OUT / "locked.sha1").write_text(hashlib.sha1((OUT / "locked.csv").read_bytes()).hexdigest() + "\n")
    print({k: len(v) for k, v in sets.items()}, "files:", len(FILES) + 1)


if __name__ == "__main__":
    main()
