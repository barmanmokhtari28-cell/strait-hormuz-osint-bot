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

SCHEDULED_HOURS_UTC = [2, 8, 14, 20]
HISTORY_FILE = "history.json"

# Strait of Hormuz Bounding Box
HORMUZ_BBOX = {
    "min_lat": 25.20,
    "max_lat": 27.50,
    "min_lon": 55.00,
    "max_lon": 57.60
}

# Reliable radar sources (MyShipTracking first, then clean VesselFinder)
RADAR_SOURCES = [
    {
        "url": "https://www.myshiptracking.com/embed?lat=26.3500&lon=56.4500&zoom=9",
        "referer": "https://www.myshiptracking.com/"
    },
    {
        "url": "https://www.vesselfinder.com/aismap?zoom=9&lat=26.3500&lon=56.4500&names=false",
        "referer": "https://www.vesselfinder.com/"
    }
]

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

def load_history():
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    default_history = {
        "date": today_str,
        "daily_inbound_mmsi": [],
        "daily_outbound_mmsi": [],
        "last_scheduled_hour": None
    }
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data.get("date") != today_str:
                    data["date"] = today_str
                    data["daily_inbound_mmsi"] = []
                    data["daily_outbound_mmsi"] = []
                return data
        except Exception as e:
            logger.error(f"Error loading history: {e}")
    return default_history

def save_history(history):
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving history: {e}")

async def inject_tactical_hud(page, metrics, daily_metrics):
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    await page.evaluate(f'''() => {{
        const existing = document.getElementById('osint-hud-overlay');
        if (existing) existing.remove();

        const hud = document.createElement('div');
        hud.id = 'osint-hud-overlay';
        hud.style.position = 'fixed';
        hud.style.top = '18px';
        hud.style.left = '18px';
        hud.style.zIndex = '99999999';
        hud.style.background = 'rgba(10, 15, 29, 0.92)';
        hud.style.backdropFilter = 'blur(8px)';
        hud.style.border = '1.5px solid #1E293B';
        hud.style.borderRadius = '10px';
        hud.style.padding = '14px 18px';
        hud.style.color = '#FFFFFF';
        hud.style.fontFamily = 'monospace, sans-serif';
        hud.style.boxShadow = '0 8px 30px rgba(0,0,0,0.8)';
        hud.style.pointerEvents = 'none';

        hud.innerHTML = `
            <div style="font-size: 14px; font-weight: bold; color: #38BDF8; margin-bottom: 5px; letter-spacing: 0.5px;">
                ⚓ STRAIT OF HORMUZ AIS RADAR
            </div>
            <div style="font-size: 11px; color: #94A3B8; margin-bottom: 8px;">
                🕒 {now_str} | 26°21'N 56°27'E
            </div>
            <div style="border-top: 1px solid #334155; padding-top: 6px; font-size: 12px; line-height: 1.6;">
                <div>🚢 <b>Active in Strait:</b> <span style="color: #F8FAFC; font-weight: bold;">{metrics['total']}</span></div>
                <div>📥 <b>Inbound (to Gulf):</b> <span style="color: #34D399; font-weight: bold;">{metrics['inbound']}</span></div>
                <div>📤 <b>Outbound (to Sea):</b> <span style="color: #F87171; font-weight: bold;">{metrics['outbound']}</span></div>
                <div>⚓ <b>Stationary / Anchored:</b> <span style="color: #FBBF24;">{metrics['anchored']}</span></div>
            </div>
            <div style="border-top: 1px solid #334155; margin-top: 6px; padding-top: 6px; font-size: 11px; color: #CBD5E1;">
                📊 <b>Today's Transits:</b> 📥 {daily_metrics['today_inbound']} | 📤 {daily_metrics['today_outbound']}
            </div>
        `;
        document.body.appendChild(hud);
    }}''')

