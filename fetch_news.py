import feedparser
import anthropic
import json
import os
import re
import html
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

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
    {
        "name": "Google Blog",
        "url": "https://blog.google/feed/",
    },
    {
        "name": "Microsoft Blog",
        "url": "https://blogs.microsoft.com/?feed=rss2",
    },
    {
        "name": "Figma Blog",
        "url": "https://www.figma.com/blog/feed/atom.xml",
    },
]

MAX_ITEMS = 15
CACHE_FILE = "summary_cache.json"
HISTORY_FILE = "article_history.json"
WEEKLY_SUMMARY_FILE = "weekly_summary.json"

CACHE_VERSION = "2"

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

# ---- 掲載ルール（コード側で明文化） ----
# 各カテゴリで「載せる」対象を定義
INCLUSION_RULES = """
## 掲載カテゴリと掲載ルール

### カテゴリ1（新ツール・サービス発表）に含めるもの
- 新しいAIツール・サービスの一般公開・ベータ公開・API公開
- 新しいAIモデルの初回リリース
- 新しいAIプラットフォーム・SDKのローンチ

### カテゴリ2（アップデート・新モデル）に含めるもの
- 既存AIツールの正式アップデート・新バージョンリリース
- 既存AIモデルの新バージョン発表
- 料金体系の変更・API仕様の変更
- 重要なポリシー変更

### カテゴリ3（既存ツールへのAI機能追加）に含めるもの
- 元々AI以外のツール（Figma・Adobe・GitHubなど）へのAI機能追加
- 既存SaaS・開発ツールへのAI統合

### カテゴリ4（業界・規制ニュース）に含めるもの
- AI関連の法律・規制の制定・改正
- 主要AI企業の買収・合併
- 業界全体の実務に影響する大きな戦略変更

### 掲載しないもの（カテゴリ0）
- 単なるAIツールの比較記事・感想記事
- 個人の使い方紹介・チュートリアル
- 資金調達のみのニュース（業界影響が小さいもの）
- 噂・未確認情報・憶測記事
- AIと無関係な記事
"""


# ---- ユーティリティ ----

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
            # 旧形式（文字列のみ）
            converted[k] = {"category": 0, "summary": v, "priority": "low", "cache_version": "0"}
        elif isinstance(v, dict):
            converted[k] = v
    return converted


def is_cache_valid(entry):
    return entry.get("cache_version") == CACHE_VERSION


def parse_published(published_str):
    try:
        return parsedate_to_datetime(published_str).timestamp()
    except Exception:
        return 0


def format_published_ja(published_str):
    JST = timezone(timedelta(hours=9))
    try:
        dt = parsedate_to_datetime(published_str).astimezone(JST)
        return f"{dt.year}年{dt.month}月{dt.day}日"
    except Exception:
        try:
            dt = datetime.fromisoformat(published_str).astimezone(JST)
            return f"{dt.year}年{dt.month}月{dt.day}日"
        except Exception:
            return published_str


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
    return len(s1 & s2) / len(s1 | s2)


def extract_domain(url):
    try:
        match = re.search(r"https?://(?:www\.)?([^/]+)", url)
        return match.group(1) if match else ""
    except Exception:
        return ""


def is_duplicate(item, existing, title_threshold=0.55, date_window_hours=72):
    # 同一URLは確実に重複
    if item["link"] == existing["link"]:
        return True

    # 同一ドメインは別記事として扱う（同サイト内の別記事を誤統合しない）
    if extract_domain(item["link"]) == extract_domain(existing["link"]):
        return False

    # タイトル類似度が閾値未満なら別記事
    sim = title_similarity(item["title"], existing["title"])
    if sim < title_threshold:
        return False

    # タイトルが似ていても公開日時が離れすぎている場合は別記事
    ts1 = item["published_ts"]
    ts2 = existing["published_ts"]
    if ts1 > 0 and ts2 > 0:
        diff_hours = abs(ts1 - ts2) / 3600
        if diff_hours > date_window_hours:
            return False

    return True


