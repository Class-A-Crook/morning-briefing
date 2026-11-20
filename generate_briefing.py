"""Daily 07:00 Morning Briefing Generator:
1. Fetches real-time authoritative RSS feeds (BBC, NPR, Nature, ScienceDaily, Ars Technica, Al Jazeera)
2. Uses Gemini (gemini-3.5-flash) with strict source_id grounding (zero fabricated URLs)
3. Verifies Trivia reading links via Wikipedia API
4. Renders mobile-friendly HTML with KaTeX math rendering for GitHub Pages
5. Pushes concise digest to iPhone via Bark with tap-to-open full webpage URL
"""
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import feedparser
import requests
from dotenv import load_dotenv
from google import genai
from google.genai import types

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DOCS_DIR = BASE_DIR / "docs"
ARCHIVE_DIR = DOCS_DIR / "archive"
DATA_DIR.mkdir(exist_ok=True)
DOCS_DIR.mkdir(exist_ok=True)
ARCHIVE_DIR.mkdir(exist_ok=True)

load_dotenv(BASE_DIR / ".env")

BJT = timezone(timedelta(hours=8))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
BARK_URL = os.getenv("BARK_URL", "").strip().rstrip("/")
PAGES_URL = "https://class-a-crook.github.io/morning-briefing/"

