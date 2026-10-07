#!/usr/bin/env python3

import argparse
import asyncio
import json
import math
import os
import random
import re
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


# ============================================================
# CONFIG
# ============================================================

SOURCE_URL = (
    "https://uslugi.yandex.ru/119737-rechitsy/category/"
    "remont-i-stroitelstvo/zaboryi-i-ograzhdeniya--1562"
)

CATEGORY_NAME = "Заборы и ограждения"
CATEGORY_SLUG = "zaboryi-i-ograzhdeniya-1562"

PER_PAGE = 10

MAX_ATTEMPTS = 4
PAGE_TIMEOUT = 60_000

SLEEP_MIN = 3.0
SLEEP_MAX = 7.0

RETRY_DELAYS = [8, 20, 45, 90]

LONG_PAUSE_EVERY = 20
LONG_PAUSE_MIN = 15
LONG_PAUSE_MAX = 30

DATA_DIR = Path("data")
RAW_DIR = DATA_DIR / "raw" / "yandex" / CATEGORY_SLUG
DEBUG_DIR = DATA_DIR / "debug"

COMPANIES_FILE = DATA_DIR / "yandex_companies.json"
STATE_FILE = DATA_DIR / "yandex_parser_state.json"
FAILED_FILE = DATA_DIR / "yandex_failed_pages.json"

SCHEMA_VERSION = 4


# ============================================================
# RUNTIME
# ============================================================

STOP_REQUESTED = False


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def request_stop(signum, frame):
    global STOP_REQUESTED
    STOP_REQUESTED = True
    print(f"\n[STOP] Signal {signum}. Finishing current operation safely...")


signal.signal(signal.SIGINT, request_stop)
signal.signal(signal.SIGTERM, request_stop)


# ============================================================
# IO
# ============================================================

def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)


def atomic_write_json(path: Path, data: Any):
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp = path.with_suffix(path.suffix + ".tmp")

    with tmp.open("w", encoding="utf-8") as f:
        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2,
            sort_keys=False,
        )
        f.flush()
        os.fsync(f.fileno())

    os.replace(tmp, path)


def load_json(path: Path, default):
    if not path.exists():
        return default

    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[WARN] Cannot read {path}: {e}")
        return default


def backup_file(path: Path):
    if not path.exists():
        return

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = path.with_name(
        f"{path.stem}.backup_{stamp}{path.suffix}"
    )

    backup.write_bytes(path.read_bytes())
    print(f"[BACKUP] {path} -> {backup}")


# ============================================================
# SAFE TYPE HELPERS
# ============================================================

def as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def as_list(value) -> list:
    return value if isinstance(value, list) else []


def as_str(value, default="") -> str:
    if value is None:
        return default

    if isinstance(value, str):
        return value.strip()

    return str(value).strip()


def as_float(value):
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def unique_strings(values):
    result = []
    seen = set()

    for value in values:
        value = as_str(value)

        if not value:
            continue

        if value not in seen:
            seen.add(value)
            result.append(value)

    return result


def recursive_strings(value):
    result = []

    if isinstance(value, str):
        if value.strip():
            result.append(value.strip())

    elif isinstance(value, list):
        for item in value:
            result.extend(recursive_strings(item))

    elif isinstance(value, dict):
        for item in value.values():
            result.extend(recursive_strings(item))

    return result


def merge_nonempty(old, new):
    if new is None:
        return old

    if isinstance(new, str) and not new.strip():
        return old

    if isinstance(new, list) and not new:
        return old

    if isinstance(new, dict) and not new:
        return old

    return new


# ============================================================
# URL
# ============================================================

def page_url(page_number: int) -> str:
    separator = "&" if "?" in SOURCE_URL else "?"
    return f"{SOURCE_URL}{separator}p={page_number}"


# ============================================================
# PRELOADED STATE
# ============================================================

def extract_preloaded_state(html: str):
    patterns = [
        r'<script[^>]+id=["\']__PRELOADED_STATE__["\'][^>]*>(.*?)</script>',
        r'<script[^>]+id=["\']__PRELOADED_STATE__["\'][^>]*>(.*?)</script\s*>',
    ]

    for pattern in patterns:
        match = re.search(pattern, html, re.DOTALL | re.IGNORECASE)

        if not match:
            continue

        raw = match.group(1).strip()

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # Sometimes HTML entities / escaping appear.
            raw2 = raw.replace("&quot;", '"')
            try:
                return json.loads(raw2)
            except Exception:
                pass

    return None


