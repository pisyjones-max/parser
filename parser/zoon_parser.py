import argparse
import asyncio
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.json"
OUTPUT_PATH = ROOT / "data" / "companies.json"
SOCIAL_HOSTS = {
    "vk.com", "m.vk.com", "instagram.com", "www.instagram.com", "t.me",
    "telegram.me", "youtube.com", "www.youtube.com", "rutube.ru", "ok.ru",
    "dzen.ru", "tiktok.com", "wa.me"
}


def clean(value):
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def unique(values):
    result, seen = [], set()
    for value in values:
        value = clean(value)
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def normalize_url(url):
    if not url:
        return ""
    parsed = urlparse(url)
    if not parsed.scheme:
        url = "https://" + url
        parsed = urlparse(url)
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/") or "/", "", parsed.query, ""))


def stable_id(url):
    return "zoon_" + hashlib.sha1(normalize_url(url).encode("utf-8")).hexdigest()[:20]


def parse_number(text):
    match = re.search(r"(\d+(?:[.,]\d+)?)", clean(text).replace(",", "."))
    return float(match.group(1)) if match else None


def parse_reviews(text):
    match = re.search(r"(\d+)\s*(?:отзыв|отзыва|отзывов|оценк|оценки)", clean(text), re.I)
    return int(match.group(1)) if match else None


def is_zoon_url(url):
    try:
        host = urlparse(url).netloc.lower()
        return host == "zoon.ru" or host.endswith(".zoon.ru")
    except Exception:
        return False


def external_website(url):
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower().split(":")[0]
        if not host or host.endswith("zoon.ru"):
            return ""
        if host in SOCIAL_HOSTS or any(host.endswith("." + item) for item in SOCIAL_HOSTS):
            return ""
        return normalize_url(url)
    except Exception:
        return ""


def looks_like_company_url(url):
    path = urlparse(url).path.rstrip("/")
    prefix = "/msk/building/"
    if not path.startswith(prefix):
        return False
    tail = path[len(prefix):]
    blocked = {"type", "metro", "rayon", "street", "district", "search", "reviews", "photo", "video", "price", "services"}
    return bool(tail) and "/" not in tail and tail not in blocked


def looks_like_category_url(url):
    path = urlparse(url).path.rstrip("/")
    return path.startswith("/msk/building/type/") and path != "/msk/building/type"


async def safe_goto(page, url, timeout):
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        await page.wait_for_timeout(900)
        return True
    except PlaywrightTimeoutError:
        print(f"[WARN] timeout: {url}", file=sys.stderr)
        return False
    except Exception as exc:
        print(f"[WARN] goto failed: {url}: {exc}", file=sys.stderr)
        return False


async def collect_category_links(page, start_url, keywords, timeout):
    if not await safe_goto(page, start_url, timeout):
        return set()
    links = set()
    for a in await page.locator("a[href]").all():
        try:
            href = await a.get_attribute("href")
            text = await a.inner_text()
        except Exception:
            continue
        if not href:
            continue
        full = normalize_url(urljoin(start_url, href))
        if not looks_like_category_url(full):
            continue
        haystack = (clean(text) + " " + full).lower()
        if not keywords or any(key.lower() in haystack for key in keywords):
            links.add(full)
    return links


async def collect_pagination_urls(page, current_url, max_pages, timeout):
    if not await safe_goto(page, current_url, timeout):
        return [current_url]
    urls = {normalize_url(current_url)}
    if max_pages == 1:
        return [current_url]
    for a in await page.locator("a[href]").all():
        try:
            href = await a.get_attribute("href")
        except Exception:
            continue
        if not href:
            continue
        full = normalize_url(urljoin(current_url, href))
        if not is_zoon_url(full):
            continue
        cur, cand = urlparse(current_url), urlparse(full)
        if cand.path.rstrip("/") != cur.path.rstrip("/"):
            continue
        if "page=" in cand.query.lower() or re.search(r"/page[-/]?\d+/?$", cand.path.lower()):
            urls.add(full)
    ordered = sorted(urls)
    return ordered[:max_pages] if max_pages else ordered


