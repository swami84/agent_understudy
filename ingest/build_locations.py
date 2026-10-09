#!/usr/bin/env python3
"""Build a "who lives where" directory from the chat corpus.

Answers the question that prompted it: "we travel places and then look for
nittians to catch up with — can I ask for that?" Retrieval over raw chats cannot
do this. The evidence is scattered across years, phrased obliquely ("landed in
Bangalore", "back in Dubai"), and a top-k search for a city returns whoever said
its name most, not who lives there.

So the inference happens once, at build time, into one consolidated card. Cities
are the retrieval key, people are the payload.

Two rules this encodes:

- People move. Every claim carries the date of its newest supporting message,
  and the card is grouped so a stale claim is visible as stale rather than
  asserted flatly.
- Absence is not evidence. A person with no location signal is listed as unknown
  instead of being guessed at from a timezone or a cricket team.

    python3 ingest/build_locations.py --dry-run
    python3 ingest/build_locations.py --base-url http://127.0.0.1:8080/v1 \
        --model qwen3.8-flash-next-iq3_s
"""
import argparse, json, pathlib, re, sys
from collections import defaultdict
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import temporal as T
import llm

# Phrases that state a location outright. Used to pick evidence, never to parse
# the place itself — "moved to the new flat" matches and means nothing.
CUES = re.compile(
    r"\b(i(?:'m| am)? (?:in|at|based in|living in|moved to|back in|now in)|"
    r"live[sd]? in|living in|based (?:in|out of)|moved (?:to|back to)|"
    r"relocat\w+ to|settled in|shifted to|landed in|reached|flying (?:to|into)|"
    r"visiting|in town|here in|currently in|shifting to|posted (?:in|to))\b", re.I)

PROMPT = """Decide where this person is based, from their own chat messages.

Person: {name}
Messages seen: {count} across {chats}

Messages that state a place directly (newest last):
{cues}

A broader sample of their messages, for context:
{sample}

Return JSON only:

{{"home": "City, Country" or null,
  "confidence": "high" | "medium" | "low",
  "as_of": "YYYY-MM" or null,
  "evidence": "one short quote that supports it",
  "also_seen": ["other places they mention being, most recent first"]}}

Rules:
- "home" is where they LIVE, not somewhere they visited. "landed in Dubai" on a
  trip is not home; "back in Dubai" repeatedly over years probably is.
- If they moved, give the most recent home and set as_of to that month.
- Use null and confidence "low" when the messages do not show it. Do NOT infer
  from language, name, timezone, or sports teams. A wrong city is worse than
  none, because someone will travel on it."""


def collect(chats_dir, min_messages):
    """-> {person: {"msgs": [(date, chat, body)], "chats": set}}"""
    people = defaultdict(lambda: {"msgs": [], "chats": set()})
    for chat in sorted(pathlib.Path(chats_dir).iterdir()):
        if not chat.is_dir():
            continue
        for month, path in T.months_for(chat):
            for ts, sender, body in T.read_month(path):
                people[sender]["msgs"].append((ts.date(), chat.name, body))
                people[sender]["chats"].add(chat.name)
    return {n: d for n, d in people.items() if len(d["msgs"]) >= min_messages}


def evidence_for(msgs, limit=30):
    """Cue-matching messages, oldest first so the model sees the trajectory."""
    hits = [(d, c, b) for d, c, b in msgs if CUES.search(b)]
    hits.sort(key=lambda r: r[0])
    return hits[-limit:]


