import feedparser
import anthropic
import json
import os
import re
import html
from datetime import datetime, timezone, timedelta

FEEDS = [
    {
        "name": "VentureBeat AI",
        "url": "https://venturebeat.com/category/ai/feed/",
    },
    {
        "name": "Hacker News (AI)",
        "url": "https://hnrss.org/newest?q=AI&points=50",
    },
    {
        "name": "GitHub Blog",
        "url": "https://github.blog/feed/",
    },
    {
        "name": "OpenAI News",
        "url": "https://openai.com/news/rss.xml",
    },
    {
        "name": "TechCrunch AI",
        "url": "https://techcrunch.com/category/artificial-intelligence/feed/",
    },
    {
        "name": "The Verge AI",
        "url": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
    },
]

MAX_ITEMS = 10
CACHE_FILE = "summary_cache.json"
HISTORY_FILE = "article_history.json"
WEEKLY_SUMMARY_FILE = "weekly_summary.json"

CATEGORY_LABELS = {
    1: "新ツール・サービス発表",
    2: "アップデート・新モデル",
    3: "既存ツールへのAI機能追加",
    4: "業界・規制ニュース",
    0: "その他",
}

CATEGORY_COLORS = {
    1: "#e8f4fd;color:#1a6bbf",
    2: "#edf7ed;color:#2e7d32",
    3: "#fdf3e8;color:#b45309",
    4: "#f3eeff;color:#6d28d9",
    0: "#f1f0ee;color:#888",
}