async def collect_detail_links(page, category_url, timeout):
    if not await safe_goto(page, category_url, timeout):
        return set()
    await page.mouse.wheel(0, 5000)
    await page.wait_for_timeout(700)
    links = set()
    for a in await page.locator("a[href]").all():
        try:
            href = await a.get_attribute("href")
        except Exception:
            continue
        if not href:
            continue
        full = normalize_url(urljoin(category_url, href))
        if is_zoon_url(full) and looks_like_company_url(full):
            links.add(full)
    return links


async def reveal_phones(page):
    selectors = ['text=/показать/i', 'button:has-text("Показать")', 'a:has-text("Показать")']
    for selector in selectors:
        try:
            loc = page.locator(selector)
            for i in range(min(await loc.count(), 8)):
                try:
                    await loc.nth(i).click(timeout=1200)
                    await page.wait_for_timeout(250)
                except Exception:
                    pass
        except Exception:
            pass


async def extract_jsonld(page):
    result = []
    try:
        scripts = await page.locator('script[type="application/ld+json"]').all_text_contents()
        for raw in scripts:
            try:
                data = json.loads(raw)
                result.extend(data if isinstance(data, list) else [data])
            except Exception:
                pass
    except Exception:
        pass
    return result


def jsonld_find(items, key):
    for item in items:
        if not isinstance(item, dict):
            continue
        if key in item:
            return item[key]
        graph = item.get("@graph")
        if isinstance(graph, list):
            for node in graph:
                if isinstance(node, dict) and key in node:
                    return node[key]
    return None


