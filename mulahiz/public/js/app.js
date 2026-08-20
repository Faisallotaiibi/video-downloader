const VERDICT_LABELS = {
  false: "معلومة خاطئة",
  partially_misleading: "مضلل جزئياً",
  missing_context: "يحتاج سياق إضافي",
  true: "صحيح فعلاً",
  insufficient_evidence: "أدلة غير كافية",
};

const el = (id) => document.getElementById(id);

const screens = {
  upload: el("uploadScreen"),
  analysis: el("analysisScreen"),
  result: el("resultScreen"),
};

function showScreen(name) {
  Object.values(screens).forEach((s) => s.classList.remove("active"));
  screens[name].classList.add("active");
}

// ---------- حالة التطبيق ----------
const state = {
  imageFile: null,
  tweetText: "",
  mediaDescription: null,
  claim: "",
  claimContext: "",
  verdict: null,
  summary: "",
  sources: [],
  charLimit: 280,
};

// ---------- شاشة الرفع: منطقة السحب والإفلات ----------
const dropzone = el("dropzone");
const imageInput = el("imageInput");
const dropzoneEmpty = el("dropzoneEmpty");
const dropzonePreviewWrap = el("dropzonePreviewWrap");
const dropzonePreview = el("dropzonePreview");

function setImageFile(file) {
  state.imageFile = file || null;
  if (file) {
    dropzonePreview.src = URL.createObjectURL(file);
    dropzoneEmpty.classList.add("hidden");
    dropzonePreviewWrap.classList.remove("hidden");
  } else {
    dropzoneEmpty.classList.remove("hidden");
    dropzonePreviewWrap.classList.add("hidden");
    imageInput.value = "";
  }
}

dropzone.addEventListener("click", (e) => {
  if (e.target.closest("#removeImage")) return;
  imageInput.click();
});
dropzone.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") imageInput.click();
});
imageInput.addEventListener("change", () => setImageFile(imageInput.files[0]));

["dragenter", "dragover"].forEach((evt) =>
  dropzone.addEventListener(evt, (e) => {
    e.preventDefault();
    dropzone.classList.add("dragover");
  }),
);
["dragleave", "drop"].forEach((evt) =>
  dropzone.addEventListener(evt, (e) => {
    e.preventDefault();
    dropzone.classList.remove("dragover");
  }),
);
dropzone.addEventListener("drop", (e) => {
  const file = e.dataTransfer.files[0];
  if (file) setImageFile(file);
});
el("removeImage").addEventListener("click", (e) => {
  e.stopPropagation();
  setImageFile(null);
});

// ---------- بدء التحليل ----------
el("startBtn").addEventListener("click", startAnalysis);

async function startAnalysis() {
  const tweetText = el("tweetText").value.trim();
  const tweetUrl = el("tweetUrl").value.trim();
  const uploadError = el("uploadError");
  uploadError.classList.add("hidden");

  if (!state.imageFile && !tweetText && !tweetUrl) {
    uploadError.textContent = "أرفق صورة أو الصق نص التغريدة أو رابطها على الأقل";
    uploadError.classList.remove("hidden");
    return;
  }

  showScreen("analysis");
  resetAnalysisSteps();
  el("analysisError").classList.add("hidden");
  el("analysisBackBtn").classList.add("hidden");

  try {
    setStep("read", "active");
    const formData = new FormData();
    if (state.imageFile) formData.append("image", state.imageFile);
    if (tweetText) formData.append("text", tweetText);
    if (tweetUrl) formData.append("url", tweetUrl);

    const readData = await postForm("/api/read", formData);
    state.tweetText = readData.tweetText || tweetText || "";
    state.mediaDescription = readData.mediaDescription || null;
    state.claim = readData.claim || "";
    state.claimContext = readData.claimContext || "";
    setStep("read", "done");

    setStep("verify", "active");
    const verifyData = await postJson("/api/verify", {
      claim: state.claim,
      claimContext: state.claimContext,
      tweetText: state.tweetText,
      mediaDescription: state.mediaDescription,
    });
    state.verdict = verifyData.verdict;
    state.summary = verifyData.summary;
    state.sources = verifyData.sources || [];
    setStep("verify", "done");

    setStep("draft", "active");
    const draftData = await postJson("/api/draft", {
      claim: state.claim,
      verdict: state.verdict,
      summary: state.summary,
      sources: state.sources,
      tweetText: state.tweetText,
      charLimit: state.charLimit,
    });
    state.charLimit = draftData.charLimit || state.charLimit;
    setStep("draft", "done");

    renderResult(draftData.draft);
    showScreen("result");
  } catch (err) {
    const currentStep = document.querySelector(".step.active");
    if (currentStep) currentStep.classList.replace("active", "error");
    el("analysisError").textContent = err.message || "حدث خطأ غير متوقع";
    el("analysisError").classList.remove("hidden");
    el("analysisBackBtn").classList.remove("hidden");
  }
}

function resetAnalysisSteps() {
  document.querySelectorAll(".step").forEach((s) => s.classList.remove("active", "done", "error"));
}

function setStep(name, status) {
  const stepEl = document.querySelector(`.step[data-step="${name}"]`);
  if (!stepEl) return;
  stepEl.classList.remove("active", "done", "error");
  stepEl.classList.add(status);
}

el("analysisBackBtn").addEventListener("click", () => showScreen("upload"));

