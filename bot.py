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

SURGE_DROP_THRESHOLD = 5
SCHEDULED_HOURS_UTC = [2, 8, 14, 20]
HISTORY_FILE = "history.json"

# Exact Strait of Hormuz Choke Point Coordinates
HORMUZ_LAT = 26.3500
HORMUZ_LON = 56.4500
HORMUZ_ZOOM = 9.5

# Bounding Box for Hormuz Choke Point Traffic Filtering
HORMUZ_BBOX = {
    "min_lat": 25.40,
    "max_lat": 27.30,
    "min_lon": 55.40,
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
    """Loads baseline history and daily cumulative sets."""
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
                if data.get("date") != today_str:
                    data["date"] = today_str
                    data["daily_inbound_mmsi"] = []
                    data["daily_outbound_mmsi"] = []
                return data
        except Exception as e:
            logger.error(f"Error loading history file: {e}")
    return default_history

def save_history(history):
    """Saves baseline history and daily cumulative sets."""
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving history file: {e}")

async def prepare_and_zoom_map(page):
    """
    1. Removes all ads, headers, sidebars, search bars.
    2. Makes map take full 100% width and height.
    3. Programmatically forces the map view to zoom directly onto Strait of Hormuz.
    """
    await page.evaluate('''() => {
        // Remove ads, banners, navbars, side panels
        const junk = [
            'header', '#header', '.header', '#top-nav', '.fc-ab-root',
            '#onetrust-consent-sdk', '.qc-cmp2-container', '.ad-banner',
            '.sidebar', '#sidebar', '.right-banner', '#right-col',
            '.leaflet-control-zoom', '.leaflet-control-layers', '.search-box',
            'iframe', 'ins.adsbygoogle', '#google_ads_iframe', '.advertisement',
            'div[class*="banner"]', 'div[id*="ad_"]', 'div[class*="ad_"]',
            'div[id*="google"]'
        ];
        junk.forEach(sel => {
            document.querySelectorAll(sel).forEach(el => el.remove());
        });

        // Expand map container to true 100% fullscreen
        const mapElements = document.querySelectorAll('#map, .map-container, #map-canvas, .leaflet-container');
        mapElements.forEach(el => {
            el.style.width = '100vw';
            el.style.height = '100vh';
            el.style.position = 'fixed';
            el.style.top = '0';
            el.style.left = '0';
            el.style.zIndex = '1';
        });

        // Force Leaflet/OpenLayers/Mapbox map centering
        try {
            if (window.map && typeof window.map.setView === 'function') {
                window.map.setView([26.3500, 56.4500], 9);
                window.map.invalidateSize();
            }
            if (window.MST && window.MST.map) {
                window.MST.map.setView([26.3500, 56.4500], 9);
                window.MST.map.invalidateSize();
            }
        } catch(e) {}
    }''')

async def extract_vessels_from_page(page, captured_vessels):
    """Extracts live AIS vessels rendered inside the DOM and window JS state."""
    vessels = await page.evaluate(f'''() => {{
        const results = [];
        const minLat = {HORMUZ_BBOX['min_lat']};
        const maxLat = {HORMUZ_BBOX['max_lat']};
        const minLon = {HORMUZ_BBOX['min_lon']};
        const maxLon = {HORMUZ_BBOX['max_lon']};

        // Check global JS vessel arrays
        const sources = [window.vessels, window.all_vessels, window.vessels_list, (window.MST && window.MST.vessels)];
        for (const src of sources) {{
            if (src && typeof src === 'object') {{
                const list = Array.isArray(src) ? src : Object.values(src);
                list.forEach(v => {{
                    if (v && (v.lat || v.latitude)) {{
                        const lat = parseFloat(v.lat || v.latitude);
                        const lon = parseFloat(v.lng || v.lon || v.longitude);
                        const cog = parseFloat(v.course || v.cog || v.heading || 0);
                        const sog = parseFloat(v.speed || v.sog || 0);
                        const mmsi = String(v.mmsi || v.id || Math.random());
                        if (lat >= minLat && lat <= maxLat && lon >= minLon && lon <= maxLon) {{
                            results.push({{ mmsi, lat, lon, cog, sog }});
                        }}
                    }}
                }});
            }}
        }}

        // If JS array not directly exposed, inspect Leaflet marker layers
        if (results.length === 0 && window.map && window.map._layers) {{
            Object.values(window.map._layers).forEach(layer => {{
                if (layer._latlng) {{
                    const lat = layer._latlng.lat;
                    const lon = layer._latlng.lng;
                    if (lat >= minLat && lat <= maxLat && lon >= minLon && lon <= maxLon) {{
                        let cog = 0;
                        if (layer.options && layer.options.rotationAngle) {{
                            cog = layer.options.rotationAngle;
                        }}
                        results.push({{
                            mmsi: String(layer._leaflet_id || Math.random()),
                            lat: lat,
                            lon: lon,
                            cog: cog,
                            sog: 10
                        }});
                    }}
                }}
            }});
        }}

        return results;
    }}''')

    for v in vessels:
        captured_vessels[v["mmsi"]] = v

async def inject_tactical_hud(page, metrics, daily_metrics):
    """Overlays the clean OSINT HUD box in the top-left."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    await page.evaluate(f'''() => {{
        const existing = document.getElementById('osint-hud-overlay');
        if (existing) existing.remove();

        const hud = document.createElement('div');
        hud.id = 'osint-hud-overlay';
        hud.style.position = 'fixed';
        hud.style.top = '20px';
        hud.style.left = '20px';
        hud.style.zIndex = '9999999';
        hud.style.background = 'rgba(10, 15, 29, 0.92)';
        hud.style.backdropFilter = 'blur(8px)';
        hud.style.border = '1.5px solid #1E293B';
        hud.style.borderRadius = '12px';
        hud.style.padding = '16px 20px';
        hud.style.color = '#FFFFFF';
        hud.style.fontFamily = 'monospace, sans-serif';
        hud.style.boxShadow = '0 10px 30px rgba(0,0,0,0.7)';
        hud.style.pointerEvents = 'none';

        hud.innerHTML = `
            <div style="font-size: 15px; font-weight: bold; color: #38BDF8; margin-bottom: 6px; letter-spacing: 0.5px;">
                ⚓ STRAIT OF HORMUZ AIS RADAR
            </div>
            <div style="font-size: 11px; color: #94A3B8; margin-bottom: 10px;">
                🕒 {now_str} | 26°27'N 56°21'E
            </div>
            <div style="border-top: 1px solid #334155; padding-top: 8px; font-size: 13px; line-height: 1.7;">
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
    captured_vessels = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-web-security"]
        )
        context = await browser.new_context(
            viewport={"width": 1400, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        # Listen for AIS network payloads
        async def handle_response(response):
            try:
                url = response.url.lower()
                if any(k in url for k in ["vessel", "ais", "request", "tile", "get"]):
                    text = await response.text()
                    lines = text.strip().split("\n")
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
                                pass
            except Exception:
                pass

        page.on("response", handle_response)

        for source_url in MAP_SOURCES:
            logger.info(f"Loading radar: {source_url}")
            try:
                await page.goto(source_url, wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(5)
                
                # Zoom into Strait of Hormuz and wipe out all ads/sidebars
                await prepare_and_zoom_map(page)
                await asyncio.sleep(3)
                
                # Extract vessels from page memory
                await extract_vessels_from_page(page, captured_vessels)
                
                if len(captured_vessels) >= 3:
                    logger.info(f"Detected {len(captured_vessels)} ships in Hormuz!")
                    break
            except Exception as e:
                logger.warning(f"Error loading {source_url}: {e}")

        # Classify ships
        inbound = 0
        outbound = 0
        anchored = 0

        daily_in_set = set(history.get("daily_inbound_mmsi", []))
        daily_out_set = set(history.get("daily_outbound_mmsi", []))

        for v in captured_vessels.values():
            cog = v["cog"]
            sog = v["sog"]
            mmsi = v["mmsi"]

            if sog < 1.5:
                anchored += 1
            elif 210 <= cog <= 350:  # Heading NW into Persian Gulf
                inbound += 1
                daily_in_set.add(mmsi)
            elif 30 <= cog <= 170:   # Heading SE out to Sea of Oman
                outbound += 1
                daily_out_set.add(mmsi)
            else:
                anchored += 1

        total = len(captured_vessels)
        history["daily_inbound_mmsi"] = list(daily_in_set)
        history["daily_outbound_mmsi"] = list(daily_out_set)

        metrics = {"total": total, "inbound": inbound, "outbound": outbound, "anchored": anchored}
        daily_metrics = {
            "today_inbound": len(daily_in_set),
            "today_outbound": len(daily_out_set),
            "today_total": len(daily_in_set) + len(daily_out_set)
        }

        # Inject HUD and capture clean screenshot of Strait of Hormuz only
        await inject_tactical_hud(page, metrics, daily_metrics)
        await asyncio.sleep(1)
        await page.screenshot(path=output_path, full_page=False)
        await browser.close()

    return output_path, metrics, daily_metrics

def generate_caption(metrics, daily_metrics, alert_type=None, changes=None):
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
            logger.info("Posting zoomed Hormuz report to Telegram...")
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
