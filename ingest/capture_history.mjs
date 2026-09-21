#!/usr/bin/env node
/**
 * Link a SEPARATE WhatsApp device with syncFullHistory enabled, capture the
 * history push, and write it out in WhatsApp "Export chat" format so the
 * existing parser consumes it unchanged.
 *
 * This does NOT touch the gateway's session: it uses its own auth directory,
 * so it registers as an additional linked device (WhatsApp allows 4). Your
 * assistant keeps running while this captures.
 *
 * Usage:
 *   node ingest/capture_history.mjs                 # allowlisted groups only
 *   node ingest/capture_history.mjs --all           # every chat WhatsApp pushes
 *   node ingest/capture_history.mjs --settle 180    # wait longer for the push
 *   node ingest/capture_history.mjs --backfill 500  # page further back per chat
 *   node ingest/capture_history.mjs --reset         # forget device, pair fresh
 *
 * When finished you can remove the device from your phone:
 *   WhatsApp -> Settings -> Linked Devices -> "openclaw-history" -> Log out
 */
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

const HOME = process.env.HOME;
const PLUGIN = path.join(HOME, ".openclaw/extensions/whatsapp");
const OCROOT = path.join(HOME, ".nvm/versions/node/v26.8.1/lib/node_modules/openclaw");
const AUTH_DEFAULT = path.join(HOME, ".openclaw/credentials/whatsapp/default");
const AUTH_SEPARATE = path.join(HOME, ".openclaw/credentials/whatsapp/history-capture");
const CONFIG = path.join(HOME, ".openclaw/openclaw.json");
const RAW = path.resolve("corpus/raw");

const argv = process.argv.slice(2);
const has = (n) => argv.includes(`--${n}`);
const val = (n, d) => { const i = argv.indexOf(`--${n}`); return i >= 0 && argv[i + 1] ? argv[i + 1] : d; };
const SETTLE_MS = Number(val("settle", "120")) * 1000;
const BACKFILL = Number(val("backfill", "0"));
const ALL = has("all");
// By default pair into the gateway's own auth dir: one linked device, and the
// gateway inherits the session afterwards. --separate-device keeps the old
// behaviour (an extra device) for when a working session must not be touched.
const SEPARATE = has("separate-device");
const AUTH = SEPARATE ? AUTH_SEPARATE : AUTH_DEFAULT;

if (!SEPARATE) {
  let active = "";
  try {
    active = execFileSync("systemctl", ["--user", "is-active", "openclaw-gateway.service"], { encoding: "utf8" }).trim();
  } catch (e) { if (e?.status === undefined) throw e; }
  if (active === "active") {
    console.error("\nThe gateway is running and owns these credentials.");
    console.error("  Stop it:     systemctl --user stop openclaw-gateway.service");
    console.error("  Then re-run. Start it again afterwards and it inherits this session.\n");
    process.exit(1);
  }
}

if (has("reset") && fs.existsSync(AUTH)) {
  fs.rmSync(AUTH, { recursive: true, force: true });
  console.log("cleared previous capture-device credentials");
}
fs.mkdirSync(AUTH, { recursive: true });
fs.mkdirSync(RAW, { recursive: true });

const cfg = JSON.parse(fs.readFileSync(CONFIG, "utf8"));
const allowedGroups = cfg?.channels?.whatsapp?.groupAllowFrom ?? [];
const allowedDms = (cfg?.channels?.whatsapp?.allowFrom ?? []).map((s) => String(s).replace(/\D/g, ""));

const { makeWASocket, useMultiFileAuthState, makeCacheableSignalKeyStore, fetchLatestBaileysVersion } =
  await import(path.join(PLUGIN, "node_modules/baileys/lib/index.js"));
const QR = (await import(path.join(OCROOT, "node_modules/qrcode/lib/index.js"))).default;

const quiet = { level: "silent", child: () => quiet, trace(){}, debug(){}, info(){}, warn(){}, error(){}, fatal(){} };

