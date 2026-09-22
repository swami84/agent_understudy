#!/usr/bin/env python3
"""Local control panel for Understudy.

    python3 web/server.py          # http://127.0.0.1:8765

Binds to loopback only. Edits ~/.openclaw/openclaw.json and the corpus context
files, validating every config write with `openclaw config validate` and rolling
back automatically if the result would not load.
"""
import html, json, os, pathlib, re, shutil, subprocess, sys, tempfile, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

def _node_bin():
    """Find a Node bin dir without pinning a version (nvm installs vary)."""
    import glob, os, shutil
    n = shutil.which("node")
    if n:
        return os.path.dirname(n)
    cands = sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin")), reverse=True)
    return cands[0] if cands else ""


# Both overridable so the panel can be run against a fixture for screenshots
# or a second profile, without touching the live install.
ROOT = pathlib.Path(os.environ.get("UNDERSTUDY_ROOT",
                                   pathlib.Path(__file__).resolve().parent.parent))
CFG = pathlib.Path(os.environ.get("UNDERSTUDY_CONFIG",
                                  pathlib.Path.home() / ".openclaw/openclaw.json"))
GROUPS_TSV = ROOT / "config/whatsapp-groups.tsv"
WORKSPACE_OVERRIDE = os.environ.get("UNDERSTUDY_WORKSPACE")
PORT = int(os.environ.get("UNDERSTUDY_PORT", "8765"))
NVM_NODE = _node_bin()


def oc(*args, timeout=90):
    env = dict(os.environ, PATH=f"{NVM_NODE}:{os.environ.get('PATH','')}")
    try:
        r = subprocess.run(["openclaw", *args], capture_output=True, text=True,
                           timeout=timeout, env=env)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return 1, str(e)


def load_cfg():
    return json.loads(CFG.read_text())


def save_cfg(cfg):
    """Write, validate, roll back on failure. Returns (ok, message)."""
    backup = CFG.read_text()
    tmp = CFG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    tmp.replace(CFG)
    code, out = oc("config", "validate")
    if code != 0:
        CFG.write_text(backup)
        first = next((l.strip() for l in out.splitlines() if "×" in l or "invalid" in l.lower()), out[:200])
        return False, f"Rejected, rolled back: {first}"
    return True, "Saved. Restart the gateway to apply."


def all_groups():
    """Every known group from the captured directory listing."""
    out = []
    if GROUPS_TSV.exists():
        for i, line in enumerate(GROUPS_TSV.read_text().splitlines()):
            if i == 0 or not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 2 and parts[1].endswith("@g.us"):
                out.append({"id": parts[1].strip(),
                            "name": (parts[2].strip() if len(parts) > 2 else parts[1])})
    return out


def context_files():
    files = []
    for sub in ("people", "groups"):
        d = ROOT / "corpus" / sub
        if d.exists():
            for f in sorted(d.glob("*.md")):
                files.append({"path": str(f.relative_to(ROOT)), "name": f.stem, "kind": sub})
    ws = pathlib.Path.home() / ".openclaw/workspace"
    for n in ("USER.md", "MEMORY.md"):
        if (ws / n).exists():
            files.append({"path": f"workspace/{n}", "name": n, "kind": "workspace"})
    return files


def resolve_ctx(rel):
    """Map an API path to a real file, refusing anything outside allowed roots."""
    if rel.startswith("workspace/"):
        p = (pathlib.Path.home() / ".openclaw/workspace" / rel.split("/", 1)[1]).resolve()
        base = (pathlib.Path.home() / ".openclaw/workspace").resolve()
    else:
        p = (ROOT / rel).resolve()
        base = (ROOT / "corpus").resolve()
    if not str(p).startswith(str(base)):
        raise ValueError("path outside allowed directory")
    if p.suffix != ".md":
        raise ValueError("only .md files")
    return p


SCHED_PREFIX = "swamai-sched-"


def cron_jobs():
    """{group_jid: job} for schedules this panel owns."""
    code, out = oc("cron", "list", "--json", timeout=60)
    if code != 0:
        return {}
    # The CLI prints a docs footer after the JSON, so decode only the value.
    try:
        start = min([i for i in (out.find("["), out.find("{")) if i >= 0])
        d, _ = json.JSONDecoder().raw_decode(out[start:])
    except Exception:
        return {}
    jobs = d if isinstance(d, list) else d.get("jobs", [])
    found = {}
    for j in jobs:
        name = j.get("name") or ""
        if not name.startswith(SCHED_PREFIX):
            continue
        sch = j.get("schedule") or {}
        if sch.get("kind") == "every":
            freq = f"{int(sch.get('everyMs', 0) / 3600000)}h"
        else:
            freq = sch.get("expr") or sch.get("expression") or sch.get("cron") or "?"
        found[j.get("displayName", "").split("|")[-1].strip()] = {
            "id": j.get("id"), "name": name, "enabled": j.get("enabled", False),
            "freq": freq, "tz": sch.get("tz", ""), "kind": sch.get("kind", "cron"),
            "nextRunAtMs": (j.get("state") or {}).get("nextRunAtMs"),
            "message": (j.get("payload") or {}).get("message", ""),
        }
    return found


def state():
    cfg = load_cfg()
    _name, _impersonate = read_identity(cfg)
    _imp = load_impersonation()
    wa = cfg.get("channels", {}).get("whatsapp", {})
    groups_cfg = wa.get("groups", {}) or {}
    gc = cfg.get("messages", {}).get("groupChat", {}) or {}
    known = all_groups()
    known_ids = {g["id"] for g in known}
    # include allowlisted groups even if absent from the TSV.
    # groupAllowFrom also holds SENDER numbers — those are not groups.
    for jid in wa.get("groupAllowFrom", []):
        if str(jid).endswith("@g.us") and jid not in known_ids:
            known.append({"id": jid, "name": jid})
    return {
        "groups": known,
        "allowFromGroups": [g for g in wa.get("groupAllowFrom", []) if str(g).endswith("@g.us")],
        "allowFromSenders": [g for g in wa.get("groupAllowFrom", []) if not str(g).endswith("@g.us")],
        "requireMention": {k: bool(v.get("requireMention", True)) for k, v in groups_cfg.items()},
        "settings": {
            "dmPolicy": wa.get("dmPolicy", "pairing"),
            "groupPolicy": wa.get("groupPolicy", "allowlist"),
            "selfChatMode": bool(wa.get("selfChatMode", True)),
            "sendReadReceipts": bool(wa.get("sendReadReceipts", False)),
            "sendMessage": bool((wa.get("actions") or {}).get("sendMessage", False)),
            "allowFrom": wa.get("allowFrom", []),
            "mentionPatterns": gc.get("mentionPatterns", []),
            "unmentionedInbound": gc.get("unmentionedInbound", "room_event"),
            "historyLimit": gc.get("historyLimit", 12),
            "responsePrefix": wa.get("responsePrefix", ""),
            "assistantName": _name,
            "impersonate": _impersonate,
            "impersonateGroups": _imp["groups"],
            "impersonateDms": _imp["dms"],
            "allowFromSenders": [g for g in wa.get("groupAllowFrom", []) if not str(g).endswith("@g.us")],
            "model": cfg.get("agents", {}).get("defaults", {}).get("model", {}).get("primary", ""),
        },
        "groupPrompts": {k: (v.get("systemPrompt") or "") for k, v in groups_cfg.items()},
        "schedules": cron_jobs(),
        "contextFiles": context_files(),
    }


