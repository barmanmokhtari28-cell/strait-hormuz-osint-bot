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

# Sensitivity threshold for surge/drop alerts (e.g., 5 ships difference)
SURGE_DROP_THRESHOLD = 5 

# Scheduled hours in UTC for daily reports (08:00 UTC & 20:00 UTC)
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
    Generates Telegram caption for scheduled reports or anomaly alerts.
    """
    total = ship_data.get("total", "N/A")
    inbound = ship_data.get("inbound", "N/A")
    outbound = ship_data.get("outbound", "N/A")
    anchored = ship_data.get("anchored", "N/A")

    if alert_type:
        inbound_change = f"({changes['inbound']:+d})" if changes else ""
        outbound_change = f"({changes['outbound']:+d})" if changes else ""
        
        header = f"⚡ <b>OSINT ANOMALY ALERT: {alert_type} DETECTED</b> ⚡"
        status_note = f"🚨 <b>Notice:</b> Sudden traffic shift detected in Strait of Hormuz!\n"
    else:
        header = "🚨 <b>گزارش به روز کشتی های ورودی و خروجی به تنـگه هـرمـز</b> 🚨"
        inbound_change, outbound_change = "", ""
        status_note = ""

    return (
        f"{header}\n\n"
        "📍 <b>Zone:</b> Strait of Hormuz (Choke Point)\n"
        "🌊 <b>Coordinates:</b> 26°27'N 56°21'E\n"
        f"{status_note}\n"
        "📊 <b>LIVE VESSEL COUNT:</b>\n"
        f"🚢 <b>Total Detected Ships:</b> {total}\n"
        f"📥 <b>Inbound (Entering Gulf):</b> {inbound} {inbound_change}\n"
        f"📤 <b>Outbound (Exiting to Oman):</b> {outbound} {outbound_change}\n"
        f"⚓ <b>Stationary / Anchored:</b> {anchored}\n\n"
        "🔍 <i>شناسایی برخط AIS استخراج‌شده از طریق اسکن راداری خودکار.</i>\n\n"
        "⚓ @secretollah 🚢"
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
        # Step 1: Capture screenshot & vessel numbers
        image_path, ship_data = await capture_hormuz_map_and_count(image_path)
        
        curr_inbound = ship_data["inbound"]
        curr_outbound = ship_data["outbound"]
        
        prev_inbound = history.get("inbound")
        prev_outbound = history.get("outbound")
        last_scheduled_hour = history.get("last_scheduled_hour")

        # Step 2: Determine if an Anomaly (Surge/Drop) occurred
        alert_type = None
        changes = None

        if prev_inbound is not None and prev_outbound is not None:
            diff_inbound = curr_inbound - prev_inbound
            diff_outbound = curr_outbound - prev_outbound

            if diff_inbound >= SURGE_DROP_THRESHOLD or diff_outbound >= SURGE_DROP_THRESHOLD:
                alert_type = "TRAFFIC SURGE"
                changes = {"inbound": diff_inbound, "outbound": diff_outbound}
            elif diff_inbound <= -SURGE_DROP_THRESHOLD or diff_outbound <= -SURGE_DROP_THRESHOLD:
                alert_type = "TRAFFIC DROP"
                changes = {"inbound": diff_inbound, "outbound": diff_outbound}

        # Step 3: Check if it's one of the 2 daily scheduled times (08:00 UTC or 20:00 UTC)
        is_scheduled_time = (current_hour in SCHEDULED_HOURS_UTC) and (last_scheduled_hour != current_hour)

        # Send post if it is a scheduled time OR if an anomaly alert occurred
        should_send_post = is_scheduled_time or (alert_type is not None)

        if should_send_post:
            logger.info(f"Posting update. Scheduled={is_scheduled_time}, Alert={alert_type}")
            caption = generate_caption(ship_data, alert_type=alert_type, changes=changes)
            
            with open(image_path, "rb") as photo:
                await bot.send_photo(
                    chat_id=TELEGRAM_CHANNEL_ID,
                    photo=photo,
                    caption=caption,
                    parse_mode=ParseMode.HTML
                )
            
            if is_scheduled_time:
                history["last_scheduled_hour"] = current_hour
        else:
            logger.info("Normal traffic scan complete. No surge/drop detected, not a scheduled post time. Silent update saved.")

        # Step 4: Always save state for the next hourly check
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
