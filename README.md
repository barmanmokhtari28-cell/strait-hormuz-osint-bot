# Strait of Hormuz OSINT Ship Tracking Bot 🚢⚓

An automated OSINT Telegram Bot that captures real-time AIS marine traffic snapshots of the Strait of Hormuz and publishes formatted updates directly to your Telegram channel.

## Features
- 📸 Automatic high-resolution screenshotting of Strait of Hormuz live ship traffic using Playwright.
- 📡 Formatted OSINT captioning with custom channel signatures.
- ⏱️ Configurable automated timer loops for periodic monitoring.

## Channel
📢 Updates posted directly to [**@secretollah**](https://t.me/secretollah).

## Setup & Deployment Instructions

### 1. Requirements
- Python 3.9+
- A Telegram Bot token (from [@BotFather](https://t.me/BotFather))
- Add your bot as an **Administrator** in your Telegram channel (`@secretollah`).

### 2. Local Installation
```bash
# Clone the repository
git clone https://github.com/YOUR_USERNAME/strait-hormuz-osint-bot.git
cd strait-hormuz-osint-bot

# Install dependencies
pip install -r requirements.txt

# Install Playwright browser binaries
playwright install chromium

# Create .env file
cp .env.example .env
