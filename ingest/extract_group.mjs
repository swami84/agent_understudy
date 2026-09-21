#!/usr/bin/env node
/**
 * Extract group participants (and whatever history WhatsApp pushes on connect)
 * for ALLOWLISTED groups only, using the WhatsApp plugin's own Baileys install
 * and the existing linked-device credentials.
 *
 * Safety rules enforced here:
 *   - refuses to run while the gateway holds the session (duplicate connections
 *     on one set of creds are what cause 401/logout loops)
 *   - only touches JIDs present in channels.whatsapp.groupAllowFrom
 *   - read-only: never sends a message, never calls logout() (that would
 *     unlink the device); disconnects with end()
 *
 * Usage:
 *   systemctl --user stop openclaw-gateway.service
 *   node ingest/extract_group.mjs
 *   node ingest/extract_group.mjs --group 120363...@g.us --settle 45
 *   systemctl --user start openclaw-gateway.service
 */
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

const PLUGIN = path.join(process.env.HOME, ".openclaw/extensions/whatsapp");
const AUTH = path.join(process.env.HOME, ".openclaw/credentials/whatsapp/default");
const CONFIG = path.join(process.env.HOME, ".openclaw/openclaw.json");
const OUT = path.resolve("corpus");

const argv = process.argv.slice(2);
const flag = (n, d) => {
  const i = argv.indexOf(`--${n}`);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : d;
};
const SETTLE_MS = Number(flag("settle", "40")) * 1000;
const ONLY = flag("group", null);

function die(msg) {
  console.error(`\n✗ ${msg}\n`);
  process.exit(1);
}

// --- guard: gateway must not own the session -------------------------------
try {
  const state = execFileSync("systemctl", ["--user", "is-active", "openclaw-gateway.service"], {
    encoding: "utf8",
  }).trim();
  if (state === "active") {
    die(
      "The gateway is running and owns the WhatsApp session.\n" +
        "  Stop it first:  systemctl --user stop openclaw-gateway.service\n" +
        "  Restart after:  systemctl --user start openclaw-gateway.service"
    );
  }
} catch (e) {
  if (e?.status === undefined) throw e; // is-active exits non-zero when inactive: fine
}

// --- scope: allowlisted groups only ----------------------------------------
const cfg = JSON.parse(fs.readFileSync(CONFIG, "utf8"));
const allowed = cfg?.channels?.whatsapp?.groupAllowFrom ?? [];
if (!allowed.length) die("channels.whatsapp.groupAllowFrom is empty. Nothing is permitted.");
if (ONLY && !allowed.includes(ONLY)) die(`Group ${ONLY} is not in groupAllowFrom. Refusing.`);
const targets = ONLY ? [ONLY] : allowed;

if (!fs.existsSync(path.join(AUTH, "creds.json"))) die(`No credentials at ${AUTH}. Link WhatsApp first.`);

const { makeWASocket, useMultiFileAuthState, makeCacheableSignalKeyStore, fetchLatestBaileysVersion } =
  await import(path.join(PLUGIN, "node_modules/baileys/lib/index.js"));

const quiet = {
  level: "silent", child: () => quiet,
  trace() {}, debug() {}, info() {}, warn() {}, error() {}, fatal() {},
};

const slug = (s) =>
  (s || "group").replace(/[^\w\s-]/g, "").trim().toLowerCase().replace(/[\s_-]+/g, "-").slice(0, 60) || "group";

const { state, saveCreds } = await useMultiFileAuthState(AUTH);
const { version } = await fetchLatestBaileysVersion();

console.log(`Baileys v${version.join(".")} · ${targets.length} permitted group(s)`);

const sock = makeWASocket({
  auth: { creds: state.creds, keys: makeCacheableSignalKeyStore(state.keys, quiet) },
  version,
  logger: quiet,
  printQRInTerminal: false,
  // Match the plugin's fingerprint exactly — a differing browser triple on the
  // same credentials looks like a new device.
  browser: ["openclaw", "cli", "2026.9.1"],
  syncFullHistory: false,
  markOnlineOnConnect: false,
});

sock.ev.on("creds.update", saveCreds);

// Capture whatever history the server pushes during initial sync.
const histMsgs = [];
const names = new Map(); // phone/lid -> display name

const learn = (id, name) => {
  if (!id || !name) return;
  const key = String(id).replace(/@.*/, "");
  if (!names.has(key)) names.set(key, name);
};

sock.ev.on("messaging-history.set", ({ messages = [], contacts = [] }) => {
  for (const c of contacts) learn(c.id, c.name || c.notify || c.verifiedName);
  for (const m of messages) {
    if (m?.key?.remoteJid && targets.includes(m.key.remoteJid)) histMsgs.push(m);
    // group messages carry the sender's self-set display name
    if (m?.pushName && m?.key?.participant) learn(m.key.participant, m.pushName);
  }
});
sock.ev.on("contacts.upsert", (cs) => { for (const c of cs) learn(c.id, c.name || c.notify || c.verifiedName); });
sock.ev.on("contacts.update", (cs) => { for (const c of cs) learn(c.id, c.name || c.notify || c.verifiedName); });

