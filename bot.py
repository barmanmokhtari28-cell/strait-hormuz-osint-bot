import os
import json
import asyncio
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv
from telegram import Bot
from telegram.constants import ParseMode
from playwright.async_api import async_playwright

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "@secretollah")
IS_MANUAL_RUN = os.getenv("MANUAL_RUN", "false").lower() == "true" or os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch"

# Surge / Drop difference threshold (5 vessels)
SURGE_DROP_THRESHOLD = 5 

# Scheduled UTC hours for quarterly reports (4 times a day: 02:00, 08:00, 14:00, 20:00 UTC)
SCHEDULED_HOURS_UTC = [2, 8, 14, 20]

HISTORY_FILE = "history.json"

# Multi-Source Radar Endpoints (Failover Order)
MAP_SOURCES = [
    "https://www.vesselfinder.com/?lat=26.4500&lon=56.3500&zoom=9",
    "https://www.myshiptracking.com/?lat=26.4500&lon=56.3500&zoom=9",
    "https://www.vesselfinder.com/aismap?zoom=9&lat=26.4500&lon=56.3500"
]

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

def load_history():
    """Loads previous vessel counts from history.json."""
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading history file: {e}")
    return {"inbound": None, "outbound": None, "total": None, "last_scheduled_hour": None}

def save_history(history):
    """Saves updated vessel counts to history.json."""
    try:
        with open(HISTORY_FILE, "w") as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving history file: {e}")

async def dismiss_cookie_banners(page):
    """Auto-clicks GDPR consent buttons and forcefully removes overlay banners."""
    consent_selectors = [
        "#onetrust-accept-btn-handler",
        ".fc-cta-consent",
        "button:has-text('Consent')",
        "button:has-text('AGREE')",
        "button:has-text('Accept')",
        "button:has-text('Accept All')",
        ".qc-cmp2-summary-buttons button",
        "#qc-cmp2-ui button"
    ]
    for sel in consent_selectors:
        try:
            btn = page.locator(sel)
            if await btn.count() > 0:
                await btn.first.click(timeout=2000)
                await asyncio.sleep(1)
        except Exception:
            pass

    # Clean up DOM overlay elements
    try:
        await page.evaluate('''() => {
            const overlays = document.querySelectorAll('.fc-ab-root, #onetrust-consent-sdk, .qc-cmp2-container, #qc-cmp2-ui, .ad-banner, #header, header');
            overlays.forEach(e => e.remove());
        }''')
    except Exception:
        pass

async def scan_radar_source(page, url):
    """Navigates to AIS radar source, dismisses overlays, and extracts vessel metrics."""
    logger.info(f"Scanning AIS radar source: {url}")
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(2)
    await dismiss_cookie_banners(page)
    await asyncio.sleep(7)  # Allow AIS markers to render completely

    # Extract ship markers and headings
    ship_data = await page.evaluate('''() => {
        const markers = document.querySelectorAll('.leaflet-marker-icon, svg g[transform], canvas, [class*="vessel"], [class*="ship"], img[src*="ship"]');
        let total = 0, inbound = 0, outbound = 0, anchored = 0;

        markers.forEach(m => {
            const style = window.getComputedStyle(m);
            const transform = style.transform || m.style.transform || '';
            const src = m.src || '';

            if (src.includes('tile') || src.includes('logo') || src.includes('control')) return;

            let angle = null;
            const rotMatch = transform.match(/rotate\((-?\d+\.?\d*)deg\)/);
            if (rotMatch) {
                angle = parseFloat(rotMatch[1]);
            } else if (transform.startsWith('matrix')) {
                const matrixValues = transform.match(/matrix\(([^)]+)\)/);
                if (matrixValues) {
                    const parts = matrixValues[1].split(',').map(p => parseFloat(p.trim()));
                    if (parts.length >= 2) {
                        angle = Math.atan2(parts[1], parts[0]) * (180 / Math.PI);
                    }
                }
            }

            if (angle !== null) {
                total++;
                if (angle < 0) angle += 360;
                if (angle >= 220 && angle <= 340) inbound++;
                else if (angle >= 40 && angle <= 160) outbound++;
                else anchored++;
            }
        });

        return { total, inbound, outbound, anchored };
    }''')

    return ship_data

async def capture_hormuz_map_and_count(output_path="hormuz_snapshot.png"):
    """
    Tries multiple AIS radar map sources to ensure real ship counts (20-60+ vessels) are captured.
    """
    ship_data = {"total": 0, "inbound": 0, "outbound": 0, "anchored": 0}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        # Try sources sequentially until a rich AIS radar response is obtained
        for source_url in MAP_SOURCES:
            try:
                ship_data = await scan_radar_source(page, source_url)
                if ship_data["total"] >= 3:
                    logger.info(f"Success! {ship_data['total']} ships detected on {source_url}")
                    break
                else:
                    logger.warning(f"Low ship count ({ship_data['total']}) on {source_url}. Trying next radar source...")
            except Exception as e:
                logger.error(f"Error scanning {source_url}: {e}")

        # Final check
        page_text = await page.content()
        if "sorry, you have been blocked" in page_text.lower():
            await browser.close()
            raise Exception("Firewall blocked map access. Aborted to protect channel.")

        # Take screenshot of clean AIS radar map
        await page.screenshot(path=output_path)
        await browser.close()

    logger.info(f"Final AIS Metrics: Total={ship_data['total']}, Inbound={ship_data['inbound']}, Outbound={ship_data['outbound']}, Anchored={ship_data['anchored']}")
    return output_path, ship_data

