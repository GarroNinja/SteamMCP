# Epic Games Store Integration Setup Guide

## Overview
This guide helps you set up Epic Games Store integration for the Steam MCP Server.

## Installation

### 1. Install Dependencies
```bash
pip install epicstore_api cloudscraper aiohttp schedule
```

### 2. Database Setup
```bash
# Apply Epic Games database schema
psql -d your_database -f epic_games_database_schema.sql
```

### 3. Environment Configuration
Add these variables to your .env file:

```bash
# Epic Games Integration
EPIC_PRICE_CHECK_HOURS=12
EPIC_FREE_GAMES_CHECK_HOURS=6
EPIC_DEALS_TIME=22:30

# Email notifications
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SENDER_EMAIL=your-email@gmail.com
SENDER_PASSWORD=your-app-password
```

## Usage

### Epic Games Tools
- `search_epic_games(query, limit)` - Search Epic Games Store
- `get_epic_free_games()` - Get current free games
- `setup_epic_price_alert(namespace, offer_id, email, target_price)` - Set price alerts
- `subscribe_epic_free_games_alerts(email)` - Subscribe to free games notifications

### Multi-Platform Tools  
- `search_games_all_platforms(query, platforms, limit)` - Cross-platform search
- `compare_game_prices(game_title)` - Compare prices between Steam and Epic

## Background Monitoring

The system includes automated monitoring:
- **Price Alerts**: Checks every 12 hours by default
- **Free Games**: Monitors every 6 hours for new free games
- **Email Notifications**: Sends alerts when conditions are met

## Troubleshooting

### Common Issues:
1. **Epic Games API Errors**: The system uses multiple fallback endpoints
2. **Email Not Working**: Check SMTP credentials and configuration
3. **Database Errors**: Ensure schema is applied correctly

### Debug Mode:
Set `LOG_LEVEL=DEBUG` in your environment to see detailed logs.

## Features

### Epic Games Store Integration:
- ✅ Game search with pricing information
- ✅ Free games tracking and alerts
- ✅ Price monitoring and notifications
- ✅ Background monitoring system
- ✅ Email notification system

### Multi-Platform Features:
- ✅ Cross-platform game search
- ✅ Price comparison between Steam and Epic Games
- ✅ Unified user experience
- ✅ Single database for both platforms

This integration transforms your Steam-only MCP server into a comprehensive PC gaming tracker covering both major platforms!
