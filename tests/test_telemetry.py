"""Observability (hosted/telemetry.py, hosted/alerts.py): what a log line may hold, correlation ids, counters and windows, and the alert rules, with a clock the test controls."""
import json
import logging

from faqengine.hosted import alerts, telemetry


def test_correlation_ids_are_kept_only_when_well_formed():
    assert telemetry.clean_correlation("abc-123_XYZ.0123") == "abc-123_XYZ.0123"
    for bad in (None, 5, "", "short", "a b c d e f g h", "x" * 65, "new\nline-12345678", "<script>alert(1)</script>"):
        assert telemetry.clean_correlation(bad) is None
    assert len(telemetry.new_correlation_id()) == 32 and telemetry.new_correlation_id() != telemetry.new_correlation_id()


def test_a_log_line_never_holds_what_was_said_or_a_secret():
    fields = telemetry.scrub({"question": "my card is 4111", "answer": "x", "query": "q", "passage_text": "t", "api_key": "sk-abcdefghijklmnop", "authorization": "Bearer abc", "token": "t", "email": "a@b.c",
                              "mode": "platform", "tokens_in": 11, "business": "b-1", "note": "key sk-abcdefghijklmnop leaked", "detail": {"k": "Bearer abcdef"}, "cost": 0.0001})
    assert set(fields) == {"mode", "tokens_in", "business", "note", "detail", "cost"}
    assert "sk-abcdefghijklmnop" not in json.dumps(fields) and "Bearer abc" not in json.dumps(fields) and "[redacted]" in fields["note"]
    assert len(telemetry.scrub({"note": "x" * 1000})["note"]) == 200


def test_every_line_is_one_json_object_with_an_event(caplog):
    log = logging.getLogger("faqengine.test")
    with caplog.at_level("INFO", logger="faqengine.test"):
        telemetry.log_event(log, "respond", correlation_id="abc-123456789", question="never", provider="openrouter", request_ms=12.5)
    line = json.loads(telemetry.JsonFormatter().format(caplog.records[0]))
    assert line["event"] == "respond" and line["correlation_id"] == "abc-123456789" and line["provider"] == "openrouter" and "question" not in line and line["level"] == "INFO"


def test_counters_windows_and_percentiles():
    clock = {"t": 1000.0}
    m = telemetry.Metrics(clock=lambda: clock["t"])
    m.inc("request", tenant="b1")
    clock["t"] += 400
    m.inc("request", 2, tenant="b2")
    m.inc("request_error")
    assert m.counters["request"] == 3 and m.window(300)["request"] == 2 and m.window(1000)["request"] == 3 and m.window(300)["request_error"] == 1
    for v in range(1, 101):
        m.observe("request_ms", v)
    lat = m.latency("request_ms")
    assert lat["n"] == 100 and lat["median_ms"] == 50.5 and abs(lat["p95_ms"] - 95.05) < 0.06 and m.latency("none") == {"n": 0, "median_ms": None, "p95_ms": None}
    snap = m.snapshot()
    assert snap["busiest_businesses"][0] == {"business": "b2", "request": 2} and "request_ms" in snap["latency"]


def test_tenants_tracked_are_bounded():
    m = telemetry.Metrics()
    for i in range(5100):
        m.inc("request", tenant=f"b{i}")
    assert len(m.tenants) <= 5001 and m.counters["request"] == 5100


def fresh(window_events: dict, latency_p95=None, breakers=None):
    clock = {"t": 0.0}
    m = telemetry.Metrics(clock=lambda: clock["t"])
    for k, n in window_events.items():
        m.inc(k, n)
    if latency_p95 is not None:
        for _ in range(30):
            m.observe("request_ms", latency_p95)
    return {a["name"]: a for a in alerts.evaluate(m, breakers or {})}


def test_nothing_fires_on_quiet_or_healthy_traffic():
    assert not [a for a in fresh({}).values() if a["firing"]]
    assert not [a for a in fresh({"request": 100, "request_error": 1, "provider_call": 50, "provider_failure": 2, "model_answer": 40, "claims_rejected": 2}, 3000).values() if a["firing"]]


def test_each_rule_fires_on_its_condition_and_not_below_its_minimum_traffic():
    assert fresh({"request": 100, "request_error": 6})["answers_failing"]["firing"] and not fresh({"request": 10, "request_error": 5})["answers_failing"]["firing"]  # too few requests to say
    assert fresh({"pipeline_error": 3})["pipeline_faults"]["firing"] and not fresh({"pipeline_error": 2})["pipeline_faults"]["firing"]
    f = fresh({"provider_call": 20, "provider_failure": 6})
    assert f["provider_failing"]["firing"] and not f["provider_down"]["firing"]
    assert fresh({"provider_call": 20, "provider_failure": 12})["provider_down"]["firing"]
    assert fresh({}, breakers={"platform/openrouter/m": "open"})["circuit_open"]["firing"] and not fresh({}, breakers={"platform/openrouter/m": "half_open"})["circuit_open"]["firing"]
    assert fresh({}, latency_p95=20000)["latency_p95_high"]["firing"] and not fresh({}, latency_p95=2000)["latency_p95_high"]["firing"]
    assert fresh({"model_answer": 50, "claims_rejected": 15})["claims_rejected_often"]["firing"]
    assert fresh({"consent_refused": 20})["consent_refusals"]["firing"] and fresh({"quota_refused": 1})["quota_refusals"]["firing"] and fresh({"rate_limited": 10})["rate_limit_events"]["firing"]
    assert fresh({"security_denied": 5})["security_events"]["firing"]
    assert all(a["severity"] in ("critical", "warning", "info") and a["says"] for a in fresh({}).values()) and len(alerts.RULES) == 11


def test_old_events_age_out_of_the_alert_window():
    clock = {"t": 0.0}
    m = telemetry.Metrics(clock=lambda: clock["t"])
    m.inc("pipeline_error", 5)
    assert {a["name"] for a in alerts.firing(m)} == {"pipeline_faults"}
    clock["t"] += 301
    assert alerts.firing(m) == []