def deduplicate(items):
    merged = []
    for item in items:
        matched = False
        for existing in merged:
            if is_duplicate(item, existing):
                sources = existing["source"].split(" / ")
                if item["source"] not in sources:
                    existing["source"] = existing["source"] + " / " + item["source"]
                matched = True
                break
        if not matched:
            merged.append(dict(item))
    return merged


# ---- 記事履歴 ----

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


# ---- 週次サマリー ----

def should_generate_weekly(weekly_data):
    today = datetime.now(timezone.utc)
    if today.weekday() != 0:
        return False
    last = weekly_data.get("generated_date", "")
    return last != today.strftime("%Y-%m-%d")


def generate_weekly_summary(client, history):
    today = datetime.now(timezone.utc)
    week_ago = today - timedelta(days=7)
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


# ---- 分類（Step1） ----

def classify_article(client, title, raw_summary):
    prompt = f"""以下のニュース記事を分類してください。

{INCLUSION_RULES}

## 記事
タイトル: {title}
本文抜粋: {raw_summary}

## 出力形式（JSONのみ、説明文不要）
{{
  "include": true/false,
  "category": <0〜4の数字>,
  "priority": "high/medium/low"
}}

- include: 上記の掲載ルールに該当するならtrue、しないならfalse
- category: 掲載ルールに基づくカテゴリ番号（includeがfalseの場合は0）
- priority: high=実務への影響が大きい、medium=参考になる、low=参考程度"""

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    text = message.content[0].text.strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        result = json.loads(text)
        include = bool(result.get("include", False))
        category = int(result.get("category", 0))
        priority = result.get("priority", "low")
        if not include:
            category = 0
        return include, category, priority
    except Exception:
        return False, 0, "low"


# ---- 要約（Step2） ----

def summarize_article(client, title, raw_summary, category):
    label = CATEGORY_LABELS.get(category, "")
    prompt = f"""以下のニュース記事（カテゴリ：{label}）をWebエンジニア視点で日本語2〜3文に要約してください。
実務への影響・使えるツール・APIの変化があれば優先して触れてください。

タイトル: {title}
本文抜粋: {raw_summary}

要約文のみを返してください（JSON不要）。"""

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip()


# ---- フィード取得 ----

def fetch_feed(feed_info, client, cache):
    parsed = feedparser.parse(feed_info["url"])
    items = []
    for entry in parsed.entries[:MAX_ITEMS]:
        title = entry.get("title", "(no title)")
        link = entry.get("link", "#")
        raw_summary = entry.get("summary", "")[:500]
        published = entry.get("published", "")

        cached = cache.get(link)

        if cached and is_cache_valid(cached):
            category = cached["category"]
            ai_summary = cached["summary"]
            priority = cached.get("priority", "medium")
            print(f"  [cache] (cat={category}) {title[:40]}")
        else:
            print(f"  [classify] {title[:40]}")
            try:
                include, category, priority = classify_article(client, title, raw_summary)
                if include:
                    ai_summary = summarize_article(client, title, raw_summary, category)
                else:
                    ai_summary = ""
                cache[link] = {
                    "category": category,
                    "summary": ai_summary,
                    "priority": priority,
                    "cache_version": CACHE_VERSION,
                }
            except Exception as e:
                print(f"    Error: {e}")
                category, ai_summary, priority = 0, "", "low"

        if category == 0:
            continue

        items.append({
            "title": html.escape(title),
            "link": link,
            "summary": html.escape(ai_summary),
            "published": published,
            "published_ja": format_published_ja(published),
            "published_ts": parse_published(published),
            "source": feed_info["name"],
            "category": category,
            "priority": priority,
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

    # カテゴリ昇順 → カテゴリ内は公開日時の新しい順
    sorted_items = sorted(
        deduped,
        key=lambda x: (x["category"], -x["published_ts"])
    )

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
        <span class="meta">{item['source']} &nbsp;·&nbsp; {item['published_ja']}</span>
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

    history = save_to_history(all_items)
    print(f"History saved. total={len(history)} articles")

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
