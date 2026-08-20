import { Router } from "express";
import multer from "multer";
import { anthropic, MODEL, firstText, extractJson } from "../lib/anthropic.js";
import { READ_SYSTEM_PROMPT } from "../lib/prompts.js";

const upload = multer({
  storage: multer.memoryStorage(),
  limits: { fileSize: 10 * 1024 * 1024 },
});

const router = Router();

router.post("/", upload.single("image"), async (req, res) => {
  try {
    const { text, url } = req.body;
    const image = req.file;

    if (!image && !text && !url) {
      return res.status(400).json({
        error: "أرفق صورة أو الصق نص التغريدة أو رابطها على الأقل",
      });
    }

    const content = [];

    if (image) {
      if (!image.mimetype.startsWith("image/")) {
        return res.status(400).json({ error: "الملف المرفوع ليس صورة صالحة" });
      }
      content.push({
        type: "image",
        source: {
          type: "base64",
          media_type: image.mimetype,
          data: image.buffer.toString("base64"),
        },
      });
    }

    const parts = [];
    if (text) parts.push(`نص التغريدة الملصق من المستخدم:\n${text}`);
    if (url) parts.push(`رابط التغريدة (للسياق فقط، قد لا تكون قادراً على فتحه):\n${url}`);
    if (image) parts.push("اقرأ محتوى التغريدة من الصورة المرفقة أعلاه.");

    content.push({ type: "text", text: parts.join("\n\n") });

    const response = await anthropic.messages.create({
      model: MODEL,
      max_tokens: 2048,
      system: READ_SYSTEM_PROMPT,
      messages: [{ role: "user", content }],
    });

    const parsed = extractJson(firstText(response.content));
    res.json(parsed);
  } catch (err) {
    console.error("read error:", err);
    res.status(500).json({ error: "تعذّرت قراءة محتوى التغريدة", details: err.message });
  }
});

export default router;
