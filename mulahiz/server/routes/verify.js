import { Router } from "express";
import { anthropic, MODEL, allText, extractJson, collectSearchResultUrls } from "../lib/anthropic.js";
import { VERIFY_SYSTEM_PROMPT } from "../lib/prompts.js";

const router = Router();

router.post("/", async (req, res) => {
  try {
    const { claim, claimContext, tweetText, mediaDescription } = req.body;

    if (!claim) {
      return res.status(400).json({ error: "الادعاء المطلوب التحقق منه مفقود" });
    }

    const userMessage = [
      `الادعاء المطلوب التحقق منه:\n${claim}`,
      claimContext ? `سياق إضافي عن نوع الادعاء:\n${claimContext}` : null,
      tweetText ? `نص التغريدة الأصلية كاملاً:\n${tweetText}` : null,
      mediaDescription ? `وصف الوسائط المرفقة بالتغريدة:\n${mediaDescription}` : null,
    ]
      .filter(Boolean)
      .join("\n\n");

    const response = await anthropic.messages.create({
      model: MODEL,
      max_tokens: 4096,
      system: VERIFY_SYSTEM_PROMPT,
      tools: [{ type: "web_search_20260209", name: "web_search", max_uses: 6 }],
      messages: [{ role: "user", content: userMessage }],
    });

    const text = allText(response.content);
    const parsed = extractJson(text);

    // نتائج البحث الفعلية كنسخة احتياطية/تقاطعية في حال لم يستشهد النموذج بمصادر كافية
    parsed.rawSearchResults = collectSearchResultUrls(response.content);

    res.json(parsed);
  } catch (err) {
    console.error("verify error:", err);
    res.status(500).json({ error: "تعذّر التحقق من الادعاء", details: err.message });
  }
});

export default router;