async def capture_radar(output_path="hormuz_snapshot.png", history=None):
    captured_vessels = {}
    successful_load = False

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        # Intercept live AIS network payloads
        async def handle_response(response):
            try:
                url = response.url.lower()
                if any(k in url for k in ["vessel", "tile", "get_vessels", "aismap", "geojson"]):
                    text = await response.text()
                    try:
                        data = json.loads(text)
                        items = data if isinstance(data, list) else data.get("data", data.get("vessels", []))
                        for item in items:
                            if isinstance(item, list) and len(item) >= 5:
                                mmsi, lat, lon = str(item[0]), float(item[1]), float(item[2])
                                cog, sog = float(item[3]), float(item[4])
                                if HORMUZ_BBOX["min_lat"] <= lat <= HORMUZ_BBOX["max_lat"] and HORMUZ_BBOX["min_lon"] <= lon <= HORMUZ_BBOX["max_lon"]:
                                    captured_vessels[mmsi] = {"mmsi": mmsi, "cog": cog, "sog": sog}
                            elif isinstance(item, dict):
                                lat = float(item.get("lat", 0))
                                lon = float(item.get("lon", item.get("lng", 0)))
                                if HORMUZ_BBOX["min_lat"] <= lat <= HORMUZ_BBOX["max_lat"] and HORMUZ_BBOX["min_lon"] <= lon <= HORMUZ_BBOX["max_lon"]:
                                    mmsi = str(item.get("mmsi", item.get("id", len(captured_vessels))))
                                    cog = float(item.get("course", item.get("cog", 0)))
                                    sog = float(item.get("speed", item.get("sog", 0)))
                                    captured_vessels[mmsi] = {"mmsi": mmsi, "cog": cog, "sog": sog}
                    except Exception:
                        # TSV format fallback
                        for line in text.strip().split("\n"):
                            parts = line.split("\t")
                            if len(parts) >= 6:
                                lat, lon = float(parts[1]), float(parts[2])
                                if HORMUZ_BBOX["min_lat"] <= lat <= HORMUZ_BBOX["max_lat"] and HORMUZ_BBOX["min_lon"] <= lon <= HORMUZ_BBOX["max_lon"]:
                                    captured_vessels[parts[0]] = {"mmsi": parts[0], "cog": float(parts[3]), "sog": float(parts[4])}
            except Exception:
                pass

        page.on("response", handle_response)

        for src in RADAR_SOURCES:
            radar_url = src["url"]
            logger.info(f"Loading Strait of Hormuz AIS map: {radar_url}")
            try:
                await page.set_extra_http_headers({"Referer": src["referer"]})
                await page.goto(radar_url, wait_until="networkidle", timeout=35000)
                await asyncio.sleep(6)

                body = await page.inner_text("body")
                if "Bad request" in body or "403 Forbidden" in body:
                    logger.warning(f"Error page at {radar_url}, trying fallback...")
                    continue

                # Remove overlay ads, cookie bars, and attribution labels
                await page.evaluate('''() => {
                    document.querySelectorAll('.fc-ab-root, #onetrust-consent-sdk, .leaflet-control-attribution, .ol-attribution, #header').forEach(el => el.remove());
                }''')

                # Confirm real map rendered
                has_map = await page.evaluate('''() => {
                    return document.querySelectorAll('canvas, .leaflet-tile-pane, .ol-layers').length > 0;
                }''')

                if has_map:
                    successful_load = True
                    logger.info(f"Map successfully loaded from {radar_url}. Vessels captured: {len(captured_vessels)}")
                    break
            except Exception as e:
                logger.warning(f"Failed to load {radar_url}: {e}")

        if not successful_load:
            await browser.close()
            raise RuntimeError("Failed to load a valid AIS map.")

        # Classify ships: Inbound, Outbound, Anchored
        inbound = 0
        outbound = 0
        anchored = 0

        daily_in = set(history.get("daily_inbound_mmsi", []))
        daily_out = set(history.get("daily_outbound_mmsi", []))

        # If data was rendered on canvas directly, use DOM marker directions
        if len(captured_vessels) == 0:
            dom_markers = await page.evaluate('''() => {
                const res = [];
                document.querySelectorAll('.leaflet-marker-pane img, svg g[class*="ship"], [class*="vessel"]').forEach((el, i) => {
                    const t = el.style.transform || window.getComputedStyle(el).transform || '';
                    const match = t.match(/rotate\((-?\d+\.?\d*)deg\)/);
                    let angle = match ? parseFloat(match[1]) : 0;
                    if (angle < 0) angle += 360;
                    res.push({ mmsi: 'marker_' + i, cog: angle, sog: 10 });
                });
                return res;
            }''')
            for m in dom_markers:
                captured_vessels[m["mmsi"]] = m

        for v in captured_vessels.values():
            cog = v.get("cog", 0.0)
            sog = v.get("sog", 0.0)
            mmsi = v.get("mmsi")

            if sog < 1.8:
                anchored += 1
            elif 200 <= cog <= 345:  # Inbound to Persian Gulf (NW)
                inbound += 1
                daily_in.add(mmsi)
            elif 25 <= cog <= 175:   # Outbound to Gulf of Oman (SE)
                outbound += 1
                daily_out.add(mmsi)
            else:
                anchored += 1

        history["daily_inbound_mmsi"] = list(daily_in)
        history["daily_outbound_mmsi"] = list(daily_out)

        metrics = {
            "total": len(captured_vessels),
            "inbound": inbound,
            "outbound": outbound,
            "anchored": anchored
        }
        daily_metrics = {
            "today_inbound": len(daily_in),
            "today_outbound": len(daily_out),
            "today_total": len(daily_in) + len(daily_out)
        }

        # Inject HUD and capture image
        await inject_tactical_hud(page, metrics, daily_metrics)
        await asyncio.sleep(1)
        await page.screenshot(path=output_path, full_page=False)
        await browser.close()

    return output_path, metrics, daily_metrics

