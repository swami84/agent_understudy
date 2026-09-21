#!/usr/bin/env python3
"""Match phone numbers from WhatsApp group rosters against your address book.

Export contacts from your phone, then:

    python3 ingest/import_contacts.py corpus/raw/contacts.vcf --apply

Supported inputs: vCard (.vcf, versions 2.1/3.0/4.0) and Google Contacts CSV.

Matching: numbers are reduced to digits and compared on their last N digits
(default 10), because address books routinely omit the country code while
WhatsApp always includes it. Ambiguous matches are reported, never guessed.
"""
import argparse, csv, json, pathlib, quopri, re, sys
from collections import defaultdict

TEL = re.compile(r"^(?:item\d+\.)?TEL", re.I)
FN = re.compile(r"^(?:item\d+\.)?FN", re.I)
N_FIELD = re.compile(r"^(?:item\d+\.)?N[;:]", re.I)


def unfold(text):
    """vCard folds long lines by starting continuations with space/tab."""
    out = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def decode_value(prop, value):
    """Android exports often quoted-printable-encode non-ASCII names."""
    if "quoted-printable" in prop.lower():
        try:
            return quopri.decodestring(value.encode("utf-8", "replace")).decode("utf-8", "replace")
        except Exception:
            return value
    return value


def digits(s):
    return re.sub(r"\D", "", s or "")


def parse_vcf(path):
    """-> list of (name, [raw_numbers])"""
    people, name, nums = [], None, []
    for line in unfold(pathlib.Path(path).read_text(encoding="utf-8", errors="replace")):
        u = line.upper()
        if u.startswith("BEGIN:VCARD"):
            name, nums = None, []
        elif u.startswith("END:VCARD"):
            if nums:
                people.append((name or "", nums))
            name, nums = None, []
        elif ":" in line:
            prop, _, value = line.partition(":")
            if FN.match(prop) and not name:
                name = decode_value(prop, value).strip()
            elif N_FIELD.match(prop + ":") and not name:
                parts = [p.strip() for p in decode_value(prop, value).split(";")]
                # N is Family;Given;Middle;Prefix;Suffix
                given = parts[1] if len(parts) > 1 else ""
                family = parts[0] if parts else ""
                composed = " ".join(x for x in (given, family) if x).strip()
                if composed:
                    name = composed
            elif TEL.match(prop):
                v = decode_value(prop, value).strip()
                if digits(v):
                    nums.append(v)
    return people


def parse_csv(path):
    people = []
    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        for row in csv.DictReader(fh):
            name = (row.get("Name") or "").strip()
            if not name:
                # Google has shipped both spellings over the years.
                first = (row.get("First Name") or row.get("Given Name") or "").strip()
                last = (row.get("Last Name") or row.get("Family Name") or "").strip()
                name = " ".join(x for x in (first, last) if x)
            nums = [v for k, v in row.items() if k and "Phone" in k and "Value" in k and v and digits(v)]
            # some exports pack multiple numbers in one cell
            flat = []
            for n in nums:
                flat.extend(p for p in re.split(r"[:;]{3}|\s*:::\s*", n) if digits(p))
            if flat:
                people.append((name, flat))
    return people


