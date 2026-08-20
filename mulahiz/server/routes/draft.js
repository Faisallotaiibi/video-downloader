import { Router } from "express";
import { anthropic, MODEL, firstText, extractJson } from "../lib/anthropic.js";
import { DRAFT_SYSTEM_PROMPT } from "../lib/prompts.js";
import { saveDraft } from "../lib/storage.js";

const router = Router();

const DEFAULT_CHAR_LIMIT = parseInt(process.env.DEFAULT_NOTE_CHAR_LIMIT || "280", 10);

router.post("/", async (req, res) => {
  try {
    const { claim, verdict, summary, sources, tweetText, charLimit } = req.body;

    if (!claim || !verdict || !summary) {
      return res.status(400).json({ error: "بيانات التحقق ناقصة لصياغة المسودة" });
    }

    const limit = Number.isFinite(Number(charLimit)) && Number(charLimit) > 0
      ? Number(charLimit)
      : DEFAULT_CHAR_LIMIT;

    const userMessage = JSON.stringify(
      {
        tweetText: tweetText || null,
        claim,
        verdict,
        summary,
        sources: Array.isArray(sources) ? sources : [],
        charLimit: limit,
      },
      null,
      2,
    );

    const response = await anthropic.messages.create({
      model: MODEL,
      max_tokens: 1024,
      system: DRAFT_SYSTEM_PROMPT,
      messages: [{ role: "user", content: userMessage }],
    });

    const parsed = extractJson(firstText(response.content));

    const saved = saveDraft({
      tweetText: tweetText || null,
      claim,
      verdict,
      summary,
      sources: Array.isArray(sources) ? sources : [],
      draft: parsed.draft,
      charLimit: limit,
    });

    res.json({ draft: parsed.draft, charLimit: limit, id: saved.id });
  } catch (err) {
    console.error("draft error:", err);
    res.status(500).json({ error: "تعذّرت صياغة المسودة", details: err.message });
  }
});

export default router;
