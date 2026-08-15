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

# Surge / Drop difference threshold
SURGE_DROP_THRESHOLD = 5 

# Scheduled UTC hours (02:00, 08:00, 14:00, 20:00 UTC)
SCHEDULED_HOURS_UTC = [2, 8, 14, 20]

HISTORY_FILE = "history.json"

# Strait of Hormuz Bounding Box (Choke point)
HORMUZ_BBOX = {
    "min_lat": 25.50,
    "max_lat": 27.20,
    "min_lon": 55.50,
    "max_lon": 57.50
}

MAP_SOURCES = [
    "https://www.myshiptracking.com/?lat=26.3500&lon=56.4500&zoom=9",
    "https://www.vesselfinder.com/?lat=26.3500&lon=56.4500&zoom=9"
]

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

def load_history():
    """Loads previous vessel counts and daily tracking sets from history.json."""
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    default_history = {
        "date": today_str,
        "inbound": 0,
        "outbound": 0,
        "total": 0,
        "last_scheduled_hour": None,
        "daily_inbound_mmsi": [],
        "daily_outbound_mmsi": []
    }
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                # Reset cumulative daily list if it's a new UTC day
                if data.get("date") != today_str:
                    data["date"] = today_str
                    data["daily_inbound_mmsi"] = []
                    data["daily_outbound_mmsi"] = []
                return data
        except Exception as e:
            logger.error(f"Error loading history file: {e}")
    return default_history

def save_history(history):
    """Saves updated history and daily sets to history.json."""
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving history file: {e}")

def parse_vessel_payload(text_data, captured_vessels):
    """Parses raw AIS network responses (JSON or TSV) from radar providers."""
    # Attempt JSON parsing
    try:
        data = json.loads(text_data)
        items = data if isinstance(data, list) else data.get("vessels", data.get("data", []))
        for item in items:
            if isinstance(item, dict):
                mmsi = str(item.get("mmsi") or item.get("id") or "")
                lat = float(item.get("lat") or 0)
                lon = float(item.get("lng") or item.get("lon") or 0)
                cog = float(item.get("course") or item.get("cog") or item.get("heading") or 0)
                sog = float(item.get("speed") or item.get("sog") or 0)
                if mmsi and HORMUZ_BBOX["min_lat"] <= lat <= HORMUZ_BBOX["max_lat"] and HORMUZ_BBOX["min_lon"] <= lon <= HORMUZ_BBOX["max_lon"]:
                    captured_vessels[mmsi] = {"mmsi": mmsi, "lat": lat, "lon": lon, "cog": cog, "sog": sog}
    except Exception:
        # Attempt TSV/CSV format parsing (MyShipTracking format)
        lines = text_data.strip().split("\n")
        for line in lines:
            parts = line.split("\t")
            if len(parts) >= 6:
                try:
                    mmsi = parts[0].strip()
                    lat = float(parts[1])
                    lon = float(parts[2])
                    cog = float(parts[3])
                    sog = float(parts[4])
                    if HORMUZ_BBOX["min_lat"] <= lat <= HORMUZ_BBOX["max_lat"] and HORMUZ_BBOX["min_lon"] <= lon <= HORMUZ_BBOX["max_lon"]:
                        captured_vessels[mmsi] = {"mmsi": mmsi, "lat": lat, "lon": lon, "cog": cog, "sog": sog}
                except Exception:
                    continue

async def dismiss_overlays_and_clean_ui(page):
    """Removes all ads, headers, search bars, and popups to prepare a clear map."""
    try:
        # Dismiss standard GDPR & Cookie banners
        for sel in ["#onetrust-accept-btn-handler", ".fc-cta-consent", "button:has-text('Consent')", "button:has-text('Accept')"]:
            btn = page.locator(sel)
            if await btn.count() > 0:
                await btn.first.click(timeout=1500)
    except Exception:
        pass

    # Clean UI clutter
    await page.evaluate('''() => {
        const selectors = [
            'header', '#header', '.header', '#top-nav', '.fc-ab-root', 
            '#onetrust-consent-sdk', '.qc-cmp2-container', '.ad-banner', 
            '.leaflet-control-zoom', '.leaflet-control-layers', '.leaflet-top.leaflet-right',
            '.search-box', '#search', '.side-panel', '.site-logo', '#map-controls'
        ];
        selectors.forEach(sel => {
            document.querySelectorAll(sel).forEach(el => el.remove());
        });
    }''')

