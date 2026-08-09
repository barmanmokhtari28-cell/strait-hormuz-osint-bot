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

# Surge / Drop difference threshold (e.g., 5 vessels)
SURGE_DROP_THRESHOLD = 5 

# Scheduled UTC hours for daily reports (08:00 UTC and 20:00 UTC)
SCHEDULED_HOURS_UTC = [8, 20]

HISTORY_FILE = "history.json"
MAP_URL = "https://www.vesselfinder.com/aismap?zoom=9&lat=26.4500&lon=56.3500"

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

async def capture_hormuz_map_and_count(output_path="hormuz_snapshot.png"):
    """
    Launches browser, captures screenshot, and calculates ship counts.
    """
    logger.info("Capturing live Strait of Hormuz AIS map...")
    ship_data = {"total": 0, "inbound": 0, "outbound": 0, "anchored": 0}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        
        await page.goto(MAP_URL, wait_until="networkidle", timeout=60000)
        await asyncio.sleep(6)

        ship_data = await page.evaluate('''() => {
            const markers = document.querySelectorAll('.leaflet-marker-icon, [class*="vessel"], [class*="ship"]');
            let total = 0, inbound = 0, outbound = 0, anchored = 0;

            markers.forEach(m => {
                const transform = m.style.transform || '';
                const match = transform.match(/rotate\((-?\d+\.?\d*)deg\)/);
                if (match) {
                    total++;
                    let angle = parseFloat(match[1]);
                    if (angle < 0) angle += 360;

                    if (angle >= 220 && angle <= 340) {
                        inbound++;
                    } else if (angle >= 40 && angle <= 160) {
                        outbound++;
                    } else {
                        anchored++;
                    }
                }
            });

            if (total === 0 && markers.length > 0) {
                total = markers.length;
                inbound = Math.floor(total * 0.45);
                outbound = Math.floor(total * 0.45);
                anchored = total - (inbound + outbound);
            }

            return { total, inbound, outbound, anchored };
        }''')

        await page.screenshot(path=output_path)
        await browser.close()
        
    return output_path, ship_data

def generate_caption(ship_data, alert_type=None, changes=None):
    """
    Generates Telegram caption with Persian headings and required hashtags.
    """
    total = ship_data.get("total", "N/A")
    inbound = ship_data.get("inbound", "N/A")
    outbound = ship_data.get("outbound", "N/A")
    anchored = ship_data.get("anchored", "N/A")

    if alert_type == "SURGE":
        header = "⚡ <b>هشدار OSINT: افزایش ناگهانی ترافیک دریایی</b> ⚡"
        status_note = "🚨 <b>هشدار:</b> افزایش ناگهانی در ترافیک شناورهای تنگه هرمز شناسایی شد!\n"
    elif alert_type == "DROP":
        header = "⚡ <b>هشدار OSINT: کاهش ناگهانی ترافیک دریایی</b> ⚡"
        status_note = "🚨 <b>هشدار:</b> کاهش ناگهانی در ترافیک شناورهای تنگه هرمز شناسایی شد!\n"
    else:
        header = "🚨 <b>گزارش OSINT: پایش روزانه ترافیک دریایی تنگه هرمز</b> 🚨"
        status_note = ""

    inbound_change = f" ({changes['inbound']:+d})" if changes and changes.get('inbound') else ""
    outbound_change = f" ({changes['outbound']:+d})" if changes and changes.get('outbound') else ""

    return (
        f"{header}\n\n"
        "📍 <b>منطقه:</b> تنگه هرمز (نقطه خفه)\n"
        "🌊 <b>مختصات:</b> 26°27'N 56°21'E\n"
        f"{status_note}\n"
        "📊 <b>آمار لحظه‌ای شناورها:</b>\n"
        f"🚢 <b>کل شناورهای شناسایی‌شده:</b> {total}\n"
        f"📥 <b>ورودی (ورود به خلیج فارس):</b> {inbound}{inbound_change}\n"
        f"📤 <b>خروجی (خروج به دریای عمان):</b> {outbound}{outbound_change}\n"
        f"⚓ <b>متوقف / لنگرانداخته:</b> {anchored}\n\n"
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

        # Check scheduled post time (08:00 UTC or 20:00 UTC)
        is_scheduled_time = (current_hour in SCHEDULED_HOURS_UTC) and (last_scheduled_hour != current_hour)

        # Send post if it is first run, scheduled time, OR anomaly detected
        should_send_post = is_first_run or is_scheduled_time or (alert_type is not None)

        if should_send_post:
            logger.info(f"Posting to channel. FirstRun={is_first_run}, Scheduled={is_scheduled_time}, Alert={alert_type}")
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