RSS_FEEDS = [
    ("BBC World", "国际", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("NPR World", "国际", "https://feeds.npr.org/1004/rss.xml"),
    ("Al Jazeera", "国际", "https://www.aljazeera.com/xml/rss/all.xml"),
    ("BBC Tech", "科技", "https://feeds.bbci.co.uk/news/technology/rss.xml"),
    ("Ars Technica", "科技", "https://feeds.arstechnica.com/arstechnica/index"),
    ("BBC Business", "经济", "https://feeds.bbci.co.uk/news/business/rss.xml"),
    ("BBC Science", "科学", "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml"),
    ("Nature", "科学", "https://www.nature.com/nature.rss"),
    ("ScienceDaily", "科学", "https://www.sciencedaily.com/rss/all.xml"),
]


def parse_entry_dt(entry) -> datetime | None:
    for key in ("published", "updated"):
        raw = entry.get(key)
        if raw:
            try:
                dt = parsedate_to_datetime(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(BJT)
            except Exception:
                pass
    if entry.get("published_parsed"):
        try:
            t = entry.published_parsed
            return datetime(*t[:6], tzinfo=timezone.utc).astimezone(BJT)
        except Exception:
            pass
    return None


def fetch_authoritative_news(hours_window: int = 36) -> dict[str, dict]:
    now_bjt = datetime.now(BJT)
    cutoff = now_bjt - timedelta(hours=hours_window)
    items = {}
    idx = 1
    seen_titles = set()

    for source_name, cat, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:12]:
                title = (entry.get("title") or "").strip()
                link = (entry.get("link") or "").strip()
                if not title or not link or title in seen_titles:
                    continue
                dt = parse_entry_dt(entry)
                if dt and dt < cutoff:
                    continue
                seen_titles.add(title)
                summary = re.sub(r"<[^>]+>", "", (entry.get("summary") or "")).strip()
                summary = re.sub(r"\s+", " ", summary)[:280]
                sid = f"N{idx}"
                items[sid] = {
                    "id": sid,
                    "source": source_name,
                    "category_hint": cat,
                    "published_bjt": dt.strftime("%Y-%m-%d %H:%M BJT") if dt else "近24小时",
                    "title": title,
                    "summary": summary,
                    "url": link,
                }
                idx += 1
        except Exception as e:
            print(f"[WARN] RSS {source_name}: {e}")
    print(f"Fetched {len(items)} fresh authoritative RSS items.")
    return items


def lookup_wikipedia(query: str) -> tuple[str, str]:
    """Verify and return (title, url) from Wikipedia API so links are 100% real."""
    headers = {"User-Agent": "MorningBriefingBot/1.0"}
    for lang in ("en", "zh"):
        try:
            r = requests.get(
                f"https://{lang}.wikipedia.org/w/api.php",
                params={"action": "opensearch", "search": query, "limit": 1, "format": "json"},
                headers=headers,
                timeout=8,
            )
            data = r.json()
            if len(data) >= 4 and data[1] and data[3]:
                return data[1][0], data[3][0]
        except Exception:
            pass
    return query, f"https://en.wikipedia.org/wiki/Special:Search?search={requests.utils.quote(query)}"


def load_history() -> dict:
    p = DATA_DIR / "history.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"trivia_topics": [], "math_topics": [], "dates": []}


def save_history(hist: dict) -> None:
    (DATA_DIR / "history.json").write_text(json.dumps(hist, ensure_ascii=False, indent=2), encoding="utf-8")


def generate_briefing_json(news_map: dict[str, dict], hist: dict) -> dict:
    client = genai.Client(api_key=GEMINI_API_KEY)
    today_bjt = datetime.now(BJT).strftime("%Y-%m-%d")

    news_block = "\n".join(
        f"[{v['id']}] ({v['source']} | {v['category_hint']} | {v['published_bjt']}) {v['title']} — {v['summary']}"
        for v in list(news_map.values())[:55]
    )

    past_trivia = ", ".join(hist.get("trivia_topics", [])[-30:]) or "无"
    past_math = ", ".join(hist.get("math_topics", [])[-30:]) or "无"

    prompt = f"""今天是 {today_bjt}（北京时间）。请根据以下真实抓取的过去24–36小时权威媒体新闻列表，撰写一份高质量的中文早间简报，严格返回 JSON 格式（不要输出 Markdown 代码块标记以外的多余文字）。

【可用真实新闻素材库】
{news_block}

【历史已讲过主题（严禁重复）】
- 往期冷知识：{past_trivia}
- 往期数学：{past_math}

【严格要求】
1. **part1_news（5~6条）**：
   - 必须从上面新闻素材库中精选，覆盖【国际、科技、经济、科学、社会】等多个领域，避免只聚焦单一国家或地区。
   - 每条必须填写准确的 `source_id`（如 "N1"），**严禁编造新闻或修改来源**。
   - `summary`：1–3 句话简要介绍背景、最新进展与重要性。
   - `fact_vs_unconfirmed`：明确区分哪些是已证实事实，哪些是尚未证实、各方表态或仍在发展的变量。
   - `perspectives`：提供多元视角下的观点、看法及对多方（政府/企业/市场/公众等）的影响。
2. **part2_trivia（1则今日冷知识）**：
   - 来自任何领域，有趣且有实质知识含量、经过科学或历史核实，不可与往期重复。
   - `title`：吸引人但严谨的标题。
   - `content`：深入浅出解释其原理或历史背景（150–250字）。
   - `authority_source`：注明权威信息来源机构/文献名称（如 Wikipedia、Nature、Smithsonian 等）。
   - `wiki_query`：给出最精准的英文维基百科检索词（用于程序自动匹配真实可点击的延伸阅读链接）。
3. **part3_math（1则今日数学）**：
   - 面向已有高中基础、目前正在学习**大学一年级高等数学（微积分/极限/级数）和线性代数（矩阵/向量空间/线性变换/特征值）**的学生，不局限于课本，每天变化类型（定理直觉、证明技巧、反直觉例子、几何意义或思考题）。
   - `title`：主题名称。
   - `category`：如「线性代数 · 几何直觉」「高等数学 · 反直觉例子」「证明技巧」等。
   - `content`：清楚准确的讲解与简短推导/例子（200–350字），数学公式使用标准 LaTeX（行内 `$...$`，独立公式 `$$...$$`，注意 JSON 字符串中反斜杠需双写如 `\\\\frac`、`\\\\int`、`\\\\begin{{pmatrix}}`）。
   - `takeaway`：一句话核心启发或留给读者的思考问题。

请输出如下 JSON 结构：
{{
  "date": "{today_bjt}",
  "part1_news": [
    {{
      "source_id": "N1",
      "category": "国际",
      "title_zh": "中文标题",
      "summary": "...",
      "fact_vs_unconfirmed": "...",
      "perspectives": "..."
    }}
  ],
  "part2_trivia": {{
    "title": "...",
    "domain": "如：物理学 / 生物学 / 历史",
    "content": "...",
    "authority_source": "...",
    "wiki_query": "..."
  }},
  "part3_math": {{
    "title": "...",
    "category": "...",
    "content": "...",
    "takeaway": "..."
  }}
}}"""

    resp = client.models.generate_content(
        model="gemini-3.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.4,
        ),
    )
    return json.loads(resp.text)