def sample_for(msgs, budget=4000):
    step = max(1, len(msgs) // 60)
    out, used = [], 0
    for d, c, b in msgs[::step]:
        line = f"[{d}] {b}"
        if used + len(line) > budget:
            break
        out.append(line)
        used += len(line)
    return "\n".join(out)


# Same city, different spelling. Without this the directory splits a city across
# two headings and "who is in Bangalore" finds half of them.
ALIASES = {
    "bengaluru": "Bangalore", "bangalore": "Bangalore",
    "new delhi": "Delhi", "delhi": "Delhi", "gurgaon": "Gurugram",
    "gurugram": "Gurugram", "bombay": "Mumbai", "mumbai": "Mumbai",
    "madras": "Chennai", "chennai": "Chennai", "calcutta": "Kolkata",
    "kolkata": "Kolkata", "trichy": "Tiruchirappalli",
    "tiruchirappalli": "Tiruchirappalli", "cochin": "Kochi", "kochi": "Kochi",
    "poona": "Pune", "pune": "Pune", "bengarulu": "Bangalore",
}
COUNTRIES = {
    "us": "USA", "u.s.": "USA", "usa": "USA", "united states": "USA",
    "united states of america": "USA", "uk": "UK", "u.k.": "UK",
    "united kingdom": "UK", "uae": "UAE", "india": "India",
}

# Residence is not a plan. Someone's city from eighteen months ago is usually
# still right, so the "stale" mark uses a horizon measured in years rather than
# temporal.py's bands, which exist for commitments and dates.
STALE_AFTER_MONTHS = 30


def normalize_place(place):
    parts = [p.strip() for p in str(place).split(",") if p.strip()]
    if not parts:
        return None
    city = ALIASES.get(parts[0].lower(), parts[0].strip().title())
    if len(parts) == 1:
        return city
    country = COUNTRIES.get(parts[-1].lower(), parts[-1].strip())
    return f"{city}, {country}"


def months_since(as_of, today):
    try:
        y, m = (int(x) for x in str(as_of).split("-")[:2])
    except Exception:
        return None
    return (today.year - y) * 12 + (today.month - m)


def render(rows, today):
    """Group by place: the query is "who is in X", not "where is Y"."""
    placed = [r for r in rows if r.get("home")]
    unknown = [r for r in rows if not r.get("home")]
    by_place = defaultdict(list)
    for r in placed:
        by_place[normalize_place(r["home"]) or r["home"]].append(r)

    L = [f"# Where people are based", "",
         f"**As of:** {today.isoformat()}. Derived from chat messages, not stated by "
         f"the people themselves — treat it as a lead to confirm, never as fact. Each "
         f"entry shows when its evidence was last seen; people move and the corpus "
         f"does not know they have.", ""]
    for place in sorted(by_place, key=lambda p: -len(by_place[p])):
        L.append(f"## {place}")
        for r in sorted(by_place[place], key=lambda r: (r.get("as_of") or ""), reverse=True):
            age = ""
            if r.get("as_of"):
                n = months_since(r["as_of"], today)
                age = f" · evidence {r['as_of']}"
                if n is not None and n >= STALE_AFTER_MONTHS:
                    age += f" ({n // 12}+ yrs old)"
            also = f" · also seen: {', '.join(r['also_seen'][:3])}" if r.get("also_seen") else ""
            L.append(f"- **{r['name']}** — {r['confidence']} confidence{age}{also}")
            if r.get("evidence"):
                L.append(f"  - \"{r['evidence']}\"")
        L.append("")
    if unknown:
        L += ["## Not established", "",
              "No location evidence in their messages. Not a guess withheld — "
              "nothing to guess from.", ""]
        L.append(", ".join(sorted(r["name"] for r in unknown)))
        L.append("")
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chats", default="corpus/chats")
    ap.add_argument("--out", default="corpus/locations.md")
    ap.add_argument("--model", default=llm.DEFAULT_MODEL)
    ap.add_argument("--base-url", default=llm.DEFAULT_URL)
    ap.add_argument("--min-messages", type=int, default=30)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--only")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    today = date.today()
    people = collect(args.chats, args.min_messages)
    if args.only:
        people = {n: d for n, d in people.items() if args.only.lower() in n.lower()}
    if not people:
        print(f"Nobody with >= {args.min_messages} messages.", file=sys.stderr)
        return 1

    man = T.Manifest()
    targets = sorted(people.items(), key=lambda kv: -len(kv[1]["msgs"]))
    print(f"{len(targets)} person(s); "
          f"{sum(1 for _, d in targets if evidence_for(d['msgs'])) } have direct cues",
          file=sys.stderr)
    if args.dry_run:
        for n, d in targets[:15]:
            print(f"  {n}: {len(d['msgs'])} msgs, {len(evidence_for(d['msgs']))} cue(s)")
        return 0

    rows, built, cached, failed = [], 0, 0, 0
    for name, d in targets:
        cues = evidence_for(d["msgs"])
        key = f"location:{T.slug(name)}"
        ihash = T.content_hash(args.model, name, *(b for _, _, b in cues))
        need, why = man.stale(key, ihash, count=len(cues), force=args.force)
        cache_key = T.content_hash("loc", ihash)
        if not need:
            hit = T.cache_get(cache_key)
            if hit:
                rows.append(hit)
                cached += 1
                continue
        prompt = PROMPT.format(
            name=name, count=len(d["msgs"]), chats=", ".join(sorted(d["chats"])),
            cues="\n".join(f"[{dt}] {b}" for dt, _, b in cues) or "(none found)",
            sample=sample_for(d["msgs"]))
        try:
            data = llm.parse_json(llm.chat(prompt, model=args.model, base_url=args.base_url,
                                          timeout=args.timeout, json_mode=True))
        except Exception as e:
            print(f"  FAILED {name}: {type(e).__name__}: {e}", file=sys.stderr)
            failed += 1
            continue
        home = (data.get("home") or "").strip() or None
        row = {
            "name": name,
            "home": home,
            "confidence": (data.get("confidence") or "low").strip().lower(),
            "as_of": (data.get("as_of") or None),
            "evidence": (data.get("evidence") or "").strip()[:200],
            "also_seen": [str(x).strip() for x in (data.get("also_seen") or [])][:5],
        }
        if row["as_of"] and not re.fullmatch(r"\d{4}-\d{2}", str(row["as_of"])):
            row["as_of"] = None
        T.cache_put(cache_key, row)
        man.record(key, ihash, count=len(cues), model=args.model)
        rows.append(row)
        built += 1
        print(f"  {name}: {home or 'unknown'} ({row['confidence']})", file=sys.stderr)

    pathlib.Path(args.out).write_text(render(rows, today), encoding="utf-8")
    man.save()
    placed = sum(1 for r in rows if r.get("home"))
    print(f"\n{placed} placed, {len(rows) - placed} unknown "
          f"(built={built} cached={cached} failed={failed}) -> {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