# ============================================================
# WORKERS
# ============================================================

def find_workers_container(state):
    if not isinstance(state, dict):
        return {}

    workers = state.get("workers")

    if isinstance(workers, dict):
        items = workers.get("items")

        if isinstance(items, dict):
            return items

        if isinstance(items, list):
            return {
                str(i): item
                for i, item in enumerate(items)
                if isinstance(item, dict)
            }

    # Fallback: recursively find a workers/items structure.
    def walk(obj):
        if isinstance(obj, dict):
            workers_obj = obj.get("workers")

            if isinstance(workers_obj, dict):
                items = workers_obj.get("items")

                if isinstance(items, dict):
                    return items

                if isinstance(items, list):
                    return {
                        str(i): item
                        for i, item in enumerate(items)
                        if isinstance(item, dict)
                    }

            for value in obj.values():
                found = walk(value)

                if found:
                    return found

        elif isinstance(obj, list):
            for value in obj:
                found = walk(value)

                if found:
                    return found

        return {}

    return walk(state)


def extract_workers(state):
    items = find_workers_container(state)

    if isinstance(items, dict):
        return [
            worker
            for worker in items.values()
            if isinstance(worker, dict)
        ]

    if isinstance(items, list):
        return [
            worker
            for worker in items
            if isinstance(worker, dict)
        ]

    return []


# ============================================================
# PAGINATION
# ============================================================

def extract_total_items(state):
    candidates = []

    def walk(obj, depth=0):
        if depth > 8:
            return

        if isinstance(obj, dict):
            pagination = obj.get("pagination")

            if isinstance(pagination, dict):
                for key in (
                    "totalItems",
                    "total_items",
                    "totalResults",
                    "total_results",
                ):
                    value = pagination.get(key)

                    if isinstance(value, (int, float)):
                        candidates.append(int(value))

            for key in (
                "totalItems",
                "total_items",
                "totalResults",
                "total_results",
            ):
                value = obj.get(key)

                if isinstance(value, (int, float)):
                    candidates.append(int(value))

            for value in obj.values():
                if isinstance(value, (dict, list)):
                    walk(value, depth + 1)

        elif isinstance(obj, list):
            for value in obj:
                walk(value, depth + 1)

    walk(state)

    # We need the largest plausible result count.
    candidates = [
        x for x in candidates
        if 0 < x < 10_000_000
    ]

    return max(candidates) if candidates else None


def calculate_total_pages(total_items):
    if not total_items:
        return None

    return max(1, math.ceil(total_items / PER_PAGE))


# ============================================================
# GEO
# ============================================================

LOCALITIES = {
    "Гжель",
    "Речицы",
    "Раменское",
    "Жуковский",
    "Бронницы",
    "Люберцы",
    "Домодедово",
    "Коломна",
    "Воскресенск",
    "Егорьевск",
}

LOCALITY_ALIASES = {
    "гжель": "Гжель",
    "речицы": "Речицы",
    "раменское": "Раменское",
    "жуковский": "Жуковский",
    "бронницы": "Бронницы",
    "люберцы": "Люберцы",
    "домодедово": "Домодедово",
    "коломна": "Коломна",
    "воскресенск": "Воскресенск",
    "егорьевск": "Егорьевск",
}


def normalize_city_name(name):
    name = as_str(name).lower()

    name = re.sub(
        r"\b(городской округ|муниципальный округ|муниципальный район)\b",
        "",
        name,
        flags=re.IGNORECASE,
    )

    name = re.sub(
        r"\b(город|г\.|посёлок|поселок|деревня|село|д\.|пгт)\b",
        "",
        name,
        flags=re.IGNORECASE,
    )

    name = re.sub(r"\s+", " ", name).strip()

    return LOCALITY_ALIASES.get(name, name.title())


def collect_area_objects(worker):
    result = []

    personal = as_dict(worker.get("personalInfo"))

    candidates = [
        personal.get("addressesList"),
        personal.get("areasList"),
        personal.get("areas"),
        worker.get("areas"),
        worker.get("serviceAreas"),
    ]

    for candidate in candidates:
        if isinstance(candidate, list):
            result.extend(
                x for x in candidate
                if isinstance(x, dict)
            )

        elif isinstance(candidate, dict):
            result.extend(
                x for x in candidate.values()
                if isinstance(x, dict)
            )

    return result


