# 🎮 Steam & Epic Games Price Tracker MCP Server

A powerful **Model Context Protocol (MCP) server** for tracking game prices, sending deals, and managing price alerts across **Steam** and **Epic Games Store**.

---

## ✨ Features

### 🕹️ Steam Store
- 🔍 Game Search - Search Steam games with prices and App IDs
- 📉 Price Alerts - Get notified when games drop below target prices
- 🔥 Top Deals - Instant email with today's hottest Steam deals
- ⏰ Daily Deals - Subscribe to daily deal notifications

### 🕹️ Epic Games Store
- 🔍 Game Search - Search Epic Games Store with pricing information
- 🆓 Free Games Tracking - Monitor Epic's weekly free games
- 📉 Price Monitoring & Alerts - Track price changes and deals
- 🔄 Cross-Platform Search - Search across Steam & Epic simultaneously
- 📊 Price Comparison - Compare prices between Steam & Epic

### 💡 Multi-User Support
- 👥 Full database support for multiple users
- 📧 Email notifications via Resend API or SMTP

---

## 🚀 Quick Start

### 1️⃣ Environment Setup

Create a `.env` file:

    # Steam
    AUTH_TOKEN=your_bearer_token_here
    MY_NUMBER=your_phone_number
    DATABASE_URL=postgresql://user:password@host:port/database
    RESEND_API_KEY=your_resend_api_key
    SENDER_EMAIL=your@email.com

    # Epic Games (Optional)
    EPIC_PRICE_CHECK_HOURS=12
    EPIC_FREE_GAMES_CHECK_HOURS=6
    EPIC_DEALS_TIME=22:30
    SMTP_HOST=smtp.gmail.com
    SMTP_PORT=587
    SENDER_PASSWORD=your-app-password

### 2️⃣ Install Dependencies

    pip install -r requirements.txt
    pip install epicstore_api cloudscraper aiohttp schedule

### 3️⃣ Start Server

    python steam_tracker_mcp.py

Server runs on `http://0.0.0.0:8091` with Bearer Token authentication.

---

## 🛠️ MCP Tools

### 🔹 Steam Tools

    # Search games
    search_steam_games(query="Cyberpunk 2077")

    # Price alerts
    setup_price_alert_by_appid(app_id=1091500, email="user@example.com", target_price=500)

    # Top deals today
    send_top_deals_today(email="user@example.com")

    # Subscribe to daily deals
    subscribe_daily_deals(email="user@example.com")

    # User registration
    register_user(email="user@example.com")

### 🔹 Epic Games Tools

    # Search Epic Games Store
    search_epic_games(query="Cyberpunk 2077", limit=10)

    # Get current free games
    get_epic_free_games()

    # Set up price alerts
    setup_epic_price_alert(epic_namespace="namespace", epic_offer_id="offer_id", email="user@example.com", target_price=29.99)

    # Subscribe to free games alerts
    subscribe_epic_free_games_alerts(email="user@example.com")

### 🔹 Multi-Platform Tools

    # Search across Steam and Epic
    search_games_all_platforms(query="Hades", platforms="steam,epic", limit=10)

    # Compare prices between platforms
    compare_game_prices(game_title="Control")

---

## 🗄️ Database Schema

- steam_users - User management
- steam_price_alerts - Steam price alerts
- daily_deals_subscriptions - Steam daily deals
- epic_price_alerts - Epic price alerts
- epic_free_games_subscriptions - Epic free game alerts

---

## ⚡ Deployment

### 🌐 Render.com Deployment
1. Build Command: `pip install -r requirements.txt`
2. Start Command: `python steam_tracker_mcp.py`
3. Environment Variables: Add `.env` variables in dashboard
4. Port: 8091

### 🖥️ Local Development

    pip install -r requirements.txt
    cp .env.example .env
    python steam_tracker_mcp.py

---

## ✅ Production Features

- Graceful database failure handling (search-only mode)
- Comprehensive error handling and logging
- Bearer token authentication
- Async/await for optimal performance
- Background job scheduling
- Multi-user support
- Multi-platform price tracking

---

## 📊 Multi-Platform Coverage

Feature              | Steam       | Epic Games
-------------------- | ----------- | -------------
Game Search           | ✅ Full API | ✅ Working
Price Alerts          | ✅ Full    | ✅ Working
Free Games            | ⚠️ Limited | ✅ Excellent
Daily Deals           | ✅ Full    | ✅ Working
Email Alerts          | ✅ Full    | ✅ Full

**Your MCP server now covers both major PC gaming platforms!** 🎯

## 💬 Support

- Steam & Epic API integration for accurate pricing
- Multi-currency support (currently INR)
- Robust error handling and recovery
- Comprehensive logging for debugging
