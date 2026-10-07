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
            if request.resource_type not in {"xhr", "fetch"}:
                return

            print()
            print("[REQ]", request.method, request.url)

            if request.method == "POST" and request.post_data:
                print("[POST DATA]")
                print(request.post_data[:3000])

        async def response_handler(response):
            request = response.request

            if request.resource_type not in {"xhr", "fetch"}:
                return

            print(
                "[RESP]",
                response.status,
                request.method,
                response.url,
            )

            content_type = response.headers.get("content-type", "")

            if "json" in content_type.lower():
                try:
                    body = await response.text()
                    print("[JSON]")
                    print(body[:5000])
                except Exception as e:
                    print("[JSON ERROR]", repr(e))

        page.on("request", request_handler)
        page.on("response", response_handler)

        print("[INFO] opening:", URL)

        response = await page.goto(
            URL,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        print(
            "[INFO] main status:",
            response.status if response else None,
        )

        await page.wait_for_timeout(12000)

        print()
        print("[INFO] PAGE TITLE:", await page.title())
        print("[INFO] FINAL URL:", page.url)
        print("[INFO] DONE")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