def extract_addresses(worker):
    personal = as_dict(worker.get("personalInfo"))

    addresses = personal.get("addressesList")

    if isinstance(addresses, list):
        return addresses

    if isinstance(addresses, dict):
        return list(addresses.values())

    return []


def extract_service_areas(worker):
    areas = collect_area_objects(worker)

    result = []

    seen = set()

    for area in areas:
        name = as_str(area.get("name"))

        if not name:
            continue

        key = (
            name,
            json.dumps(
                area.get("lon_lat"),
                ensure_ascii=False,
                sort_keys=True,
            ),
        )

        if key in seen:
            continue

        seen.add(key)

        result.append(area)

    return result


def geo_information(worker):
    addresses = extract_addresses(worker)
    areas = extract_service_areas(worker)

    strings = []

    for value in addresses:
        strings.extend(recursive_strings(value))

    for value in areas:
        strings.extend(recursive_strings(value.get("name")))
        strings.extend(
            recursive_strings(value.get("name_components"))
        )

    strings.extend(
        recursive_strings(worker.get("relevantAddress"))
    )

    haystack = " ".join(strings).lower()

    matched = []

    for locality in LOCALITIES:
        if locality.lower() in haystack:
            matched.append(locality)

    return {
        "matched_localities": unique_strings(matched),
        "addresses": addresses,
        "service_areas": areas,
    }


# ============================================================
# SPECIALIZATIONS
# ============================================================

def extract_specializations(worker):
    """
    Яндекс реально отдаёт occupations в разных форматах:
      dict
      list
      None

    Никаких .get() без проверки типа.
    """

    occupations = worker.get("occupations")

    result = []

    if isinstance(occupations, dict):
        specializations = occupations.get("specializations")

        if isinstance(specializations, dict):
            specializations = list(specializations.values())

        if isinstance(specializations, list):
            for item in specializations:
                if isinstance(item, dict):
                    name = (
                        item.get("specialistName")
                        or item.get("name")
                        or item.get("title")
                    )
                else:
                    name = item

                if name:
                    result.append(as_str(name))

    elif isinstance(occupations, list):
        for item in occupations:
            if isinstance(item, dict):
                name = (
                    item.get("specialistName")
                    or item.get("name")
                    or item.get("title")
                )

                if name:
                    result.append(as_str(name))

                nested = item.get("specializations")

                if isinstance(nested, list):
                    for subitem in nested:
                        if isinstance(subitem, dict):
                            name = (
                                subitem.get("specialistName")
                                or subitem.get("name")
                                or subitem.get("title")
                            )

                            if name:
                                result.append(as_str(name))

                        elif subitem:
                            result.append(as_str(subitem))

            elif item:
                result.append(as_str(item))

    return unique_strings(result)


# ============================================================
# PORTFOLIO
# ============================================================

def extract_portfolio(worker):
    portfolio = worker.get("portfolio")

    if isinstance(portfolio, dict):
        for key in ("items", "works", "portfolio"):
            value = portfolio.get(key)

            if isinstance(value, list):
                return value

        return [portfolio]

    if isinstance(portfolio, list):
        return portfolio

    return []


def extract_videos(worker):
    videos = worker.get("workerVideos")

    if isinstance(videos, dict):
        videos = videos.get("uploadedVideos")

    if isinstance(videos, list):
        return videos

    return []


# ============================================================
# PROFILE
# ============================================================

def extract_profile(worker):
    personal = as_dict(worker.get("personalInfo"))

    social_links = personal.get("socialLinks")

    if not isinstance(social_links, list):
        if isinstance(social_links, dict):
            social_links = list(social_links.values())
        else:
            social_links = []

    site = personal.get("site")

    if isinstance(site, list):
        site = site[0] if site else ""

    return {
        "name": (
            personal.get("displayName")
            or personal.get("firstName")
            or ""
        ),
        "first_name": personal.get("firstName"),
        "last_name": personal.get("lastName"),
        "middle_name": personal.get("middleName"),
        "description": personal.get("description"),
        "avatar": personal.get("avatar"),
        "site": site,
        "social_links": social_links,
        "phone_id": personal.get("phoneId"),
        "account_type": personal.get("accountType"),
        "is_trusted": personal.get("isTrustedWorker"),
        "finished_orders": personal.get("finishedOrders"),
    }