// ---------- شاشة النتيجة ----------
function renderResult(draftText) {
  el("claimText").textContent = state.claim;
  el("verifySummary").textContent = state.summary;

  const badge = el("verdictBadge");
  badge.textContent = VERDICT_LABELS[state.verdict] || state.verdict || "—";
  badge.className = "badge badge-" + (state.verdict || "");

  const list = el("sourcesList");
  list.innerHTML = "";
  if (!state.sources.length) {
    const li = document.createElement("li");
    li.className = "no-sources";
    li.textContent = "لم يُعثر على مصادر كافية.";
    list.appendChild(li);
  } else {
    state.sources.forEach((s) => {
      const li = document.createElement("li");
      const a = document.createElement("a");
      a.href = s.url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.textContent = s.title || s.url;
      li.appendChild(a);
      if (s.publisher) {
        const span = document.createElement("span");
        span.className = "source-publisher";
        span.textContent = "  —  " + s.publisher;
        li.appendChild(span);
      }
      list.appendChild(li);
    });
  }

  el("charLimitInput").value = state.charLimit;
  el("draftText").value = draftText;
  updateCharCounter();
}

const draftTextArea = el("draftText");
draftTextArea.addEventListener("input", updateCharCounter);

function updateCharCounter() {
  const len = draftTextArea.value.length;
  const limit = Number(el("charLimitInput").value) || state.charLimit;
  const counter = el("charCounter");
  counter.textContent = `${len} / ${limit}`;
  counter.classList.toggle("over", len > limit);
}

el("charLimitInput").addEventListener("input", updateCharCounter);

el("copyBtn").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(draftTextArea.value);
    const confirm = el("copyConfirm");
    confirm.classList.remove("hidden");
    setTimeout(() => confirm.classList.add("hidden"), 2000);
  } catch {
    draftTextArea.select();
    document.execCommand("copy");
  }
});

el("regenerateBtn").addEventListener("click", async () => {
  const btn = el("regenerateBtn");
  btn.disabled = true;
  btn.textContent = "جارٍ الصياغة...";
  try {
    state.charLimit = Number(el("charLimitInput").value) || state.charLimit;
    const draftData = await postJson("/api/draft", {
      claim: state.claim,
      verdict: state.verdict,
      summary: state.summary,
      sources: state.sources,
      tweetText: state.tweetText,
      charLimit: state.charLimit,
    });
    draftTextArea.value = draftData.draft;
    updateCharCounter();
  } catch (err) {
    alert(err.message || "تعذّرت إعادة الصياغة");
  } finally {
    btn.disabled = false;
    btn.textContent = "إعادة الصياغة";
  }
});

el("newBtn").addEventListener("click", () => {
  setImageFile(null);
  el("tweetText").value = "";
  el("tweetUrl").value = "";
  Object.assign(state, {
    imageFile: null,
    tweetText: "",
    mediaDescription: null,
    claim: "",
    claimContext: "",
    verdict: null,
    summary: "",
    sources: [],
    charLimit: 280,
  });
  showScreen("upload");
});

// ---------- السجل ----------
const historyPanel = el("historyPanel");
const historyOverlay = el("historyOverlay");

el("historyToggle").addEventListener("click", openHistory);
el("historyClose").addEventListener("click", closeHistory);
historyOverlay.addEventListener("click", closeHistory);

function openHistory() {
  historyPanel.classList.add("open");
  historyOverlay.classList.remove("hidden");
  loadHistory();
}

function closeHistory() {
  historyPanel.classList.remove("open");
  historyOverlay.classList.add("hidden");
}

async function loadHistory() {
  const list = el("historyList");
  list.innerHTML = '<p class="history-empty">جارٍ التحميل...</p>';
  try {
    const records = await getJson("/api/history");
    if (!records.length) {
      list.innerHTML = '<p class="history-empty">لا توجد مسودات سابقة بعد.</p>';
      return;
    }
    list.innerHTML = "";
    records.forEach((r) => {
      const item = document.createElement("div");
      item.className = "history-item";

      const claimP = document.createElement("p");
      claimP.className = "history-item-claim";
      claimP.textContent = r.claim || "(بدون عنوان)";
      item.appendChild(claimP);

      const meta = document.createElement("div");
      meta.className = "history-item-meta";
      const date = document.createElement("span");
      date.textContent = new Date(r.createdAt).toLocaleDateString("ar-SA");
      meta.appendChild(date);
      const del = document.createElement("button");
      del.className = "history-item-delete";
      del.textContent = "حذف";
      del.addEventListener("click", async (e) => {
        e.stopPropagation();
        await fetch(`/api/history/${r.id}`, { method: "DELETE" });
        loadHistory();
      });
      meta.appendChild(del);
      item.appendChild(meta);

      item.addEventListener("click", () => {
        Object.assign(state, {
          tweetText: r.tweetText || "",
          claim: r.claim,
          verdict: r.verdict,
          summary: r.summary,
          sources: r.sources || [],
          charLimit: r.charLimit || 280,
        });
        renderResult(r.draft);
        showScreen("result");
        closeHistory();
      });

      list.appendChild(item);
    });
  } catch {
    list.innerHTML = '<p class="history-empty">تعذّر تحميل السجل.</p>';
  }
}

// ---------- أدوات مساعدة للطلبات ----------
async function postJson(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return handleResponse(res);
}

async function postForm(url, formData) {
  const res = await fetch(url, { method: "POST", body: formData });
  return handleResponse(res);
}

async function getJson(url) {
  const res = await fetch(url);
  return handleResponse(res);
}

async function handleResponse(res) {
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.error || `خطأ في الطلب (${res.status})`);
  }
  return data;
}
