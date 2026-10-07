import asyncio

from playwright.async_api import async_playwright


URL = "https://uslugi.yandex.ru/119737-rechitsy/category/remont-i-stroitelstvo/zaboryi-i-ograzhdeniya--1562"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        page = await browser.new_page(
            viewport={"width": 1440, "height": 1000},
            locale="ru-RU",
        )

        def request_handler(request):
            url = request.url

            interesting = any(
                x in url.lower()
                for x in [
                    "/api/",
                    "search",
                    "profile",
                    "performer",
                    "service",
                    "master",
                    "graphql",
                ]
            )

            if interesting:
                print("[REQ]", request.method, url)

        page.on("request", request_handler)

        print("[INFO] opening")

        response = await page.goto(
            URL,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        print("[INFO] status:", response.status if response else None)
        print("[INFO] url:", page.url)

        await page.wait_for_timeout(10000)

        print("[INFO] done")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
