"""Daily 07:00 「晨间读本」Generator:
- Fetches real-time authoritative RSS feeds (last 24-36h)
- Calls gemini-4-hydrogen (fallback gemini-3.5-flash) with the user's editorial prompt
- Prevents repetition of news background, trivia, and math topics/angles via history.json
- Verifies all news & trivia links against real fetched URLs / Wikipedia API
- Renders a clean, relaxed, magazine-style mobile HTML page with KaTeX math rendering
- Sends a crisp Bark push linking to the GitHub Pages reader
"""
import json
import os
import re
import sys
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
    ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("NPR World", "https://feeds.npr.org/1004/rss.xml"),
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
    ("BBC Tech", "https://feeds.bbci.co.uk/news/technology/rss.xml"),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
    ("BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml"),
    ("BBC Science", "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml"),
    ("Nature", "https://www.nature.com/nature.rss"),
    ("ScienceDaily", "https://www.sciencedaily.com/rss/all.xml"),
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

    for source_name, url in RSS_FEEDS:
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
                summary = re.sub(r"\s+", " ", summary)[:320]
                sid = f"N{idx}"
                items[sid] = {
                    "id": sid,
                    "source": source_name,
                    "published_bjt": dt.strftime("%Y-%m-%d %H:%M") if dt else "近24小时",
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
    headers = {"User-Agent": "MorningReaderBot/1.0"}
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
            data = json.loads(p.read_text(encoding="utf-8"))
            data.setdefault("news_history", [])
            data.setdefault("trivia_history", [])
            data.setdefault("math_history", [])
            data.setdefault("dates", [])
            return data
        except Exception:
            pass
    return {"dates": [], "news_history": [], "trivia_history": [], "math_history": [], "trivia_topics": [], "math_topics": []}


def save_history(hist: dict) -> None:
    (DATA_DIR / "history.json").write_text(json.dumps(hist, ensure_ascii=False, indent=2), encoding="utf-8")


def generate_briefing_json(news_map: dict[str, dict], hist: dict) -> dict:
    client = genai.Client(api_key=GEMINI_API_KEY)
    today_bjt = datetime.now(BJT).strftime("%Y-%m-%d")

    news_block = "\n".join(
        f"[{v['id']}] ({v['source']} | {v['published_bjt']}) {v['title']} — {v['summary']}"
        for v in list(news_map.values())[:60]
    )

    recent_news = "\n".join(f"- {x}" for x in hist.get("news_history", [])[-12:]) or "无"
    past_trivia = "\n".join(f"- {x}" for x in (hist.get("trivia_history") or hist.get("trivia_topics", []))[-25:]) or "无"
    past_math = "\n".join(f"- {x}" for x in (hist.get("math_history") or hist.get("math_topics", []))[-25:]) or "无"

    prompt = f"""今天是 {today_bjt}（北京时间）。

请你撰写一份有编辑判断力的中文早间读物，标题为「晨间读本」，包含「今日见闻」「今日新知」「数学一页」三部分。以约五分钟的舒适阅读量为尺度，三部分合计通常控制在约 1500—2000 汉字，数学推导按需要适度伸缩。排版简洁舒展，以清楚的小标题和自然段落为主，图示或表格只在真正有助于理解时使用。语言准确、具体、流畅、有节奏，兼有优秀新闻写作的叙述力与科普文章的清晰度。

【可信新闻素材库（最近 24–36 小时真实权威来源）】
{news_block}

【此前简报记录（编写前请核对，减少重复；新闻关注增量进展）】
- 近期已报道新闻脉络：
{recent_news}
- 往期「今日新知」主题与角度：
{past_trivia}
- 往期「数学一页」主题与角度：
{past_math}

【各部分要求】
1. **「今日见闻」（3—5 条）**：
   - 从素材库中精选 3—5 件真正值得了解的事，按重要性而非地域、行业或类别配额取舍。
   - 围绕事件的来龙去脉、最新实质变化及其与现实世界的联系组织叙述，使读者获得可理解、可记住的认识，而非一串孤立动态。重要的事件写得稍透，相关事件可合并成一条（此时 `source_ids` 可填多个，如 `["N1", "N4"]`）；有持续价值的主线可跟进新的进展，避免反复复述前日已交代过的背景。
   - 用自然叙事交代背景、因果与影响（不要写成“背景：/进展：/预测：”的八股模板），篇章轻重有别。
   - 各条标明报道或事件日期，且 `source_ids` **必须严格引用素材库中的真实编号**，不得编造来源；将已核实的事实、当事方说法、分析与预测准确归属来源，以简练措辞呈现必要的不确定性。

2. **「今日新知」（1 篇）**：
   - 从自然、历史、社会、语言、艺术、技术、日常生活以及任何值得探索的领域，选择一则真正拓宽视野的知识。
   - 以值得好奇的问题、现象或故事切入，用自然段落讲清缘由、机制或更广泛的联系（约 350–500 字）。题材持续变化，重视知识本身的发现感与理解上的收获。
   - 提供 `wiki_query`（精准的英文或中文维基词条名，程序将自动核验并附真实可点击延伸阅读链接）和 `source_note`（权威参考来源名称）。

3. **「数学一页」（1 篇）**：
   - 面向具备高中基础、正在学习大学数学的读者。广泛探索数论、组合、几何、概率、统计、逻辑、图论、算法、优化、建模、数学史及分析、代数等领域。
   - 优先选择能增长数学成熟度、审美、基本技能、证明直觉和解决新问题灵感的主题。每次专注一个有启发的思想，可以是一种证明策略、精巧构造、意外联系、反例、实用技巧、思想实验或值得动手的问题；根据选题自然安排例子、公式、推导与思考，不套固定的“定理—证明—练习”格式。
   - 解释要有动机、有关键推理、有恰当的深度（约 400–600 字），公式使用标准 LaTeX（行内 `$...$`，独立公式 `$$...$$`，JSON 字符串内反斜杠双写如 `\\\\frac`、`\\\\sum`）。

成稿时请自我检查信息时效、来源对应、数学正确性与表达清晰度，并做一轮删减润色，使每一段都推进理解，整体像经过认真编辑的短篇晨读，而非模板化条目汇编。

请严格输出如下 JSON 结构（不要包含多余说明）：
{{
  "date": "{today_bjt}",
  "news": [
    {{
      "headline": "精炼而有信息量的中文小标题",
      "event_date": "2026-11-20",
      "source_ids": ["N1"],
      "paragraphs": ["第一自然段……", "第二自然段……"]
    }}
  ],
  "trivia": {{
    "headline": "有吸引力的中文小标题",
    "domain": "领域标签，如：语言学 / 海洋生物学",
    "paragraphs": ["切入问题或现象……", "机制与更广泛联系……"],
    "core_takeaway": "一句话核心结论（用于历史去重记录）",
    "source_note": "权威来源名称，如 Smithsonian / Nature / Wikipedia",
    "wiki_query": "Exact Wikipedia article title"
  }},
  "math": {{
    "headline": "有启发性的小标题",
    "domain": "领域标签，如：组合与概率 / 线性代数 / 数论",
    "paragraphs": ["动机与问题引入……", "关键推理与 LaTeX 公式……", "延伸思考或直觉收尾……"],
    "core_idea": "一句话核心思想与讲解角度（用于历史去重记录）"
  }}
}}"""

    models_to_try = ["gemini-4-hydrogen", "gemini-3.5-flash"]
    last_err = None
    for model_name in models_to_try:
        for attempt in range(2):
            try:
                print(f"Calling {model_name} (attempt {attempt + 1})...")
                resp = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.45,
                    ),
                )
                print(f"Succeeded with {model_name}")
                return json.loads(resp.text)
            except Exception as e:
                last_err = e
                print(f"[WARN] {model_name} failed: {e}")
                time.sleep(3)
    raise RuntimeError(f"All Gemini models failed: {last_err}")


