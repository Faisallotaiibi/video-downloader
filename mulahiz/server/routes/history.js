import { Router } from "express";
import { listDrafts, getDraft, deleteDraft } from "../lib/storage.js";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DB_PATH = path.join(__dirname, "..", "..", "data", "drafts.json");

const router = Router();

router.get("/", (req, res) => {
  res.json(listDrafts());
});

router.get("/:id", (req, res) => {
  const record = getDraft(req.params.id);
  if (!record) return res.status(404).json({ error: "غير موجود" });
  res.json(record);
});

router.put("/:id", (req, res) => {
  const { draft } = req.body;
  if (typeof draft !== "string" || !draft.trim()) {
    return res.status(400).json({ error: "نص المسودة مطلوب" });
  }
  const records = JSON.parse(fs.readFileSync(DB_PATH, "utf8"));
  const idx = records.findIndex((r) => r.id === req.params.id);
  if (idx === -1) return res.status(404).json({ error: "غير موجود" });
  records[idx].draft = draft;
  records[idx].updatedAt = new Date().toISOString();
  fs.writeFileSync(DB_PATH, JSON.stringify(records, null, 2), "utf8");
  res.json(records[idx]);
});

router.delete("/:id", (req, res) => {
  const ok = deleteDraft(req.params.id);
  if (!ok) return res.status(404).json({ error: "غير موجود" });
  res.status(204).end();
});

export default router;