# ============================================================
# NORMALIZATION
# ============================================================

def stable_worker_id(worker):
    for key in (
        "ydoWorkerId",
        "id",
        "seoid",
        "seoname",
    ):
        value = worker.get(key)

        if value is not None and as_str(value):
            return as_str(value)

    personal = as_dict(worker.get("personalInfo"))

    for key in ("displayName", "firstName"):
        value = personal.get(key)

        if value:
            return f"name:{as_str(value).lower()}"

    return None


def normalize_worker(worker, source_url):
    if not isinstance(worker, dict):
        raise ValueError("worker is not dict")

    profile = extract_profile(worker)
    geo = geo_information(worker)

    name = profile["name"]

    result = {
        "schema_version": SCHEMA_VERSION,

        "id": stable_worker_id(worker),

        "source": "yandex_uslugi",
        "source_category": CATEGORY_NAME,
        "source_url": source_url,

        "import_status": "seed",

        "name": name,
        "first_name": profile["first_name"],
        "last_name": profile["last_name"],
        "middle_name": profile["middle_name"],

        "description": profile["description"],

        "avatar": profile["avatar"],
        "site": profile["site"],
        "social_links": profile["social_links"],

        "phone_id": profile["phone_id"],

        "account_type": profile["account_type"],
        "is_trusted": profile["is_trusted"],
        "finished_orders": profile["finished_orders"],

        "worker_id": worker.get("id"),
        "ydo_worker_id": worker.get("ydoWorkerId"),

        "profile_geo_id": worker.get("profileGeoId"),
        "seoname": worker.get("seoname"),
        "seoid": worker.get("seoid"),

        "rating": worker.get("rating"),

        "rating_stats": worker.get("ratingStats"),
        "reviews_count": worker.get("reviewsAmount"),
        "completed_reviews_count": worker.get(
            "completedReviewsAmount"
        ),

        "relevant_address": worker.get("relevantAddress"),
        "has_relevant_address_matched": worker.get(
            "hasRelevantAddressMatched"
        ),

        "distance_to": worker.get("distanceTo"),
        "is_near": worker.get("isNear"),

        "specializations": extract_specializations(worker),

        "addresses": geo["addresses"],
        "service_areas": geo["service_areas"],
        "matched_localities": geo["matched_localities"],

        "portfolio": extract_portfolio(worker),
        "videos": extract_videos(worker),

        "settings": worker.get("settings"),
        "display_options": worker.get("displayOptions"),

        "first_seen_at": now_iso(),
        "last_seen_at": now_iso(),

        "source_urls": [source_url],
    }

    return result


# ============================================================
# MERGE
# ============================================================

def merge_record(old, new):
    if not old:
        return new

    merged = dict(old)

    for key, value in new.items():

        if key in ("first_seen_at",):
            continue

        if key == "source_urls":
            merged[key] = unique_strings(
                as_list(old.get(key))
                + as_list(value)
            )
            continue

        if key in (
            "specializations",
            "addresses",
            "service_areas",
            "matched_localities",
            "portfolio",
            "videos",
            "social_links",
        ):
            old_value = old.get(key)
            new_value = value

            if isinstance(old_value, list) and isinstance(
                new_value, list
            ):
                if key in (
                    "specializations",
                    "matched_localities",
                ):
                    merged[key] = unique_strings(
                        old_value + new_value
                    )
                else:
                    merged[key] = old_value + [
                        x for x in new_value
                        if x not in old_value
                    ]

            elif new_value:
                merged[key] = new_value

            continue

        merged[key] = merge_nonempty(
            merged.get(key),
            value,
        )

    merged["last_seen_at"] = now_iso()

    return merged


# ============================================================
# STATE
# ============================================================

def default_state():
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "created",

        "source_url": SOURCE_URL,
        "category": CATEGORY_NAME,

        "current_page": -1,
        "total_pages": None,
        "max_total_items": 0,

        "completed_pages": [],
        "failed_pages": [],

        "workers_seen": 0,
        "companies_count": 0,

        "started_at": None,
        "updated_at": now_iso(),
        "finished_at": None,
    }


def load_state():
    state = load_json(STATE_FILE, None)

    if not isinstance(state, dict):
        return default_state()

    base = default_state()
    base.update(state)

    return base


def save_state(state):
    state["updated_at"] = now_iso()
    atomic_write_json(STATE_FILE, state)


