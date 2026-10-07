import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

URL = "https://uslugi.yandex.ru/119737-rechitsy/category/remont-i-stroitelstvo/zaboryi-i-ograzhdeniya--1562"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True
        )

        page = await browser.new_page(
            viewport={"width": 1440, "height": 1000},
            locale="ru-RU",
        )

        print("[INFO] opening:", URL)

        response = await page.goto(
            URL,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        print("[INFO] status:", response.status if response else None)
        print("[INFO] final URL:", page.url)
        print("[INFO] title:", await page.title())

        await page.wait_for_timeout(5000)

        body = await page.locator("body").inner_text()

        print("\n========== BODY PREVIEW ==========\n")
        print(body[:12000])

        print("\n========== LINKS ==========\n")

        links = await page.locator("a").evaluate_all(
            """
            els => els.map(a => ({
                text: (a.innerText || '').trim(),
                href: a.href
            })).filter(x => x.href)
            """
        )

        seen = set()

        for item in links:
            href = item["href"]

            if href in seen:
                continue

            seen.add(href)

            text = " ".join(item["text"].split())

            if "uslugi.yandex.ru" in href:
                print(f"{text[:100]} | {href}")

        Path("data/yandex_probe.html").write_text(
            await page.content(),
            encoding="utf-8"
        )

        print("\n[INFO] HTML saved to data/yandex_probe.html")
        print("[INFO] unique Yandex links:", len(seen))

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