def render_html(briefing: dict, news_map: dict[str, dict], trivia_url: str, trivia_wiki_title: str) -> str:
    today_str = briefing["date"]
    news_cards = ""
    for item in briefing.get("part1_news", []):
        src = news_map.get(item.get("source_id", ""), {})
        url = src.get("url", "#")
        src_name = src.get("source", "权威媒体")
        pub_time = src.get("published_bjt", "近24小时")
        cat = item.get("category", "综合")
        news_cards += f"""
        <article class="card news-card">
          <div class="meta-row">
            <span class="badge">{cat}</span>
            <span class="time">{src_name} · {pub_time}</span>
          </div>
          <h3><a href="{url}" target="_blank" rel="noopener">{item.get('title_zh','')}</a></h3>
          <p class="summary">{item.get('summary','')}</p>
          <div class="sub-box fact"><strong>事实与未证实信息：</strong>{item.get('fact_vs_unconfirmed','')}</div>
          <div class="sub-box impact"><strong>多元视角与影响：</strong>{item.get('perspectives','')}</div>
          <div class="link-row"><a class="src-link" href="{url}" target="_blank" rel="noopener">阅读 {src_name} 原文 ↗</a></div>
        </article>"""

    tr = briefing.get("part2_trivia", {})
    mt = briefing.get("part3_math", {})

    # Archive links
    arch_files = sorted(ARCHIVE_DIR.glob("*.html"), reverse=True)[:10]
    arch_html = " · ".join(f'<a href="archive/{f.name}">{f.stem}</a>' for f in arch_files if f.stem != today_str)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>早间简报 · {today_str}</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css">
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/contrib/auto-render.min.js"
  onload="renderMathInElement(document.body, {{delimiters:[{{left:'$$',right:'$$',display:true}},{{left:'$',right:'$',display:false}}]}});"></script>
