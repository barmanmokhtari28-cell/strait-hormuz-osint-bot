import os
import json
import asyncio
import logging
from datetime import datetime, timezone
import websockets
from dotenv import load_dotenv
from telegram import Bot
from telegram.constants import ParseMode
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patheffects as pe
from staticmap import StaticMap, CircleMarker

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "@secretollah")
AISSTREAM_API_KEY = os.getenv("AISSTREAM_API_KEY")
IS_MANUAL_RUN = os.getenv("MANUAL_RUN", "false").lower() == "true" or os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch"

SCHEDULED_HOURS_UTC = [2, 8, 14, 20]
HISTORY_FILE = "history.json"

# Strait of Hormuz + Immediate Transit Approaches (Bandar Abbas / Musandam / UAE approach)
MIN_LAT = 24.80
MAX_LAT = 27.60
MIN_LON = 54.50
MAX_LON = 57.80

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

async def fetch_live_ais(duration_seconds=55):
    """Subscribes to live AIS stream with correct Top-Left to Bottom-Right corner order."""
    vessels = {}
    
    # AISStream strictly requires: [[[MAX_LAT, MIN_LON], [MIN_LAT, MAX_LON]]]
    subscription = {
        "APIKey": AISSTREAM_API_KEY,
        "BoundingBoxes": [
            [
                [MAX_LAT, MIN_LON],
                [MIN_LAT, MAX_LON]
            ]
        ],
        "FilterMessageTypes": ["PositionReport", "StandardClassBPositionReport", "ShipStaticData"]
    }

    logger.info("Connecting to AISStream live WebSocket...")
    try:
        async with websockets.connect("wss://stream.aisstream.io/v0/stream", ping_interval=20) as ws:
            await ws.send(json.dumps(subscription))
            logger.info("Subscription sent. Listening for incoming AIS transponder data...")
            start_time = asyncio.get_event_loop().time()
            message_count = 0

            while asyncio.get_event_loop().time() - start_time < duration_seconds:
                try:
                    raw_data = await asyncio.wait_for(ws.recv(), timeout=8.0)
                    msg = json.loads(raw_data)

                    # Log any server confirmation or error alerts
                    if "error" in msg:
                        logger.error(f"AISStream Server Error: {msg['error']}")
                        break
                    
                    msg_type = msg.get("MessageType")
                    if msg_type == "SubscriptionConfirmation":
                        logger.info("AISStream confirmed active subscription! Receiving stream...")
                        continue

                    mmsi = str(msg.get("MetaData", {}).get("MMSI", ""))
                    if not mmsi:
                        continue

                    pos = None
                    if msg_type == "PositionReport":
                        pos = msg.get("Message", {}).get("PositionReport", {})
                    elif msg_type == "StandardClassBPositionReport":
                        pos = msg.get("Message", {}).get("StandardClassBPositionReport", {})

                    if pos:
                        lat = pos.get("Latitude")
                        lon = pos.get("Longitude")
                        cog = pos.get("Cog", 0.0)
                        sog = pos.get("Sog", 0.0)

                        if lat is not None and lon is not None:
                            if MIN_LAT <= lat <= MAX_LAT and MIN_LON <= lon <= MAX_LON:
                                message_count += 1
                                if mmsi not in vessels:
                                    vessels[mmsi] = {}
                                vessels[mmsi].update({
                                    "mmsi": mmsi,
                                    "name": msg.get("MetaData", {}).get("ShipName", "UNKNOWN").strip(),
                                    "lat": lat,
                                    "lon": lon,
                                    "cog": cog,
                                    "sog": sog
                                })

                    elif msg_type == "ShipStaticData":
                        static_info = msg.get("Message", {}).get("ShipStaticData", {})
                        ship_type = static_info.get("Type", 0)
                        if mmsi in vessels:
                            vessels[mmsi]["type"] = ship_type

                except asyncio.TimeoutError:
                    continue

            logger.info(f"Total AIS messages processed: {message_count}")

    except Exception as e:
        logger.error(f"WebSocket connection issue: {e}", exc_info=True)

    logger.info(f"Successfully collected {len(vessels)} authentic vessels in the Strait of Hormuz.")
    return vessels

def classify_traffic(vessels, history):
    inbound = 0
    outbound = 0
    anchored = 0
    tankers = 0

    daily_in = set(history.get("daily_inbound_mmsi", []))
    daily_out = set(history.get("daily_outbound_mmsi", []))

    for v in vessels.values():
        cog = v.get("cog", 0.0)
        sog = v.get("sog", 0.0)
        mmsi = v.get("mmsi")
        stype = v.get("type", 0)

        # Marine AIS Type 80-89: Crude / Chemical / Gas Tankers
        if 80 <= stype <= 89:
            tankers += 1

        if sog < 1.8:
            v["status"] = "anchored"
            anchored += 1
        elif 200 <= cog <= 345:  # Heading Northwest into Persian Gulf
            v["status"] = "inbound"
            inbound += 1
            daily_in.add(mmsi)
        elif 25 <= cog <= 175:   # Heading Southeast into Sea of Oman
            v["status"] = "outbound"
            outbound += 1
            daily_out.add(mmsi)
        else:
            v["status"] = "anchored"
            anchored += 1

    history["daily_inbound_mmsi"] = list(daily_in)
    history["daily_outbound_mmsi"] = list(daily_out)

    metrics = {
        "total": len(vessels),
        "inbound": inbound,
        "outbound": outbound,
        "anchored": anchored,
        "tankers": tankers
    }
    daily_metrics = {
        "today_inbound": len(daily_in),
        "today_outbound": len(daily_out),
        "today_total": len(daily_in) + len(daily_out)
    }
    return metrics, daily_metrics