const opened = new Promise((resolve, reject) => {
  const t = setTimeout(() => reject(new Error("timed out waiting for connection")), 90_000);
  sock.ev.on("connection.update", (u) => {
    if (u.qr) {
      clearTimeout(t);
      reject(new Error("WhatsApp asked for a QR — the session is no longer linked. Re-run: openclaw channels login --channel whatsapp"));
    }
    if (u.connection === "open") { clearTimeout(t); resolve(); }
    if (u.connection === "close") {
      const code = u.lastDisconnect?.error?.output?.statusCode;
      clearTimeout(t);
      reject(new Error(`connection closed before open (status ${code ?? "unknown"})`));
    }
  });
});

try {
  await opened;
  console.log("connected\n");

  console.log(`settling ${SETTLE_MS / 1000}s to collect contact names…`);
  await new Promise((r) => setTimeout(r, SETTLE_MS));
  console.log(`learned ${names.size} contact name(s)\n`);

  fs.mkdirSync(path.join(OUT, "people"), { recursive: true });
  fs.mkdirSync(path.join(OUT, "groups"), { recursive: true });

  const roster = new Map(); // id -> record, deduped across groups

  for (const jid of targets) {
    let meta;
    try {
      meta = await sock.groupMetadata(jid);
    } catch (e) {
      console.error(`  ✗ ${jid}: ${e?.message ?? e}`);
      continue;
    }
    const parts = meta.participants ?? [];
    console.log(`${meta.subject} — ${parts.length} participants`);

    const rows = parts.map((p) => {
      const phone = (p.phoneNumber || (String(p.id).includes("@s.whatsapp.net") ? p.id : "") || "")
        .replace(/@.*/, "");
      const rec = {
        id: p.id,
        phone,
        lid: p.lid || (String(p.id).includes("@lid") ? p.id : ""),
        name:
          p.name || p.notify || p.verifiedName ||
          names.get(String(p.id).replace(/@.*/, "")) ||
          names.get(String(p.phoneNumber || "").replace(/@.*/, "")) ||
          names.get(String(p.lid || "").replace(/@.*/, "")) || "",
        admin: p.admin || (p.isSuperAdmin ? "superadmin" : p.isAdmin ? "admin" : ""),
      };
      const key = rec.phone || rec.id;
      const prev = roster.get(key) ?? { ...rec, groups: [] };
      prev.name ||= rec.name;
      prev.phone ||= rec.phone;
      prev.groups.push(meta.subject);
      roster.set(key, prev);
      return rec;
    });

    const md = [
      `# ${meta.subject}`,
      "",
      `**Group JID:** \`${meta.id}\``,
      meta.desc ? `**Description:** ${meta.desc}` : null,
      `**Participants:** ${parts.length}`,
      meta.creation ? `**Created:** ${new Date(meta.creation * 1000).toISOString().slice(0, 10)}` : null,
      "",
      "## Members",
      "",
      "| Name | Phone | Role |",
      "| --- | --- | --- |",
      ...rows.map((r) => `| ${r.name || "_(unknown)_"} | ${r.phone ? "+" + r.phone : "_(lid only)_"} | ${r.admin || "member"} |`),
      "",
    ].filter(Boolean).join("\n");

    fs.writeFileSync(path.join(OUT, "groups", `${slug(meta.subject)}.md`), md);
    fs.writeFileSync(
      path.join(OUT, "groups", `${slug(meta.subject)}.json`),
      JSON.stringify({ id: meta.id, subject: meta.subject, desc: meta.desc, participants: rows }, null, 2)
    );
  }

  fs.writeFileSync(
    path.join(OUT, "groups", "_roster.json"),
    JSON.stringify([...roster.values()], null, 2)
  );
  console.log(`\nroster: ${roster.size} unique people -> corpus/groups/_roster.json`);

  if (histMsgs.length) {
    const byChat = {};
    for (const m of histMsgs) (byChat[m.key.remoteJid] ??= []).push(m);
    fs.writeFileSync(path.join(OUT, "groups", "_pushed_history.json"), JSON.stringify(byChat, null, 2));
    const oldest = histMsgs.reduce((a, b) =>
      Number(a.messageTimestamp ?? 0) < Number(b.messageTimestamp ?? 0) ? a : b);
    console.log(`pushed history: ${histMsgs.length} message(s) -> corpus/groups/_pushed_history.json`);
    console.log(`oldest anchor: ${new Date(Number(oldest.messageTimestamp) * 1000).toISOString()}`);
  } else {
    console.log("pushed history: none (expected — the plugin sets syncFullHistory:false)");
  }
} catch (e) {
  console.error(`\n✗ ${e?.message ?? e}`);
  process.exitCode = 1;
} finally {
  // end(), never logout() — logout would unlink the device.
  try { await sock.end(undefined); } catch {}
  console.log("\ndisconnected (session intact)");
  setTimeout(() => process.exit(process.exitCode ?? 0), 500);
}
