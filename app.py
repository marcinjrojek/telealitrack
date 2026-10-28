import asyncio
import datetime
import logging
import os
import sys
from pathlib import Path

import aiosqlite
from dotenv import load_dotenv
from playwright.async_api import async_playwright
from telethon import TelegramClient, events
from telethon.tl.types import PeerUser

import stock

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
PROFILE_DIR = DATA_DIR / "profile"
DATABASE_FILENAME = DATA_DIR / "database.db"
BOT_SESSION = DATA_DIR / "bot.session"

API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHECK_INTERVAL_HOURS = float(os.getenv("CHECK_INTERVAL_HOURS", 1))
HEADLESS = int(os.getenv("HEADLESS", 1)) == 1
ALI_DOMAIN = os.getenv("ALI_DOMAIN", "www.aliexpress.com")
DEFAULT_COUNTRY = os.getenv("DEFAULT_COUNTRY", "TH").upper()
DEFAULT_CURRENCY = os.getenv("DEFAULT_CURRENCY", "USD").upper()

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s [%(name)s.%(funcName)s:%(lineno)d] %(message)s",
    datefmt="%d/%b/%Y %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

client = TelegramClient(BOT_SESSION, API_ID, API_HASH)

# One browser context shared by everything; pages are opened one at a time.
_pw = None
_ctx = None
_browser_lock = asyncio.Lock()
_blocked_notified: set[str] = set()