def render_html(briefing: dict, news_map: dict[str, dict], trivia_url: str, trivia_wiki_title: str) -> str:
    today_str = briefing["date"]
    news_html = ""
    for item in briefing.get("news", []):
        sids = item.get("source_ids") or []
        links = []
        for sid in sids:
            src = news_map.get(sid)
            if src:
                links.append(f'<a href="{src["url"]}" target="_blank" rel="noopener">{src["source"]} ({src["published_bjt"]}) ↗</a>')
        links_html = " · ".join(links) if links else "权威信源"
        paras = "".join(f"<p>{p}</p>" for p in item.get("paragraphs", []))
        news_html += f"""
        <section class="story">
          <div class="story-meta">{item.get('event_date', today_str)}</div>
          <h3>{item.get('headline','')}</h3>
          {paras}
          <div class="sources">来源：{links_html}</div>
        </section>"""

    tr = briefing.get("trivia", {})
    tr_paras = "".join(f"<p>{p}</p>" for p in tr.get("paragraphs", []))

    mt = briefing.get("math", {})
    mt_paras = "".join(f"<p>{p}</p>" for p in mt.get("paragraphs", []))

    arch_files = sorted(ARCHIVE_DIR.glob("*.html"), reverse=True)[:12]
    arch_html = " · ".join(f'<a href="archive/{f.name}">{f.stem}</a>' for f in arch_files if f.stem != today_str)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>晨间读本 · {today_str}</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css">
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/contrib/auto-render.min.js"
  onload="renderMathInElement(document.body, {{delimiters:[{{left:'$$',right:'$$',display:true}},{{left:'$',right:'$',display:false}}]}});"></script>