# ============================================================
# FAILED PAGES
# ============================================================

def load_failed():
    data = load_json(FAILED_FILE, [])

    if not isinstance(data, list):
        return []

    return data


def save_failed(failed):
    atomic_write_json(FAILED_FILE, failed)


def add_failed_page(failed, page, reason, attempts):
    existing = None

    for item in failed:
        if isinstance(item, dict) and item.get("page") == page:
            existing = item
            break

    record = {
        "page": page,
        "url": page_url(page),
        "reason": reason,
        "attempts": attempts,
        "first_failed_at": (
            existing.get("first_failed_at", now_iso())
            if existing
            else now_iso()
        ),
        "last_failed_at": now_iso(),
        "recovery_attempts": (
            existing.get("recovery_attempts", 0)
            if existing
            else 0
        ),
    }

    if existing:
        existing.update(record)
    else:
        failed.append(record)

    save_failed(failed)


def remove_failed_page(failed, page):
    failed[:] = [
        item for item in failed
        if not (
            isinstance(item, dict)
            and item.get("page") == page
        )
    ]

    save_failed(failed)


# ============================================================
# RAW
# ============================================================

def save_raw_page(page_number, state):
    path = RAW_DIR / f"page-{page_number:04d}.json"

    workers = extract_workers(state)

    payload = {
        "page": page_number,
        "url": page_url(page_number),
        "saved_at": now_iso(),
        "workers": workers,
    }

    atomic_write_json(path, payload)


# ============================================================
# PAGE FETCH
# ============================================================

async def fetch_page(page, page_number, attempt):
    url = page_url(page_number)

    print(
        f"[PAGE] {url} "
        f"(attempt {attempt}/{MAX_ATTEMPTS})"
    )

    try:
        response = await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT,
        )

        status = response.status if response else None

        print(f"[HTTP] {status}")

        if status in (403, 429):
            return None, f"HTTP {status}"

        if status and status >= 500:
            return None, f"HTTP {status}"

        try:
            await page.locator(
                "#__PRELOADED_STATE__"
            ).wait_for(timeout=20_000)
        except PlaywrightTimeoutError:
            pass

        html = await page.content()

        # Obvious anti-bot/block page.
        low = html.lower()

        blocked_markers = (
            "captcha",
            "вы робот",
            "проверяем, что вы не робот",
            "too many requests",
        )

        if any(marker in low for marker in blocked_markers):
            return None, "possible_block_or_captcha"

        state = extract_preloaded_state(html)

        if state is None:
            debug_path = (
                DEBUG_DIR
                / f"page-{page_number:04d}-attempt-{attempt}.html"
            )

            debug_path.write_text(
                html,
                encoding="utf-8",
            )

            return None, "preloaded_state_not_found"

        workers = extract_workers(state)

        total_items = extract_total_items(state)

        print(
            f"[DATA] workers={len(workers)} "
            f"page={page_number} "
            f"total_items={total_items}"
        )

        if not workers:
            return None, "workers_empty"

        return {
            "state": state,
            "workers": workers,
            "total_items": total_items,
        }, None

    except PlaywrightTimeoutError:
        return None, "navigation_timeout"

    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


# ============================================================
# PROCESS PAGE
# ============================================================

def process_successful_page(
    page_number,
    payload,
    companies,
    state,
):
    workers = payload["workers"]
    total_items = payload["total_items"]

    if total_items:
        old_total = state.get("max_total_items") or 0

        if total_items > old_total:
            state["max_total_items"] = total_items

        candidate_pages = calculate_total_pages(
            state["max_total_items"]
        )

        old_pages = state.get("total_pages")

        if old_pages is None:
            state["total_pages"] = candidate_pages
        else:
            # Never shrink the crawl.
            state["total_pages"] = max(
                old_pages,
                candidate_pages or 0,
            )

    for index, worker in enumerate(workers):
        try:
            normalized = normalize_worker(
                worker,
                page_url(page_number),
            )

            worker_id = normalized.get("id")

            if not worker_id:
                print(
                    f"[WARN] Worker without stable ID "
                    f"page={page_number} index={index}"
                )
                continue

            if worker_id in companies:
                companies[worker_id] = merge_record(
                    companies[worker_id],
                    normalized,
                )
            else:
                companies[worker_id] = normalized

            state["workers_seen"] += 1

        except Exception as e:
            # IMPORTANT:
            # A single malformed worker must NEVER
            # kill the whole page / parser.
            print(
                f"[WORKER-ERROR] "
                f"page={page_number} "
                f"index={index}: "
                f"{type(e).__name__}: {e}"
            )

    save_raw_page(
        page_number,
        payload["state"],
    )

    if page_number not in state["completed_pages"]:
        state["completed_pages"].append(page_number)

    state["completed_pages"] = sorted(
        set(state["completed_pages"])
    )

    state["current_page"] = page_number
    state["companies_count"] = len(companies)

    save_state(state)

    atomic_write_json(
        COMPANIES_FILE,
        list(companies.values()),
    )

    print(
        f"[SAVED] page={page_number} "
        f"companies={len(companies)}"
    )


