import feedparser
import anthropic
import json
import os
import html
from datetime import datetime, timezone

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
]

MAX_ITEMS = 10
CACHE_FILE = "summary_cache.json"

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


def load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        # 古い形式（文字列）のキャッシュを新形式に変換
        converted = {}
        for k, v in raw.items():
            if isinstance(v, str):
                converted[k] = {"category": 0, "summary": v}
            else:
                converted[k] = v
        return converted
    return {}


def save_cache(cache):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


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


def build_html(all_items):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    sorted_items = sorted(all_items, key=lambda x: x["category"])

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
        items_html += f"""
      <li class="news-item">
        <div class="item-header">
          <a href="{item['link']}" target="_blank" rel="noopener">{item['title']}</a>
          <span class="badge" style="background:{color_style.split(';')[0].replace('background:','')};{color_style.split(';')[1] if ';' in color_style else ''}">{CATEGORY_LABELS.get(cat,'')}</span>
        </div>
        <span class="meta">{item['source']} &nbsp;·&nbsp; {item['published']}</span>
        <p class="summary">{item['summary']}</p>
      </li>"""

    if current_cat is not None:
        items_html += "</ul>"

    total = len(sorted_items)

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
    .meta-bar {{ font-size: 12px; color: #888; margin-bottom: 32px; }}
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

    save_cache(cache)

    output = build_html(all_items)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(output)
    print(f"index.html generated. total={len(all_items)} items")


if __name__ == "__main__":
    main()