// ---- helpers ---------------------------------------------------------------
const digits = (s) => String(s || "").replace(/\D/g, "");
const jidPhone = (jid) => digits(String(jid || "").split("@")[0].split(":")[0]);

// name resolution: roster + address-book map built by import_contacts.py
const names = new Map();
for (const f of ["corpus/groups/_contacts.json", "corpus/groups/_roster.json"]) {
  try {
    const j = JSON.parse(fs.readFileSync(path.resolve(f), "utf8"));
    if (Array.isArray(j)) for (const r of j) { if (r.phone && r.name) names.set(digits(r.phone), r.name); }
    else for (const [k, v] of Object.entries(j)) names.set(digits(k), v);
  } catch {}
}

function extractText(m) {
  const c = m?.message;
  if (!c) return null;
  if (c.conversation) return c.conversation;
  if (c.extendedTextMessage?.text) return c.extendedTextMessage.text;
  for (const [k, label] of [["imageMessage","image"],["videoMessage","video"],["audioMessage","audio"],
                            ["documentMessage","document"],["stickerMessage","sticker"]]) {
    if (c[k]) return c[k].caption || `<${label} omitted>`;
  }
  if (c.ephemeralMessage) return extractText({ message: c.ephemeralMessage.message });
  if (c.viewOnceMessageV2) return extractText({ message: c.viewOnceMessageV2.message });
  if (c.reactionMessage) return null;
  if (c.protocolMessage) return null;
  return null;
}

function senderName(m, selfPhone) {
  if (m.key?.fromMe) return names.get(selfPhone) || "You";
  const isGroup = String(m.key?.remoteJid || "").endsWith("@g.us");
  // In groups the sender lives in participant-ish fields. NEVER fall back to
  // remoteJid there — that is the group itself, which mis-attributes every
  // message to the group name.
  const cand = m.key?.participant || m.key?.participantPn || m.key?.participantAlt
            || m.participant || (isGroup ? "" : m.key?.remoteJid) || "";
  const phone = jidPhone(cand);
  return names.get(phone) || m.pushName || (phone ? `+${phone}` : "Unknown");
}

function fmtTs(sec) {
  const d = new Date(Number(sec) * 1000);
  const p = (n) => String(n).padStart(2, "0");
  let h = d.getHours(); const ap = h >= 12 ? "PM" : "AM"; h = h % 12 || 12;
  return `${d.getMonth()+1}/${d.getDate()}/${String(d.getFullYear()).slice(2)}, ${h}:${p(d.getMinutes())}:${p(d.getSeconds())} ${ap}`;
}

const slug = (s) => (s || "chat").replace(/[^\w\s-]/g, "").trim().replace(/\s+/g, " ").slice(0, 60) || "chat";

// ---- connect ---------------------------------------------------------------
const { state, saveCreds } = await useMultiFileAuthState(AUTH);
const { version } = await fetchLatestBaileysVersion();

console.log(`Baileys v${version.join(".")} · syncFullHistory=true · auth=${path.basename(AUTH)}`);
console.log(state.creds?.registered ? "Reusing existing credentials.\n" : "No credentials yet — a QR will appear below.\n");

// ---- shared capture state (survives socket restarts) -----------------------
const chats = new Map();
const chatNames = new Map();
let pushes = 0, captured = 0, latest = false, qrShown = false;

