#!/usr/bin/env python3
"""Shared temporal machinery: month buckets, dated facts, and the build manifest.

Two ideas carry the whole design.

**Calendar buckets, not sliding windows.** A "last 30 days" window changes its
contents every single day, so anything derived from it must be rebuilt every day,
and at 200 groups that is the entire corpus, nightly. A calendar month is frozen
the moment it ends. `parse_export.py` already writes one file per month, so a
closed month's extraction can be cached by content hash and never recomputed.
Only the current month is volatile. That is what makes refresh incremental:
steady-state cost is one LLM call per active chat per month, not one per chat per
run.

**Event time vs. observation time.** Every extracted fact carries both the date it
refers to and the month it was learned in. That is what lets a later month
supersede an earlier one deterministically, without asking a model to reconcile
them, and what lets the renderer mark a fact stale instead of asserting it flatly.
"""
import hashlib, json, pathlib, re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

ROOT = pathlib.Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "corpus" / ".manifest.json"
CACHE = ROOT / "corpus" / ".cache" / "months"

# Bump when a prompt or output shape changes, to invalidate every cached month.
EXTRACT_VERSION = 1

SESSION = re.compile(
    r"^## (?P<d>\d{4}-\d{2}-\d{2}) (?P<t>\d{2}:\d{2})"
    r"(?: → (?P<d2>\d{4}-\d{2}-\d{2}) (?P<t2>\d{2}:\d{2}))?"
    r" — (?P<who>.*)$"
)
BULLET = re.compile(r"^- \*\*(?P<sender>.+?)\*\* \((?P<time>\d{2}:\d{2})\): (?P<body>.*)$")
MONTH_FILE = re.compile(r"^\d{4}-\d{2}$")


def slug(s):
    s = re.sub(r"[^\w\s-]", "", s).strip().lower()
    return re.sub(r"[\s_-]+", "-", s)[:60] or "item"


def content_hash(*parts):
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


# ---------------------------------------------------------------- parsing

def read_month(path):
    """Parse one corpus/chats/<chat>/YYYY-MM.md into [(datetime, sender, body)].

    Bullets carry only HH:MM; the date comes from the enclosing session header.
    A session that runs past midnight has its bullets' times wrap backwards, so
    roll the date forward when that happens rather than stamping every message
    in an overnight session with the start date.
    """
    out, cur_date, prev_t = [], None, None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = SESSION.match(line)
        if m:
            cur_date = datetime.strptime(m.group("d"), "%Y-%m-%d").date()
            prev_t = None
            continue
        b = BULLET.match(line)
        if not b or cur_date is None:
            continue
        body = b.group("body").strip()
        if not body or body == "[media]":
            continue
        t = datetime.strptime(b.group("time"), "%H:%M").time()
        if prev_t is not None and t < prev_t:
            cur_date += timedelta(days=1)
        prev_t = t
        out.append((datetime.combine(cur_date, t), b.group("sender").strip(), body))
    return out


def months_for(chat_dir):
    """-> [(month_str, path)] oldest first."""
    return sorted(
        (p.stem, p) for p in pathlib.Path(chat_dir).glob("*.md") if MONTH_FILE.match(p.stem)
    )


# ---------------------------------------------------------------- recency

def month_end(month):
    y, m = (int(x) for x in month.split("-"))
    return date(y + (m == 12), 1 if m == 12 else m + 1, 1) - timedelta(days=1)


def band(month, today=None):
    """Recency band for a calendar month, evaluated at read time.

    The band is never stored — it is recomputed on every render, so a card written
    in June does not still claim June is 'current' when read in October.
    """
    today = today or date.today()
    age = (today - month_end(month)).days
    if age <= 0:
        return "current"
    if age <= 45:
        return "recent"
    if age <= 180:
        return "earlier this year"
    return "archive"


def month_gap(a, b):
    """Calendar months between two YYYY-MM strings. Not the distance between
    their positions in a list — a chat silent from May to September has three
    month files, and counting those says the gap is one."""
    ay, am = (int(x) for x in a.split("-"))
    by, bm = (int(x) for x in b.split("-"))
    return abs((by * 12 + bm) - (ay * 12 + am))


BAND_ORDER = ["current", "recent", "earlier this year", "archive"]
STALE_BANDS = {"earlier this year", "archive"}


def human_age(d, today=None):
    today = today or date.today()
    n = (today - d).days
    if n <= 0:
        return "today"
    if n == 1:
        return "yesterday"
    if n < 31:
        return f"{n} days ago"
    if n < 365:
        return f"{n // 30} month{'s' if n // 30 > 1 else ''} ago"
    return f"{n // 365} year{'s' if n // 365 > 1 else ''} ago"