# ── assistant identity ───────────────────────────────────────────────────────
WORKSPACE = pathlib.Path(WORKSPACE_OVERRIDE or (pathlib.Path.home() / ".openclaw/workspace"))
AGENTS_MD = WORKSPACE / "AGENTS.md"
BLOCK_START = "<!-- swamai:identity:start -->"
BLOCK_END = "<!-- swamai:identity:end -->"
DEFAULT_NAME = "Assistant"


def mention_pattern_for(name):
    r"""Build a summon pattern that cannot match the assistant's own output.

    The negative lookahead skips a leading attribution prefix (including an
    emoji, matched as \W so no literal emoji ends up in the pattern — OpenClaw's
    safe-regex check rejects patterns containing one, silently, leaving nothing
    able to summon it).
    """
    esc = re.escape(name.strip().lower())
    return rf"^(?!\W{{0,4}}\s*{esc}\s*:).*\b{esc}\b"


def pattern_is_self_safe(pattern, name, prefix):
    """Would the assistant's own messages re-trigger this pattern?"""
    try:
        rx = re.compile(pattern, re.I)
    except re.error:
        return False, "pattern does not compile"
    samples = [
        f"{prefix} {name} is now set up and listening.",
        f"{prefix} I could not fetch that. Ask {name} again later.",
        f"{prefix} Noted.",
        f"{name}: mentioning {name} again without the emoji",
    ]
    for smp in samples:
        if rx.search(smp.lower()):
            return False, f"matches the assistant's own message: {smp[:52]!r}"
    for human in (f"{name} what is the score?", f"hey {name} check this",
                  f'"{name}" what does this mean'):
        if not rx.search(human.lower()):
            return False, f"does not match a normal summon: {human[:40]!r}"
    return True, "ok"


def read_identity(cfg):
    wa = (cfg.get("channels") or {}).get("whatsapp") or {}
    prefix = wa.get("responsePrefix", "")
    impersonate = not prefix.strip()
    m = re.match(r"^\s*(?:\W{1,4}\s*)?([A-Za-z][\w .-]{0,30}?)\s*:", prefix)
    name = m.group(1).strip() if m else ""
    if not name:
        pats = ((cfg.get("messages") or {}).get("groupChat") or {}).get("mentionPatterns") or []
        if pats:
            mm = re.search(r"\\b([A-Za-z][\w-]{1,30})\\b", pats[0])
            if mm:
                name = mm.group(1)
    return (name or DEFAULT_NAME), impersonate


def write_identity_block(name, impersonate, emoji="🤖"):
    """Rewrite the managed section of AGENTS.md. Everything else is untouched."""
    if impersonate:
        body = (
            f"## Who you are\n\n"
            f"You write **as the account owner, in the first person**. Do not refer to "
            f"yourself as an assistant, a bot, or by a name. Do not add any prefix or "
            f"signature. Match the owner's voice and register as seen in the chat "
            f"history.\n\n"
            f"Recipients are not told a machine wrote the message. Never claim to have "
            f"done something in the physical world, never agree to a commitment on the "
            f"owner's behalf, and never state a fact about the owner you cannot support "
            f"from the conversation or the corpus. When unsure, say less.\n\n"
            f"You are summoned in groups by the word \"{name}\". Never write that word "
            f"yourself — it re-triggers you and causes a reply loop.\n"
        )
    else:
        body = (
            f"## Who you are\n\n"
            f"You are **{name}**, an AI assistant acting for the account owner.\n\n"
            f"Begin every message you send with exactly `{emoji} {name}:` then a space. "
            f"Recipients are real people who must be able to tell an assistant wrote it. "
            f"Never omit or reword it.\n"
        )
    block = f"{BLOCK_START}\n{body}{BLOCK_END}"
    AGENTS_MD.parent.mkdir(parents=True, exist_ok=True)
    cur = AGENTS_MD.read_text() if AGENTS_MD.exists() else ""
    if BLOCK_START in cur and BLOCK_END in cur:
        pre = cur.split(BLOCK_START)[0]
        post = cur.split(BLOCK_END, 1)[1]
        new = pre + block + post
    else:
        new = cur.rstrip() + "\n\n" + block + "\n"
    AGENTS_MD.write_text(new)


IMP_STORE = ROOT / "config" / "impersonation.json"
PROMPT_START = "<<<identity>>>"
PROMPT_END = "<<</identity>>>"


def load_impersonation():
    """{"groups": {jid: bool}, "dms": {digits: bool}} — which conversations are impersonated."""
    if IMP_STORE.exists():
        try:
            d = json.loads(IMP_STORE.read_text())
            return {"groups": d.get("groups") or {}, "dms": d.get("dms") or {}}
        except Exception:
            pass
    return {"groups": {}, "dms": {}}


def save_impersonation(m):
    IMP_STORE.parent.mkdir(parents=True, exist_ok=True)
    IMP_STORE.write_text(json.dumps(m, indent=2))


def identity_prompt(name, impersonate, emoji="🤖"):
    """The managed instruction injected into a conversation's systemPrompt."""
    if impersonate:
        return (
            f"You write AS THE ACCOUNT OWNER, in the first person. Never refer to "
            f"yourself as an assistant or bot, never use a name, never add a prefix or "
            f"signature. Match the owner's voice from the chat history. Recipients are "
            f"not told a machine wrote this. Never claim to have done something in the "
            f"physical world, never accept a commitment on the owner's behalf, and never "
            f"assert a fact about the owner you cannot support from this conversation or "
            f"the corpus — when unsure, say less. You are summoned by the word "
            f"\"{name}\"; never write that word yourself or you will re-trigger."
        )
    return (
        f"You are {name}, an AI assistant acting for the account owner. Begin every "
        f"message with exactly \"{emoji} {name}:\" then a space, so recipients can tell "
        f"an assistant wrote it. Never omit or reword it."
    )