function attach(sock) {
  sock.ev.on("creds.update", saveCreds);
  sock.ev.on("messaging-history.set", ({ chats: cs = [], contacts = [], messages = [], isLatest, progress }) => {
    pushes++;
    for (const c of contacts) {
      const ph = digits(c.id?.split("@")[0]);
      const n = c.name || c.notify || c.verifiedName;
      if (ph && n && !names.has(ph)) names.set(ph, n);
    }
    for (const c of cs) if (c.id && (c.name || c.subject)) chatNames.set(c.id, c.name || c.subject);
    for (const m of messages) {
      const jid = m.key?.remoteJid;
      if (!jid) continue;
      if (!ALL) {
        const isGroup = jid.endsWith("@g.us");
        const ok = isGroup ? allowedGroups.includes(jid) : allowedDms.includes(jidPhone(jid));
        if (!ok) continue;
      }
      if (!chats.has(jid)) chats.set(jid, { msgs: [] });
      chats.get(jid).msgs.push(m);
      captured++;
    }
    if (isLatest) latest = true;
    const pct = typeof progress === "number" ? ` ${progress}%` : "";
    process.stdout.write(`\r  push #${pushes}${pct} · ${captured} message(s) kept · ${chats.size} chat(s)      `);
  });
}

function newSocket() {
  const sock = makeWASocket({
    auth: { creds: state.creds, keys: makeCacheableSignalKeyStore(state.keys, quiet) },
    version,
    logger: quiet,
    printQRInTerminal: false,
    browser: SEPARATE ? ["openclaw-history", "cli", "2026.9.1"] : ["openclaw", "cli", "2026.9.1"],
    syncFullHistory: true,
    markOnlineOnConnect: false,
    // History sync is heavy; the defaults are too tight and cause the phone to
    // report "Couldn't log in" while the initial queries are still running.
    connectTimeoutMs: 120_000,
    defaultQueryTimeoutMs: 120_000,
    keepAliveIntervalMs: 25_000,
    retryRequestDelayMs: 500,
  });
  attach(sock);
  return sock;
}

/** Resolve 'open', or 'restart' when WhatsApp asks us to reconnect (515/428). */
function waitForOpen(sock) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("timed out waiting for pairing (5 min)")), 300_000);
    const done = (v) => { clearTimeout(timer); resolve(v); };
    sock.ev.on("connection.update", async (u) => {
      if (u.qr && !qrShown) {
        qrShown = true;
        console.log(await QR.toString(u.qr, { type: "terminal", small: true }));
        console.log("Scan: WhatsApp → Settings → Linked Devices → Link a device");
        console.log(SEPARATE
          ? "(adds a second device; the assistant's session is unaffected)\n"
          : "(this becomes the assistant's session too — only one device)\n");
      }
      if (u.connection === "connecting") process.stdout.write("\r  connecting…            ");
      if (u.connection === "open") { console.log("\r  connected.               "); return done("open"); }
      if (u.connection === "close") {
        const code = u.lastDisconnect?.error?.output?.statusCode;
        // 515 = restartRequired (ALWAYS follows a successful QR pair)
        // 428 = connectionClosed
        if (code === 515 || code === 428) { console.log("\r  pairing accepted — reconnecting…"); return done("restart"); }
        if (code === 401 || code === 403) { clearTimeout(timer); return reject(new Error(`logged out (status ${code}) — re-run with --reset`)); }
        clearTimeout(timer);
        return reject(new Error(`connection closed (status ${code ?? "unknown"})`));
      }
    });
  });
}

let sock = null;
for (let attempt = 1; attempt <= 6; attempt++) {
  sock = newSocket();
  const outcome = await waitForOpen(sock);
  if (outcome === "open") break;
  try { await sock.end(undefined); } catch {}
  await new Promise((r) => setTimeout(r, 1500));
  if (attempt === 6) throw new Error("could not establish a session after 6 attempts");
}