# ---------------------------------------------------------------- stats

def stats(msgs, today=None):
    """Deterministic temporal facts. No LLM — these are counted, not inferred,
    so they are the part of a card that is never wrong."""
    today = today or date.today()
    if not msgs:
        return {}
    dates = [m[0].date() for m in msgs]
    first, last = min(dates), max(dates)
    per_month = Counter(d.strftime("%Y-%m") for d in dates)

    recent = sum(1 for d in dates if (today - d).days <= 90)
    prior = sum(1 for d in dates if 90 < (today - d).days <= 180)
    if (today - last).days > 120:
        trend = "dormant"
    elif prior == 0:
        # No traffic in the 90-180d window. That is a brand-new chat only if it
        # has no older history either; otherwise it went quiet and came back.
        if not recent:
            trend = "dormant"
        elif (today - first).days > 180:
            trend = "resumed"
        else:
            trend = "new"
    elif recent > prior * 1.5:
        trend = "rising"
    elif recent < prior * 0.5:
        trend = "cooling"
    else:
        trend = "steady"

    hours = Counter(m[0].hour for m in msgs)
    return {
        "first_seen": first.isoformat(),
        "last_seen": last.isoformat(),
        "last_seen_age": human_age(last, today),
        "days_since_last": (today - last).days,
        "messages": len(msgs),
        "months_active": len(per_month),
        "per_month": dict(sorted(per_month.items())),
        "last_90d": recent,
        "prior_90d": prior,
        "trend": trend,
        "peak_hours": [f"{h:02d}:00" for h, _ in hours.most_common(3)],
    }


def cooccurrence(chat_dir):
    """Who appears in the same session as whom, per chat. The seed of an entity
    index: it is the cross-group identity signal that flat retrieval cannot see."""
    pairs = Counter()
    for _, path in months_for(chat_dir):
        cur = set()
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if SESSION.match(line):
                for a in cur:
                    for b in cur:
                        if a < b:
                            pairs[(a, b)] += 1
                cur = set()
            else:
                m = BULLET.match(line)
                if m:
                    cur.add(m.group("sender").strip())
        for a in cur:
            for b in cur:
                if a < b:
                    pairs[(a, b)] += 1
    return pairs


# ---------------------------------------------------------------- manifest

class Manifest:
    """Input-hash ledger. An artifact is rebuilt when the bytes that feed it
    change by more than a threshold, not merely when they change at all — one
    new message should not trigger a 27B rewrite of every card in the corpus."""

    def __init__(self, path=MANIFEST):
        self.path = pathlib.Path(path)
        try:
            self.data = json.loads(self.path.read_text())
        except Exception:
            self.data = {}

    def stale(self, key, inputs_hash, *, count=0, min_delta=0, max_age_days=None, force=False):
        """-> (should_rebuild, reason)"""
        if force:
            return True, "forced"
        rec = self.data.get(key)
        if not rec:
            return True, "new"
        if rec.get("hash") == inputs_hash:
            return False, "unchanged"
        delta = abs(count - rec.get("count", 0))
        if max_age_days is not None:
            try:
                age = (date.today() - date.fromisoformat(rec["built"][:10])).days
                if age >= max_age_days:
                    return True, f"aged {age}d"
            except Exception:
                pass
        if min_delta and delta < min_delta:
            return False, f"changed, +{delta} < {min_delta}"
        return True, f"changed, +{delta}" if delta else "changed"

    def adopt(self, key, inputs_hash, *, count=0, **extra):
        """Take ownership of an artifact that was built before this ledger existed.

        Without this, the first incremental run sees a card on disk with no record,
        calls it new, and rebuilds the entire corpus on the GPU — the exact cost
        the ledger exists to avoid. Adopting assumes an existing card reflects the
        corpus as it currently stands; --force is how you say otherwise."""
        if key in self.data:
            return False
        self.record(key, inputs_hash, count=count, adopted=True, **extra)
        return True

    def record(self, key, inputs_hash, *, count=0, **extra):
        self.data[key] = {
            "hash": inputs_hash,
            "count": count,
            "built": datetime.now().isoformat(timespec="seconds"),
            **extra,
        }

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n")


# ---------------------------------------------------------------- month cache

def cache_get(key):
    f = CACHE / f"{key}.json"
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:
            return None
    return None


def cache_put(key, value):
    CACHE.mkdir(parents=True, exist_ok=True)
    (CACHE / f"{key}.json").write_text(json.dumps(value, indent=1) + "\n")