<style>
  :root {{ --bg:#f8fafc; --card:#ffffff; --text:#0f172a; --muted:#475569; --border:#e2e8f0; --accent:#1d4ed8; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#0b1120; --card:#111827; --text:#f1f5f9; --muted:#94a3b8; --border:#1f2937; --accent:#60a5fa; }}
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; padding:16px; background:var(--bg); color:var(--text); font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif; line-height:1.65; }}
  .container {{ max-width:740px; margin:0 auto; }}
  header {{ background:linear-gradient(135deg,#0f172a,#1e3a8a); color:#fff; padding:20px 22px; border-radius:14px; margin-bottom:20px; }}
  header h1 {{ margin:0 0 4px; font-size:21px; }}
  header .sub {{ font-size:13px; color:#bfdbfe; }}
  h2.sec {{ font-size:17px; margin:24px 0 12px; padding-left:10px; border-left:4px solid var(--accent); }}
  .card {{ background:var(--card); border:1px solid var(--border); border-radius:12px; padding:16px 18px; margin-bottom:14px; }}
  .meta-row {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:6px; font-size:12px; color:var(--muted); }}
  .badge {{ background:#dbeafe; color:#1e40af; padding:2px 8px; border-radius:10px; font-weight:600; font-size:11.5px; }}
  .card h3 {{ margin:4px 0 8px; font-size:16px; line-height:1.4; }}
  .card h3 a {{ color:var(--text); text-decoration:none; }}
  .card h3 a:hover {{ color:var(--accent); text-decoration:underline; }}
  .summary {{ margin:0 0 10px; font-size:14.5px; }}
  .sub-box {{ font-size:13.5px; color:var(--muted); padding:8px 10px; border-radius:8px; margin-bottom:6px; background:rgba(148,163,184,0.09); }}
  .link-row {{ margin-top:8px; text-align:right; }}
  .src-link {{ font-size:13px; color:var(--accent); text-decoration:none; font-weight:600; }}
  .math-body {{ font-size:14.8px; white-space:pre-line; overflow-x:auto; }}
  .takeaway {{ margin-top:10px; padding:10px 12px; border-left:3px solid #10b981; background:rgba(16,185,129,0.1); border-radius:6px; font-size:13.8px; }}
  footer {{ text-align:center; font-size:12px; color:var(--muted); margin:24px 0; }}
  footer a {{ color:var(--accent); text-decoration:none; }}
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>☀️ 每日早间简报 · {today_str}</h1>
    <div class="sub">世界动态（权威信源）· 今日冷知识 · 大一高数/线代每日数学</div>
  </header>

  <h2 class="sec">一、世界上正在发生什么</h2>
  {news_cards}

  <h2 class="sec">二、今日冷知识</h2>
  <article class="card">
    <div class="meta-row"><span class="badge">{tr.get('domain','科普')}</span><span class="time">来源：{tr.get('authority_source','Wikipedia')}</span></div>
    <h3>{tr.get('title','')}</h3>
    <p class="summary" style="white-space:pre-line;">{tr.get('content','')}</p>
    <div class="link-row"><a class="src-link" href="{trivia_url}" target="_blank" rel="noopener">延伸阅读：{trivia_wiki_title} (Wikipedia) ↗</a></div>
  </article>

  <h2 class="sec">三、今日数学（高数 / 线代视野）</h2>
  <article class="card">
    <div class="meta-row"><span class="badge">{mt.get('category','数学思维')}</span></div>
    <h3>{mt.get('title','')}</h3>
    <div class="math-body">{mt.get('content','')}</div>
    <div class="takeaway"><strong>💡 思考与启发：</strong>{mt.get('takeaway','')}</div>
  </article>

  <footer>
    <div>往期存档：{arch_html or '今日为首期'}</div>
    <div style="margin-top:6px;">生成时间：{datetime.now(BJT).strftime('%Y-%m-%d %H:%M BJT')} · 点击来源链接可核验原文</div>
  </footer>
</div>
</body>
</html>"""


def send_bark(briefing: dict, news_map: dict[str, dict]) -> None:
    if not BARK_URL:
        print("[WARN] BARK_URL not set; skip push.")
        return
    today_str = briefing["date"]
    lines = ["【一、世界正在发生什么】"]
    for i, item in enumerate(briefing.get("part1_news", [])[:5], 1):
        src = news_map.get(item.get("source_id", ""), {}).get("source", "")
        lines.append(f"{i}. [{item.get('category','')}] {item.get('title_zh','')}（{src}）")
        lines.append(f"   {item.get('summary','')[:85]}…")
    tr = briefing.get("part2_trivia", {})
    mt = briefing.get("part3_math", {})
    lines += [
        "",
        f"【二、今日冷知识】{tr.get('title','')}",
        f"▸ {tr.get('content','')[:95]}…",
        "",
        f"【三、今日数学】{mt.get('title','')}",
        f"▸ {mt.get('takeaway','')}",
        "",
        "👆 点击本通知打开完整网页版（含全部原文链接、多方视角与数学公式渲染）",
    ]
    body = "\n".join(lines)
    while len(body.encode("utf-8")) > 2800 and len(lines) > 6:
        lines.pop(-3)
        body = "\n".join(lines)

    endpoint = BARK_URL if BARK_URL.startswith("http") else f"https://api.day.app/{BARK_URL}"
    r = requests.post(
        endpoint,
        json={"title": f"☀️ 早间简报 {today_str[5:]}", "body": body, "group": "早间简报", "url": PAGES_URL},
        timeout=15,
    )
    print("[BARK]", r.status_code, r.text[:200])


def main() -> None:
    news_map = fetch_authoritative_news()
    if not news_map:
        raise RuntimeError("No RSS items fetched")
    hist = load_history()
    briefing = generate_briefing_json(news_map, hist)
    trivia_title, trivia_url = lookup_wikipedia(briefing.get("part2_trivia", {}).get("wiki_query", "Science"))

    html = render_html(briefing, news_map, trivia_url, trivia_title)
    today_str = briefing["date"]
    (DOCS_DIR / "index.html").write_text(html, encoding="utf-8")
    (ARCHIVE_DIR / f"{today_str}.html").write_text(html, encoding="utf-8")
    (DATA_DIR / f"briefing_{today_str}.json").write_text(json.dumps(briefing, ensure_ascii=False, indent=2), encoding="utf-8")

    if today_str not in hist.get("dates", []):
        hist.setdefault("dates", []).append(today_str)
        hist.setdefault("trivia_topics", []).append(briefing.get("part2_trivia", {}).get("title", ""))
        hist.setdefault("math_topics", []).append(briefing.get("part3_math", {}).get("title", ""))
        save_history(hist)

    send_bark(briefing, news_map)
    print(f"Briefing generated for {today_str}")


if __name__ == "__main__":
    main()
