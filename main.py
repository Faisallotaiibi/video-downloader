import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.parse import urlparse

import requests
import telebot
import yt_dlp
from flask import Flask, Response, jsonify, render_template_string, request, send_from_directory

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set")

PORT = int(os.environ.get("PORT", 10000))
SELF_URL = os.environ.get("SELF_URL", "https://video-downloader-0ea4.onrender.com")
MAX_TELEGRAM_FILE_SIZE = 50 * 1024 * 1024  # Telegram bot API upload limit

DASHBOARD_USERNAME = os.environ.get("DASHBOARD_USERNAME", "admin")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "usage.db")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__, static_folder=".")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS usage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            source TEXT NOT NULL,
            requester TEXT,
            url TEXT NOT NULL,
            platform TEXT,
            status TEXT NOT NULL,
            error TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def detect_platform(url):
    host = urlparse(url).netloc.lower().removeprefix("www.")
    if "tiktok" in host:
        return "TikTok"
    if "instagram" in host:
        return "Instagram"
    if "twitter" in host or host == "x.com":
        return "Twitter/X"
    if "youtube" in host or "youtu.be" in host:
        return "YouTube"
    return host or "غير معروف"


def log_usage(source, requester, url, status, error=None):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO usage_log (timestamp, source, requester, url, platform, status, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                source,
                requester,
                url,
                detect_platform(url),
                status,
                error,
            ),
        )
        conn.commit()
        conn.close()
    except Exception:
        logger.exception("Failed to log usage")


def keep_alive():
    while True:
        try:
            requests.get(SELF_URL, timeout=10)
        except requests.RequestException:
            logger.warning("keep_alive ping failed", exc_info=True)
        time.sleep(840)


@bot.message_handler(commands=['start'])
def start(message):
    logger.info("Received /start from chat_id=%s", message.chat.id)
    bot.reply_to(message, "أهلاً! أرسل لي رابط الفيديو وأنا أحمله لك بدون علامة مائية 🎬")