<style>
  :root {{ --bg:#fafaf9; --paper:#ffffff; --text:#1c1917; --muted:#57534e; --rule:#e7e5e4; --accent:#1d4ed8; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#0c0a09; --paper:#141210; --text:#f5f5f4; --muted:#a8a29e; --rule:#292524; --accent:#60a5fa; }}
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; padding:18px 14px 40px; background:var(--bg); color:var(--text); font-family:"PingFang SC","Noto Serif SC","Microsoft YaHei",sans-serif; line-height:1.78; font-size:16px; }}
  .container {{ max-width:680px; margin:0 auto; background:var(--paper); padding:26px 22px; border-radius:14px; border:1px solid var(--rule); }}
  header {{ border-bottom:2px solid var(--text); padding-bottom:14px; margin-bottom:26px; display:flex; justify-content:space-between; align-items:baseline; flex-wrap:wrap; gap:8px; }}
  header h1 {{ margin:0; font-size:24px; letter-spacing:1px; }}
  header .date {{ font-size:13.5px; color:var(--muted); }}
  h2.part {{ font-size:18px; margin:30px 0 14px; padding-bottom:6px; border-bottom:1px solid var(--rule); color:var(--accent); letter-spacing:0.5px; }}
  .story {{ margin-bottom:24px; }}
  .story-meta {{ font-size:12.5px; color:var(--muted); margin-bottom:2px; }}
  h3 {{ margin:0 0 8px; font-size:17.5px; line-height:1.4; color:var(--text); }}
  p {{ margin:0 0 11px; text-align:justify; }}
  .sources {{ font-size:13px; color:var(--muted); margin-top:6px; }}
  .sources a {{ color:var(--accent); text-decoration:none; }}
  .sources a:hover {{ text-decoration:underline; }}
  .math-wrap {{ overflow-x:auto; }}
  footer {{ margin-top:34px; padding-top:14px; border-top:1px solid var(--rule); font-size:12.5px; color:var(--muted); text-align:center; }}
  footer a {{ color:var(--accent); text-decoration:none; }}