def load_json(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_cache():
    raw = load_json(CACHE_FILE)
    converted = {}
    for k, v in raw.items():
        if isinstance(v, str):
            converted[k] = {"category": 0, "summary": v}
        else:
            converted[k] = v
    return converted


# ---- 重複統合 ----

def normalize_title(title):
    title = title.lower()
    title = re.sub(r"[^\w\s]", "", title)
    stopwords = {"the", "a", "an", "of", "in", "to", "for", "and", "or", "is", "are", "with", "on", "at", "by"}
    words = [w for w in title.split() if w not in stopwords and len(w) > 2]
    return set(words)


def title_similarity(t1, t2):
    s1 = normalize_title(t1)
    s2 = normalize_title(t2)
    if not s1 or not s2:
        return 0.0
    intersection = s1 & s2
    union = s1 | s2
    return len(intersection) / len(union)


def deduplicate(items, threshold=0.55):
    merged = []
    for item in items:
        matched = False
        for existing in merged:
            if title_similarity(item["title"], existing["title"]) >= threshold:
                # ソース名を統合
                sources = existing["source"].split(" / ")
                if item["source"] not in sources:
                    existing["source"] = existing["source"] + " / " + item["source"]
                matched = True
                break
        if not matched:
            merged.append(dict(item))
    return merged


# ---- 記事履歴の保存 ----

def save_to_history(items):
    history = load_json(HISTORY_FILE)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for item in items:
        if item["link"] not in history:
            history[item["link"]] = {
                "title": item["title"],
                "category": item["category"],
                "summary": item["summary"],
                "source": item["source"],
                "date": today,
            }
    save_json(HISTORY_FILE, history)
    return history


# ---- 週次サマリー生成 ----

def should_generate_weekly(weekly_data):
    today = datetime.now(timezone.utc)
    # 月曜日 (weekday=0) に生成
    if today.weekday() != 0:
        return False
    # 今週すでに生成済みならスキップ
    last = weekly_data.get("generated_date", "")
    if last == today.strftime("%Y-%m-%d"):
        return False
    return True


def generate_weekly_summary(client, history):
    today = datetime.now(timezone.utc)
    week_ago = today - timedelta(days=7)

    # 直近7日のカテゴリ1・2の記事を抽出
    target_articles = []
    for link, data in history.items():
        if data.get("category") in (1, 2):
            try:
                date = datetime.strptime(data["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if date >= week_ago:
                    target_articles.append(data)
            except Exception:
                continue

    if not target_articles:
        return None

    articles_text = "\n".join(
        f"- [{CATEGORY_LABELS[a['category']]}] {a['title']}: {a['summary']}"
        for a in target_articles[:30]
    )

    prompt = f"""以下は先週1週間のAI関連ニュース（新ツール発表・アップデート）の一覧です。
Webエンジニア視点で「今週のAIトレンド」を3〜5箇条で日本語にまとめてください。
各箇条は1〜2文で簡潔に。

{articles_text}

## 出力形式（JSONのみ、説明文不要）
{{"points": ["箇条1", "箇条2", "箇条3"]}}"""

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    text = message.content[0].text.strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        result = json.loads(text)
        return result.get("points", [])
    except Exception:
        return None


# ---- 分類・要約 ----

def classify_and_summarize(client, title, raw_summary):
    prompt = f"""以下のニュース記事について、下記の2点をJSONで返してください。

## 分類ルール（categoryに数字を入れる）
1: 新しいAIツール・サービスの発表（新製品・新サービスのローンチ）
2: 既存AIツールのアップデート・新モデル発表（機能追加、新バージョン、新モデルリリース）
3: 既存の非AIツールへのAI機能追加（FigmaにAI追加、AdobeにAI追加など）
4: AI業界全体への影響（企業買収、法律・規制、大型資金調達など）
0: 上記に該当しない（無関係な記事）

## 要約ルール（summaryに文字列を入れる）
- categoryが0の場合は空文字列
- それ以外はWebエンジニア視点で日本語2〜3文で要約
- 実務への影響・使えるツール・APIの変化があれば優先して触れる

## 記事
タイトル: {title}
本文抜粋: {raw_summary}

## 出力形式（JSONのみ、説明文不要）
{{"category": <数字>, "summary": "<要約文>"}}"""

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    text = message.content[0].text.strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        result = json.loads(text)
        category = int(result.get("category", 0))
        summary = result.get("summary", "")
        return category, summary
    except Exception:
        return 0, ""


def fetch_feed(feed_info, client, cache):
    parsed = feedparser.parse(feed_info["url"])
    items = []
    for entry in parsed.entries[:MAX_ITEMS]:
        title = entry.get("title", "(no title)")
        link = entry.get("link", "#")
        raw_summary = entry.get("summary", "")[:500]
        published = entry.get("published", "")

        if link in cache:
            category = cache[link]["category"]
            ai_summary = cache[link]["summary"]
            print(f"  [cache] (cat={category}) {title[:40]}")
        else:
            print(f"  [classify] {title[:40]}")
            try:
                category, ai_summary = classify_and_summarize(client, title, raw_summary)
                cache[link] = {"category": category, "summary": ai_summary}
            except Exception as e:
                print(f"    Error: {e}")
                category, ai_summary = 0, ""

        if category == 0:
            continue

        items.append({
            "title": html.escape(title),
            "link": link,
            "summary": html.escape(ai_summary),
            "published": published,
            "source": feed_info["name"],
            "category": category,
        })
    return items


# ---- HTML生成 ----

def build_weekly_html(points):
    if not points:
        return ""
    items_html = "".join(f"<li>{html.escape(p)}</li>" for p in points)
    week_str = datetime.now(timezone.utc).strftime("%Y年第%Wweek")
    return f"""
  <div class="weekly-summary">
    <div class="weekly-title">今週のAIトレンド（{week_str}）</div>
    <ul class="weekly-list">{items_html}</ul>
  </div>"""


def build_html(all_items, weekly_points=None):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    deduped = deduplicate(all_items)
    sorted_items = sorted(deduped, key=lambda x: x["category"])

    current_cat = None
    items_html = ""
    for item in sorted_items:
        cat = item["category"]
        if cat != current_cat:
            if current_cat is not None:
                items_html += "</ul>"
            label = CATEGORY_LABELS.get(cat, "その他")
            items_html += f'<h2>{label}</h2><ul class="news-list">'
            current_cat = cat

        color_style = CATEGORY_COLORS.get(cat, CATEGORY_COLORS[0])
        bg = color_style.split(";")[0].replace("background:", "")
        fg = color_style.split(";")[1] if ";" in color_style else ""
        items_html += f"""
      <li class="news-item">
        <div class="item-header">
          <a href="{item['link']}" target="_blank" rel="noopener">{item['title']}</a>
          <span class="badge" style="background:{bg};{fg}">{CATEGORY_LABELS.get(cat,'')}</span>
        </div>
        <span class="meta">{item['source']} &nbsp;·&nbsp; {item['published']}</span>
        <p class="summary">{item['summary']}</p>
      </li>"""

    if current_cat is not None:
        items_html += "</ul>"

    total = len(sorted_items)
    weekly_html = build_weekly_html(weekly_points) if weekly_points else ""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AI News for Web Engineers</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      max-width: 800px;
      margin: 0 auto;
      padding: 24px 16px;
      background: #f9f9f7;
      color: #1a1a18;
    }}
    h1 {{ font-size: 20px; font-weight: 600; margin-bottom: 4px; }}
    .meta-bar {{ font-size: 12px; color: #888; margin-bottom: 24px; }}
    .weekly-summary {{
      background: #fffbea;
      border: 1px solid #f0d060;
      border-radius: 8px;
      padding: 14px 18px;
      margin-bottom: 32px;
    }}
    .weekly-title {{
      font-size: 13px;
      font-weight: 600;
      color: #b45309;
      margin-bottom: 8px;
    }}
    .weekly-list {{
      margin: 0;
      padding-left: 18px;
      font-size: 13px;
      color: #444;
      line-height: 1.7;
    }}
    h2 {{
      font-size: 14px;
      font-weight: 600;
      color: #444;
      border-left: 3px solid #4a90d9;
      padding-left: 10px;
      margin-top: 36px;
      margin-bottom: 12px;
    }}
    .news-list {{ list-style: none; padding: 0; margin: 0; }}
    .news-item {{ padding: 12px 0; border-bottom: 1px solid #e8e8e4; }}
    .item-header {{ display: flex; align-items: flex-start; gap: 8px; margin-bottom: 2px; }}
    .item-header a {{
      font-size: 14px;
      font-weight: 500;
      color: #1a6bbf;
      text-decoration: none;
      flex: 1;
    }}
    .item-header a:hover {{ text-decoration: underline; }}
    .badge {{
      flex-shrink: 0;
      font-size: 10px;
      font-weight: 500;
      padding: 2px 7px;
      border-radius: 8px;
      white-space: nowrap;
      margin-top: 2px;
    }}
    .meta {{ font-size: 11px; color: #999; display: block; margin-bottom: 4px; }}
    .summary {{ font-size: 13px; color: #555; margin: 0; line-height: 1.5; }}
  </style>
</head>
<body>
  <h1>AI News for Web Engineers</h1>
  <p class="meta-bar">最終更新: {now} &nbsp;·&nbsp; {total}件</p>
  {weekly_html}
  {items_html}
</body>
</html>"""


def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY が設定されていません")

    client = anthropic.Anthropic(api_key=api_key)
    cache = load_cache()

    all_items = []
    for feed_info in FEEDS:
        print(f"Fetching {feed_info['name']}...")
        items = fetch_feed(feed_info, client, cache)
        all_items.extend(items)
        print(f"  -> {len(items)} relevant items")

    save_json(CACHE_FILE, cache)

    # 履歴に保存
    history = save_to_history(all_items)
    print(f"History saved. total={len(history)} articles")

    # 週次サマリー（月曜のみ生成）
    weekly_data = load_json(WEEKLY_SUMMARY_FILE)
    weekly_points = weekly_data.get("points")

    if should_generate_weekly(weekly_data):
        print("Generating weekly summary...")
        points = generate_weekly_summary(client, history)
        if points:
            weekly_points = points
            save_json(WEEKLY_SUMMARY_FILE, {
                "generated_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "points": points,
            })
            print(f"Weekly summary generated: {len(points)} points")

    output = build_html(all_items, weekly_points)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(output)
    print(f"index.html generated. total={len(deduplicate(all_items))} items (after dedup)")


if __name__ == "__main__":
    main()
