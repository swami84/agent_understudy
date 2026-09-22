#!/usr/bin/env python3
"""Build dated timeline cards for every chat and every person.

The profile cards say *what is true*; this says *when it was true*. Without it a
card asserts "planning a ride Saturday June 13th" with equal confidence whether
that Saturday is next week or fifteen months gone, and the assistant has no way to
tell — it cannot even see today's date. Every fact here carries the date it refers
to and the month it was observed in, so the renderer can mark it current, stale, or
superseded instead of stating all of them flatly.

Extraction is per calendar month and cached by content hash, so a closed month is
read by the model exactly once, ever. Steady state is one call per active chat per
month.

    python3 ingest/build_temporal.py --no-llm      # deterministic only, no GPU
    python3 ingest/build_temporal.py               # full, uses local Ollama
    python3 ingest/build_temporal.py --only dfs --force
"""
import argparse, json, os, pathlib, re, sys, urllib.error, urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import temporal as T

OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/") + "/api/chat"

PROMPT = """Extract dated facts from one month of a group chat. Output JSON only.

Chat: {chat}
Month: {month} (this month runs {start} to {end})
Participants: {members}

Messages:
{sample}

Return this exact shape:

{{"events": [{{"date": "YYYY-MM-DD", "who": ["Name"], "kind": "plan|decision|change|commitment|fact", "what": "one short sentence"}}],
 "open": [{{"what": "one short sentence", "who": ["Name"], "due": "YYYY-MM-DD or null"}}]}}

Rules:
- "events": things that happened or were decided, with the date they refer to.
  Resolve relative dates ("Saturday", "next week", "the 15th") against {month}.
- "kind=change" is for a fact about a person that replaced an earlier one — moved,
  changed job, new phone, plans cancelled.
- "open": commitments or questions still unresolved at the end of {month}.
- Use only what the messages support. Never invent a date. If you cannot date an
  event, leave it out.
- At most 12 events and 6 open items. Empty lists are fine."""


def ask_json(model, prompt, timeout):
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "think": False,
        "format": "json",
        "keep_alive": "5m",
        "options": {"temperature": 0.1, "num_ctx": 32768},
    }
    req = urllib.request.Request(
        OLLAMA, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = json.loads(r.read())["message"]["content"].strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    return json.loads(raw)


def valid_date(s, month):
    """Keep dates the model could plausibly mean; drop hallucinated ones.

    A model resolving "Saturday" against June 2026 can legitimately land in the
    next month or the previous one. Landing in 2019 means it invented the date."""
    try:
        d = date.fromisoformat(str(s)[:10])
    except Exception:
        return None
    lo, hi = date.fromisoformat(f"{month}-01"), T.month_end(month)
    return d if -400 <= (d - lo).days and (d - hi).days <= 400 else None


STOP = {"the", "and", "for", "with", "that", "this", "was", "are", "but", "not",
        "has", "had", "you", "his", "her", "their", "they", "will", "would",
        "about", "from", "into", "out", "get", "got", "can", "one", "any", "all"}


def norm(s):
    return {w for w in re.findall(r"[a-z0-9]{3,}", str(s).lower()) if w not in STOP}


def similar(a, b, thresh=0.6):
    """Overlap coefficient, not Jaccard.

    Supersession is asymmetric: "Ravi pulled out of the ride, injured knee"
    replaces "Ravi is training for the ride", but the replacement carries extra
    words, and Jaccard reads that extra detail as evidence the two are unrelated.
    Dividing by the shorter side asks the right question — is the smaller claim
    contained in the larger one."""
    x, y = norm(a), norm(b)
    if not x or not y:
        return False
    shared = x & y
    return len(shared) >= 2 and len(shared) / min(len(x), len(y)) >= thresh


def extract_month(chat, month, path, members, model, timeout, sample_chars, use_llm, force):
    """-> dict with events/open, cached by (content, prompt version, model)."""
    msgs = T.read_month(path)
    if not msgs:
        return None, msgs, "empty"
    body = "\n".join(f"{m[0].strftime('%m-%d %H:%M')} {m[1]}: {m[2]}" for m in msgs)
    key = T.content_hash(chat, month, model, T.EXTRACT_VERSION, body)
    if not force:
        hit = T.cache_get(key)
        if hit is not None:
            return hit, msgs, "cached"
    if not use_llm:
        return None, msgs, "no-llm"

    sample = body[:sample_chars]
    prompt = PROMPT.format(
        chat=chat, month=month, members=", ".join(members),
        start=f"{month}-01", end=T.month_end(month).isoformat(), sample=sample,
    )
    try:
        data = ask_json(model, prompt, timeout)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError) as e:
        print(f"    ! {chat} {month}: {type(e).__name__}: {e}", file=sys.stderr)
        return None, msgs, "failed"

    events, open_items = [], []
    for e in (data.get("events") or [])[:12]:
        d = valid_date(e.get("date"), month)
        what = str(e.get("what", "")).strip()
        if not d or not what:
            continue
        who = [str(w).strip() for w in (e.get("who") or []) if str(w).strip()][:6]
        kind = str(e.get("kind", "fact")).strip().lower()
        events.append({"date": d.isoformat(), "who": who,
                       "kind": kind if kind in
                       {"plan", "decision", "change", "commitment", "fact"} else "fact",
                       "what": what, "seen": month})
    for o in (data.get("open") or [])[:6]:
        what = str(o.get("what", "")).strip()
        if not what:
            continue
        due = valid_date(o.get("due"), month)
        open_items.append({"what": what,
                           "who": [str(w).strip() for w in (o.get("who") or []) if str(w).strip()][:6],
                           "due": due.isoformat() if due else None, "seen": month})
    out = {"events": events, "open": open_items}
    T.cache_put(key, out)
    return out, msgs, "built"