def set_managed_prompt(existing, managed):
    """Replace the managed block in a systemPrompt, preserving the user's own text."""
    existing = existing or ""
    block = f"{PROMPT_START}\n{managed}\n{PROMPT_END}"
    if PROMPT_START in existing and PROMPT_END in existing:
        pre = existing.split(PROMPT_START)[0]
        post = existing.split(PROMPT_END, 1)[1]
        return (pre + block + post).strip()
    return (block + "\n\n" + existing).strip() if existing.strip() else block


def apply_identity(cfg, name, imp):
    """Write per-conversation identity into config. Returns (warnings, any_impersonated).

    responsePrefix is channel-global in OpenClaw — there is no per-conversation
    prefix and groups has additionalProperties:false — so as soon as ONE
    conversation is impersonated the code-applied prefix must come off, and
    signing for the rest falls back to the per-conversation instruction.
    """
    wa = cfg.setdefault("channels", {}).setdefault("whatsapp", {})
    warnings = []
    any_imp = any(imp["groups"].values()) or any(imp["dms"].values())

    groups = wa.setdefault("groups", {})
    for jid in list(wa.get("groupAllowFrom", [])):
        if not str(jid).endswith("@g.us"):
            continue
        g = groups.setdefault(jid, {})
        g.setdefault("requireMention", True)
        g["systemPrompt"] = set_managed_prompt(
            g.get("systemPrompt"), identity_prompt(name, bool(imp["groups"].get(jid))))

    direct = wa.setdefault("direct", {})
    for num, on in imp["dms"].items():
        d = direct.setdefault(num, {})
        d["systemPrompt"] = set_managed_prompt(d.get("systemPrompt"),
                                               identity_prompt(name, bool(on)))

    if any_imp:
        wa.pop("responsePrefix", None)
        warnings.append(
            "Attribution prefix removed channel-wide: OpenClaw applies responsePrefix "
            "globally, so it cannot stay on while any conversation is impersonated. "
            "Signed conversations now rely on their instruction, which a model can miss.")
    else:
        wa["responsePrefix"] = f"🤖 {name}:"
    if not direct:
        wa.pop("direct", None)
    return warnings, any_imp