async def inject_tactical_hud(page, metrics, daily_metrics):
    """Injects a high-visibility OSINT Tactical HUD on top of the screenshot."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    await page.evaluate(f'''() => {{
        const hud = document.createElement('div');
        hud.id = 'osint-hud-overlay';
        hud.style.position = 'absolute';
        hud.style.top = '15px';
        hud.style.left = '15px';
        hud.style.zIndex = '999999';
        hud.style.background = 'rgba(10, 15, 29, 0.88)';
        hud.style.backdropFilter = 'blur(6px)';
        hud.style.border = '1px solid #1E293B';
        hud.style.borderRadius = '10px';
        hud.style.padding = '14px 18px';
        hud.style.color = '#FFFFFF';
        hud.style.fontFamily = 'monospace, sans-serif';
        hud.style.boxShadow = '0 8px 24px rgba(0,0,0,0.6)';
        hud.style.pointerEvents = 'none';

        hud.innerHTML = `
            <div style="font-size: 14px; font-weight: bold; color: #38BDF8; margin-bottom: 6px; letter-spacing: 0.5px;">
                ⚓ STRAIT OF HORMUZ AIS RADAR
            </div>
            <div style="font-size: 11px; color: #94A3B8; margin-bottom: 10px;">
                🕒 ${{'{now_str}'}} | 26°27'N 56°21'E
            </div>
            <div style="border-top: 1px solid #334155; padding-top: 8px; font-size: 12px; line-height: 1.6;">
                <div>🚢 <b>Active in Strait:</b> <span style="color: #F8FAFC; font-weight: bold;">{metrics['total']}</span></div>
                <div>📥 <b>Inbound (to Gulf):</b> <span style="color: #34D399; font-weight: bold;">{metrics['inbound']}</span></div>
                <div>📤 <b>Outbound (to Sea):</b> <span style="color: #F87171; font-weight: bold;">{metrics['outbound']}</span></div>
                <div>⚓ <b>Stationary / Anchored:</b> <span style="color: #FBBF24;">{metrics['anchored']}</span></div>
            </div>
            <div style="border-top: 1px solid #334155; margin-top: 8px; padding-top: 6px; font-size: 11px; color: #CBD5E1;">
                📊 <b>Today's Total Transits:</b> 📥 {daily_metrics['today_inbound']} | 📤 {daily_metrics['today_outbound']}
            </div>
        `;
        document.body.appendChild(hud);
    }}''')

async def capture_radar(output_path="hormuz_snapshot.png", history=None):
    """Loads map, captures AIS responses, updates daily transits, and saves clean screenshot."""
    captured_vessels = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1400, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        # Intercept AIS data responses
        async def handle_response(response):
            try:
                url = response.url.lower()
                if any(k in url for k in ["vessel", "ais", "request", "tile", "get"]):
                    body = await response.text()
                    parse_vessel_payload(body, captured_vessels)
            except Exception:
                pass

        page.on("response", handle_response)

        for source_url in MAP_SOURCES:
            logger.info(f"Navigating to: {source_url}")
            try:
                await page.goto(source_url, wait_until="networkidle", timeout=45000)
                await asyncio.sleep(6)  # Give time to receive AIS stream packets
                await dismiss_overlays_and_clean_ui(page)

                if len(captured_vessels) >= 5:
                    logger.info(f"Successfully captured {len(captured_vessels)} live vessels!")
                    break
            except Exception as e:
                logger.warning(f"Source {source_url} attempt error: {e}")

        # Classify instantaneous traffic
        inbound = 0
        outbound = 0
        anchored = 0

        daily_in_set = set(history.get("daily_inbound_mmsi", []))
        daily_out_set = set(history.get("daily_outbound_mmsi", []))

        for v in captured_vessels.values():
            cog = v["cog"]
            sog = v["sog"]
            mmsi = v["mmsi"]

            if sog < 2.0:
                anchored += 1
            elif 210 <= cog <= 350:  # Inbound towards Persian Gulf (NW)
                inbound += 1
                daily_in_set.add(mmsi)
            elif 30 <= cog <= 170:   # Outbound towards Gulf of Oman (SE)
                outbound += 1
                daily_out_set.add(mmsi)
            else:
                anchored += 1

        total = len(captured_vessels)
        
        # Fallback if canvas map rendered without exposing plain text packets
        if total == 0:
            total = inbound = outbound = anchored = 0

        # Update daily sets
        history["daily_inbound_mmsi"] = list(daily_in_set)
        history["daily_outbound_mmsi"] = list(daily_out_set)

        metrics = {"total": total, "inbound": inbound, "outbound": outbound, "anchored": anchored}
        daily_metrics = {
            "today_inbound": len(daily_in_set),
            "today_outbound": len(daily_out_set),
            "today_total": len(daily_in_set) + len(daily_out_set)
        }

        # Inject HUD and take screenshot
        await inject_tactical_hud(page, metrics, daily_metrics)
        await asyncio.sleep(1)
        await page.screenshot(path=output_path)
        await browser.close()

    return output_path, metrics, daily_metrics

def generate_caption(metrics, daily_metrics, alert_type=None, changes=None):
    """Generates informative Telegram caption in Persian with live & daily cumulative metrics."""
    now_utc = datetime.now(timezone.utc)
    date_str = now_utc.strftime("%Y-%m-%d")
    time_str = now_utc.strftime("%H:%M UTC")

    if alert_type == "SURGE":
        header = "⚡ <b>هشدار OSINT: افزایش ناگهانی ترافیک دریایی</b> ⚡"
    elif alert_type == "DROP":
        header = "⚡ <b>هشدار OSINT: کاهش ناگهانی ترافیک دریایی</b> ⚡"
    else:
        header = "🚢 <b>گزارش ترافیک و پایش ناوبری تنگه هرمز</b> 🚨"

    inbound_change = f" (<code>{changes['inbound']:+d}</code>)" if changes and changes.get('inbound') else ""
    outbound_change = f" (<code>{changes['outbound']:+d}</code>)" if changes and changes.get('outbound') else ""

    return (
        f"{header}\n\n"
        f"📅 <b>تاریخ و زمان:</b> <code>{date_str} | {time_str}</code>\n"
        "📍 <b>منطقه پایش:</b> <code>تنگه هرمز (آبراه بین‌المللی)</code>\n\n"
        "<blockquote>📊 <b>وضعیت ترافیک لحظه‌ای (در این لحظه):</b>\n"
        f"🚢 <b>کل شناورهای حاضر در آبراه:</b> <code>{metrics['total']}</code>\n"
        f"📥 <b>ورودی (به سمت خلیج فارس):</b> <code>{metrics['inbound']}</code>{inbound_change}\n"
        f"📤 <b>خروجی (به سمت دریای عمان):</b> <code>{metrics['outbound']}</code>{outbound_change}\n"
        f"⚓ <b>متوقف / لنگرانداخته:</b> <code>{metrics['anchored']}</code></blockquote>\n\n"
        "<blockquote>📈 <b>آمار کل تردد امروز تا این لحظه (۲۴ ساعته):</b>\n"
        f"🔹 <b>مجموع شناورهای ورودی امروز:</b> <code>{daily_metrics['today_inbound']}</code>\n"
        f"🔸 <b>مجموع شناورهای خروجی امروز:</b> <code>{daily_metrics['today_outbound']}</code>\n"
        f"🌐 <b>کل تردد ثبتی امروز:</b> <code>{daily_metrics['today_total']}</code> فروند</blockquote>\n\n"
        "🔍 <i>داده‌ها از طریق پردازش مستقیم سیگنال‌های زنده راداری AIS استخراج شده‌اند.</i>\n\n"
        "⚓ @secretollah 🚢\n"
        "#تنگه_هرمز #OSINT"
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
        image_path, metrics, daily_metrics = await capture_radar(image_path, history)

        prev_inbound = history.get("inbound")
        prev_outbound = history.get("outbound")
        last_scheduled_hour = history.get("last_scheduled_hour")

        is_first_run = prev_inbound is None or prev_outbound is None

        alert_type = None
        changes = None

        if not is_first_run:
            diff_inbound = metrics["inbound"] - prev_inbound
            diff_outbound = metrics["outbound"] - prev_outbound

            if diff_inbound >= SURGE_DROP_THRESHOLD or diff_outbound >= SURGE_DROP_THRESHOLD:
                alert_type = "SURGE"
                changes = {"inbound": diff_inbound, "outbound": diff_outbound}
            elif diff_inbound <= -SURGE_DROP_THRESHOLD or diff_outbound <= -SURGE_DROP_THRESHOLD:
                alert_type = "DROP"
                changes = {"inbound": diff_inbound, "outbound": diff_outbound}

        is_scheduled_time = (current_hour in SCHEDULED_HOURS_UTC) and (last_scheduled_hour != current_hour)
        should_send_post = IS_MANUAL_RUN or is_first_run or is_scheduled_time or (alert_type is not None)

        if should_send_post:
            logger.info("Posting report to Telegram...")
            caption = generate_caption(metrics, daily_metrics, alert_type=alert_type, changes=changes)
            
            with open(image_path, "rb") as photo:
                await bot.send_photo(
                    chat_id=TELEGRAM_CHANNEL_ID,
                    photo=photo,
                    caption=caption,
                    parse_mode=ParseMode.HTML
                )
            
            if is_scheduled_time or is_first_run:
                history["last_scheduled_hour"] = current_hour

        # Save history
        history["inbound"] = metrics["inbound"]
        history["outbound"] = metrics["outbound"]
        history["total"] = metrics["total"]
        save_history(history)

    except Exception as e:
        logger.error(f"Error executing bot workflow: {e}")
    finally:
        if os.path.exists(image_path):
            os.remove(image_path)

if __name__ == "__main__":
    asyncio.run(run_bot())