def resolve_open(per_month, today):
    """Walk months oldest to newest and decide what each open item became.

    Deterministic on purpose. Asking a model to reconcile fifteen months of
    overlapping commitments is both expensive and unreliable; matching an item
    forward through later months is neither."""
    tracked = []
    for month in sorted(per_month):
        for item in per_month[month].get("open", []):
            for t in tracked:
                if similar(t["what"], item["what"]):
                    t["last_seen"] = month
                    t["due"] = item["due"] or t["due"]
                    break
            else:
                tracked.append({**item, "first_seen": item["seen"], "last_seen": month})

    months = sorted(per_month)
    latest = months[-1] if months else None
    for t in tracked:
        due = date.fromisoformat(t["due"]) if t["due"] else None
        if t["last_seen"] == latest and (due is None or due >= today):
            t["status"] = "open"
        elif due and due < today:
            t["status"] = "date passed"
        else:
            gap = T.month_gap(t["last_seen"], latest) if latest else 0
            t["status"] = "open" if gap <= 1 else "went quiet"
    return tracked


def supersede(events):
    """A kind=change event overrides earlier events about the same subject."""
    changes = [e for e in events if e["kind"] == "change"]
    out = []
    for e in events:
        killed = next(
            (c for c in changes
             if c["date"] > e["date"] and c is not e and similar(c["what"], e["what"])),
            None,
        )
        out.append({**e, "superseded_by": killed["date"] if killed else None})
    return out


BLOCKS = " \u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"


def spark(per_month, keep=18):
    """Volume per month as a sparkline. Free, exact, and it is what tells the
    assistant a chat is winding down rather than merely old."""
    items = sorted(per_month.items())[-keep:]
    top = max(v for _, v in items) or 1
    # Walk the calendar, not the dict: a month with no messages must render as a
    # gap, or a dormant stretch looks identical to a quiet one.
    y, m = (int(x) for x in items[0][0].split("-"))
    ly, lm = (int(x) for x in items[-1][0].split("-"))
    bars, seen = [], dict(items)
    while (y, m) <= (ly, lm):
        v = seen.get(f"{y:04d}-{m:02d}", 0)
        bars.append("\u00b7" if not v else BLOCKS[max(1, min(8, round(v / top * 8)))])
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return f"{items[0][0]} {''.join(bars)} {items[-1][0]}  (peak {top}/mo)"


