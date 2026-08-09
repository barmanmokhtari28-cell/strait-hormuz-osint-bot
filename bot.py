import os
import asyncio
import logging
from dotenv import load_dotenv
from telegram import Bot
from telegram.constants import ParseMode
from playwright.async_api import async_playwright

# Load local environment variables if available
load_dotenv()

# Read secret credentials injected safely by GitHub Secrets
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "@secretollah")

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Live AIS map centered over the Strait of Hormuz
MAP_URL = "https://www.vesselfinder.com/aismap?zoom=9&lat=26.4500&lon=56.3500"

async def capture_hormuz_map(output_path="hormuz_snapshot.png"):
    """
    Launches Playwright headless browser to take a snapshot of ship traffic.
    """
    logger.info("Capturing live Strait of Hormuz AIS traffic snapshot...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        
        # Open map and wait for AIS ship markers to populate
        await page.goto(MAP_URL, wait_until="networkidle", timeout=60000)
        await asyncio.sleep(5) 
        
        await page.screenshot(path=output_path)
        await browser.close()
        
    logger.info("Snapshot captured successfully.")
    return output_path

def generate_caption():
    """
    Generates structured OSINT vessel traffic caption.
    """
    return (
        "🚨 <b>OSINT ALERT: Strait of Hormuz Transit Scan</b> 🚨\n\n"
        "📍 <b>Zone:</b> Strait of Hormuz (Choke Point)\n"
        "🌊 <b>Coordinates:</b> 26°27'N 56°21'E\n"
        "🚢 <b>Traffic Status:</b> Active Commercial / Tanker Transits\n"
        "📊 <b>Source:</b> Open-Source Satellite & AIS Network\n\n"
        "🔍 <i>Live situational monitoring report.</i>\n\n"
        "⚓ @secretollah 🚢"
    )

async def run_bot():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("FATAL ERROR: TELEGRAM_BOT_TOKEN is missing! Add it in GitHub Secrets.")
        return

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    image_path = "hormuz_snapshot.png"

    try:
        # Step 1: Capture screenshot
        await capture_hormuz_map(image_path)
        
        # Step 2: Post photo & caption to Telegram channel
        with open(image_path, "rb") as photo:
            await bot.send_photo(
                chat_id=TELEGRAM_CHANNEL_ID,
                photo=photo,
                caption=generate_caption(),
                parse_mode=ParseMode.HTML
            )
        logger.info(f"Successfully posted OSINT update to {TELEGRAM_CHANNEL_ID}")

    except Exception as e:
        logger.error(f"Error executing bot workflow: {e}")
        
    finally:
        # Clean up temporary screenshot
        if os.path.exists(image_path):
            os.remove(image_path)

if __name__ == "__main__":
    asyncio.run(run_bot())