def generate_caption(metrics, daily_metrics):
    now_utc = datetime.now(timezone.utc)
    return (
        "🚢 <b>گزارش ترافیک و پایش ناوبری تنگه هرمز</b> 🚨\n\n"
        f"📅 <b>تاریخ و زمان:</b> <code>{now_utc.strftime('%Y-%m-%d | %H:%M UTC')}</code>\n"
        "📍 <b>منطقه پایش:</b> <code>تنگه هرمز (آبراه بین‌المللی)</code>\n\n"
        "<blockquote>📊 <b>وضعیت ترافیک لحظه‌ای (در این لحظه):</b>\n"
        f"🚢 <b>کل شناورهای حاضر در آبراه:</b> <code>{metrics['total']}</code>\n"
        f"📥 <b>ورودی (به سمت خلیج فارس):</b> <code>{metrics['inbound']}</code>\n"
        f"📤 <b>خروجی (به سمت دریای عمان):</b> <code>{metrics['outbound']}</code>\n"
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
        logger.error("Missing TELEGRAM_BOT_TOKEN!")
        return

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    image_path = "hormuz_snapshot.png"
    history = load_history()
    current_hour = datetime.now(timezone.utc).hour

    try:
        image_path, metrics, daily_metrics = await capture_radar(image_path, history)

        last_scheduled = history.get("last_scheduled_hour")
        is_scheduled = (current_hour in SCHEDULED_HOURS_UTC) and (last_scheduled != current_hour)

        if IS_MANUAL_RUN or is_scheduled or last_scheduled is None:
            logger.info("Posting zoomed Strait of Hormuz image...")
            caption = generate_caption(metrics, daily_metrics)
            with open(image_path, "rb") as photo:
                await bot.send_photo(
                    chat_id=TELEGRAM_CHANNEL_ID,
                    photo=photo,
                    caption=caption,
                    parse_mode=ParseMode.HTML
                )
            history["last_scheduled_hour"] = current_hour

        save_history(history)

    except Exception as e:
        logger.error(f"Execution failed: {e}", exc_info=True)
    finally:
        if os.path.exists(image_path):
            os.remove(image_path)

if __name__ == "__main__":
    asyncio.run(run_bot())
