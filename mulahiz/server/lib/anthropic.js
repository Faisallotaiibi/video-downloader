import Anthropic from "@anthropic-ai/sdk";

if (!process.env.ANTHROPIC_API_KEY) {
  throw new Error("ANTHROPIC_API_KEY غير موجود في متغيرات البيئة. أضفه في ملف .env");
}

export const MODEL = process.env.ANTHROPIC_MODEL || "claude-opus-5";

export const anthropic = new Anthropic();

/** يستخرج أول كتلة نصية من رد Claude */
export function firstText(content) {
  const block = content.find((b) => b.type === "text");
  return block ? block.text : "";
}

/** يجمع كل الكتل النصية من رد Claude بالترتيب (مفيد لردود تتخللها نتائج بحث) */
export function allText(content) {
  return content
    .filter((b) => b.type === "text")
    .map((b) => b.text)
    .join("\n");
}

/** يجمع روابط نتائج البحث الفعلية التي أرجعتها أداة web_search، للتحقق التقاطعي */
export function collectSearchResultUrls(content) {
  const urls = [];
  for (const block of content) {
    if (block.type !== "web_search_tool_result") continue;
    const results = Array.isArray(block.content) ? block.content : [];
    for (const r of results) {
      if (r.type === "web_search_result" && r.url) {
        urls.push({ title: r.title, url: r.url });
      }
    }
  }
  return urls;
}

/**
 * يلقط أول كائن JSON صالح داخل نص الرد (سواء داخل ```json ... ``` أو خام).
 * Claude أحياناً يضيف كلاماً قبل/بعد الـ JSON رغم التعليمات، فنستخرجه بأمان بدل افتراض أن النص كله JSON.
 */
export function extractJson(text) {
  const fenced = text.match(/```(?:json)?\s*([\s\S]*?)```/i);
  const candidate = fenced ? fenced[1] : text;
  const start = candidate.indexOf("{");
  const end = candidate.lastIndexOf("}");
  if (start === -1 || end === -1 || end < start) {
    throw new Error("تعذر العثور على JSON في رد النموذج");
  }
  return JSON.parse(candidate.slice(start, end + 1));
}