def render_radar_map(vessels, metrics, daily_metrics, output_path="hormuz_snapshot.png"):
    """Generates an OSINT tactical dark basemap with ship markers and HUD."""
    tile_url = "https://basemaps.cartocdn.com/rastertiles/dark_all/{z}/{x}/{y}.png"
    m = StaticMap(1366, 768, url_template=tile_url)

    # Plot ship markers by status
    for v in vessels.values():
        color = "#FBBF24"  # Anchored (Yellow)
        if v.get("status") == "inbound":
            color = "#34D399"  # Inbound (Green)
        elif v.get("status") == "outbound":
            color = "#F87171"  # Outbound (Red)
        m.add_marker(CircleMarker((v["lon"], v["lat"]), color, 6))

    image = m.render(zoom=9, center=[56.35, 26.30])
    image.save(output_path)

    # Stamp Tactical HUD
    fig, ax = plt.subplots(figsize=(13.66, 7.68), dpi=100)
    img_data = plt.imread(output_path)
    ax.imshow(img_data)
    ax.axis("off")

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    hud_text = (
        "⚓ STRAIT OF HORMUZ AIS RADAR\n"
        f"🕒 {now_str} | 26°21'N 56°27'E\n"
        "───────────────────────────────\n"
        f"🚢 Active in Strait:     {metrics['total']}\n"
        f"📥 Inbound (to Gulf):    {metrics['inbound']}\n"
        f"📤 Outbound (to Sea):    {metrics['outbound']}\n"
        f"🛢️ Tankers (Crude/Gas): {metrics['tankers']}\n"
        f"⚓ Anchored/Stationary:  {metrics['anchored']}\n"
        "───────────────────────────────\n"
        f"📊 Today's Total: 📥 {daily_metrics['today_inbound']} | 📤 {daily_metrics['today_outbound']}"
    )

    ax.text(
        0.03, 0.95, hud_text,
        transform=ax.transAxes,
        fontsize=11,
        family="monospace",
        fontweight="bold",
        color="#F8FAFC",
        verticalalignment="top",
        bbox=dict(boxstyle="round,pad=0.8", facecolor="#0A0F1D", edgecolor="#1E293B", alpha=0.92, lw=1.5),
        path_effects=[pe.withStroke(linewidth=2, foreground="#000000")]
    )

    plt.tight_layout(pad=0)
    plt.savefig(output_path, dpi=100, bbox_inches='tight', pad_inches=0)
    plt.close()

def generate_caption(metrics, daily_metrics):
    now_utc = datetime.now(timezone.utc)
    return (
        "🚢 <b>گزارش ترافیک و پایش ناوبری تنگه هرمز (AIS زنده)</b> 🚨\n\n"
        f"📅 <b>تاریخ و زمان:</b> <code>{now_utc.strftime('%Y-%m-%d | %H:%M UTC')}</code>\n"
        "📍 <b>منطقه پایش:</b> <code>تنگه هرمز (سیگنال‌های زنده راداری AIS)</code>\n\n"
        "<blockquote>📊 <b>وضعیت ترافیک لحظه‌ای:</b>\n"
        f"🚢 <b>کل شناورهای رهگیری شده:</b> <code>{metrics['total']}</code>\n"
        f"📥 <b>ورودی (به سمت خلیج فارس):</b> <code>{metrics['inbound']}</code> فروند\n"
        f"📤 <b>خروجی (به سمت دریای عمان):</b> <code>{metrics['outbound']}</code> فروند\n"
        f"🛢️ <b>نفتکش‌ها و حامل‌های سوخت:</b> <code>{metrics['tankers']}</code> فروند\n"
        f"⚓ <b>متوقف / لنگرانداخته:</b> <code>{metrics['anchored']}</code> فروند</blockquote>\n\n"
        "<blockquote>📈 <b>آمار کل تردد ۲۴ ساعت گذشته:</b>\n"
        f"🔹 <b>مجموع شناورهای ورودی:</b> <code>{daily_metrics['today_inbound']}</code>\n"
        f"🔸 <b>مجموع شناورهای خروجی:</b> <code>{daily_metrics['today_outbound']}</code>\n"
        f"🌐 <b>کل ثبت تردد امروز:</b> <code>{daily_metrics['today_total']}</code> فروند</blockquote>\n\n"
        "🔍 <i>داده‌ها بدون واسطه از فرستنده‌های AIS ماهواره‌ای و ساحلی استخراج شده‌اند.</i>\n\n"
        "⚓ @secretollah 🚢\n"
        "#تنگه_هرمز #نفتکش #OSINT"
    )

async def run_bot():
    if not TELEGRAM_BOT_TOKEN or not AISSTREAM_API_KEY:
        logger.error("Missing TELEGRAM_BOT_TOKEN or AISSTREAM_API_KEY in environment!")
        return

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    image_path = "hormuz_snapshot.png"
    history = load_history()
    current_hour = datetime.now(timezone.utc).hour

    try:
        vessels = await fetch_live_ais(duration_seconds=55)
        if len(vessels) == 0:
            logger.warning("No vessels captured in stream window; aborting to avoid blank post.")
            return

        metrics, daily_metrics = classify_traffic(vessels, history)
        render_radar_map(vessels, metrics, daily_metrics, image_path)

        last_scheduled = history.get("last_scheduled_hour")
        is_scheduled = (current_hour in SCHEDULED_HOURS_UTC) and (last_scheduled != current_hour)

        if IS_MANUAL_RUN or is_scheduled or last_scheduled is None:
            logger.info("Publishing tactical update to Telegram...")
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
        logger.error(f"Bot failed: {e}", exc_info=True)
    finally:
        if os.path.exists(image_path):
            os.remove(image_path)

if __name__ == "__main__":
    asyncio.run(run_bot())