# ============================================================
# MAIN CRAWL
# ============================================================

async def crawl(args):
    ensure_dirs()

    if args.fresh:
        print("[FRESH] Backing up current data...")

        backup_file(COMPANIES_FILE)
        backup_file(STATE_FILE)
        backup_file(FAILED_FILE)

        # Start clean.
        if COMPANIES_FILE.exists():
            COMPANIES_FILE.unlink()

        if STATE_FILE.exists():
            STATE_FILE.unlink()

        if FAILED_FILE.exists():
            FAILED_FILE.unlink()

    state = load_state()
    failed = load_failed()

    existing_records = load_json(
        COMPANIES_FILE,
        [],
    )

    companies = {}

    if isinstance(existing_records, list):
        for record in existing_records:
            if not isinstance(record, dict):
                continue

            worker_id = record.get("id")

            if worker_id:
                companies[worker_id] = record

    if state["started_at"] is None:
        state["started_at"] = now_iso()

    state["status"] = "running"

    save_state(state)

    print("=" * 70)
    print("YANDEX PARSER")
    print("=" * 70)
    print(f"URL:       {SOURCE_URL}")
    print(f"CATEGORY:  {CATEGORY_NAME}")
    print(f"PER PAGE:  {PER_PAGE}")
    print(f"COMPANIES: {len(companies)}")
    print(
        f"COMPLETED: "
        f"{len(state.get('completed_pages', []))}"
    )
    print(
        f"FAILED:    "
        f"{len(failed)}"
    )
    print("=" * 70)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
        )

        context = await browser.new_context(
            locale="ru-RU",
        )

        page = await context.new_page()

        page.set_default_timeout(30_000)

        # Reduce bandwidth. We still receive the SSR JSON,
        # including portfolio/video URLs.
        async def route_handler(route):
            try:
                resource_type = route.request.resource_type

                if resource_type in (
                    "image",
                    "media",
                    "font",
                ):
                    await route.abort()
                else:
                    await route.continue_()
            except Exception:
                pass

        await page.route("**/*", route_handler)

        total_pages = state.get("total_pages")

        if total_pages is None:
            total_pages = 1

        completed = set(
            state.get("completed_pages", [])
        )

        # ----------------------------------------------------
        # MAIN PASS
        # ----------------------------------------------------

        page_number = 0

        while True:
            if STOP_REQUESTED:
                break

            total_pages = state.get("total_pages") or total_pages

            # If we already know the total, finish at its end.
            if page_number >= total_pages:
                break

            if page_number in completed:
                page_number += 1
                continue

            success = False
            last_reason = "unknown"

            for attempt in range(1, MAX_ATTEMPTS + 1):
                if STOP_REQUESTED:
                    break

                payload, reason = await fetch_page(
                    page,
                    page_number,
                    attempt,
                )

                if payload:
                    process_successful_page(
                        page_number,
                        payload,
                        companies,
                        state,
                    )

                    remove_failed_page(
                        failed,
                        page_number,
                    )

                    success = True
                    break

                last_reason = reason

                print(
                    f"[RETRY] page={page_number} "
                    f"reason={reason}"
                )

                if attempt < MAX_ATTEMPTS:
                    delay = RETRY_DELAYS[
                        min(
                            attempt - 1,
                            len(RETRY_DELAYS) - 1,
                        )
                    ]

                    print(
                        f"[WAIT] {delay}s before retry..."
                    )

                    await asyncio.sleep(delay)

            if not success:
                print(
                    f"[FAILED] page={page_number} "
                    f"after {MAX_ATTEMPTS} attempts"
                )

                add_failed_page(
                    failed,
                    page_number,
                    last_reason,
                    MAX_ATTEMPTS,
                )

                state["failed_pages"] = [
                    item["page"]
                    for item in failed
                    if isinstance(item, dict)
                    and "page" in item
                ]

                state["current_page"] = page_number
                save_state(state)

            if (
                page_number > 0
                and page_number % LONG_PAUSE_EVERY == 0
            ):
                delay = random.uniform(
                    LONG_PAUSE_MIN,
                    LONG_PAUSE_MAX,
                )

                print(
                    f"[PAUSE] Long pause "
                    f"{delay:.1f}s"
                )

                await asyncio.sleep(delay)

            else:
                delay = random.uniform(
                    SLEEP_MIN,
                    SLEEP_MAX,
                )

                await asyncio.sleep(delay)

            page_number += 1

        # ----------------------------------------------------
        # RECOVERY PASS
        # ----------------------------------------------------

        if not STOP_REQUESTED and failed:
            print("\n" + "=" * 70)
            print("RECOVERY PASS")
            print("=" * 70)

            recovery_pages = [
                item.get("page")
                for item in failed
                if isinstance(item, dict)
                and isinstance(item.get("page"), int)
            ]

            for page_number in sorted(
                set(recovery_pages)
            ):
                if STOP_REQUESTED:
                    break

                print(
                    f"\n[RECOVERY] page={page_number}"
                )

                success = False

                for attempt in range(
                    1,
                    MAX_ATTEMPTS + 1,
                ):
                    if STOP_REQUESTED:
                        break

                    payload, reason = await fetch_page(
                        page,
                        page_number,
                        attempt,
                    )

                    if payload:
                        process_successful_page(
                            page_number,
                            payload,
                            companies,
                            state,
                        )

                        remove_failed_page(
                            failed,
                            page_number,
                        )

                        success = True
                        break

                    print(
                        f"[RECOVERY-RETRY] "
                        f"page={page_number} "
                        f"reason={reason}"
                    )

                    if attempt < MAX_ATTEMPTS:
                        delay = RETRY_DELAYS[
                            min(
                                attempt,
                                len(RETRY_DELAYS) - 1,
                            )
                        ]

                        await asyncio.sleep(delay)

                if not success:
                    for item in failed:
                        if (
                            isinstance(item, dict)
                            and item.get("page")
                            == page_number
                        ):
                            item["recovery_attempts"] = (
                                item.get(
                                    "recovery_attempts",
                                    0,
                                )
                                + MAX_ATTEMPTS
                            )
                            item["last_failed_at"] = (
                                now_iso()
                            )

                    save_failed(failed)

                await asyncio.sleep(
                    random.uniform(
                        SLEEP_MIN,
                        SLEEP_MAX,
                    )
                )

        await context.close()
        await browser.close()

    # --------------------------------------------------------
    # FINISH
    # --------------------------------------------------------

    state["companies_count"] = len(companies)

    state["failed_pages"] = [
        item["page"]
        for item in failed
        if isinstance(item, dict)
        and "page" in item
    ]

    if STOP_REQUESTED:
        state["status"] = "stopped"
    elif failed:
        state["status"] = "completed_with_failed_pages"
    else:
        state["status"] = "completed"

    state["finished_at"] = now_iso()

    atomic_write_json(
        COMPANIES_FILE,
        list(companies.values()),
    )

    save_state(state)
    save_failed(failed)

    print("\n" + "=" * 70)
    print("DONE")
    print("=" * 70)
    print(f"Companies:       {len(companies)}")
    print(
        f"Completed pages: "
        f"{len(state.get('completed_pages', []))}"
    )
    print(
        f"Failed pages:    "
        f"{len(failed)}"
    )
    print(f"FILE:            {COMPANIES_FILE}")
    print(f"STATE:           {STATE_FILE}")
    print(f"FAILED:          {FAILED_FILE}")
    print(f"RAW:             {RAW_DIR}")
    print("=" * 70)


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Robust Yandex Services parser"
    )

    parser.add_argument(
        "--fresh",
        action="store_true",
        help="backup and rebuild database from scratch",
    )

    parser.add_argument(
        "--resume",
        action="store_true",
        help="resume existing state",
    )

    args = parser.parse_args()

    if not args.fresh and not args.resume:
        args.resume = True

    asyncio.run(crawl(args))


if __name__ == "__main__":
    main()