def image_search(query, limit=5):
    """Keyless DuckDuckGo image search. Two-step: grab a vqd token, then query i.js."""
    import urllib.parse, urllib.request
    ua = {"User-Agent": "Mozilla/5.0", "Referer": "https://duckduckgo.com/"}
    q = urllib.parse.quote_plus(query)
    tok_req = urllib.request.Request(
        f"https://duckduckgo.com/?q={q}&iax=images&ia=images", headers=ua)
    with urllib.request.urlopen(tok_req, timeout=20) as r:
        html_body = r.read().decode("utf-8", "replace")
    m = re.search(r'vqd=\\?"?([0-9-]{10,})', html_body)
    if not m:
        return []
    api = (f"https://duckduckgo.com/i.js?l=us-en&o=json&q={q}"
           f"&vqd={m.group(1)}&f=,,,&p=1")
    with urllib.request.urlopen(urllib.request.Request(api, headers=ua), timeout=20) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    out = []
    for item in data.get("results", []):
        url = item.get("image") or ""
        if url.lower().split("?")[0].endswith((".jpg", ".jpeg", ".png", ".webp")):
            out.append({"url": url, "title": item.get("title", ""),
                        "source": item.get("url", "")})
        if len(out) >= limit:
            break
    return out


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if u.path == "/api/state":
            return self._send(200, json.dumps(state()))
        if u.path == "/api/status":
            code, out = oc("gateway", "status", timeout=45)
            running = "Runtime: running" in out
            linked = "linked" in (oc("channels", "list", timeout=45)[1] or "")
            return self._send(200, json.dumps({"gateway": running, "whatsappLinked": linked}))
        if u.path == "/api/imagesearch":
            q = urllib.parse.parse_qs(u.query)
            query = (q.get("q", [""])[0] or "").strip()
            if not query:
                return self._send(400, json.dumps({"error": "missing q"}))
            try:
                hits = image_search(query, int(q.get("n", ["5"])[0]))
            except Exception as e:
                return self._send(502, json.dumps({"error": str(e)}))
            if not hits:
                return self._send(200, json.dumps({"query": query, "results": [],
                                                   "note": "no image results"}))
            return self._send(200, json.dumps({"query": query, "results": hits}))

        if u.path == "/api/context":
            q = urllib.parse.parse_qs(u.query)
            try:
                p = resolve_ctx(q.get("path", [""])[0])
                return self._send(200, json.dumps({"content": p.read_text() if p.exists() else ""}))
            except Exception as e:
                return self._send(400, json.dumps({"error": str(e)}))
        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, json.dumps({"error": "bad json"}))
        u = urllib.parse.urlparse(self.path)

        if u.path == "/api/groups":
            cfg = load_cfg()
            wa = cfg.setdefault("channels", {}).setdefault("whatsapp", {})
            senders = [x for x in wa.get("groupAllowFrom", []) if not str(x).endswith("@g.us")]
            chosen = [x for x in data.get("allow", []) if str(x).endswith("@g.us")]
            wa["groupAllowFrom"] = list(dict.fromkeys(chosen + senders))
            gset = wa.setdefault("groups", {})
            # Only real groups get entries; groupAllowFrom also holds sender numbers.
            allowed_jids = [j for j in wa["groupAllowFrom"] if str(j).endswith("@g.us")]
            for jid in list(gset):
                if jid not in allowed_jids:
                    gset.pop(jid, None)
            for jid in allowed_jids:
                # MERGE — replacing the dict wipes systemPrompt (and did, once).
                entry = gset.setdefault(jid, {})
                entry["requireMention"] = bool(data.get("requireMention", {}).get(jid, True))
            if not gset:
                wa.pop("groups", None)
            ok, msg = save_cfg(cfg)
            return self._send(200 if ok else 400, json.dumps({"ok": ok, "message": msg}))

        if u.path == "/api/settings":
            cfg = load_cfg()
            wa = cfg.setdefault("channels", {}).setdefault("whatsapp", {})
            s = data
            wa["dmPolicy"] = s.get("dmPolicy", "allowlist")
            wa["groupPolicy"] = s.get("groupPolicy", "allowlist")
            wa["selfChatMode"] = bool(s.get("selfChatMode", True))
            rp = (s.get("responsePrefix") or "").strip()
            if rp:
                wa["responsePrefix"] = rp
            else:
                wa.pop("responsePrefix", None)
            wa["sendReadReceipts"] = bool(s.get("sendReadReceipts", False))
            wa.setdefault("actions", {})["sendMessage"] = bool(s.get("sendMessage", False))
            nums = [re.sub(r"\D", "", x) for x in s.get("allowFrom", []) if re.sub(r"\D", "", x)]
            if nums:
                wa["allowFrom"] = list(dict.fromkeys(nums))
            gc = cfg.setdefault("messages", {}).setdefault("groupChat", {})
            pats = [p for p in s.get("mentionPatterns", []) if p.strip()]
            bad = [p for p in pats if not _valid_re(p)]
            if bad:
                return self._send(400, json.dumps({"ok": False, "message": f"invalid regex: {bad[0]}"}))
            gc["mentionPatterns"] = pats
            gc["unmentionedInbound"] = s.get("unmentionedInbound", "room_event")
            try:
                gc["historyLimit"] = max(0, int(s.get("historyLimit", 12)))
            except Exception:
                pass
            ok, msg = save_cfg(cfg)
            return self._send(200 if ok else 400, json.dumps({"ok": ok, "message": msg}))

        if u.path == "/api/context":
            try:
                p = resolve_ctx(data.get("path", ""))
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(data.get("content", ""))
                return self._send(200, json.dumps({"ok": True, "message": f"Saved {p.name}"}))
            except Exception as e:
                return self._send(400, json.dumps({"ok": False, "message": str(e)}))

        if u.path == "/api/identity":
            cfg = load_cfg()
            name = (data.get("name") or read_identity(cfg)[0]).strip()
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9 _.-]{1,29}", name):
                return self._send(400, json.dumps({"ok": False,
                    "message": "name must start with a letter and be 2-30 chars"}))
            imp = load_impersonation()
            if "impersonateGroups" in data:
                imp["groups"] = {k: bool(v) for k, v in (data["impersonateGroups"] or {}).items()}
            if "impersonateDms" in data:
                imp["dms"] = {re.sub(r"\D", "", k): bool(v)
                              for k, v in (data["impersonateDms"] or {}).items() if re.sub(r"\D", "", k)}

            pattern = mention_pattern_for(name)
            any_imp = any(imp["groups"].values()) or any(imp["dms"].values())
            # With any conversation impersonated the prefix comes off channel-wide,
            # so test the unprefixed case — that is what messages will look like.
            ok_safe, why = pattern_is_self_safe(pattern, name, "" if any_imp else f"🤖 {name}:")
            warnings = []
            if not ok_safe:
                if any_imp:
                    warnings.append(
                        "No attribution prefix while impersonating, so the summon word "
                        "cannot be excluded structurally — if the assistant writes its own "
                        "name it re-triggers. guard/watchdog.py is the backstop.")
                else:
                    return self._send(400, json.dumps({"ok": False,
                        "message": f"unsafe summon pattern: {why}"}))

            cfg.setdefault("messages", {}).setdefault("groupChat", {})["mentionPatterns"] = [pattern]
            w, any_imp = apply_identity(cfg, name, imp)
            warnings += w
            ok, msg = save_cfg(cfg)
            if not ok:
                return self._send(400, json.dumps({"ok": False, "message": msg}))
            save_impersonation(imp)
            try:
                write_identity_block(name, False)     # AGENTS.md keeps the default (signed) rule
            except Exception:
                pass
            n_on = sum(1 for v in imp["groups"].values() if v) + sum(1 for v in imp["dms"].values() if v)
            return self._send(200, json.dumps({
                "ok": True,
                "message": f'Saved. Summon word: "{name}". '
                           + (f"Impersonating in {n_on} conversation(s)." if n_on
                              else "Signed in every conversation."),
                "warning": "  ".join(warnings), "pattern": pattern}))

        if u.path == "/api/context/new":
            kind = data.get("kind", "people")
            name = (data.get("name") or "").strip()
            if kind not in ("people", "groups") or not name:
                return self._send(400, json.dumps({"ok": False, "message": "need kind + name"}))
            fn = re.sub(r"[^\w\s-]", "", name).strip().lower()
            fn = re.sub(r"[\s_-]+", "-", fn)[:60]
            if not fn:
                return self._send(400, json.dumps({"ok": False, "message": "name has no usable characters"}))
            dest = ROOT / "corpus" / kind / f"{fn}.md"
            if dest.exists():
                return self._send(400, json.dumps({"ok": False, "message": f"{fn}.md already exists"}))
            tpl = (f"# {name}\n\n"
                   "**Source:** hand-written\n\n## Context\n"
                   + ("Who they are and how they relate to me.\n\n## Communication style\n"
                      "How they write; tone to match when drafting.\n\n## Open threads\n- \n"
                      if kind == "people" else
                      "What this group is for.\n\n## Who's who\n- \n\n## Norms\n"
                      "Tone and what kind of message fits here.\n\n## Active threads\n- \n"))
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(tpl)
            return self._send(200, json.dumps({"ok": True, "message": f"Created {fn}.md",
                                               "path": str(dest.relative_to(ROOT))}))

        if u.path == "/api/group":
            jid = data.get("jid", "")
            if not jid.endswith("@g.us"):
                return self._send(400, json.dumps({"ok": False, "message": "bad group id"}))
            cfg = load_cfg()
            wa = cfg.setdefault("channels", {}).setdefault("whatsapp", {})
            g = wa.setdefault("groups", {}).setdefault(jid, {})
            g["requireMention"] = bool(data.get("requireMention", True))
            prompt = (data.get("systemPrompt") or "").strip()
            if prompt:
                g["systemPrompt"] = prompt
            else:
                g.pop("systemPrompt", None)
            ok, msg = save_cfg(cfg)
            return self._send(200 if ok else 400, json.dumps({"ok": ok, "message": msg}))

        if u.path == "/api/schedule":
            jid = data.get("jid", "")
            if not jid.endswith("@g.us"):
                return self._send(400, json.dumps({"ok": False, "message": "bad group id"}))
            short = jid.split("@")[0][-12:]
            job = SCHED_PREFIX + short
            existing = cron_jobs().get(jid)
            if existing:
                oc("cron", "rm", existing["id"], timeout=60)
            if not data.get("enabled"):
                return self._send(200, json.dumps({"ok": True, "message": "Schedule removed."}))
            msg_text = (data.get("message") or "").strip()
            if not msg_text:
                return self._send(400, json.dumps({"ok": False, "message": "instructions are required"}))
            args = ["cron", "add", "--name", job,
                    "--display-name", f"swamai schedule | {jid}",
                    "--description", "scheduled group message (swamai control panel)",
                    "--channel", "whatsapp", "--to", jid,
                    "--message", msg_text, "--announce", "--session", "isolated"]
            mode = data.get("mode", "every")
            if mode == "cron":
                args += ["--cron", data.get("cron", "0 9 * * *"), "--tz", data.get("tz", "America/New_York")]
            else:
                args += ["--every", data.get("every", "24h")]
            code, out = oc(*args, timeout=90)
            tail = (out or "").strip().splitlines()
            return self._send(200 if code == 0 else 400, json.dumps({
                "ok": code == 0,
                "message": "Schedule saved." if code == 0 else (tail[-1] if tail else "cron add failed")}))

        if u.path == "/api/reindex":
            code, out = oc("memory", "index", "--force", "--agent", "main", timeout=900)
            tail = out.strip().splitlines()[-1] if out.strip() else "done"
            return self._send(200, json.dumps({"ok": code == 0, "message": tail}))

        if u.path == "/api/restart":
            r = subprocess.run(["systemctl", "--user", "restart", "openclaw-gateway.service"],
                               capture_output=True, text=True)
            ok = r.returncode == 0
            return self._send(200, json.dumps({"ok": ok, "message": "Gateway restarting…" if ok
                                               else (r.stderr or "restart failed")}))
        return self._send(404, json.dumps({"error": "not found"}))