def build_index(people, tail):
    """-> {tail_digits: {names}}"""
    idx = defaultdict(set)
    for name, nums in people:
        if not name:
            continue
        for raw in nums:
            d = digits(raw)
            if len(d) < 7:
                continue
            idx[d[-tail:]].add(name)
    return idx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+", help=".vcf or .csv exports")
    ap.add_argument("--groups", default="corpus/groups")
    ap.add_argument("--tail", type=int, default=10, help="digits to match on (default 10)")
    ap.add_argument("--apply", action="store_true", help="rewrite roster + group markdown with names")
    ap.add_argument("--self-name", help="your own display name (you are not in your own address book)")
    ap.add_argument("--self-number", help="your number; defaults to channels.whatsapp.allowFrom[0]")
    args = ap.parse_args()

    people = []
    for f in args.files:
        p = pathlib.Path(f)
        if not p.exists():
            print(f"skip (missing): {p}", file=sys.stderr)
            continue
        got = parse_csv(p) if p.suffix.lower() == ".csv" else parse_vcf(p)
        print(f"{p.name}: {len(got)} contact(s) with numbers", file=sys.stderr)
        people.extend(got)

    if not people:
        print("No contacts parsed. Is the export a vCard or Google CSV?", file=sys.stderr)
        return 1

    idx = build_index(people, args.tail)

    # You are never in your own address book, so self is supplied out of band
    # and remembered in config/identity.json for subsequent runs.
    ident_path = pathlib.Path("config/identity.json")
    ident = json.loads(ident_path.read_text()) if ident_path.exists() else {}
    self_num = digits(args.self_number or ident.get("selfNumber") or "")
    if not self_num:
        try:
            oc = json.loads((pathlib.Path.home() / ".openclaw/openclaw.json").read_text())
            allow = oc.get("channels", {}).get("whatsapp", {}).get("allowFrom", [])
            self_num = digits(allow[0]) if allow else ""
        except Exception:
            self_num = ""
    self_name = args.self_name or ident.get("selfName") or ""
    if self_num and self_name:
        idx[self_num[-args.tail:]] = {self_name}
        if args.apply:
            ident_path.parent.mkdir(parents=True, exist_ok=True)
            ident_path.write_text(
                json.dumps({"selfNumber": self_num, "selfName": self_name}, indent=2), encoding="utf-8"
            )
        print(f"self: +{self_num} -> {self_name}", file=sys.stderr)
    elif self_num and not self_name:
        print(f"self: +{self_num} has no name — pass --self-name \"Your Name\"", file=sys.stderr)
    print(f"indexed {len(idx)} distinct number(s) from {len(people)} contact(s)\n", file=sys.stderr)

    gdir = pathlib.Path(args.groups)
    roster_path = gdir / "_roster.json"
    if not roster_path.exists():
        print(f"No roster at {roster_path}. Run ingest/extract_group.mjs first.", file=sys.stderr)
        return 1

    roster = json.loads(roster_path.read_text())
    matched = ambiguous = unmatched = 0
    resolved = {}

    for rec in roster:
        d = digits(rec.get("phone", ""))
        if not d:
            unmatched += 1
            continue
        hits = idx.get(d[-args.tail:], set())
        if len(hits) == 1:
            nm = next(iter(hits))
            resolved[d] = nm
            if args.apply:
                rec["name"] = rec.get("name") or nm
            matched += 1
        elif len(hits) > 1:
            ambiguous += 1
            print(f"  ambiguous +{d}: {', '.join(sorted(hits))}", file=sys.stderr)
        else:
            unmatched += 1

    print(f"\nmatched={matched} ambiguous={ambiguous} unmatched={unmatched}", file=sys.stderr)

    if not args.apply:
        print("\n(dry run — pass --apply to write names into the roster and markdown)", file=sys.stderr)
        for d, nm in list(resolved.items())[:20]:
            print(f"  +{d} -> {nm}")
        return 0

    roster_path.write_text(json.dumps(roster, indent=2), encoding="utf-8")
    (gdir / "_contacts.json").write_text(json.dumps(resolved, indent=2), encoding="utf-8")

    # Rewrite each group's markdown with resolved names.
    for jf in sorted(gdir.glob("*.json")):
        if jf.name.startswith("_"):
            continue
        data = json.loads(jf.read_text())
        parts = data.get("participants", [])
        for p in parts:
            d = digits(p.get("phone", ""))
            if d and not p.get("name") and d in resolved:
                p["name"] = resolved[d]
        jf.write_text(json.dumps(data, indent=2), encoding="utf-8")
        md = [
            f"# {data.get('subject','')}",
            "",
            f"**Group JID:** `{data.get('id','')}`",
            f"**Participants:** {len(parts)}",
            "",
            "## Members",
            "",
            "| Name | Phone | Role |",
            "| --- | --- | --- |",
        ]
        for p in parts:
            nm = p.get("name") or "_(unknown)_"
            ph = "+" + p["phone"] if p.get("phone") else "_(lid only)_"
            md.append(f"| {nm} | {ph} | {p.get('admin') or 'member'} |")
        md.append("")
        (gdir / f"{jf.stem}.md").write_text("\n".join(md), encoding="utf-8")
        print(f"  rewrote {jf.stem}.md", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
