#!/usr/bin/env python3
"""Offline tests for the temporal layer. No model, no network, no GPU.

The model-facing half of this pipeline is expensive to exercise, so everything
that decides what a card *says* — date validation, supersession, thread
resolution, recency banding — is deterministic and tested here with fixtures.

    python3 ingest/test_temporal.py
"""
import json, pathlib, sys, tempfile
from datetime import date, datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import temporal as T
import build_temporal as B

fails = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


print("date validation")
check("keeps an in-month date", B.valid_date("2026-06-13", "2026-06") == date(2026, 6, 13))
check("keeps a nearby spillover", B.valid_date("2026-07-02", "2026-06") is not None)
check("drops a hallucinated year", B.valid_date("2019-06-13", "2026-06") is None)
check("drops junk", B.valid_date("next Saturday", "2026-06") is None)
check("drops an empty date", B.valid_date(None, "2026-06") is None)

print("\nrecency banding (recomputed at read time, never stored)")
jun = "2026-06"
check("current when the month is now", T.band(jun, date(2026, 6, 15)) == "current")
check("recent a month later", T.band(jun, date(2026, 7, 20)) == "recent")
check("stale four months later", T.band(jun, date(2026, 10, 20)) == "earlier this year")
check("archive a year later", T.band(jun, date(2027, 8, 1)) == "archive")

print("\nsupersession")
events = [
    {"date": "2026-03-02", "kind": "fact", "who": ["Ravi"], "what": "Ravi is training for the Vermont ride", "seen": "2026-03"},
    {"date": "2026-05-09", "kind": "change", "who": ["Ravi"], "what": "Ravi pulled out of the Vermont ride, injured knee", "seen": "2026-05"},
    {"date": "2026-04-01", "kind": "fact", "who": ["Meera"], "what": "Meera booked the Lisbon flights", "seen": "2026-04"},
]
out = {e["what"].split(",")[0]: e for e in B.supersede(events)}
check("older fact is marked superseded", out["Ravi is training for the Vermont ride"]["superseded_by"] == "2026-05-09")
check("the change itself survives", out["Ravi pulled out of the Vermont ride"]["superseded_by"] is None)
check("an unrelated fact is untouched", out["Meera booked the Lisbon flights"]["superseded_by"] is None)

print("\nopen-thread resolution")
per_month = {
    "2026-04": {"events": [], "open": [
        {"what": "book the Lisbon flights", "who": ["Meera"], "due": "2026-05-01", "seen": "2026-04"},
        {"what": "decide on the Vermont ride date", "who": ["Ravi"], "due": None, "seen": "2026-04"},
    ]},
    "2026-05": {"events": [], "open": [
        {"what": "decide the Vermont ride date finally", "who": ["Ravi"], "due": None, "seen": "2026-05"},
    ]},
    "2026-09": {"events": [], "open": [
        {"what": "split the rental car cost", "who": ["Ravi", "Meera"], "due": None, "seen": "2026-09"},
    ]},
}
res = {t["what"]: t for t in B.resolve_open(per_month, date(2026, 9, 21))}
check("a past due date is called out", res["book the Lisbon flights"]["status"] == "date passed")
check("a thread nobody has mentioned in months went quiet", res["decide on the Vermont ride date"]["status"] == "went quiet")
check("fuzzy match carried it forward", res["decide on the Vermont ride date"]["last_seen"] == "2026-05")
check("a thread in the newest month is open", res["split the rental car cost"]["status"] == "open")

print("\novernight session dates")
with tempfile.TemporaryDirectory() as td:
    f = pathlib.Path(td) / "2026-06.md"
    f.write_text("# x — 2026-06\n\n## 2026-06-13 23:40 → 2026-06-14 00:30 — A, B\n"
                 "- **A** (23:40): heading out\n- **B** (00:12): see you there\n")
    got = [(m[0].isoformat(), m[1]) for m in T.read_month(f)]
check("pre-midnight keeps the start date", got[0][0].startswith("2026-06-13"))
check("post-midnight rolls to the next day", got[1][0].startswith("2026-06-14"), str(got))

print("\nmanifest thresholds")
with tempfile.TemporaryDirectory() as td:
    man = T.Manifest(pathlib.Path(td) / "m.json")
    check("unknown key rebuilds", man.stale("k", "h1")[0] is True)
    man.record("k", "h1", count=100)
    check("identical input skips", man.stale("k", "h1", count=100)[0] is False)
    check("a trivial delta skips", man.stale("k", "h2", count=104, min_delta=25)[0] is False)
    check("a material delta rebuilds", man.stale("k", "h2", count=180, min_delta=25)[0] is True)
    check("force always rebuilds", man.stale("k", "h1", count=100, force=True)[0] is True)
    man.save()
    check("manifest round-trips", T.Manifest(pathlib.Path(td) / "m.json").data["k"]["hash"] == "h1")

print("\nmonth extraction is cached, and malformed model output is dropped")
with tempfile.TemporaryDirectory() as td:
    T.CACHE = pathlib.Path(td)
    f = pathlib.Path(td) / "2026-06.md"
    f.write_text("# dfs — 2026-06\n\n## 2026-06-13 10:00 — Ravi\n"
                 "- **Ravi** (10:00): ride saturday?\n- **Ravi** (10:05): ok booked\n")
    calls = []

    def fake(model, prompt, timeout):
        calls.append(prompt)
        return {"events": [
            {"date": "2026-06-13", "who": ["Ravi"], "kind": "plan", "what": "ride on Saturday"},
            {"date": "1999-01-01", "who": ["Ravi"], "kind": "fact", "what": "hallucinated"},
            {"date": "2026-06-14", "who": ["Ravi"], "kind": "nonsense", "what": "odd kind"},
            {"date": "2026-06-15", "who": ["Ravi"], "kind": "fact", "what": ""},
        ], "open": [{"what": "confirm the route", "who": ["Ravi"], "due": "2026-06-20"}]}

    B.ask_json = fake
    d1, _, how1 = B.extract_month("dfs", "2026-06", f, ["Ravi"], "m", 10, 5000, True, False)
    d2, _, how2 = B.extract_month("dfs", "2026-06", f, ["Ravi"], "m", 10, 5000, True, False)
check("first pass calls the model", how1 == "built" and len(calls) == 1)
check("second pass is a cache hit", how2 == "cached" and len(calls) == 1)
check("cache returns the same events", d1 == d2)
check("hallucinated date dropped", not any(e["date"].startswith("1999") for e in d1["events"]))
check("empty body dropped", all(e["what"] for e in d1["events"]))
check("unknown kind coerced to fact",
      [e["kind"] for e in d1["events"] if e["date"] == "2026-06-14"] == ["fact"])
check("open item kept with its due date", d1["open"][0]["due"] == "2026-06-20")
check("prompt states the month bounds", "2026-06-01 to 2026-06-30" in calls[0])

print("\nrender marks stale bands and dates every line")
md = B.render("dfs", "chat", T.stats([(datetime(2026, 6, 13, 10, 0), "Ravi", "x")], date(2026, 9, 21)),
              B.supersede(events), B.resolve_open(per_month, date(2026, 9, 21)), date(2026, 9, 21))
check("states today's date", "2026-09-21" in md)
check("later-wins rule is stated", "the later date wins" in md)
check("stale band is labelled", "may be out of date" in md)
check("supersession is visible", "superseded 2026-05-09" in md)
check("a passed deadline is visible", "date passed" in md)

print()
if fails:
    print(f"{len(fails)} FAILED: {', '.join(fails)}")
    sys.exit(1)
print("all temporal tests passed")
