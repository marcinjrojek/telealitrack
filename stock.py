"""Fetch variation stock for an AliExpress item with Playwright.

The product page is client-side rendered: SKU/stock data arrives in the page's own
`mtop.aliexpress.pdp.pc.query` response, which we listen to (we never call the API ourselves).
We do not try to evade bot detection; if a challenge appears, fetch() returns BLOCKED.
"""
import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import TimeoutError as PWTimeout

logger = logging.getLogger(__name__)

API_MARKERS = ("mtop.aliexpress.pdp.pc.query", "mtop.aliexpress.itemdetail")
CAPTCHA_WAIT_SECONDS = 180


@dataclass
class Variation:
    sku_id: str
    label: str
    stock: int | None
    in_stock: bool | None
    price: str | None


@dataclass
class ItemStock:
    item_id: str
    title: str
    variations: list[Variation]


class Blocked(Exception):
    """Bot check could not be passed."""


def parse_item_id(text: str) -> str | None:
    m = re.search(r"(\d{10,})", text)
    return m.group(1) if m else None


def parse_mtop(text: str):
    """mtop answers may be plain JSON or JSONP like cb({...})."""
    text = text.strip()
    m = re.match(r"^[\w$.]+\((.*)\)\s*;?\s*$", text, re.S)
    if m:
        text = m.group(1)
    return json.loads(text)


def find_key(obj, *keys):
    """Depth-first search for the first dict key matching any of `keys`."""
    stack = [obj]
    while stack:
        o = stack.pop()
        if isinstance(o, dict):
            for k in keys:
                if k in o and o[k] not in (None, [], {}):
                    return o[k]
            stack.extend(o.values())
        elif isinstance(o, list):
            stack.extend(o)
    return None


def _num(v):
    if isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def build_item(item_id: str, data) -> ItemStock | None:
    props = find_key(data, "skuProperties", "productSKUPropertyList")
    paths = find_key(data, "skuPaths", "skuPriceList")
    price_map = find_key(data, "skuIdStrPriceInfoMap") or {}
    if not paths:
        return None

    names = {}  # "propId:valueId" -> "Property: Value"
    for prop in props or []:
        pid = prop.get("skuPropertyId")
        pname = prop.get("skuPropertyName", str(pid))
        for v in prop.get("skuPropertyValues", []):
            vid = v.get("propertyValueIdLong", v.get("propertyValueId"))
            vname = v.get("propertyValueDisplayName") or v.get("propertyValueName") or str(vid)
            names[f"{pid}:{vid}"] = f"{pname}: {vname}"

    rows = []
    for sku in paths:
        val = sku.get("skuVal") if isinstance(sku.get("skuVal"), dict) else {}
        sku_id = str(sku.get("skuIdStr") or sku.get("skuId") or "")
        raw_ids = sku.get("path") or sku.get("skuPropIds") or sku.get("skuAttr") or ""
        ids = [p.split("#")[0] for p in re.split(r"[;,]", str(raw_ids)) if p]
        label = " | ".join(names.get(p, p) for p in ids) or sku_id

        info = price_map.get(sku_id, {}) if isinstance(price_map, dict) else {}
        qty = None
        for src in (info, val, sku):
            for k in ("skuStock", "availQuantity", "skuStockNum", "inventory", "stock"):
                if _num(src.get(k)) is not None:
                    qty = _num(src.get(k))
                    break
            if qty is not None:
                break

        salable = sku.get("salable")
        if qty is not None:
            in_stock = qty > 0 and salable is not False
        elif isinstance(salable, bool):
            in_stock = salable
        elif isinstance(sku.get("skuStock"), bool):
            in_stock = sku["skuStock"]
        else:
            in_stock = None

        activity = info.get("skuActivityAmount")
        price = (
            info.get("salePriceString")
            or (activity.get("formatedAmount") if isinstance(activity, dict) else None)
            or (val.get("skuAmount") or {}).get("formatedAmount")
        )
        rows.append(Variation(sku_id, label, qty, in_stock, price))

    title = find_key(data, "subject") or f"Item {item_id}"
    return ItemStock(item_id, str(title), rows)


async def _is_challenge(page) -> bool:
    url = page.url.lower()
    if "_____tmd_____" in url or "punish" in url or "captcha" in url:
        return True
    try:
        return "captcha" in (await page.content())[:5000].lower()
    except Exception:
        return False


async def set_region(ctx, domain: str, country: str, currency: str):
    """AliExpress takes ship-to country and currency from the `aep_usuc_f` cookie."""
    root = ".".join(domain.split(".")[-2:])
    await ctx.add_cookies(
        [
            {
                "name": "aep_usuc_f",
                "value": f"site=glo&c_tp={currency}&region={country}&b_locale=en_US",
                "domain": f".{root}",
                "path": "/",
            }
        ]
    )


async def fetch(
    ctx, item_id: str, domain: str, headless: bool, debug_dir: Path,
    country: str | None = None, currency: str | None = None,
) -> ItemStock:
    """Return the item's stock. Raises Blocked on a bot check, RuntimeError if nothing parsed."""
    if country and currency:
        await set_region(ctx, domain, country, currency)
    url = f"https://{domain}/item/{item_id}.html"
    page = await ctx.new_page()
    captured: list[str] = []

    async def on_response(resp):
        if any(mk in resp.url for mk in API_MARKERS):
            try:
                captured.append(await resp.text())
            except Exception:
                pass

    page.on("response", lambda r: asyncio.ensure_future(on_response(r)))
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3000)

        if await _is_challenge(page):
            if headless:
                raise Blocked("bot check in headless mode")
            logger.warning("Bot check: solve it in the browser window (%ss)...", CAPTCHA_WAIT_SECONDS)
            deadline = time.time() + CAPTCHA_WAIT_SECONDS
            while time.time() < deadline and await _is_challenge(page):
                await page.wait_for_timeout(2000)
            if await _is_challenge(page):
                raise Blocked("bot check not solved")
            captured.clear()
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)

        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except PWTimeout:
            pass

        deadline = time.time() + 20
        while not captured and time.time() < deadline:
            await page.wait_for_timeout(500)

        for text in captured:
            try:
                item = build_item(item_id, parse_mtop(text))
            except (json.JSONDecodeError, AttributeError):
                continue
            if item:
                return item

        debug_dir.mkdir(parents=True, exist_ok=True)
        (debug_dir / f"debug_mtop_{item_id}.json").write_text(
            "\n\n=====\n\n".join(captured) or "(no API response captured)", encoding="utf-8"
        )
        (debug_dir / f"debug_page_{item_id}.html").write_text(await page.content(), encoding="utf-8")
        raise RuntimeError(f"could not build variations ({len(captured)} API responses); debug files saved")
    finally:
        await page.close()