async def init_db():
    async with aiosqlite.connect(DATABASE_FILENAME) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS watches (
                telegram_usr_id INTEGER,
                item_id TEXT,
                sku_id TEXT,            -- '*' = every variation
                title TEXT,
                creation_date TEXT,
                PRIMARY KEY (telegram_usr_id, item_id, sku_id)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS sku_state (
                item_id TEXT,
                sku_id TEXT,
                label TEXT,
                stock INTEGER,
                in_stock INTEGER,
                price TEXT,
                PRIMARY KEY (item_id, sku_id)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS user_settings (
                telegram_usr_id INTEGER PRIMARY KEY,
                country TEXT,
                currency TEXT
            )
            """
        )
        await db.commit()


async def get_ctx():
    global _pw, _ctx
    if _ctx is None:
        _pw = await async_playwright().start()
        _ctx = await _pw.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=HEADLESS, locale="en-US", viewport={"width": 1280, "height": 900}
        )
    return _ctx


async def fetch_item(item_id: str, uid: int | None = None) -> stock.ItemStock:
    """Fetch an item using the country/currency chosen by user `uid` (or the defaults)."""
    country, currency = await get_settings(uid)
    async with _browser_lock:
        ctx = await get_ctx()
        return await stock.fetch(ctx, item_id, ALI_DOMAIN, HEADLESS, DATA_DIR, country, currency)


async def get_settings(uid: int | None) -> tuple[str, str]:
    async with aiosqlite.connect(DATABASE_FILENAME) as db:
        row = await (
            await db.execute(
                "SELECT country, currency FROM user_settings WHERE telegram_usr_id = ?", (uid,)
            )
        ).fetchone()
    return (row[0] or DEFAULT_COUNTRY, row[1] or DEFAULT_CURRENCY) if row else (DEFAULT_COUNTRY, DEFAULT_CURRENCY)


async def save_setting(uid: int, column: str, value: str):
    assert column in ("country", "currency")
    async with aiosqlite.connect(DATABASE_FILENAME) as db:
        await db.execute("INSERT OR IGNORE INTO user_settings (telegram_usr_id) VALUES (?)", (uid,))
        await db.execute(f"UPDATE user_settings SET {column} = ? WHERE telegram_usr_id = ?", (value, uid))
        await db.commit()


def state_icon(in_stock):
    return {True: "✅", False: "❌", None: "❔"}[in_stock]


def fmt_variation(i, v: stock.Variation):
    qty = f" ({v.stock})" if v.stock is not None else ""
    price = f" – {v.price}" if v.price else ""
    return f"{i}. {state_icon(v.in_stock)} {v.label}{qty}{price}"


# ---------------------------------------------------------------- polling


async def check_item(item_id: str):
    """Fetch an item, diff against stored state, notify watchers of changes."""
    async with aiosqlite.connect(DATABASE_FILENAME) as db:
        watchers = await (
            await db.execute(
                "SELECT telegram_usr_id, sku_id FROM watches WHERE item_id = ?", (item_id,)
            )
        ).fetchall()
        if not watchers:
            return
        old = {
            r[0]: (r[1], r[2])
            for r in await (
                await db.execute(
                    "SELECT sku_id, in_stock, price FROM sku_state WHERE item_id = ?", (item_id,)
                )
            ).fetchall()
        }

        try:
            # Item state is shared between watchers, so the first watcher's region is used.
            item = await fetch_item(item_id, watchers[0][0])
        except stock.Blocked:
            logger.warning(f"Blocked while checking {item_id}")
            for uid in {w[0] for w in watchers} - _blocked_notified:
                _blocked_notified.add(uid)
                await client.send_message(
                    PeerUser(uid),
                    "⚠️ AliExpress shows a bot check, stock checks are paused. "
                    "Run the bot once with HEADLESS=0, solve it, then restart.",
                )
            return
        except Exception as ex:
            logger.error(f"Checking {item_id} failed: {ex}")
            return
        _blocked_notified.clear()
        logger.info(f"Checked {item_id} ({item.title[:40]}): {len(item.variations)} variation(s)")

        for v in item.variations:
            known = v.sku_id in old
            prev, prev_price = old.get(v.sku_id, (None, None))
            now = None if v.in_stock is None else int(v.in_stock)
            # keep the last known price if this fetch didn't return one
            price = v.price or prev_price
            await db.execute(
                "INSERT OR REPLACE INTO sku_state VALUES (?, ?, ?, ?, ?, ?)",
                (item_id, v.sku_id, v.label, v.stock, now, price),
            )
            if not known:
                continue  # baseline

            url = f"https://{ALI_DOMAIN}/item/{item_id}.html"
            msgs = []
            if now is not None and prev != now:
                logger.info(f"{item_id} {v.label}: {prev} -> {now}")
                head = "🟢 *Back in stock!*" if now else "🔴 *Sold out*"
                msgs.append(
                    f"{head}\n{item.title}\n🔹 {v.label}"
                    + (f"\n💰 {price}" if price else "")
                    + (f"\n📦 {v.stock} left" if v.stock is not None else "")
                    + f"\n{url}"
                )
            if v.price and prev_price and v.price != prev_price:
                logger.info(f"{item_id} {v.label}: price {prev_price} -> {v.price}")
                msgs.append(
                    f"💸 *Price changed*\n{item.title}\n🔹 {v.label}\n"
                    f"{prev_price} → {v.price}\n{url}"
                )
            for msg in msgs:
                for uid, sku in watchers:
                    if sku in ("*", v.sku_id):
                        await client.send_message(PeerUser(uid), msg, parse_mode="markdown")
        await db.execute(
            "UPDATE watches SET title = ? WHERE item_id = ?", (item.title, item_id)
        )
        await db.commit()


async def poll_loop():
    while True:
        try:
            async with aiosqlite.connect(DATABASE_FILENAME) as db:
                items = [
                    r[0]
                    for r in await (
                        await db.execute("SELECT DISTINCT item_id FROM watches")
                    ).fetchall()
                ]
            logger.info(f"Polling {len(items)} item(s)")
            for item_id in items:
                await check_item(item_id)
        except Exception as ex:
            logger.error(f"Poll loop error: {ex}")
        await asyncio.sleep(CHECK_INTERVAL_HOURS * 60 * 60)


# ---------------------------------------------------------------- commands


@client.on(events.NewMessage(pattern=r"^/watch\b"))
async def watch(event):
    parts = event.raw_text.split(maxsplit=2)
    item_id = stock.parse_item_id(parts[1]) if len(parts) > 1 else None
    if not item_id:
        await event.reply(
            "❌ Usage: `/watch <item id or URL> [all | 1,3,4]`", parse_mode="markdown"
        )
        return
    selection = parts[2].strip().lower() if len(parts) > 2 else None

    await event.reply("⏳ Fetching variations, this can take ~30s...")
    try:
        item = await fetch_item(item_id, event.sender_id)
    except stock.Blocked:
        await event.reply("⚠️ AliExpress bot check blocked the request. See README (HEADLESS=0).")
        return
    except Exception as ex:
        logger.error(f"/watch fetch failed: {ex}")
        await event.reply("❌ Could not read that item.")
        return

    if selection is None:
        lines = "\n".join(fmt_variation(i, v) for i, v in enumerate(item.variations, 1))
        await event.reply(
            f"📦 *{item.title}*\n{lines}\n\n"
            f"Pick what to watch:\n`/watch {item_id} all`\n`/watch {item_id} 1,3`",
            parse_mode="markdown",
        )
        return

    if selection == "all":
        sku_ids = ["*"]
    else:
        try:
            idx = [int(x) for x in selection.replace(" ", "").split(",") if x]
            sku_ids = [item.variations[i - 1].sku_id for i in idx if i >= 1]
            if not sku_ids or len(sku_ids) != len(idx):
                raise IndexError
        except (ValueError, IndexError):
            await event.reply("❌ Invalid selection. Use `all` or numbers like `1,3`.", parse_mode="markdown")
            return

    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    async with aiosqlite.connect(DATABASE_FILENAME) as db:
        # '*' and specific picks are mutually exclusive per user+item
        await db.execute(
            "DELETE FROM watches WHERE telegram_usr_id = ? AND item_id = ?",
            (event.sender_id, item_id),
        )
        for sku in sku_ids:
            await db.execute(
                "INSERT INTO watches VALUES (?, ?, ?, ?, ?)",
                (event.sender_id, item_id, sku, item.title, ts),
            )
        for v in item.variations:  # baseline so only future changes alert
            await db.execute(
                "INSERT OR IGNORE INTO sku_state VALUES (?, ?, ?, ?, ?, ?)",
                (item_id, v.sku_id, v.label, v.stock, None if v.in_stock is None else int(v.in_stock), v.price),
            )
        await db.commit()

    what = "all variations" if sku_ids == ["*"] else f"{len(sku_ids)} variation(s)"
    await event.reply(
        f"🔔 Watching {what} of *{item.title}*\nChecking every {CHECK_INTERVAL_HOURS:g} h.",
        parse_mode="markdown",
    )


@client.on(events.NewMessage(pattern=r"^/list\b"))
async def list_watches(event):
    async with aiosqlite.connect(DATABASE_FILENAME) as db:
        rows = await (
            await db.execute(
                """
                SELECT w.item_id, w.title, w.sku_id, s.label, s.in_stock
                FROM watches w LEFT JOIN sku_state s
                  ON s.item_id = w.item_id AND (w.sku_id = '*' OR s.sku_id = w.sku_id)
                WHERE w.telegram_usr_id = ? ORDER BY w.item_id
                """,
                (event.sender_id,),
            )
        ).fetchall()
    if not rows:
        await event.reply("❌ You are not watching anything. Try `/watch <id>`.", parse_mode="markdown")
        return
    out, current = [], None
    for item_id, title, sku, label, in_stock in rows:
        if item_id != current:
            current = item_id
            out.append(f"\n📦 *{title}*\n`{item_id}`")
        ic = state_icon(None if in_stock is None else bool(in_stock))
        out.append(f"  {ic} {label or sku}")
    await event.reply("\n".join(out), parse_mode="markdown")


@client.on(events.NewMessage(pattern=r"^/check\b"))
async def check_now(event):
    parts = event.raw_text.split()
    item_id = stock.parse_item_id(parts[1]) if len(parts) > 1 else None
    if not item_id:
        await event.reply("❌ Usage: `/check <item id or URL>`", parse_mode="markdown")
        return
    await event.reply("⏳ Checking...")
    try:
        item = await fetch_item(item_id, event.sender_id)
    except Exception as ex:
        logger.error(f"/check failed: {ex}")
        await event.reply("❌ Could not read that item.")
        return
    lines = "\n".join(fmt_variation(i, v) for i, v in enumerate(item.variations, 1))
    avail = sum(bool(v.in_stock) for v in item.variations)
    await event.reply(
        f"📦 *{item.title}*\n{lines}\n\nAvailable: {avail}/{len(item.variations)}",
        parse_mode="markdown",
    )


@client.on(events.NewMessage(pattern=r"^/unwatch\b"))
async def unwatch(event):
    parts = event.raw_text.split()
    item_id = stock.parse_item_id(parts[1]) if len(parts) > 1 else None
    if not item_id:
        await event.reply("❌ Usage: `/unwatch <item id or URL>`", parse_mode="markdown")
        return
    async with aiosqlite.connect(DATABASE_FILENAME) as db:
        cur = await db.execute(
            "DELETE FROM watches WHERE telegram_usr_id = ? AND item_id = ?",
            (event.sender_id, item_id),
        )
        left = await (
            await db.execute("SELECT 1 FROM watches WHERE item_id = ? LIMIT 1", (item_id,))
        ).fetchone()
        if not left:
            await db.execute("DELETE FROM sku_state WHERE item_id = ?", (item_id,))
        await db.commit()
    await event.reply("🗑️ Removed." if cur.rowcount else "⚠️ You weren't watching that item.")


@client.on(events.NewMessage(pattern=r"^/setcountry\b"))
async def set_country(event):
    parts = event.raw_text.split()
    value = parts[1].upper() if len(parts) > 1 else ""
    if not (len(value) == 2 and value.isalpha()):
        country, _ = await get_settings(event.sender_id)
        await event.reply(
            f"❌ Usage: `/setcountry <2-letter code>` e.g. `/setcountry PL`\nCurrent: {country}",
            parse_mode="markdown",
        )
        return
    await save_setting(event.sender_id, "country", value)
    await event.reply(f"🌍 Country set to {value}.")


@client.on(events.NewMessage(pattern=r"^/setcurrency\b"))
async def set_currency(event):
    parts = event.raw_text.split()
    value = parts[1].upper() if len(parts) > 1 else ""
    if not (len(value) == 3 and value.isalpha()):
        _, currency = await get_settings(event.sender_id)
        await event.reply(
            f"❌ Usage: `/setcurrency <3-letter code>` e.g. `/setcurrency EUR`\nCurrent: {currency}",
            parse_mode="markdown",
        )
        return
    await save_setting(event.sender_id, "currency", value)
    await event.reply(f"💱 Currency set to {value}.")


@client.on(events.NewMessage(pattern=r"^/(help|start)\b"))
async def help_command(event):
    await event.reply(
        "📚 Available Commands:\n\n"
        "/watch [id or URL] - List variations of an item\n"
        "/watch [id] all - Alert on any variation\n"
        "/watch [id] 1,3 - Alert on chosen variations\n"
        "/check [id or URL] - Check stock right now\n"
        "/list - Show what you watch\n"
        "/setcountry [code] - Ship-to country, e.g. PL\n"
        "/setcurrency [code] - Currency, e.g. EUR\n"
        "/unwatch [id] - Stop watching an item\n"
        "/help - Show this message"
    )


async def announce_start():
    """Tell every known user (anyone with a watch or saved settings) that the bot is up."""
    async with aiosqlite.connect(DATABASE_FILENAME) as db:
        uids = [
            r[0]
            for r in await (
                await db.execute(
                    "SELECT telegram_usr_id FROM watches UNION SELECT telegram_usr_id FROM user_settings"
                )
            ).fetchall()
        ]
    for uid in uids:
        try:
            await client.send_message(
                PeerUser(uid), f"🤖 Bot started. Checking every {CHECK_INTERVAL_HOURS:g} h."
            )
        except Exception as ex:
            logger.warning(f"Could not notify {uid} about start: {ex}")


async def main():
    await init_db()
    await client.start(bot_token=BOT_TOKEN)
    logger.info(f"Bot started, polling every {CHECK_INTERVAL_HOURS:g} h")
    await announce_start()
    poller = asyncio.create_task(poll_loop())
    try:
        await client.run_until_disconnected()
    finally:
        poller.cancel()
        if _ctx:
            await _ctx.close()
        if _pw:
            await _pw.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