async def parse_detail(page, url, timeout):
    if not await safe_goto(page, url, timeout):
        return None
    await reveal_phones(page)
    body = clean(await page.locator("body").inner_text())
    jsonld = await extract_jsonld(page)

    title = ""
    for selector in ["h1", "h1[itemprop='name']", "[data-name]"]:
        try:
            title = clean(await page.locator(selector).first.inner_text(timeout=2500))
            if title:
                break
        except Exception:
            pass

    address = ""
    for selector in ['[itemprop="address"]', '[class*="address"]', '[data-test*="address"]']:
        try:
            address = clean(await page.locator(selector).first.inner_text(timeout=1800))
            if address:
                break
        except Exception:
            pass
    if not address:
        address_ld = jsonld_find(jsonld, "address")
        if isinstance(address_ld, dict):
            address = clean(address_ld.get("streetAddress") or address_ld.get("addressLocality"))
        elif isinstance(address_ld, str):
            address = clean(address_ld)

    phones = []
    for a in await page.locator('a[href^="tel:"]').all():
        try:
            href = await a.get_attribute("href")
            text = await a.inner_text()
            phones.append(clean(text) or clean(href.replace("tel:", "")))
        except Exception:
            pass

    candidates = []
    for a in await page.locator("a[href]").all():
        try:
            candidates.append((clean(await a.inner_text()), await a.get_attribute("href")))
        except Exception:
            pass

    website = ""
    for text, href in candidates:
        candidate = external_website(urljoin(url, href or ""))
        if candidate and ("официальн" in text.lower() or "сайт" in text.lower()):
            website = candidate
            break
    if not website:
        for _, href in candidates:
            candidate = external_website(urljoin(url, href or ""))
            if candidate:
                website = candidate
                break

    rating = None
    for selector in ['[itemprop="ratingValue"]', '[data-rating]', '[class*="rating"]']:
        try:
            loc = page.locator(selector).first
            rating = parse_number(await loc.get_attribute("content") or await loc.inner_text(timeout=1200))
            if rating is not None:
                break
        except Exception:
            pass
    reviews_count = parse_reviews(body)

    hours = ""
    for selector in ['[itemprop="openingHours"]', '[class*="working"]', '[class*="schedule"]', '[class*="hours"]']:
        try:
            hours = clean(await page.locator(selector).first.inner_text(timeout=1200))
            if hours:
                break
        except Exception:
            pass

    categories = []
    for selector in ['a[href*="/msk/building/type/"]', '[class*="category"] a', '[class*="tag"] a']:
        try:
            for a in await page.locator(selector).all():
                text = clean(await a.inner_text())
                if text:
                    categories.append(text)
        except Exception:
            pass

    prices = unique([line for line in body.splitlines() if "₽" in clean(line) and len(clean(line)) <= 180])[:100]

    description = ""
    for selector in ['[itemprop="description"]', '[class*="description"]', 'meta[name="description"]']:
        try:
            loc = page.locator(selector).first
            description = clean(await loc.get_attribute("content") or await loc.inner_text(timeout=1200))
            if description:
                break
        except Exception:
            pass

    latitude = longitude = None
    geo = jsonld_find(jsonld, "geo")
    if isinstance(geo, dict):
        try:
            latitude = float(geo.get("latitude"))
            longitude = float(geo.get("longitude"))
        except Exception:
            pass

    return {
        "id": stable_id(url),
        "name": title,
        "categories": unique(categories),
        "city": "Москва / Московская область",
        "address": address,
        "phone": unique(phones),
        "website": website,
        "zoon_url": normalize_url(url),
        "rating": rating,
        "reviews_count": reviews_count,
        "working_hours": hours,
        "description": description,
        "prices": prices,
        "latitude": latitude,
        "longitude": longitude,
        "source": "zoon.ru",
        "source_region": "msk",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def merge_records(records):
    merged = {}
    for item in records:
        url = normalize_url(item.get("zoon_url", ""))
        if not url:
            continue
        if url not in merged:
            merged[url] = item
            continue
        old = merged[url]
        for key in ["categories", "phone", "prices"]:
            old[key] = unique(list(old.get(key, [])) + list(item.get(key, [])))
        for key in ["name", "address", "website", "working_hours", "description", "rating", "reviews_count", "latitude", "longitude"]:
            if not old.get(key) and item.get(key):
                old[key] = item[key]
        old["updated_at"] = item.get("updated_at") or old.get("updated_at")
    return list(merged.values())


async def run(args):
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    max_pages = args.max_pages if args.max_pages is not None else config.get("max_pages_per_category", 0)
    max_companies = args.max_companies if args.max_companies is not None else config.get("max_companies", 0)
    timeout = config.get("navigation_timeout_ms", 45000)
    delay = max(0, int(config.get("delay_ms", 1200)))

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=config.get("headless", True))
        context = await browser.new_context(locale="ru-RU")
        page = await context.new_page()
        page.set_default_timeout(7000)

        category_links = set()
        for start_url in config["start_urls"]:
            category_links |= await collect_category_links(page, start_url, config.get("category_keywords", []), timeout)
        print(f"[INFO] categories: {len(category_links)}")

        detail_links = set()
        for category_url in sorted(category_links):
            pages = await collect_pagination_urls(page, category_url, max_pages, timeout)
            for page_url in pages:
                found = await collect_detail_links(page, page_url, timeout)
                detail_links |= found
                print(f"[INFO] {category_url}: +{len(found)}, total={len(detail_links)}")
                await page.wait_for_timeout(delay)
                if max_companies and len(detail_links) >= max_companies:
                    break
            if max_companies and len(detail_links) >= max_companies:
                break

        detail_links = sorted(detail_links)
        if max_companies:
            detail_links = detail_links[:max_companies]
        print(f"[INFO] companies to parse: {len(detail_links)}")

        records = []
        for index, detail_url in enumerate(detail_links, 1):
            record = await parse_detail(page, detail_url, timeout)
            if record and record.get("name"):
                records.append(record)
                print(f"[{index}/{len(detail_links)}] {record['name']}")
            else:
                print(f"[{index}/{len(detail_links)}] skipped: {detail_url}")
            await page.wait_for_timeout(delay)
        await browser.close()

    records = merge_records(records)
    records.sort(key=lambda x: (x.get("name") or "").lower())
    payload = {
        "version": 1,
        "source": "Zoon.ru",
        "scope": config.get("region_scope", "Москва + Московская область"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(records),
        "companies": records,
    }
    OUTPUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[DONE] wrote {len(records)} companies to {OUTPUT_PATH}")


def parse_args():
    parser = argparse.ArgumentParser(description="Zoon.ru parser")
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--max-companies", type=int, default=None)
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