def generate_caption(ship_data, alert_type=None, changes=None):
    """
    Generates Telegram caption with rich HTML formatting (Blockquotes & Monospace Code)
    including 4x daily report schedule and report timestamp.
    """
    total = ship_data.get("total", 0)
    inbound = ship_data.get("inbound", 0)
    outbound = ship_data.get("outbound", 0)
    anchored = ship_data.get("anchored", 0)

    # Current UTC date & time
    now_utc = datetime.now(timezone.utc)
    date_str = now_utc.strftime("%Y-%m-%d")
    time_str = now_utc.strftime("%H:%M UTC")

    if alert_type == "SURGE":
        header = "⚡ <b>هشدار OSINT: افزایش ناگهانی ترافیک دریایی</b> ⚡"
        status_note = "🚨 <b>هشدار:</b> تغییر ناگهانی در ترافیک شناورهای تنگه هرمز شناسایی شد!\n\n"
    elif alert_type == "DROP":
        header = "⚡ <b>هشدار OSINT: کاهش ناگهانی ترافیک دریایی</b> ⚡"
        status_note = "🚨 <b>هشدار:</b> کاهش ناگهانی در ترافیک شناورهای تنگه هرمز شناسایی شد!\n\n"
    else:
        header = "🚨 <b>گزارش OSINT: پایش دوری ترافیک دریایی تنگه هرمز</b> 🚨"
        status_note = ""

    inbound_change = f" (<code>{changes['inbound']:+d}</code>)" if changes and changes.get('inbound') else ""
    outbound_change = f" (<code>{changes['outbound']:+d}</code>)" if changes and changes.get('outbound') else ""

    return (
        f"{header}\n\n"
        f"{status_note}"
        f"📅 <b>تاریخ و زمان:</b> <code>{date_str} | {time_str}</code>\n"
        "📍 <b>منطقه:</b> <code>تنگه هرمز (نقطه خفه)</code>\n"
        "🌊 <b>مختصات:</b> <code>26°27'N 56°21'E</code>\n\n"
        "<blockquote>📊 <b>آمار لحظه‌ای شناورها:</b>\n"
        f"🚢 <b>کل شناورهای شناسایی‌شده:</b> <code>{total}</code>\n"
        f"📥 <b>ورودی (ورود به خلیج فارس):</b> <code>{inbound}</code>{inbound_change}\n"
        f"📤 <b>خروجی (خروج به دریای عمان):</b> <code>{outbound}</code>{outbound_change}\n"
        f"⚓ <b>متوقف / لنگرانداخته:</b> <code>{anchored}</code></blockquote>\n\n"
        "🔍 <i>شناسایی برخط AIS استخراج‌شده از طریق اسکن راداری خودکار.</i>\n\n"
        "⚓ @secretollah 🚢\n"
        "#تنگه_هرمز"
    )

async def run_bot():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("FATAL ERROR: TELEGRAM_BOT_TOKEN missing!")
        return

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    image_path = "hormuz_snapshot.png"
    history = load_history()
    now_utc = datetime.now(timezone.utc)
    current_hour = now_utc.hour

    try:
        # Capture screenshot & vessel numbers
        image_path, ship_data = await capture_hormuz_map_and_count(image_path)
        
        curr_inbound = ship_data["inbound"]
        curr_outbound = ship_data["outbound"]
        
        prev_inbound = history.get("inbound")
        prev_outbound = history.get("outbound")
        last_scheduled_hour = history.get("last_scheduled_hour")

        # Check for first run ever
        is_first_run = prev_inbound is None or prev_outbound is None

        # Check for Anomaly (Surge/Drop)
        alert_type = None
        changes = None

        if not is_first_run:
            diff_inbound = curr_inbound - prev_inbound
            diff_outbound = curr_outbound - prev_outbound

            if diff_inbound >= SURGE_DROP_THRESHOLD or diff_outbound >= SURGE_DROP_THRESHOLD:
                alert_type = "SURGE"
                changes = {"inbound": diff_inbound, "outbound": diff_outbound}
            elif diff_inbound <= -SURGE_DROP_THRESHOLD or diff_outbound <= -SURGE_DROP_THRESHOLD:
                alert_type = "DROP"
                changes = {"inbound": diff_inbound, "outbound": diff_outbound}

        # Check scheduled quarterly post time (02:00, 08:00, 14:00, 20:00 UTC)
        is_scheduled_time = (current_hour in SCHEDULED_HOURS_UTC) and (last_scheduled_hour != current_hour)

        # Send post if manual run, first run, scheduled quarterly time, OR anomaly detected
        should_send_post = IS_MANUAL_RUN or is_first_run or is_scheduled_time or (alert_type is not None)

        if should_send_post:
            logger.info(f"Posting to channel. Manual={IS_MANUAL_RUN}, FirstRun={is_first_run}, Scheduled={is_scheduled_time}, Alert={alert_type}")
            caption = generate_caption(ship_data, alert_type=alert_type, changes=changes)
            
            with open(image_path, "rb") as photo:
                await bot.send_photo(
                    chat_id=TELEGRAM_CHANNEL_ID,
                    photo=photo,
                    caption=caption,
                    parse_mode=ParseMode.HTML
                )
            
            if is_scheduled_time or is_first_run:
                history["last_scheduled_hour"] = current_hour
        else:
            logger.info("Normal traffic scan complete. Silent update saved.")

        # Save history state for next scan
        history["inbound"] = curr_inbound
        history["outbound"] = curr_outbound
        history["total"] = ship_data["total"]
        save_history(history)

    except Exception as e:
        logger.error(f"Error executing bot workflow: {e}")
        
    finally:
        if os.path.exists(image_path):
            os.remove(image_path)

if __name__ == "__main__":
    asyncio.run(run_bot())
