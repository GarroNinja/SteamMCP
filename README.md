# Steam Price Tracker MCP Server

A powerful Model Context Protocol (MCP) server for tracking Steam game prices, sending deals, and managing price alerts.

## Features

🎮 **Steam Game Search** - Search for games with prices and App IDs  
📧 **Price Alerts** - Get notified when games drop below target prices  
🔥 **Top Deals** - Instant email with today's hottest Steam deals  
⏰ **Daily Deals** - Subscribe to daily deal notifications  
💰 **Multi-user Support** - Full database support for multiple users  

## Quick Start

### 1. Environment Setup

Create a `.env` file with your credentials:

```env
AUTH_TOKEN=your_bearer_token_here
MY_NUMBER=your_phone_number
DATABASE_URL=postgresql://user:password@host:port/database
RESEND_API_KEY=your_resend_api_key
SENDER_EMAIL=your@email.com
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Start Server

```bash
python steam_tracker_mcp.py
```

### 4. Connect to MCP

Server runs on `http://0.0.0.0:8091` with Bearer Token authentication.

## Available Tools

### Search Games
```python
search_steam_games(query="Cyberpunk 2077")
```

### Price Alerts  
```python
setup_price_alert_by_appid(
    app_id=1091500,
    email="user@example.com", 
    target_price=500
)
```

### Top Deals
```python
send_top_deals_today(email="user@example.com")
```

### Daily Deals Subscription
```python
subscribe_daily_deals(email="user@example.com")
```

### User Registration
```python
register_user(email="user@example.com")
```

## Database Schema

The server automatically creates these tables:

- `steam_users` - User management
- `steam_price_alerts` - Price tracking alerts  
- `daily_deals_subscriptions` - Daily deal subscriptions

## Configuration

- **Country Code**: Set to India (IN) for INR pricing
- **Background Jobs**: Price checks every 12 hours, daily deals at 10:30 PM
- **Email Provider**: Uses Resend API for notifications
- **Steam APIs**: Official Steam Store API integration

## Deployment

### Render.com Deployment

1. **Build Command**: `pip install -r requirements.txt`
2. **Start Command**: `python steam_tracker_mcp.py`
3. **Environment Variables**: Add your `.env` variables in dashboard
4. **Port**: Server uses port 8091

### Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Create .env file with your credentials
cp .env.example .env

# Start server
python steam_tracker_mcp.py
```

## File Structure

```
SteamMCP/
├── steam_tracker_mcp.py    # Main MCP server
├── requirements.txt        # Dependencies
├── README.md              # This file
├── LICENSE                # License file
├── .gitignore            # Git ignore rules
└── .env                  # Environment variables (not in repo)
```

## Production Features

- ✅ Graceful database failure handling (search-only mode)
- ✅ Comprehensive error handling and logging
- ✅ Bearer token authentication for security
- ✅ Async/await for optimal performance
- ✅ Background job scheduling
- ✅ Multi-user database support

## API Integration

- **Steam Store API**: For game pricing and details
- **Steam Apps API**: For game search and App ID lookup
- **Resend API**: For email notifications
- **PostgreSQL**: For user and alert data storage

## Support

- Steam API integration for accurate pricing
- Multi-currency support (currently INR)
- Robust error handling and recovery
- Comprehensive logging for debugging 