def render(title, kind, st, events, open_items, today, extra_lines=()):
    tag = {"current": "", "recent": "", "earlier this year": " · stale",
           "archive": " · archive"}
    L = [f"# {title} — timeline", ""]
    L.append(f"**As of:** {today.isoformat()}. Every fact below is dated; where two "
             f"disagree, the later date wins. Nothing here is current unless its date says so.")
    if st:
        L.append(f"**Activity:** {st['messages']} messages, {st['first_seen']} to "
                 f"{st['last_seen']} ({st['last_seen_age']}) · {st['trend']} · "
                 f"{st['last_90d']} in the last 90 days vs {st['prior_90d']} the 90 before")
        if st.get("peak_hours"):
            L.append(f"**Usually active:** {', '.join(st['peak_hours'])}")
        if st.get("per_month"):
            L.append("**By month:** " + spark(st["per_month"]))
    L += list(extra_lines)
    L.append("")

    by_band = defaultdict(list)
    for e in events:
        by_band[T.band(e["date"][:7], today)].append(e)

    for b in T.BAND_ORDER:
        rows = sorted(by_band.get(b, []), key=lambda e: e["date"], reverse=True)
        if not rows:
            continue
        head = {"current": "This month", "recent": "Recent (last ~6 weeks)",
                "earlier this year": "Earlier this year — may be out of date",
                "archive": "Archive — old, treat as background only"}[b]
        L += [f"## {head}", ""]
        for e in rows[: 40 if b in ("current", "recent") else 15]:
            who = f" [{', '.join(e['who'])}]" if e["who"] else ""
            mark = f"  ⤳ superseded {e['superseded_by']}" if e.get("superseded_by") else ""
            L.append(f"- **{e['date']}**{who} {e['what']}{mark}")
        if len(rows) > (40 if b in ("current", "recent") else 15):
            L.append(f"- _… and {len(rows) - (40 if b in ('current','recent') else 15)} more_")
        L.append("")

    if open_items:
        L += ["## Open threads", ""]
        for t in sorted(open_items, key=lambda t: (t["status"] != "open", t["last_seen"]),
                        reverse=False):
            due = f" (due {t['due']}, {T.human_age(date.fromisoformat(t['due']), today)})" if t["due"] else ""
            who = f" [{', '.join(t['who'])}]" if t["who"] else ""
            L.append(f"- **{t['status']}**{who} {t['what']}{due} "
                     f"_— raised {t['first_seen']}, last mentioned {t['last_seen']}_")
        L.append("")
    return "\n".join(L) + "\n"


