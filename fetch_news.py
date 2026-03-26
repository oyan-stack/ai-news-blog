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
        "lang": "en",
    },
    {
        "name": "Gigazine",
        "url": "https://gigazine.net/news/rss_2.0/",
        "lang": "ja",
    },
]

MAX_ITEMS = 10
CACHE_FILE = "summary_cache.json"


def load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(cache):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def summarize(client, title, summary, lang):
    text = f"タイトル: {title}\n本文抜粋: {summary}"
    prompt = (
        "以下のニュース記事を日本語で2〜3文に要約してください。"
        "簡潔に、重要なポイントだけを伝えてください。\n\n" + text
    )
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip()


def fetch_feed(feed_info, client, cache):
    parsed = feedparser.parse(feed_info["url"])
    items = []
    for entry in parsed.entries[:MAX_ITEMS]:
        title = entry.get("title", "(no title)")
        link = entry.get("link", "#")
        raw_summary = entry.get("summary", "")[:500]
        published = entry.get("published", "")

        # キャッシュ済みならスキップ
        if link in cache:
            ai_summary = cache[link]
            print(f"  [cache] {title[:40]}")
        else:
            print(f"  [summarize] {title[:40]}")
            try:
                ai_summary = summarize(client, title, raw_summary, feed_info["lang"])
                cache[link] = ai_summary
            except Exception as e:
                print(f"    Error: {e}")
                ai_summary = ""

        items.append({
            "title": html.escape(title),
            "link": link,
            "summary": html.escape(ai_summary),
            "published": published,
        })
    return items


def build_html(sections):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    section_html = ""
    for section in sections:
        items_html = ""
        for item in section["items"]:
            items_html += f"""
        <li class="news-item">
          <a href="{item['link']}" target="_blank" rel="noopener">{item['title']}</a>
          <span class="meta">{item['published']}</span>
          <p class="summary">{item['summary']}</p>
        </li>"""
        section_html += f"""
    <section>
      <h2>{html.escape(section['name'])}</h2>
      <ul class="news-list">{items_html}
      </ul>
    </section>"""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AI News</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      max-width: 800px;
      margin: 0 auto;
      padding: 24px 16px;
      background: #f9f9f7;
      color: #1a1a18;
    }}
    h1 {{
      font-size: 20px;
      font-weight: 600;
      margin-bottom: 4px;
    }}
    .updated {{
      font-size: 12px;
      color: #888;
      margin-bottom: 32px;
    }}
    h2 {{
      font-size: 15px;
      font-weight: 600;
      color: #444;
      border-left: 3px solid #4a90d9;
      padding-left: 10px;
      margin-top: 36px;
      margin-bottom: 12px;
    }}
    .news-list {{
      list-style: none;
      padding: 0;
      margin: 0;
    }}
    .news-item {{
      padding: 12px 0;
      border-bottom: 1px solid #e8e8e4;
    }}
    .news-item a {{
      font-size: 14px;
      font-weight: 500;
      color: #1a6bbf;
      text-decoration: none;
      display: block;
      margin-bottom: 2px;
    }}
    .news-item a:hover {{ text-decoration: underline; }}
    .meta {{
      font-size: 11px;
      color: #999;
      display: block;
      margin-bottom: 4px;
    }}
    .summary {{
      font-size: 13px;
      color: #555;
      margin: 0;
      line-height: 1.5;
    }}
  </style>
</head>
<body>
  <h1>AI News</h1>
  <p class="updated">最終更新: {now}</p>
  {section_html}
</body>
</html>"""


def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY が設定されていません")

    client = anthropic.Anthropic(api_key=api_key)
    cache = load_cache()

    sections = []
    for feed_info in FEEDS:
        print(f"Fetching {feed_info['name']}...")
        items = fetch_feed(feed_info, client, cache)
        sections.append({"name": feed_info["name"], "items": items})

    save_cache(cache)

    output = build_html(sections)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(output)
    print("index.html generated.")


if __name__ == "__main__":
    main()
