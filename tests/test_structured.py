"""Structured reasoning over the rows of tables (runtime/structured.py): comparing, filtering, counting, adding and multiplying the values of rows read from a business's own files, with the
rows they came from named, no value invented and no unit assumed. Standard library only (no model)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "faqengine" / "runtime"))
import structured as S  # noqa: E402
from test_sources import _xlsx  # noqa: E402

from faqengine import sources  # noqa: E402

PLANS = '''Plan,Monthly price (USD),Annual price (USD),Agents,Storage (GB),Channels,Extra agent (USD per agent)
Free,0,0,1,5,Email,
Team,29,290,5,50,"Email, Chat",8
Growth,79,790,20,200,"Email, Chat and WhatsApp",6
Scale,199,1990,Unlimited,1000,"Email, Chat, WhatsApp and Voice",5
Enterprise,Contact us,Contact us,Unlimited,Unlimited,All channels,Contact us
'''
LIMITS = '''Limit,Free,Team,Growth,Scale,Enterprise
API requests per minute,30,120,600,3000,Unlimited
Max file upload (MB),10,25,100,500,2000
'''
FEATURES = [["Feature", "Available on", "Notes"], ["WhatsApp inbox", "Growth, Scale and Enterprise", ""], ["SSO", "Scale and Enterprise", "SAML only"], ["API access", "All plans", ""],
            ["Custom reports", "Growth and above", ""], ["Mobile app", "iOS and Android", ""]]
BUNDLES = "Name,Included in,Setup time (minutes)\nZapier,Starter Kit,10\nSlack,Starter Kit and Pro Bundle,5\n"


@pytest.fixture(scope="module")
def model(tmp_path_factory):
    d = tmp_path_factory.mktemp("kb")
    (d / "plans.csv").write_text(PLANS)
    (d / "limits.csv").write_text(LIMITS)
    (d / "integrations.csv").write_text(BUNDLES)
    _xlsx(d / "features.xlsx", [("Features", FEATURES)])
    entries = []
    for f in sorted(d.iterdir()):
        entries += sources.load_sources([f]).entries
    recs = [{"entry_id": e.id, "question": e.question, "answer": e.answer, "kind": "passage", "source": e.source, "record": e.record} for e in entries if e.record]
    return S.Model(recs)


def say(res):
    lines, order = S.render(res)
    return " ".join(t for t, _ in lines), lines, order


def solve(model, q):
    r = S.solve(model, q)
    assert r is not None, q
    return r


# ---- values: units are kept, never assumed
def test_a_cell_is_read_as_what_it_says_and_nothing_more():
    v = S.Value("20 USD")
    assert (v.kind, v.num, v.dim, v.unit) == ("number", 20.0, "money", "USD")
    assert S.Value("$1,250.50").num == 1250.5 and S.Value("$1,250.50").unit == "USD"
    assert S.Value("500 MB").num == pytest.approx(0.5) and S.Value("500 MB").dim == "data"
    assert S.Value("Unlimited").kind == "unlimited"
    assert S.Value("Contact us").kind == "text" and S.Value("").kind == "missing"
    assert S.Value("5").dim == "bare"  # a number with no unit stays without one
    assert S.Value("20 USD").comparable(S.Value("45 USD")) and not S.Value("20 USD").comparable(S.Value("20 EUR")) and not S.Value("20 USD").comparable(S.Value("20"))
    assert S.unit_in("Storage (GB)") == "GB" and S.unit_in("Extra agent (USD per agent)") == "USD per agent" and S.unit_in("Seats (approx)") == ""


# ---- comparisons
def test_the_cheapest_plan_is_found_among_the_prices_that_exist_and_the_missing_one_is_said(model):
    res = solve(model, "Which plan is the cheapest?")
    text, lines, _ = say(res)
    assert text.startswith("Free has the lowest Monthly price: 0 USD.") and "Enterprise (Contact us)" in text
    assert all(refs for _, refs in lines)  # every sentence names the rows it was worked out from


def test_a_question_that_does_not_say_which_price_gets_every_price_column(model):
    text, _, _ = say(solve(model, "Which plan is the cheapest?"))
    assert "Monthly price" in text and "Annual price" in text  # two columns are about price: the question does not choose, so both are answered
    only, _, _ = say(solve(model, "Which plan has the cheapest annual price?"))
    assert "Annual price" in only and "Monthly price" not in only


def test_unlimited_wins_a_maximum_and_a_minimum_ignores_it_and_a_tie_names_both(model):
    t, _, _ = say(solve(model, "Which plan has the most agents?"))
    assert "Scale and Enterprise have the highest Agents: Unlimited" in t
    t, _, _ = say(solve(model, "Which plan has the lowest API rate limit?"))
    assert t.startswith("Free has the lowest API requests per minute: 30.")


def test_a_table_turned_round_is_read_with_its_columns_as_the_things(model):
    t, _, order = say(solve(model, "Which plan allows the largest file upload?"))
    assert "Enterprise has the highest Max file upload: 2000 MB" in t and len(order) == 1


def test_numbers_in_different_units_are_not_compared(tmp_path):
    (tmp_path / "t.csv").write_text("Plan,Price\nA,10 USD\nB,9 EUR\nC,12 USD\n")
    m = S.Model([{"entry_id": e.id, "question": e.question, "answer": e.answer, "kind": "passage", "source": e.source, "record": e.record} for e in sources.load_sources([tmp_path / "t.csv"]).entries])
    t, _, _ = say(solve(m, "Which plan is the cheapest?"))
    assert "A has the lowest" in t and "B (9 EUR)" in t and "not compared" in t


def test_counts_sums_and_averages_use_only_what_can_be_added(model):
    assert "There are 5 listed" in say(solve(model, "How many plans are there?"))[0]
    t, _, _ = say(solve(model, "What is the average monthly price of the plans?"))
    assert "76.75 USD" in t and "Enterprise (Contact us)" in t
    assert "250 GB" in say(solve(model, "How much storage do the Team and Growth plans have in total?"))[0]


def test_differences_name_both_values_and_the_gap(model):
    t, _, _ = say(solve(model, "What is the difference between the Team and Growth plans?"))
    assert "Monthly price: Team 29 USD; Growth 79 USD (Growth is higher by 50 USD)" in t and "Agents: Team 5; Growth 20" in t
    only, _, _ = say(solve(model, "How much cheaper is the Team plan than the Growth plan?"))
    assert only.startswith("Monthly price: Team 29 USD; Growth 79 USD") and "Storage" not in only


# ---- arithmetic and limits
def test_arithmetic_keeps_the_unit_and_says_what_it_assumed(model):
    t, _, _ = say(solve(model, "How much would 3 Team plans cost per month?"))
    assert "3 × 29 USD = 87 USD" in t and "Reading:" in t
    assert "12 × 6 USD per agent = 72 USD" in say(solve(model, "How much would 12 extra agents cost on the Growth plan?"))[0]
    assert "6 × 29 USD = 174 USD" in say(solve(model, "How much do 6 months of the Team plan cost?"))[0]


def test_nothing_is_calculated_from_a_price_that_is_not_a_number(model):
    res = solve(model, "How much would 5 Enterprise plans cost?")
    assert not res["lines"] and "I cannot calculate this" in res["gaps"][0][0] and "Contact us" in res["gaps"][0][0]


def test_a_limit_is_compared_in_the_unit_the_table_uses(model):
    assert say(solve(model, "Can I upload a 50 MB file on the Team plan?"))[0].startswith("No: Team lists Max file upload: 25 MB")
    assert say(solve(model, "Is a 200 MB upload possible on the Scale plan?"))[0].startswith("Yes")
    t, _, _ = say(solve(model, "Can I have 10 agents on the Team plan?"))
    assert t.startswith("No") and "read as the amount Team includes" in t
    assert say(solve(model, "Does the Scale plan support 1000 agents?"))[0].startswith("Yes: Scale lists Agents as Unlimited")


def test_thresholds_filter_with_strict_and_inclusive_bounds(model):
    t, _, _ = say(solve(model, "Which plans support more than 20 agents?"))
    assert "Scale, Enterprise" in t and "Growth" not in t.split(":")[0]
    t, _, _ = say(solve(model, "Which plans have at least 50 GB of storage?"))
    assert t.startswith("Team, Growth, Scale, Enterprise") and "Free" not in t.split(":")[0]
    t, _, _ = say(solve(model, "Which plans allow uploads of at least 100 MB?"))
    assert t.startswith("Growth, Scale, Enterprise")


# ---- columns named alike: read as what they hold
def test_a_column_that_sounds_like_plans_is_read_as_plans_only_if_it_names_plans(model):
    t, _, _ = say(solve(model, "Which plans have SSO?"))
    assert "Listed with it: Scale, Enterprise" in t
    t, _, _ = say(solve(model, "Which tiers come with SSO?"))
    assert "Scale, Enterprise" in t  # tier is what the table calls a plan
    res = solve(model, "Which plans have the mobile app?")
    assert not res["lines"] and "does not name plans" in res["gaps"][0][0] and "iOS and Android" in res["gaps"][0][0]
    res = solve(model, "Which plans include Slack?")
    assert not res["lines"] and "Starter Kit and Pro Bundle" in res["gaps"][0][0]  # a bundle is not a plan


def test_all_plans_and_and_above_are_read_and_the_second_says_how(model):
    t, _, _ = say(solve(model, "Which plans have API access?"))
    assert "Listed with it: Free, Team, Growth, Scale, Enterprise" in t
    t, _, _ = say(solve(model, "Which plans have custom reports?"))
    assert "Listed with it: Growth, Scale, Enterprise" in t and "in which the table lists the plans" in t


def test_two_ways_of_saying_which_plans_both_count_and_a_disagreement_is_shown(model):
    t, _, _ = say(solve(model, "Which plans include WhatsApp?"))
    assert "Growth, Scale, Enterprise" in t and "give different plans" in t  # the features sheet says Enterprise; the Channels column of the plans table does not
    assert say(solve(model, "How many plans include WhatsApp?"))[0].startswith("3 of them")


def test_a_plan_does_not_have_a_feature_unless_the_table_could_have_listed_it(model):
    assert say(solve(model, "Is SSO available on the Growth plan?"))[0].startswith("No: SSO is listed under 'Available on': Scale and Enterprise, which does not include Growth")
    assert say(solve(model, "Is SSO available on the Scale plan?"))[0].startswith("Yes")
    assert say(solve(model, "Is WhatsApp included in Team?"))[0].startswith("No")
    assert S.solve(model, "Does the Growth plan include a dedicated account manager?") is None  # no table ever lists one: not being listed proves nothing


# ---- what it must not do
def test_questions_that_are_not_about_the_rows_are_left_alone(model):
    for q in ("What is the refund policy?", "How do I contact support?", "Which plan is best for a team of 30 agents?", "When will the Growth plan price change?"):
        assert S.solve(model, q) is None, q


def test_an_empty_cell_is_reported_as_empty_never_filled_in(model):
    res = solve(model, "What is the extra agent price on the Free plan?")
    assert not res["lines"] and "nothing in it" in res["gaps"][0][0]
    assert say(solve(model, "How much does the Enterprise plan cost per month?"))[0].startswith("Enterprise plan, monthly price")  # "Contact us", quoted as it is


def test_every_result_names_the_entries_its_figures_came_from(model):
    for q in ("Which plan is the cheapest?", "What is the difference between the Team and Growth plans?", "How many plans are there?", "Which plans have SSO?"):
        res = solve(model, q)
        _, lines, order = say(res)
        assert order and all(e in res["entries"] for e in order)
        assert all(refs for _, refs in lines if not _.startswith("Reading"))