def plan_months(chats_dir, model, only=None, min_messages=20):
    """-> (cached, uncached, chats) without calling the model.

    Answers the only question that matters before a run: how many months has the
    model never read? On a first build that is every month; afterwards it is the
    current one, plus whatever a re-export revised."""
    cached = uncached = chats = 0
    root = pathlib.Path(chats_dir)
    if not root.exists():
        return 0, 0, 0
    for d in sorted(x for x in root.iterdir() if x.is_dir()):
        if only and only.lower() not in d.name.lower():
            continue
        months = T.months_for(d)
        if sum(len(T.read_month(p)) for _, p in months) < min_messages:
            continue
        chats += 1
        for month, path in months:
            msgs = T.read_month(path)
            if not msgs:
                continue
            body = "\n".join(f"{m[0].strftime('%m-%d %H:%M')} {m[1]}: {m[2]}" for m in msgs)
            key = T.content_hash(d.name, month, model, T.EXTRACT_VERSION, body)
            if T.cache_get(key) is not None:
                cached += 1
            else:
                uncached += 1
    return cached, uncached, chats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chats", default="corpus/chats")
    ap.add_argument("--out", default="corpus/timeline")
    ap.add_argument("--model", default="qwen3.8:27b")
    ap.add_argument("--sample-chars", type=int, default=24000)
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--only")
    ap.add_argument("--force", action="store_true", help="ignore the month cache")
    ap.add_argument("--no-llm", action="store_true",
                    help="deterministic facts only — no model calls, no GPU")
    ap.add_argument("--min-messages", type=int, default=20)
    ap.add_argument("--plan", action="store_true",
                    help="report how many months need the model, then exit")
    args = ap.parse_args()

    if args.plan:
        c, u, n = plan_months(args.chats, args.model, args.only, args.min_messages)
        print(f"months cached={c} uncached={u} across {n} chat(s)")
        return 0

    today = date.today()
    root = pathlib.Path(args.chats)
    if not root.exists():
        print(f"No {root}. Run ingest/parse_export.py first.", file=sys.stderr)
        return 1
    dirs = sorted(d for d in root.iterdir() if d.is_dir())
    if args.only:
        dirs = [d for d in dirs if args.only.lower() in d.name.lower()]
    if not dirs:
        print("No chats matched.", file=sys.stderr)
        return 1

    outdir = pathlib.Path(args.out)
    (outdir / "people").mkdir(parents=True, exist_ok=True)
    man = T.Manifest()
    use_llm = not args.no_llm
    tally = Counter()
    person_events, person_msgs = defaultdict(list), defaultdict(list)
    person_chats = defaultdict(Counter)

    for d in dirs:
        months = T.months_for(d)
        if not months:
            continue
        all_msgs, per_month, cost = [], {}, Counter()
        members = Counter()
        for month, path in months:
            for m in T.read_month(path):
                members[m[1]] += 1
        top = [n for n, _ in members.most_common(15)]

        for month, path in months:
            data, msgs, how = extract_month(d.name, month, path, top, args.model,
                                            args.timeout, args.sample_chars, use_llm, args.force)
            all_msgs += msgs
            cost[how] += 1
            if data:
                per_month[month] = data
        if len(all_msgs) < args.min_messages:
            print(f"  skip {d.name}: {len(all_msgs)} message(s)", file=sys.stderr)
            continue

        st = T.stats(all_msgs, today)
        events = supersede([e for m in sorted(per_month) for e in per_month[m]["events"]])
        open_items = resolve_open(per_month, today)

        pairs = T.cooccurrence(d)
        extra = []
        if pairs:
            extra.append("**Talks with:** " + ", ".join(
                f"{a}↔{b} ({c})" for (a, b), c in pairs.most_common(6)))
        dest = outdir / f"{d.name}-timeline.md"
        dest.write_text(render(d.name, "chat", st, events, open_items, today, extra),
                        encoding="utf-8")
        man.record(f"timeline:{d.name}", T.content_hash(*(p.read_text() for _, p in months)),
                   count=len(all_msgs), model=args.model, llm=use_llm)
        tally["chats"] += 1
        print(f"  {d.name}: {len(all_msgs)} msgs, {len(months)} months "
              f"({dict(cost)}) -> {len(events)} events, {len(open_items)} threads",
              file=sys.stderr)

        for e in events:
            for w in e["who"]:
                person_events[w].append({**e, "chat": d.name})
        for m in all_msgs:
            person_msgs[m[1]].append(m)
            person_chats[m[1]][d.name] += 1

    for person, msgs in sorted(person_msgs.items(), key=lambda kv: -len(kv[1])):
        if len(msgs) < args.min_messages:
            continue
        ev = sorted(person_events.get(person, []), key=lambda e: e["date"])
        extra = ["**Seen in:** " + ", ".join(
            f"{c} ({n})" for c, n in person_chats[person].most_common())]
        dest = outdir / "people" / f"{T.slug(person)}-timeline.md"
        dest.write_text(
            render(person, "person", T.stats(msgs, today), ev, [], today, extra),
            encoding="utf-8")
        tally["people"] += 1

    (outdir / "_now.md").write_text(
        f"# Today\n\n"
        f"Today's date is **{today.isoformat()}** ({today.strftime('%A, %d %B %Y')}).\n\n"
        f"Corpus memory is historical. Timeline cards date every fact — read the date "
        f"before treating anything as current. A plan dated before today already "
        f"happened or lapsed; do not present it as upcoming.\n",
        encoding="utf-8")
    man.save()
    print(f"\ntimeline: {tally['chats']} chat card(s), {tally['people']} person card(s)"
          + ("  [--no-llm: deterministic only]" if args.no_llm else ""), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