@bot.message_handler(content_types=['text'])
def download(message):
    logger.info("Received message from chat_id=%s: %r", message.chat.id, message.text)
    url = message.text.strip()
    requester = f"@{message.from_user.username}" if message.from_user.username else (
        message.from_user.first_name or str(message.chat.id)
    )

    if not url.lower().startswith(("http://", "https://")):
        bot.reply_to(message, "من فضلك أرسل رابط فيديو صالح.")
        return

    bot.reply_to(message, "⏳ جاري التحميل...")
    filename = None
    try:
        ydl_opts = {
            'format': 'best[ext=mp4]/best',
            'noplaylist': True,
            'outtmpl': '/tmp/%(id)s.%(ext)s',
            'max_filesize': MAX_TELEGRAM_FILE_SIZE,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)

        if not os.path.exists(filename):
            raise FileNotFoundError("تعذر تنزيل الملف (قد يكون حجمه أكبر من 50MB)")

        with open(filename, 'rb') as f:
            bot.send_video(message.chat.id, f)
        log_usage("telegram", requester, url, "success")
    except Exception as e:
        logger.exception("Download failed for url=%s", url)
        bot.reply_to(message, f"❌ فشل التحميل: {e}")
        log_usage("telegram", requester, url, "failed", str(e))
    finally:
        if filename and os.path.exists(filename):
            os.remove(filename)


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/download", methods=["POST"])
def api_download():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    requester = request.remote_addr or "web"

    if not url.lower().startswith(("http://", "https://")):
        return jsonify({"error": "رابط غير صالح"}), 400

    try:
        ydl_opts = {
            'format': 'best[ext=mp4]/best',
            'noplaylist': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        direct_url = info.get("url")
        if not direct_url and info.get("formats"):
            direct_url = info["formats"][-1].get("url")

        if not direct_url:
            log_usage("web", requester, url, "failed", "no direct url found")
            return jsonify({"error": "تعذر العثور على رابط التحميل"}), 502

        log_usage("web", requester, url, "success")
        return jsonify({"download_url": direct_url})
    except Exception as e:
        logger.exception("API download failed for url=%s", url)
        log_usage("web", requester, url, "failed", str(e))
        return jsonify({"error": str(e)}), 500


def check_dashboard_auth(auth):
    return bool(
        DASHBOARD_PASSWORD
        and auth
        and auth.username == DASHBOARD_USERNAME
        and auth.password == DASHBOARD_PASSWORD
    )


def require_dashboard_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not check_dashboard_auth(request.authorization):
            return Response(
                "الرجاء تسجيل الدخول لعرض لوحة التحكم",
                401,
                {"WWW-Authenticate": 'Basic realm="Dashboard"'},
            )
        return f(*args, **kwargs)

    return wrapper


def dashboard_data():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    total = conn.execute("SELECT COUNT(*) AS c FROM usage_log").fetchone()["c"]
    success = conn.execute(
        "SELECT COUNT(*) AS c FROM usage_log WHERE status = 'success'"
    ).fetchone()["c"]
    by_platform = conn.execute(
        "SELECT platform, COUNT(*) AS c FROM usage_log GROUP BY platform ORDER BY c DESC"
    ).fetchall()
    daily = conn.execute(
        "SELECT strftime('%Y-%m-%d', timestamp) AS day, COUNT(*) AS c FROM usage_log "
        "WHERE timestamp >= date('now', '-13 days') GROUP BY day ORDER BY day"
    ).fetchall()
    recent = conn.execute(
        "SELECT * FROM usage_log ORDER BY id DESC LIMIT 200"
    ).fetchall()
    conn.close()

    daily_by_day = {row["day"]: row["c"] for row in daily}
    daily_series = []
    for i in range(13, -1, -1):
        day = (datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%d")
        daily_series.append({"day": day, "count": daily_by_day.get(day, 0)})

    return {
        "total": total,
        "success": success,
        "failed": total - success,
        "by_platform": [{"platform": r["platform"], "count": r["c"]} for r in by_platform],
        "daily": daily_series,
        "recent": [dict(r) for r in recent],
    }


@app.route("/api/dashboard-data")
@require_dashboard_auth
def api_dashboard_data():
    return jsonify(dashboard_data())


DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>لوحة التحكم</title>
<style>
:root {
  --bg: #0a0a0a;
  --surface: #141414;
  --surface-2: #1c1c1c;
  --border: #2a2a2a;
  --text-primary: #f2f2f2;
  --text-secondary: #9a9a9a;
  --text-muted: #6b6b6b;
  --bar-dim: #3a3a3a;
  --bar-bright: #f2f2f2;
  --status-good: #0ca30c;
  --status-critical: #d03b3b;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body { overflow-x: hidden; max-width: 100%; }
body {
  font-family: 'Segoe UI', sans-serif;
  background: var(--bg);
  color: var(--text-primary);
  min-height: 100vh;
  padding: 30px 15px 60px;
}
.wrap { max-width: 1100px; margin: 0 auto; }
header { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 22px; flex-wrap: wrap; gap: 8px; }
h1 { font-size: 20px; font-weight: 600; }
.refresh-note { font-size: 12px; color: var(--text-muted); }
.stats { display: flex; gap: 14px; flex-wrap: wrap; margin-bottom: 16px; }
.card {
  background: linear-gradient(180deg, rgba(255,255,255,0.04), rgba(255,255,255,0) 45%), var(--surface);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 20px;
  box-shadow: 0 12px 30px rgba(0,0,0,0.35);
  margin-bottom: 16px;
}
.stat-card { flex: 1; min-width: 140px; }
.stat-card .num { font-size: 32px; font-weight: 700; font-variant-numeric: proportional-nums; }
.stat-card .label { font-size: 12px; color: var(--text-secondary); margin-top: 4px; }
.stat-card.good .num { color: var(--status-good); }
.stat-card.critical .num { color: var(--status-critical); }
.card h2 { font-size: 14px; font-weight: 600; color: var(--text-secondary); margin-bottom: 16px; }
#loading { color: var(--text-muted); font-size: 13px; padding: 20px 0; text-align: center; }
#content { transition: opacity 0.2s; }
#content.stale { opacity: 0.55; }

.chart-scroll { overflow-x: auto; }
.chart-inner { min-width: 480px; }
.bar-chart { display: flex; align-items: flex-end; gap: 4px; height: 120px; position: relative; }
.bar-col { flex: 1; min-width: 0; display: flex; flex-direction: column; align-items: center; justify-content: flex-end; height: 100%; }
.bar {
  width: 100%;
  max-width: 22px;
  border-radius: 4px 4px 0 0;
  background: var(--bar-dim);
  min-height: 3px;
  cursor: pointer;
  transition: filter 0.15s;
}
.bar:hover, .bar:focus { filter: brightness(1.3); outline: none; }
.bar-labels { display: flex; gap: 4px; margin-top: 8px; }
.bar-labels span { flex: 1; min-width: 0; text-align: center; font-size: 10px; color: var(--text-muted); }
.tooltip {
  position: fixed;
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 6px 10px;
  font-size: 12px;
  pointer-events: none;
  z-index: 10;
  display: none;
  box-shadow: 0 8px 20px rgba(0,0,0,0.4);
}
.tooltip .v { font-weight: 700; color: var(--text-primary); }
.tooltip .d { color: var(--text-secondary); }

.platform-row { display: flex; align-items: center; gap: 12px; padding: 8px 0; font-size: 13px; }
.platform-name { width: 100px; flex-shrink: 0; color: var(--text-secondary); }
.platform-track { flex: 1; background: var(--surface-2); border-radius: 6px; height: 10px; overflow: hidden; }
.platform-fill { display: block; height: 100%; background: linear-gradient(90deg, #6b6b6b, var(--bar-bright)); border-radius: 6px; }
.platform-count { width: 90px; text-align: left; direction: ltr; color: var(--text-muted); font-size: 12px; }

.toolbar { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 14px; }
.toolbar input[type="text"] {
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px 12px;
  color: var(--text-primary);
  font-size: 13px;
  min-width: 200px;
  flex: 1;
}
.toolbar input[type="text"]::placeholder { color: var(--text-muted); }
.chip {
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: 20px;
  padding: 6px 14px;
  font-size: 12px;
  color: var(--text-secondary);
  cursor: pointer;
  user-select: none;
}
.chip.active { background: var(--text-primary); color: #000; border-color: var(--text-primary); font-weight: 600; }

table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 9px 10px; text-align: right; border-bottom: 1px solid var(--border); white-space: nowrap; }
th { color: var(--text-muted); font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: 0.03em; cursor: pointer; user-select: none; }
th:hover { color: var(--text-secondary); }
td { color: var(--text-secondary); }
.table-wrap { overflow-x: auto; }
td.url { max-width: 280px; overflow: hidden; text-overflow: ellipsis; direction: ltr; text-align: left; }
td.url a { color: var(--text-primary); text-decoration: none; border-bottom: 1px dotted var(--text-muted); }
td.url a:hover { color: #fff; }
.status { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; }
.status-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.status-dot.success { background: var(--status-good); }
.status-dot.failed { background: var(--status-critical); }
.empty-row td { text-align: center; color: var(--text-muted); padding: 24px; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>📊 لوحة تحكم البوت</h1>
    <span class="refresh-note" id="refreshNote">يحدّث تلقائيًا كل 30 ثانية</span>
  </header>

  <div id="loading">جاري التحميل...</div>
  <div id="content" style="display:none">
    <div class="stats">
      <div class="card stat-card"><div class="num" id="statTotal">0</div><div class="label">إجمالي الاستخدامات</div></div>
      <div class="card stat-card good"><div class="num" id="statSuccess">0</div><div class="label">ناجحة</div></div>
      <div class="card stat-card critical"><div class="num" id="statFailed">0</div><div class="label">فاشلة</div></div>
    </div>

    <div class="card">
      <h2>الاستخدام آخر 14 يوم</h2>
      <div class="chart-scroll">
        <div class="chart-inner">
          <div class="bar-chart" id="barChart"></div>
          <div class="bar-labels" id="barLabels"></div>
        </div>
      </div>
    </div>

    <div class="card">
      <h2>حسب المنصة</h2>
      <div id="platformList"></div>
    </div>

    <div class="card">
      <h2>آخر الطلبات</h2>
      <div class="toolbar">
        <input type="text" id="search" placeholder="ابحث بالمستخدم أو الرابط أو المنصة...">
        <span class="chip active" data-filter="status" data-value="all">الكل</span>
        <span class="chip" data-filter="status" data-value="success">ناجحة</span>
        <span class="chip" data-filter="status" data-value="failed">فاشلة</span>
        <span class="chip active" data-filter="source" data-value="all">كل المصادر</span>
        <span class="chip" data-filter="source" data-value="telegram">تيليجرام</span>
        <span class="chip" data-filter="source" data-value="web">الموقع</span>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th data-key="timestamp">الوقت (UTC)</th>
              <th data-key="source">المصدر</th>
              <th data-key="requester">المستخدم</th>
              <th data-key="platform">المنصة</th>
              <th>الرابط</th>
              <th data-key="status">الحالة</th>
            </tr>
          </thead>
          <tbody id="tableBody"></tbody>
        </table>
      </div>
    </div>
  </div>
</div>

<div class="tooltip" id="tooltip"></div>

<script>
let allRows = [];
let sortKey = 'timestamp';
let sortDir = -1;
let statusFilter = 'all';
let sourceFilter = 'all';

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function renderStats(data) {
  document.getElementById('statTotal').textContent = data.total;
  document.getElementById('statSuccess').textContent = data.success;
  document.getElementById('statFailed').textContent = data.failed;
}

function renderBarChart(daily) {
  const chart = document.getElementById('barChart');
  const labels = document.getElementById('barLabels');
  const tooltip = document.getElementById('tooltip');
  chart.innerHTML = '';
  labels.innerHTML = '';
  const max = Math.max(1, ...daily.map(d => d.count));

  daily.forEach(d => {
    const col = document.createElement('div');
    col.className = 'bar-col';
    const bar = document.createElement('div');
    bar.className = 'bar';
    const pct = d.count / max;
    bar.style.height = Math.max(3, pct * 100) + '%';
    const lightness = 35 + pct * 55;
    bar.style.background = `hsl(0, 0%, ${lightness}%)`;
    bar.tabIndex = 0;
    bar.addEventListener('mouseenter', e => showTooltip(e, d));
    bar.addEventListener('focus', e => showTooltip(e, d));
    bar.addEventListener('mousemove', e => positionTooltip(e));
    bar.addEventListener('mouseleave', hideTooltip);
    bar.addEventListener('blur', hideTooltip);
    col.appendChild(bar);
    chart.appendChild(col);

    const lbl = document.createElement('span');
    const shortDay = d.day.slice(5).replace('-', '/');
    lbl.textContent = shortDay;
    labels.appendChild(lbl);
  });

  function showTooltip(e, d) {
    tooltip.innerHTML = `<span class="v">${d.count}</span> <span class="d">— ${d.day}</span>`;
    tooltip.style.display = 'block';
    positionTooltip(e);
  }
  function positionTooltip(e) {
    const rect = e.target.getBoundingClientRect();
    tooltip.style.left = (rect.left + rect.width / 2 - tooltip.offsetWidth / 2) + 'px';
    tooltip.style.top = (rect.top - 40) + 'px';
  }
  function hideTooltip() { tooltip.style.display = 'none'; }
}

function renderPlatforms(byPlatform) {
  const container = document.getElementById('platformList');
  container.innerHTML = '';
  const max = Math.max(1, ...byPlatform.map(p => p.count));
  if (byPlatform.length === 0) {
    container.innerHTML = '<div class="empty-row" style="color:var(--text-muted);font-size:13px;">لا توجد بيانات بعد</div>';
    return;
  }
  byPlatform.forEach(p => {
    const row = document.createElement('div');
    row.className = 'platform-row';
    const pct = (p.count / max) * 100;
    row.innerHTML = `
      <span class="platform-name">${escapeHtml(p.platform)}</span>
      <span class="platform-track"><span class="platform-fill" style="width:${pct}%"></span></span>
      <span class="platform-count">${p.count}</span>
    `;
    container.appendChild(row);
  });
}

function applyFiltersAndSort() {
  const q = document.getElementById('search').value.trim().toLowerCase();
  let rows = allRows.filter(r => {
    if (statusFilter !== 'all' && r.status !== statusFilter) return false;
    if (sourceFilter !== 'all' && r.source !== sourceFilter) return false;
    if (q) {
      const hay = `${r.requester || ''} ${r.url || ''} ${r.platform || ''}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
  rows.sort((a, b) => {
    const av = (a[sortKey] || '').toString();
    const bv = (b[sortKey] || '').toString();
    return av > bv ? sortDir : av < bv ? -sortDir : 0;
  });
  renderTable(rows);
}

function renderTable(rows) {
  const tbody = document.getElementById('tableBody');
  tbody.innerHTML = '';
  if (rows.length === 0) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="6">لا توجد نتائج</td></tr>';
    return;
  }
  rows.forEach(r => {
    const tr = document.createElement('tr');
    const statusLabel = r.status === 'success' ? 'نجح' : 'فشل';
    tr.innerHTML = `
      <td>${escapeHtml(r.timestamp)}</td>
      <td>${escapeHtml(r.source)}</td>
      <td>${escapeHtml(r.requester || '')}</td>
      <td>${escapeHtml(r.platform || '')}</td>
      <td class="url"></td>
      <td><span class="status"><span class="status-dot ${r.status}"></span>${statusLabel}</span></td>
    `;
    const urlCell = tr.querySelector('.url');
    const a = document.createElement('a');
    a.href = r.url;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.title = r.url;
    a.textContent = r.url;
    urlCell.appendChild(a);
    tbody.appendChild(tr);
  });
}

async function loadData() {
  const content = document.getElementById('content');
  const hasData = allRows.length > 0;
  if (hasData) content.classList.add('stale');
  try {
    const res = await fetch('/api/dashboard-data');
    if (!res.ok) throw new Error('request failed');
    const data = await res.json();
    allRows = data.recent;
    renderStats(data);
    renderBarChart(data.daily);
    renderPlatforms(data.by_platform);
    applyFiltersAndSort();
    document.getElementById('loading').style.display = 'none';
    content.style.display = 'block';
  } catch (e) {
    document.getElementById('refreshNote').textContent = 'تعذر تحديث البيانات';
  } finally {
    content.classList.remove('stale');
  }
}

document.getElementById('search').addEventListener('input', applyFiltersAndSort);
document.querySelectorAll('.chip').forEach(chip => {
  chip.addEventListener('click', () => {
    const group = chip.dataset.filter;
    document.querySelectorAll(`.chip[data-filter="${group}"]`).forEach(c => c.classList.remove('active'));
    chip.classList.add('active');
    if (group === 'status') statusFilter = chip.dataset.value;
    if (group === 'source') sourceFilter = chip.dataset.value;
    applyFiltersAndSort();
  });
});
document.querySelectorAll('th[data-key]').forEach(th => {
  th.addEventListener('click', () => {
    const key = th.dataset.key;
    if (sortKey === key) { sortDir *= -1; } else { sortKey = key; sortDir = -1; }
    applyFiltersAndSort();
  });
});

loadData();
setInterval(loadData, 30000);
</script>
</body>
</html>
"""


@app.route("/dashboard")
@require_dashboard_auth
def dashboard():
    return render_template_string(DASHBOARD_TEMPLATE)


def run_server():
    app.run(host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    init_db()
    threading.Thread(target=keep_alive, daemon=True).start()
    threading.Thread(target=run_server, daemon=True).start()
    logger.info("Starting Telegram bot polling as @%s", bot.get_me().username)
    bot.infinity_polling(logger_level=logging.INFO)
