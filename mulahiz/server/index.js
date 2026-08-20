import "dotenv/config";
import express from "express";
import path from "path";
import { fileURLToPath } from "url";

import readRouter from "./routes/read.js";
import verifyRouter from "./routes/verify.js";
import draftRouter from "./routes/draft.js";
import historyRouter from "./routes/history.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PORT = process.env.PORT || 3000;

const app = express();

app.use(express.json({ limit: "2mb" }));
app.use(express.static(path.join(__dirname, "..", "public")));

app.use("/api/read", readRouter);
app.use("/api/verify", verifyRouter);
app.use("/api/draft", draftRouter);
app.use("/api/history", historyRouter);

app.use((err, req, res, next) => {
  if (err && err.name === "MulterError") {
    return res.status(400).json({ error: "مشكلة في رفع الصورة: " + err.message });
  }
  console.error(err);
  res.status(500).json({ error: "خطأ غير متوقع في السيرفر" });
});

app.listen(PORT, () => {
  console.log(`ملاحظ يعمل على http://localhost:${PORT}`);
});