</style>
</head>
<body>
<article class="container">
  <header>
    <h1>晨间读本</h1>
    <div class="date">{today_str} · 约 5 分钟阅读</div>
  </header>

  <h2 class="part">今日见闻</h2>
  {news_html}

  <h2 class="part">今日新知 · {tr.get('domain','探索')}</h2>
  <section class="story">
    <h3>{tr.get('headline','')}</h3>
    {tr_paras}
    <div class="sources">参考与延伸阅读：{tr.get('source_note','Wikipedia')} · <a href="{trivia_url}" target="_blank" rel="noopener">{trivia_wiki_title} ↗</a></div>
  </section>

  <h2 class="part">数学一页 · {mt.get('domain','数学思想')}</h2>
  <section class="story math-wrap">
    <h3>{mt.get('headline','')}</h3>
    {mt_paras}
  </section>

  <footer>
    <div>往期存档：{arch_html or '今日为新改版首期'}</div>
    <div style="margin-top:6px;">生成于 {datetime.now(BJT).strftime('%Y-%m-%d %H:%M BJT')}</div>
  </footer>
</article>
</body>
</html>"""


def send_bark(briefing: dict, news_map: dict[str, dict]) -> None:
    if not BARK_URL:
        print("[WARN] BARK_URL not set; skip push.")
        return
    today_str = briefing["date"]
    lines = ["【今日见闻】"]
    for i, item in enumerate(briefing.get("news", [])[:5], 1):
        lines.append(f"{i}. {item.get('headline','')}")
    tr = briefing.get("trivia", {})
    mt = briefing.get("math", {})
    lines += [
        "",
        f"【今日新知】{tr.get('headline','')}",
        f"▸ {tr.get('core_takeaway','')}",
        "",
        f"【数学一页】{mt.get('headline','')}",
        f"▸ {mt.get('core_idea','')}",
        "",
        "👆 点开阅读完整「晨间读本」（含权威原文链接与公式排版）",
    ]
    body = "\n".join(lines)
    endpoint = BARK_URL if BARK_URL.startswith("http") else f"https://api.day.app/{BARK_URL}"
    r = requests.post(
        endpoint,
        json={"title": f"☀️ 晨间读本 {today_str[5:]}", "body": body, "group": "晨间读本", "url": PAGES_URL},
        timeout=15,
    )
    print("[BARK]", r.status_code, r.text[:200])


def main() -> None:
    no_push = "--no-push" in sys.argv
    news_map = fetch_authoritative_news()
    if not news_map:
        raise RuntimeError("No RSS items fetched")
    hist = load_history()
    briefing = generate_briefing_json(news_map, hist)
    trivia_title, trivia_url = lookup_wikipedia(briefing.get("trivia", {}).get("wiki_query", "Science"))

    html = render_html(briefing, news_map, trivia_url, trivia_title)
    today_str = briefing["date"]
    (DOCS_DIR / "index.html").write_text(html, encoding="utf-8")
    (ARCHIVE_DIR / f"{today_str}.html").write_text(html, encoding="utf-8")
    (DATA_DIR / f"briefing_{today_str}.json").write_text(json.dumps(briefing, ensure_ascii=False, indent=2), encoding="utf-8")

    # Update rich history for deduplication & incremental news awareness
    news_notes = [f"{today_str}｜{n.get('headline','')}" for n in briefing.get("news", [])]
    hist.setdefault("news_history", []).extend(news_notes)
    hist["news_history"] = hist["news_history"][-40:]

    tr = briefing.get("trivia", {})
    hist.setdefault("trivia_history", []).append(f"{today_str}｜{tr.get('headline','')}（{tr.get('core_takeaway','')}）")
    hist["trivia_history"] = hist["trivia_history"][-60:]

    mt = briefing.get("math", {})
    hist.setdefault("math_history", []).append(f"{today_str}｜[{mt.get('domain','')}] {mt.get('headline','')}（{mt.get('core_idea','')}）")
    hist["math_history"] = hist["math_history"][-60:]

    if today_str not in hist.get("dates", []):
        hist.setdefault("dates", []).append(today_str)
    save_history(hist)

    if not no_push:
        send_bark(briefing, news_map)
    print(f"Briefing generated for {today_str}")


if __name__ == "__main__":
    main()
