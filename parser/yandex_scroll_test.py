from pathlib import Path
from playwright.sync_api import sync_playwright
import json
import re
import time

URL = "https://uslugi.yandex.ru/119737-rechitsy/category/remont-i-stroitelstvo/zaboryi-i-ograzhdeniya--1562"


def get_workers(html):
    m = re.search(
        r'<script[^>]+id="__PRELOADED_STATE__"[^>]*>(.*?)</script>',
        html,
        re.S
    )

    if not m:
        return {}

    raw = m.group(1)

    try:
        state = json.loads(raw)
    except Exception as e:
        print("JSON ERROR:", e)
        return {}

    workers = state.get("workers", {})
    return workers.get("items", {})


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)

    context = browser.new_context(
        locale="ru-RU",
        timezone_id="Europe/Moscow",
        viewport={"width": 1440, "height": 1000},
    )

    page = context.new_page()

    print("OPEN:", URL)

    page.goto(
        URL,
        wait_until="domcontentloaded",
        timeout=60000
    )

    page.wait_for_timeout(3000)

    previous = 0

    for i in range(30):
        html = page.content()
        workers = get_workers(html)

        count = len(workers)

        print(f"SCROLL {i + 1}: workers={count}")

        if count == previous:
            print("NO CHANGE")

        previous = count

        page.evaluate("""
            window.scrollTo({
                top: document.body.scrollHeight,
                behavior: 'instant'
            });
        """)

        page.wait_for_timeout(2000)

    html = page.content()

    Path("data/yandex_scroll.html").write_text(
        html,
        encoding="utf-8"
    )

    workers = get_workers(html)

    print()
    print("=" * 60)
    print("FINAL WORKERS:", len(workers))
    print("=" * 60)

    for i, worker in enumerate(workers.values(), 1):
        personal = worker.get("personalInfo", {})
        print(
            i,
            "|",
            personal.get("displayName"),
            "| rating:",
            worker.get("rating"),
            "| reviews:",
            worker.get("reviewsAmount")
        )

    browser.close()
