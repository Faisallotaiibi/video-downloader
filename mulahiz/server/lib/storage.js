import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import crypto from "crypto";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DATA_DIR = path.join(__dirname, "..", "..", "data");
const DB_PATH = path.join(DATA_DIR, "drafts.json");

function ensureDb() {
  if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });
  if (!fs.existsSync(DB_PATH)) fs.writeFileSync(DB_PATH, "[]", "utf8");
}

function readAll() {
  ensureDb();
  try {
    return JSON.parse(fs.readFileSync(DB_PATH, "utf8"));
  } catch {
    return [];
  }
}

function writeAll(records) {
  ensureDb();
  fs.writeFileSync(DB_PATH, JSON.stringify(records, null, 2), "utf8");
}

export function saveDraft(record) {
  const records = readAll();
  const entry = {
    id: crypto.randomUUID(),
    createdAt: new Date().toISOString(),
    ...record,
  };
  records.unshift(entry);
  writeAll(records);
  return entry;
}

export function listDrafts() {
  return readAll();
}

export function getDraft(id) {
  return readAll().find((r) => r.id === id) || null;
}

export function deleteDraft(id) {
  const records = readAll();
  const next = records.filter((r) => r.id !== id);
  const changed = next.length !== records.length;
  if (changed) writeAll(next);
  return changed;
}
