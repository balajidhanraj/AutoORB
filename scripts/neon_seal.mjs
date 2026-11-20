/**
 * Neon Realtime Query Sealer & Build Validator for AutoORB
 * Uses @neon/realtime/server (createRealtime, rawSql) with NEON_REALTIME_SECRET.
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createRealtime, rawSql } from "@neon/realtime/server";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT_DIR = path.resolve(__dirname, "..");

function loadDotEnv() {
  const envPath = path.join(ROOT_DIR, ".env");
  if (!fs.existsSync(envPath)) return;
  const lines = fs.readFileSync(envPath, "utf8").split(/\r?\n/);
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const eqIdx = trimmed.indexOf("=");
    if (eqIdx <= 0) continue;
    const key = trimmed.slice(0, eqIdx).trim();
    let val = trimmed.slice(eqIdx + 1).trim();
    if (
      (val.startsWith('"') && val.endsWith('"')) ||
      (val.startsWith("'") && val.endsWith("'"))
    ) {
      val = val.slice(1, -1);
    }
    if (!(key in process.env)) {
      process.env[key] = val;
    }
  }
}

function resolveDbName() {
  const dbUrl = process.env.DATABASE_URL || "";
  try {
    const parsed = new URL(dbUrl.replace(/^postgresql\+asyncpg:\/\//, "postgresql://"));
    return (parsed.pathname || "/neondb").replace(/^\/+/, "") || "neondb";
  } catch {
    return "neondb";
  }
}

async function main() {
  loadDotEnv();
  const args = process.argv.slice(2);
  const secret = process.env.NEON_REALTIME_SECRET || "";
  const dbName = resolveDbName();

  if (args.includes("--build-check")) {
    const neonTsPath = path.join(ROOT_DIR, "neon.ts");
    if (!fs.existsSync(neonTsPath)) {
      throw new Error("Missing neon.ts in project root");
    }
    if (!secret) {
      throw new Error("Missing NEON_REALTIME_SECRET in environment/.env");
    }
    const realtime = createRealtime({ secret, db: dbName });
    const sampleQuery = rawSql(
      "SELECT id, trade_ref, user_id, symbol, side, quantity, pnl, status FROM user_trades WHERE user_id = $1 ORDER BY opened_at DESC LIMIT 50",
      ["build-check-user"]
    );
    const sealed = await realtime.seal({ query: sampleQuery });
    console.log(
      JSON.stringify({
        ok: true,
        db: dbName,
        branch: process.env.NEON_BRANCH || "production",
        realtime_url: process.env.NEON_REALTIME_URL || "",
        auth_base_url: process.env.NEON_AUTH_BASE_URL || "",
        sealed_preview_length: JSON.stringify(sealed).length,
      })
    );
    return;
  }

  const rawPayload = args[0] ? JSON.parse(args[0]) : {};
  const sqlText =
    rawPayload.sql ||
    "SELECT id, trade_ref, user_id, symbol, side, quantity, entry_price, exit_price, sl_price, target1_price, pnl, status, exit_reason, opened_at, closed_at FROM user_trades WHERE user_id = $1 ORDER BY opened_at DESC LIMIT 50";
  const params = Array.isArray(rawPayload.params) ? rawPayload.params : [];

  if (!secret) {
    throw new Error("NEON_REALTIME_SECRET is not configured");
  }

  const realtime = createRealtime({ secret, db: dbName });
  const sealed = await realtime.seal({ query: rawSql(sqlText, params) });
  process.stdout.write(
    JSON.stringify({
      ok: true,
      db: dbName,
      realtime_url: process.env.NEON_REALTIME_URL || "",
      sealed_query: sealed,
    })
  );
}

main().catch((err) => {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
});
