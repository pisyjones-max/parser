from playwright.sync_api import sync_playwright

URL = "https://uslugi.yandex.ru/119737-rechitsy/category/remont-i-stroitelstvo/zaboryi-i-ograzhdeniya--1562"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)

    context = browser.new_context(
        locale="ru-RU",
        timezone_id="Europe/Moscow",
        viewport={"width": 1440, "height": 1000},
    )

    page = context.new_page()

    page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)

    print("BEFORE URL:")
    print(page.url)

    # ищем кнопки/ссылки, связанные с пагинацией
    elements = page.locator("button, a")

    print("\nPOSSIBLE PAGINATION ELEMENTS:")

    for i in range(elements.count()):
        el = elements.nth(i)

        try:
            text = el.inner_text().strip()
            aria = el.get_attribute("aria-label")
            href = el.get_attribute("href")
            disabled = el.get_attribute("disabled")

            combined = f"{text} {aria or ''} {href or ''}".lower()

            if any(x in combined for x in [
                "следующ",
                "предыдущ",
                "next",
                "pagination",
                "страниц"
            ]):
                print(
                    i,
                    "| text=", repr(text),
                    "| aria=", repr(aria),
                    "| href=", repr(href),
                    "| disabled=", repr(disabled)
                )

        except Exception:
            pass

    print("\nBODY END:")
    print(page.locator("body").inner_text()[-5000:])

    browser.close()