def _valid_re(p):
    try:
        re.compile(p)
        return True
    except re.error:
        return False


PAGE = r"""<!doctype html><meta charset=utf-8><title>Understudy</title>
<style>
:root{
  --bg:#0b0d12; --panel:#12151c; --panel-2:#171b24; --line:#232836;
  --ink:#e8ebf2; --ink-dim:#98a1b5; --ink-faint:#6b7488;
  --accent:#5b8dff; --accent-soft:#1c2740;
  --ok:#3fb950; --warn:#e3b341; --bad:#f85149;
  --radius:10px;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:14.5px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,system-ui,sans-serif;
  -webkit-font-smoothing:antialiased}
code{font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;
  background:var(--panel-2);padding:1px 5px;border-radius:4px;color:#c9d4ee}

header{display:flex;gap:14px;align-items:center;padding:14px 24px;
  background:rgba(11,13,18,.92);backdrop-filter:blur(8px);
  border-bottom:1px solid var(--line);position:sticky;top:0;z-index:20}
h1{margin:0;font-size:15.5px;font-weight:650;letter-spacing:-.01em}
h1 .tag{color:var(--ink-faint);font-weight:400;margin-left:8px;font-size:12.5px}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:12px;
  padding:3px 10px;border-radius:999px;background:var(--panel-2);
  border:1px solid var(--line);color:var(--ink-dim)}
.dot{width:7px;height:7px;border-radius:50%;flex:0 0 auto}
.on{background:var(--ok);box-shadow:0 0 0 3px rgba(63,185,80,.15)}
.off{background:var(--bad);box-shadow:0 0 0 3px rgba(248,81,73,.15)}

nav{display:flex;gap:2px;padding:0 24px;background:var(--bg);
  border-bottom:1px solid var(--line);position:sticky;top:57px;z-index:19}
nav button{background:none;border:0;color:var(--ink-dim);padding:12px 16px;
  cursor:pointer;font:inherit;font-size:14px;border-bottom:2px solid transparent;
  transition:color .15s,border-color .15s}
nav button:hover{color:var(--ink)}
nav button.sel{color:var(--ink);border-bottom-color:var(--accent);font-weight:550}

main{padding:26px 24px 60px;max-width:1040px}
section{display:none;animation:fade .18s ease}
section.sel{display:block}
@keyframes fade{from{opacity:0;transform:translateY(3px)}to{opacity:1}}

.card{background:var(--panel);border:1px solid var(--line);
  border-radius:var(--radius);padding:18px;margin-bottom:18px}
.card h3{margin:0 0 3px;font-size:14.5px;font-weight:600;letter-spacing:-.01em}
.lead{color:var(--ink-dim);font-size:13px;margin-bottom:16px}

.row{display:flex;gap:11px;align-items:center;padding:9px 11px;border-radius:8px;
  transition:background .12s}
.row:hover{background:var(--panel-2)}
.row.on{background:rgba(91,141,255,.07);box-shadow:inset 2px 0 0 var(--accent)}
.muted{color:var(--ink-dim);font-size:12.5px}
.count{color:var(--ink-faint);font-weight:400}

input[type=text],textarea,select{background:var(--panel-2);border:1px solid var(--line);
  color:var(--ink);border-radius:8px;padding:9px 11px;font:inherit;font-size:13.5px;
  width:100%;transition:border-color .15s,box-shadow .15s}
input[type=text]:focus,textarea:focus,select:focus{outline:0;border-color:var(--accent);
  box-shadow:0 0 0 3px var(--accent-soft)}
textarea{min-height:420px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  font-size:12.5px;line-height:1.65;resize:vertical}
input[type=checkbox]{accent-color:var(--accent);width:15px;height:15px;cursor:pointer}
label.f{display:block;margin:16px 0 6px;font-size:12.5px;color:var(--ink-dim);
  font-weight:600;letter-spacing:.01em}

button.act{background:var(--accent);border:0;color:#fff;padding:9px 17px;
  border-radius:8px;cursor:pointer;font:inherit;font-size:13.5px;font-weight:550;
  transition:background .15s,transform .05s}
button.act:hover{background:#6f9bff}
button.act:active{transform:translateY(1px)}
button.ghost{background:var(--panel-2);border:1px solid var(--line);color:var(--ink-dim)}
button.ghost:hover{background:#1d2230;color:var(--ink)}

.bar{position:sticky;bottom:0;background:linear-gradient(transparent,var(--bg) 26%);
  padding:18px 0 8px;display:flex;gap:10px;align-items:center;margin-top:18px}
#msg,#msg2,#msg3,#msg4,#msg5{font-size:13px}
.ok{color:var(--ok)}.err{color:var(--bad)}

.grid{display:grid;grid-template-columns:250px 1fr;gap:18px}
.list{max-height:520px;overflow:auto;border:1px solid var(--line);
  border-radius:var(--radius);padding:7px;background:var(--panel)}
.list button{display:block;width:100%;text-align:left;background:none;border:0;
  color:var(--ink-dim);padding:8px 10px;border-radius:7px;cursor:pointer;font:inherit;
  font-size:13.5px;transition:background .12s,color .12s}
.list button:hover{background:var(--panel-2);color:var(--ink)}
.list button.sel{background:var(--accent-soft);color:#fff;font-weight:550}

.danger{background:rgba(227,179,65,.07);border:1px solid rgba(227,179,65,.28);
  border-radius:8px;padding:12px 13px}
.danger b{color:var(--warn)}
#glist{max-height:460px;overflow:auto;margin-top:4px}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:#242a38;border-radius:6px;border:2px solid var(--bg)}
::-webkit-scrollbar-thumb:hover{background:#2e3546}
</style>
<header>
  <h1>Understudy<span class=tag>control panel</span></h1>
  <span id=health class=muted>checking…</span>
  <span style=flex:1></span>
  <button class="act ghost" onclick=restart()>Restart gateway</button>
</header>
<nav>
  <button data-t=groups class=sel>Groups</button>
  <button data-t=context>Context</button>
  <button data-t=settings>Settings</button>
</nav>
<main>
  <section id=groups class=sel>
    <p class=muted>Only enabled groups may reach the assistant — and it can only post where it's enabled. <b>Summon</b> means it stays silent unless a trigger phrase is used.</p>
    <input type=text id=gfilter placeholder="Filter groups…" oninput=renderGroups()>
    <p class=muted style=margin:10px_0><span id=gcount></span></p>
    <div id=glist></div>
    <div class=bar><button class=act onclick=saveGroups()>Save groups</button><span id=msg></span></div>

    <div id=gcfg class=card style="display:none;margin-top:26px">
      <h3>Configure <span id=gcfgname></span></h3>
      <div class=muted id=gcfgid style=margin-bottom:12px></div>

      <label class=row style="background:#132018;margin-bottom:10px">
        <input type=checkbox id=gRequireMention>
        <b>Only reply when summoned</b> — otherwise it answers every message in the group, including its own
      </label>

      <label class="row danger" style="margin-bottom:12px;align-items:flex-start">
        <input type=checkbox id=gImpersonate style=margin-top:3px>
        <div>
          <b>Write as me in this group</b>
          <div class=muted style=margin-top:3px>
            Off: messages carry the <code>🤖 Name:</code> marker.
            On: no marker, first person, in your voice — this group's members are not
            told a machine wrote it.
          </div>
        </div>
      </label>

      <label class=f>Instructions for this group</label>
      <div class=muted style=margin-bottom:6px>How the assistant should behave here — tone, what to focus on, what to avoid.</div>
      <textarea id=gprompt style=min-height:120px placeholder="e.g. This is a cycling group. Keep replies short and practical. Use metric distances. Never discuss work topics."></textarea>

      <label class=f>Scheduled messages</label>
      <label class=row><input type=checkbox id=schedOn onchange=schedToggle()> Send a message on a schedule</label>
      <div id=schedBox style=display:none>
        <div class=muted id=sendWarn style="margin:6px 0;color:#d29922"></div>
        <label class=f>What should it send?</label>
        <textarea id=schedMsg style=min-height:90px placeholder="e.g. Post a short summary of any unresolved plans from the last week, and ask if anyone wants a weekend ride."></textarea>
        <label class=f>How often</label>
        <div style="display:flex;gap:8px;align-items:center">
          <select id=schedMode onchange=schedModeChange() style=width:150px>
            <option value=every>Every…</option><option value=cron>At a set time</option>
          </select>
          <input type=text id=schedEvery placeholder="24h" style=max-width:120px>
          <input type=text id=schedCron placeholder="0 9 * * 1  (Mon 9am)" style="max-width:220px;display:none">
          <input type=text id=schedTz placeholder="America/New_York" style="max-width:200px;display:none">
        </div>
        <div class=muted id=schedNext style=margin-top:8px></div>
      </div>
      <div class=bar>
        <button class=act onclick=saveGroupCfg()>Save this group</button>
        <button class="act ghost" onclick="document.getElementById('gcfg').style.display='none'">Close</button>
        <span id=msg4></span>
      </div>
    </div>
  </section>

  <section id=context>
    <p class=muted>Free-text context the assistant retrieves. Markdown. Reindex after editing.</p>
    <div style="display:flex;gap:8px;align-items:center;margin:10px 0 14px">
      <select id=newkind style=width:130px><option value=people>Person</option><option value=groups>Group</option></select>
      <input type=text id=newname placeholder="Name, e.g. Alex Kim" style=max-width:260px>
      <button class="act ghost" onclick=newCtx()>+ Add context</button>
    </div>
    <div class=grid>
      <div class=list id=ctxlist></div>
      <div>
        <div class=muted id=ctxpath>select a file</div>
        <textarea id=ctxbody placeholder="# Name&#10;&#10;## Context&#10;Who they are and how they relate to you.&#10;&#10;## Communication style&#10;&#10;## Open threads"></textarea>
      </div>
    </div>
    <div class=bar>
      <button class=act onclick=saveCtx()>Save file</button>
      <button class="act ghost" onclick=reindex()>Reindex memory</button>
      <span id=msg2></span>
    </div>
  </section>

  <section id=settings>
    <div class=card>
      <h3>Identity</h3>
      <div class=lead>What it is called, and whether it signs its messages.</div>

      <label class=f>Assistant name (also the summon word in groups)</label>
      <input type=text id=assistantName placeholder="Assistant" style=max-width:320px>
      <div class=muted style=margin-top:5px>Saying this word in an enabled group summons it. The summon
        pattern is regenerated and checked so it cannot match the assistant's own messages.</div>

      <label class=f>Write as me in these DMs (one number per line)</label>
      <div class=muted style=margin-bottom:6px>Leave empty to sign every DM. Per-group
        impersonation is set in <b>Groups → Configure</b>.</div>
      <textarea id=impersonateDms style=min-height:70px placeholder="15551234567"></textarea>
      <div class=muted id=impWarn style="margin-top:8px;color:#d29922"></div>
      <div class=bar style=border:0;margin-top:10px;padding-bottom:0>
        <button class=act onclick=saveIdentity()>Save identity</button>
        <span id=msg5></span>
      </div>
    </div>

    <label class=f>Who may DM the assistant (one number per line)</label>
    <textarea id=allowFrom style=min-height:80px></textarea>
    <label class=f>DM policy</label>
    <select id=dmPolicy><option>allowlist<option>pairing<option>open<option>disabled</select>
    <label class=f>Group policy</label>
    <select id=groupPolicy><option>allowlist<option>open<option>disabled</select>
    <label class=f>Summon phrases — regex, one per line (groups only)</label>
    <textarea id=mentionPatterns style=min-height:90px></textarea>
    <label class=f>Unmentioned group messages</label>
    <select id=unmentionedInbound>
      <option value=room_event>room_event — read silently, never reply</option>
      <option value=user_request>user_request — treat every message as a request</option>
    </select>
    <label class=f>Attribution prefix on every message it sends</label>
    <div class=muted style=margin-bottom:6px>Prepended inline, e.g. "🤖 SwamAI Sure, I'll check." Leave empty for none. WhatsApp has no native footer option.</div>
    <input type=text id=responsePrefix placeholder="🤖 SwamAI">
    <label class=f>Group context messages per turn</label>
    <input type=text id=historyLimit>
    <div style=margin-top:16px>
      <label class=row><input type=checkbox id=selfChatMode> Self-chat mode (message yourself to command it)</label>
      <label class=row><input type=checkbox id=sendReadReceipts> Send read receipts</label>
      <label class=row><input type=checkbox id=sendMessage> <b>Allow unprompted outbound</b> — lets it start messages, not just reply</label>
    </div>
    <p class=muted style=margin-top:14px>Model: <span id=model></span></p>
    <div class=bar><button class=act onclick=saveSettings()>Save settings</button><span id=msg3></span></div>
  </section>
</main>
<script>
let S=null, curCtx=null;
const $=id=>document.getElementById(id);
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('nav button').forEach(x=>x.classList.toggle('sel',x===b));
  document.querySelectorAll('section').forEach(s=>s.classList.toggle('sel',s.id===b.dataset.t));
});
function flash(el,m,ok){el.textContent=m;el.className=ok?'ok':'err';setTimeout(()=>el.textContent='',6000)}

async function load(){
  S=await (await fetch('/api/state')).json();
  renderGroups(); renderCtx();
  const s=S.settings;
  $('allowFrom').value=(s.allowFrom||[]).join('\n');
  $('dmPolicy').value=s.dmPolicy; $('groupPolicy').value=s.groupPolicy;
  $('mentionPatterns').value=(s.mentionPatterns||[]).join('\n');
  $('unmentionedInbound').value=s.unmentionedInbound;
  $('historyLimit').value=s.historyLimit;
  $('selfChatMode').checked=s.selfChatMode;
  $('sendReadReceipts').checked=s.sendReadReceipts;
  $('sendMessage').checked=s.sendMessage;
  $('responsePrefix').value=s.responsePrefix||'';
  $('assistantName').value=s.assistantName||'';
  $('impersonateDms').value=Object.keys(s.impersonateDms||{}).filter(k=>s.impersonateDms[k]).join('\n');
  const nImp=Object.values(s.impersonateGroups||{}).filter(Boolean).length
            +Object.values(s.impersonateDms||{}).filter(Boolean).length;
  $('impWarn').textContent = nImp
    ? `Impersonating in ${nImp} conversation(s). The 🤖 marker is off channel-wide — `
      +`OpenClaw applies responsePrefix globally, so signed conversations now rely on `
      +`an instruction the model can miss.`
    : '';
  $('model').textContent=s.model||'—';
  health();
}
async function health(){
  try{const h=await (await fetch('/api/status')).json();
    $('health').innerHTML=
      `<span class=pill><span class="dot ${h.gateway?'on':'off'}"></span>gateway</span>`+
      `<span class=pill style=margin-left:8px><span class="dot ${h.whatsappLinked?'on':'off'}"></span>whatsapp</span>`;
  }catch(e){$('health').textContent='status unavailable'}
}
function renderGroups(){
  const f=($('gfilter').value||'').toLowerCase();
  const on=new Set(S.allowFromGroups);
  const list=S.groups.filter(g=>!f||g.name.toLowerCase().includes(f)||g.id.includes(f));
  $('gcount').textContent=`${on.size} enabled of ${S.groups.length} groups`+
    (S.settings.allowFromSenders&&S.settings.allowFromSenders.length
      ? `  ·  ${S.settings.allowFromSenders.length} allowed sender(s)` : '');
  $('glist').innerHTML=list.map(g=>{
    const en=on.has(g.id), rm=S.requireMention[g.id]!==false;
    return `<div class="row ${en?'on':''}">
      <input type=checkbox ${en?'checked':''} onchange=togGroup('${g.id}',this.checked)>
      <div style=flex:1><div>${esc(g.name)}</div><div class=muted>${g.id}</div></div>
      <label class=muted style=white-space:nowrap>
        <input type=checkbox ${rm?'checked':''} ${en?'':'disabled'} onchange=togRM('${g.id}',this.checked)> summon only
      </label>
      ${en?`<button class="act ghost" style=padding:4px_10px onclick="cfgGroup('${g.id}')">Configure${S.schedules[g.id]?' ⏱':''}${(S.groupPrompts[g.id]||'').trim()?' ✎':''}</button>`:''}
      </div>`}).join('')||'<p class=muted>no matches</p>';
}
const esc=s=>s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function togGroup(id,on){
  const s=new Set(S.allowFromGroups);
  on?s.add(id):s.delete(id);
  S.allowFromGroups=[...s];
  if(on&&S.requireMention[id]===undefined)S.requireMention[id]=true;
  renderGroups();
}
function togRM(id,v){S.requireMention[id]=v}

let cfgJid=null;
function cfgGroup(jid){
  cfgJid=jid;
  const g=S.groups.find(x=>x.id===jid)||{name:jid};
  $('gcfgname').textContent=g.name; $('gcfgid').textContent=jid;
  $('gprompt').value=S.groupPrompts[jid]||'';
  $('gRequireMention').checked=S.requireMention[jid]!==false;
  $('gImpersonate').checked=!!(S.settings.impersonateGroups||{})[jid];
  const sc=S.schedules[jid];
  $('schedOn').checked=!!sc;
  $('schedMsg').value=sc?.message||'';
  $('schedEvery').value=sc?.freq&&/^[0-9]+h$/.test(sc.freq)?sc.freq:'24h';
  $('schedCron').value=sc&&!/^[0-9]+h$/.test(sc.freq)?sc.freq:'0 9 * * 1';
  $('schedTz').value='America/New_York';
  $('schedMode').value=sc&&!/^[0-9]+h$/.test(sc.freq)?'cron':'every';
  $('schedNext').textContent=sc?.nextRunAtMs?('next run: '+new Date(sc.nextRunAtMs).toLocaleString()):'';
  $('sendWarn').textContent=S.settings.sendMessage?'':
    'Note: "Allow unprompted outbound" is off in Settings. Scheduled sends deliver via the channel, but enable it if messages do not appear.';
  schedToggle(); schedModeChange();
  $('gcfg').style.display='block';
  $('gcfg').scrollIntoView({behavior:'smooth',block:'start'});
}
function schedToggle(){$('schedBox').style.display=$('schedOn').checked?'block':'none'}
function schedModeChange(){
  const c=$('schedMode').value==='cron';
  $('schedEvery').style.display=c?'none':'';
  $('schedCron').style.display=c?'':'none';
  $('schedTz').style.display=c?'':'none';
}
async function saveGroupCfg(){
  if(!cfgJid)return;
  const rm=$('gRequireMention').checked;
  S.requireMention[cfgJid]=rm;
  const r1=await post('/api/group',{jid:cfgJid,requireMention:rm,systemPrompt:$('gprompt').value});
  if(!r1.ok)return flash($('msg4'),r1.message,false);
  const imp=$('gImpersonate').checked;
  if(imp && !(S.settings.impersonateGroups||{})[cfgJid] && !confirm(
      'Write as you in this group?\n\nMessages will be sent in your voice with no '+
      'marker. Members are not told an AI wrote them.\n\nThis also removes the '+
      'attribution prefix for EVERY conversation — OpenClaw applies it channel-wide.'))
    return;
  const groups=Object.assign({},S.settings.impersonateGroups||{}); groups[cfgJid]=imp;
  const ri=await post('/api/identity',{name:S.settings.assistantName,impersonateGroups:groups,
                                       impersonateDms:S.settings.impersonateDms||{}});
  if(!ri.ok)return flash($('msg4'),ri.message,false);
  if(ri.warning) flash($('msg4'),'⚠ '+ri.warning,false);
  const r2=await post('/api/schedule',{jid:cfgJid,enabled:$('schedOn').checked,
    message:$('schedMsg').value,mode:$('schedMode').value,
    every:$('schedEvery').value,cron:$('schedCron').value,tz:$('schedTz').value});
  flash($('msg4'),r2.ok?(r1.message+' '+r2.message):r2.message,r2.ok);
  if(r2.ok){S=await (await fetch('/api/state')).json();renderGroups()}
}
async function newCtx(){
  const name=$('newname').value.trim();
  if(!name)return flash($('msg2'),'enter a name',false);
  const r=await post('/api/context/new',{kind:$('newkind').value,name});
  flash($('msg2'),r.message,r.ok);
  if(r.ok){$('newname').value='';S=await (await fetch('/api/state')).json();renderCtx();openCtx(r.path)}
}
async function saveGroups(){
  const r=await post('/api/groups',{allow:S.allowFromGroups,requireMention:S.requireMention});
  flash($('msg'),r.message,r.ok);
}
function renderCtx(){
  $('ctxlist').innerHTML=S.contextFiles.map(f=>
    `<button data-p="${f.path}" onclick="openCtx('${f.path}')">${esc(f.name)} <span class=muted>· ${f.kind}</span></button>`
  ).join('')||'<p class=muted style=padding:8px>no context files yet — run ./ingest/run.sh</p>';
}
async function openCtx(p){
  curCtx=p;
  document.querySelectorAll('#ctxlist button').forEach(b=>b.classList.toggle('sel',b.dataset.p===p));
  const r=await (await fetch('/api/context?path='+encodeURIComponent(p))).json();
  $('ctxpath').textContent=p; $('ctxbody').value=r.content||'';
}
async function saveCtx(){
  if(!curCtx)return flash($('msg2'),'select a file first',false);
  const r=await post('/api/context',{path:curCtx,content:$('ctxbody').value});
  flash($('msg2'),r.message,r.ok);
}
async function reindex(){
  flash($('msg2'),'reindexing…',true);
  const r=await post('/api/reindex',{}); flash($('msg2'),r.message,r.ok);
}
async function saveIdentity(){
  const name=$('assistantName').value.trim();
  const dms={};
  $('impersonateDms').value.split('\n').map(x=>x.replace(/\D/g,'')).filter(Boolean)
    .forEach(n=>dms[n]=true);
  if(Object.keys(dms).length && !confirm(
      'Write as you in '+Object.keys(dms).length+' DM(s)?\n\n'+
      'Those messages carry no marker and are written in your voice. Recipients are '+
      'not told an AI wrote them.')) return;
  const r=await post('/api/identity',{name,impersonateGroups:S.settings.impersonateGroups||{},
                                      impersonateDms:dms});
  flash($('msg5'), r.ok ? (r.message+(r.warning?'  ⚠ '+r.warning:'')) : r.message, r.ok);
  if(r.ok){S=await (await fetch('/api/state')).json();$('responsePrefix').value=S.settings.responsePrefix||''}
}
async function saveSettings(){
  const lines=id=>$(id).value.split('\n').map(s=>s.trim()).filter(Boolean);
  const r=await post('/api/settings',{
    allowFrom:lines('allowFrom'),dmPolicy:$('dmPolicy').value,groupPolicy:$('groupPolicy').value,
    mentionPatterns:lines('mentionPatterns'),unmentionedInbound:$('unmentionedInbound').value,
    historyLimit:$('historyLimit').value,selfChatMode:$('selfChatMode').checked,
    responsePrefix:$('responsePrefix').value,
    sendReadReceipts:$('sendReadReceipts').checked,sendMessage:$('sendMessage').checked});
  flash($('msg3'),r.message,r.ok);
}
async function restart(){const r=await post('/api/restart',{});flash($('health'),r.message,r.ok);setTimeout(health,9000)}
async function post(u,b){return (await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)})).json()}
load();
</script>"""

if __name__ == "__main__":
    if not CFG.exists():
        sys.exit(f"no config at {CFG}")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    print(f"swamai control → http://127.0.0.1:{PORT}   (loopback only, ctrl-c to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