let exitCode = 0;
try {
  const selfPhone = jidPhone(state.creds?.me?.id);
  console.log(`connected as +${selfPhone}\n`);
  console.log(ALL ? "capturing ALL chats" : `capturing allowlisted only (${allowedGroups.length} group(s), ${allowedDms.length} DM number(s))`);
  console.log(`waiting up to ${SETTLE_MS / 1000}s for the history push…\n`);

  const started = Date.now();
  while (Date.now() - started < SETTLE_MS && !latest) await new Promise((r) => setTimeout(r, 1000));
  console.log(`\n\nhistory push ${latest ? "complete" : "settled (timeout)"} — ${pushes} push(es), ${captured} message(s)`);

  if (BACKFILL > 0 && chats.size) {
    console.log(`\nbackfilling up to ${BACKFILL} older message(s) per chat…`);
    for (const [jid, entry] of chats) {
      const sorted = entry.msgs.filter((m) => m.messageTimestamp)
        .sort((a, b) => Number(a.messageTimestamp) - Number(b.messageTimestamp));
      const oldest = sorted[0];
      if (!oldest) continue;
      try {
        await sock.fetchMessageHistory(BACKFILL, oldest.key, oldest.messageTimestamp);
        console.log(`  requested older history for ${chatNames.get(jid) || jid}`);
      } catch (e) {
        console.log(`  backfill failed for ${jid}: ${e?.message ?? e}`);
      }
    }
    console.log("  waiting 60s for backfill pushes…");
    await new Promise((r) => setTimeout(r, 60_000));
    console.log(`  now ${captured} message(s) total`);
  }

  if (!captured) {
    console.log("\nNothing captured. If this device was already linked, re-run with --reset to pair fresh —");
    console.log("the history push only happens on an INITIAL link, not on reconnects.");
  }

  // Dump raw JSON first: pairing is expensive (history only pushes on an
  // INITIAL link), so never let a formatting bug cost another QR scan.
  try {
    const rawDir = path.resolve("corpus/raw/_capture_json");
    fs.mkdirSync(rawDir, { recursive: true });
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    fs.writeFileSync(
      path.join(rawDir, `capture-${stamp}.json`),
      JSON.stringify({
        capturedAt: new Date().toISOString(),
        selfPhone,
        chatNames: Object.fromEntries(chatNames),
        chats: Object.fromEntries([...chats].map(([j, e]) => [j, e.msgs])),
      }, null, 2)
    );
    console.log(`  raw JSON -> corpus/raw/_capture_json/capture-${stamp}.json`);
  } catch (e) {
    console.log(`  (raw JSON dump failed: ${e?.message ?? e})`);
  }

  let written = 0;
  for (const [jid, entry] of chats) {
    const name = chatNames.get(jid) || names.get(jidPhone(jid)) || jid.split("@")[0];
    const seen = new Set();
    const lines = [];
    for (const m of entry.msgs.sort((a, b) => Number(a.messageTimestamp||0) - Number(b.messageTimestamp||0))) {
      const id = m.key?.id;
      if (id && seen.has(id)) continue;
      if (id) seen.add(id);
      const text = extractText(m);
      if (!text || !m.messageTimestamp) continue;
      lines.push(`[${fmtTs(m.messageTimestamp)}] ${senderName(m, selfPhone)}: ${text.replace(/\r?\n/g, "\n")}`);
    }
    if (!lines.length) continue;
    const file = path.join(RAW, `WhatsApp Chat with ${slug(name)}.txt`);
    fs.writeFileSync(file, lines.join("\n") + "\n", "utf8");
    console.log(`  wrote ${lines.length.toString().padStart(6)} msg(s) -> ${path.basename(file)}`);
    written++;
  }
  console.log(`\n${written} chat file(s) in corpus/raw/`);
  if (written) console.log("next:  ./ingest/run.sh");
} catch (e) {
  console.error(`\n✗ ${e?.message ?? e}`);
  exitCode = 1;
} finally {
  try { await sock.end(undefined); } catch {}   // end(), never logout()
  console.log(SEPARATE
    ? "\ndisconnected (extra capture device remains linked; remove it from your phone when done)"
    : "\ndisconnected — this pairing IS the gateway's session.\n  Start it:  systemctl --user start openclaw-gateway.service");
  setTimeout(() => process.exit(exitCode), 500);
}
