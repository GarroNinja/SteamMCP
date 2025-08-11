#!/usr/bin/env python3
"""
Steam Price Tracker MCP Server
A comprehensive Steam game price tracking and alerting system using FastMCP.
"""

import asyncio
import logging
import os
import re
import schedule
import time
import threading
import json
import urllib.parse
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Dict, List, Optional, Tuple

import aiohttp
import asyncpg
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.auth.providers.bearer import BearerAuthProvider, RSAKeyPair
from mcp import ErrorData, McpError
from mcp.server.auth.provider import AccessToken
from mcp.types import TextContent, ImageContent, INVALID_PARAMS, INTERNAL_ERROR
from pydantic import BaseModel, Field, AnyUrl

# Load environment variables
load_dotenv()

# Configuration from environment variables
DATABASE_URL = os.environ.get("DATABASE_URL")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY") 
SENDER_EMAIL = os.environ.get("SENDER_EMAIL")
TOKEN = os.environ.get("AUTH_TOKEN")
MY_NUMBER = os.environ.get("MY_NUMBER")
STEAM_WEB_API_KEY = os.environ.get("STEAM_WEB_API_KEY")
STEAM_API_BASE_URL = "https://api.steampowered.com"
STEAM_STORE_API_BASE_URL = "https://store.steampowered.com/api"
STEAM_WEB_API_BASE_URL = "https://steamwebapi.com"
COUNTRY_CODE = "IN"

# Global cache for deals and popular games
deals_cache = {
    "last_updated": None,
    "deals": [],
    "cache_file": "steam_deals_cache.json"
}

# Top 50 popular games for instant price responses
popular_games_cache = {
    "games": [],
    "cache_file": "popular_games_cache.json"
}

# Validate required environment variables
if not DATABASE_URL:
    raise ValueError("DATABASE_URL must be set in .env file")
if not TOKEN:
    raise ValueError("AUTH_TOKEN must be set in .env file")
if not MY_NUMBER:
    raise ValueError("MY_NUMBER must be set in .env file")
if not STEAM_WEB_API_KEY:
    raise ValueError("STEAM_WEB_API_KEY must be set in .env file")
if not RESEND_API_KEY:
    print("⚠️  RESEND_API_KEY not set - email notifications will be simulated")
if not SENDER_EMAIL:
    print("⚠️  SENDER_EMAIL not set - using default sender")

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Auth Provider
class SimpleBearerAuthProvider(BearerAuthProvider):
    def __init__(self, token: str):
        k = RSAKeyPair.generate()
        super().__init__(public_key=k.public_key, jwks_uri=None, issuer=None, audience=None)
        self.token = token

    async def load_access_token(self, token: str) -> AccessToken | None:
        if token == self.token:
            return AccessToken(
                token=token,
                client_id="puch-client",
                scopes=["*"],
                expires_at=None,
            )
        return None

# Rich Tool Description model
class RichToolDescription(BaseModel):
    description: str
    use_when: str
    side_effects: str | None = None

class DatabaseManager:
    """Manages database connections and operations for Steam tracker."""
    
    def __init__(self, database_url: str):
        self.database_url = database_url
        self.pool = None
        
    async def initialize(self):
        """Initialize database connection pool and create tables."""
        self.pool = await asyncpg.create_pool(
            self.database_url,
            min_size=5,
            max_size=20,
            command_timeout=30,
            statement_cache_size=0  # Disable prepared statements for pgbouncer compatibility
        )
        await self.create_tables()
        logger.info("Database initialized successfully")
        
    async def create_tables(self):
        """Create all required tables for Steam tracker."""
        async with self.pool.acquire() as conn:
            # Drop old tables if they exist
            await conn.execute("""
                DROP TABLE IF EXISTS price_alerts CASCADE;
                DROP TABLE IF EXISTS daily_deals_subscriptions CASCADE;
                DROP TABLE IF EXISTS steam_games CASCADE;
                DROP TABLE IF EXISTS steam_users CASCADE;
                DROP TABLE IF EXISTS users CASCADE;
                DROP TABLE IF EXISTS user_sessions CASCADE;
                DROP TABLE IF EXISTS audit_log CASCADE;
            """)
            
            # Create users table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS steam_users (
                    id SERIAL PRIMARY KEY,
                    email VARCHAR(255) UNIQUE NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_active BOOLEAN DEFAULT TRUE,
                    daily_deals_subscription BOOLEAN DEFAULT FALSE
                )
            """)
            
            # Create steam games table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS steam_games (
                    app_id INTEGER PRIMARY KEY,
                    name VARCHAR(500) NOT NULL,
                    current_price DECIMAL(10,2),
                    currency VARCHAR(10) DEFAULT 'INR',
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_free BOOLEAN DEFAULT FALSE
                )
            """)
            
            # Create price alerts table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS price_alerts (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES steam_users(id) ON DELETE CASCADE,
                    app_id INTEGER REFERENCES steam_games(app_id) ON DELETE CASCADE,
                    target_price DECIMAL(10,2) NOT NULL,
                    alert_type VARCHAR(20) DEFAULT 'below_target',
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    triggered_at TIMESTAMP,
                    UNIQUE(user_id, app_id, alert_type)
                )
            """)
            
            # Create daily deals subscriptions table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_deals_subscriptions (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES steam_users(id) ON DELETE CASCADE,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id)
                )
            """)
            
            # Create indexes for better performance
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_price_alerts_active ON price_alerts(is_active, app_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_steam_games_name ON steam_games(name)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON steam_users(email)")
            
            logger.info("Database tables created successfully")

# Steam API Helper Functions  
async def find_steam_game(query: str):
    """Enhanced Steam game search with fuzzy matching."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.steampowered.com/ISteamApps/GetAppList/v2/") as response:
                if response.status != 200:
                    return []
                
                data = await response.json()
                apps = data.get('applist', {}).get('apps', [])
                
                query_lower = query.lower().strip()
                matches = []
                
                for app in apps:
                    name = app.get('name', '').strip()
                    if not name:
                        continue
                    
                    name_lower = name.lower()
                    
                    # Skip technical entries
                    if any(skip in name_lower for skip in ['dedicated server', 'sdk', 'authoring tools', 'workshop', 'demo']):
                        continue
                    
                    # Calculate similarity score
                    similarity_score = calculate_similarity(query_lower, name_lower)
                    
                    # Include matches with good similarity or exact substring matches
                    if query_lower in name_lower or similarity_score > 0.6:
                        matches.append({
                            'name': name,
                            'appid': app['appid'],
                            'exact': query_lower == name_lower,
                            'similarity': similarity_score
                        })
                
                # Sort: exact matches first, then by similarity score, then alphabetical
                matches.sort(key=lambda x: (not x['exact'], -x['similarity'], x['name'].lower()))
                return matches[:15]  # Return more matches
                
    except Exception as e:
        logger.error(f"Search error: {e}")
        return []

def calculate_similarity(query: str, name: str) -> float:
    """Calculate similarity between query and game name using multiple methods."""
    # Exact match
    if query == name:
        return 1.0
    
    # Substring match
    if query in name:
        return 0.9
    
    # Simple fuzzy matching for common variations
    query_words = set(query.split())
    name_words = set(name.split())
    
    if not query_words or not name_words:
        return 0.0
    
    # Jaccard similarity (intersection over union)
    intersection = len(query_words.intersection(name_words))
    union = len(query_words.union(name_words))
    
    if union == 0:
        return 0.0
    
    jaccard = intersection / union
    
    # Boost score if most words match
    if intersection >= len(query_words) * 0.8:
        jaccard += 0.2
    
    # Handle common variations
    query_clean = query.replace("-", " ").replace(":", "").replace("'", "")
    name_clean = name.replace("-", " ").replace(":", "").replace("'", "")
    
    if query_clean in name_clean or any(word in name_clean for word in query_clean.split() if len(word) > 3):
        jaccard = max(jaccard, 0.7)
    
    return min(jaccard, 1.0)

async def get_steam_price(app_id: int):
    """Get price for a specific Steam app ID."""
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://store.steampowered.com/api/appdetails?appids={app_id}&cc=IN"
            async with session.get(url) as response:
                if response.status != 200:
                    return None
                
                data = await response.json()
                app_data = data.get(str(app_id))
                
                if not app_data or not app_data.get('success'):
                    return None
                
                return app_data.get('data', {})
                
    except Exception as e:
        logger.error(f"Price error: {e}")
        return None

class EmailService:
    """Handles email notifications using Resend API."""
    
    def __init__(self):
        self.api_key = RESEND_API_KEY
        self.sender_email = SENDER_EMAIL
        
    async def send_email(self, to_email: str, subject: str, html_content: str) -> bool:
        """Send email using Resend API."""
        if self.api_key == "your_resend_api_key_here":
            logger.warning("Resend API key not configured, email simulation mode")
            logger.info(f"📧 Would send email to {to_email}: {subject}")
            return True
            
        try:
            headers = {
                'Authorization': f'Bearer {self.api_key}',
                'Content-Type': 'application/json'
            }
            
            data = {
                'from': self.sender_email,
                'to': [to_email],
                'subject': subject,
                'html': html_content
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    'https://api.resend.com/emails',
                    headers=headers,
                    json=data
                ) as response:
                    if response.status == 200:
                        logger.info(f"Email sent successfully to {to_email}")
                        return True
                    else:
                        logger.error(f"Failed to send email: {response.status}")
                        
        except Exception as e:
            logger.error(f"Error sending email: {e}")
            
        return False
        
    def create_price_alert_email(self, game_name: str, current_price: float, target_price: float) -> str:
        """Create HTML email for price alert."""
        return f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                <h2 style="color: #1b2838;">🎮 Steam Price Alert!</h2>
                <div style="background: #f5f5f5; padding: 20px; border-radius: 8px; margin: 20px 0;">
                    <h3 style="margin-top: 0; color: #1b2838;">{game_name}</h3>
                    <p><strong>Current Price:</strong> ₹{current_price}</p>
                    <p><strong>Your Target:</strong> ₹{target_price}</p>
                    <p style="color: #27ae60; font-weight: bold;">✅ Price target reached!</p>
                </div>
                <p>Don't miss this deal! Visit Steam to purchase now.</p>
            </div>
        </body>
        </html>
        """

class PriceTracker:
    """Handles price tracking and alerting logic."""
    
    def __init__(self, db_manager: DatabaseManager, email_service: EmailService):
        self.db = db_manager
        self.email_service = email_service
        
    async def check_price_alerts(self):
        """Check all active price alerts and send notifications if needed."""
        async with self.db.pool.acquire() as conn:
            alerts = await conn.fetch("""
                SELECT pa.id, pa.user_id, pa.app_id, pa.target_price, pa.alert_type,
                       sg.name as game_name, sg.current_price, su.email
                FROM price_alerts pa
                JOIN steam_games sg ON pa.app_id = sg.app_id
                JOIN steam_users su ON pa.user_id = su.id
                WHERE pa.is_active = TRUE AND su.is_active = TRUE
            """)
            
            alerts_triggered = 0
            
            for alert in alerts:
                game_details = await get_steam_price(alert['app_id'])
                
                if game_details:
                    price_overview = game_details.get('price_overview')
                    if price_overview:
                        current_price = price_overview.get('final', 0) / 100.0
                        
                        await conn.execute("""
                            UPDATE steam_games 
                            SET current_price = $1, last_updated = CURRENT_TIMESTAMP 
                            WHERE app_id = $2
                        """, current_price, alert['app_id'])
                        
                        should_trigger = False
                        
                        if alert['alert_type'] == 'below_target':
                            should_trigger = current_price <= alert['target_price']
                        elif alert['alert_type'] == 'below_current':
                            should_trigger = current_price < alert['current_price']
                            
                        if should_trigger:
                            subject = f"🎮 Price Alert: {alert['game_name']}"
                            html_content = self.email_service.create_price_alert_email(
                                alert['game_name'], current_price, alert['target_price']
                            )
                            
                            if await self.email_service.send_email(alert['email'], subject, html_content):
                                await conn.execute("""
                                    UPDATE price_alerts 
                                    SET is_active = FALSE, triggered_at = CURRENT_TIMESTAMP 
                                    WHERE id = $1
                                """, alert['id'])
                                
                                alerts_triggered += 1
                                
            logger.info(f"Price check completed. {alerts_triggered} alerts triggered.")

# Global instances
db_manager = DatabaseManager(DATABASE_URL)
email_service = EmailService()

# Global context for maintaining search state
last_search_results = []
last_search_query = ""
price_tracker = PriceTracker(db_manager, email_service)

# Background scheduler for price checks
def run_scheduler():
    """Run background scheduler for price checks and cache refresh."""
    schedule.every(12).hours.do(lambda: asyncio.run(price_tracker.check_price_alerts()))
    
    # Schedule cache refresh every 6 hours
    schedule.every(6).hours.do(refresh_deals_cache)
    
    while True:
        schedule.run_pending()
        time.sleep(60)

def refresh_deals_cache():
    """Refresh the deals cache (called by scheduler)."""
    try:
        logger.info("🔄 Scheduled cache refresh...")
        # Run the async function in a new event loop
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(fetch_and_cache_deals())
        loop.close()
    except Exception as e:
        logger.error(f"Error refreshing cache: {e}")

# Initialize FastMCP server
mcp = FastMCP(
    "Steam Price Tracker MCP Server",
    auth=SimpleBearerAuthProvider(TOKEN),
)

# No custom health check endpoints needed - FastMCP handles this automatically

# Required validate tool for Puch compatibility
@mcp.tool
async def validate() -> str:
    """Validate bearer token and return user phone number for Puch compatibility."""
    return MY_NUMBER

# Tool descriptions
RegisterUserDescription = RichToolDescription(
    description="Register a new user with email for Steam price tracking",
    use_when="When a user wants to start tracking Steam game prices and needs to register their email",
    side_effects="Creates a new user account in the database"
)

SearchGamesDescription = RichToolDescription(
    description="Search Steam games and return complete numbered list. Tool output contains formatted game list that should be displayed in full to allow user selection.",
    use_when="When user asks for Steam game information or pricing. Show complete tool output to enable user selection workflow.",
    side_effects="Returns numbered game options with prices. After showing results, user will likely provide a number for selection."
)

ChooseGameDescription = RichToolDescription(
    description="Handle user selections from search results. When user provides any number (1, 2, 3, etc.) or selection phrase after seeing game search results, this tool retrieves detailed information for that specific game.",
    use_when="When user responds with a number or makes a selection after search_games has shown numbered options. Use this tool to get details for the selected game.",
    side_effects="Returns detailed game information including current pricing, discounts, and availability status."
)

ProcessSelectionDescription = RichToolDescription(
    description="Handle user responses that contain numbers or selections after search results. Use this when user provides responses like '1', '2', 'option 3', 'I want 2', etc.",
    use_when="When user provides a response containing a number or selection reference after seeing search results. This tool extracts the selection and gets game details.",
    side_effects="Processes user selection and returns detailed game information. Maintains workflow by interpreting user choices."
)

AddPriceAlertDescription = RichToolDescription(
    description="Add a price alert for a specific Steam game",
    use_when="When user wants to be notified when a game's price drops below a target",
    side_effects="Creates price alert in database and enables automatic monitoring"
)

# MCP Tools
@mcp.tool(description="Refresh the deals cache with fresh Steam data")
async def refresh_deals_cache_tool() -> str:
    """Manually refresh the deals cache."""
    try:
        logger.info("🔄 Manual cache refresh requested...")
        deals = await fetch_and_cache_deals()
        return f"✅ Cache refreshed successfully! Found {len(deals)} deals with 10%+ discounts."
    except Exception as e:
        logger.error(f"Cache refresh failed: {e}")
        return f"❌ Cache refresh failed: {str(e)}"

@mcp.tool(description="Get information about this Steam Price Tracker MCP server")
async def about() -> dict[str, str]:
    """Get detailed information about this MCP server."""
    from textwrap import dedent
    
    server_name = "Steam Price Tracker MCP"
    server_description = dedent("""
        This MCP server is designed to help users track Steam game prices and discover deals.
        It provides comprehensive tools to search Steam games, set up price alerts with email
        notifications, get instant top deals, and subscribe to daily deal updates.
        
        Key Features:
        • Steam Game Search - Find games with current prices and App IDs
        • Price Alerts - Get notified when games drop below target prices  
        • Top Deals Email - Instant email with today's hottest Steam deals
        • Daily Deals - Subscribe to daily deal notifications at 10:30 PM
        • Multi-user Support - Full database support for multiple users
        
        The server integrates with Steam's official APIs for accurate pricing data
        and uses Resend API for reliable email notifications. All price data is
        in Indian Rupees (INR) and includes discount percentages and savings calculations.
    """)

    return {
        "name": server_name,
        "description": server_description.strip()
    }

@mcp.tool(description=RegisterUserDescription.model_dump_json())
async def register_user(
    email: Annotated[str, Field(description="User's email address for notifications")]
) -> str:
    """Register a new user with email for Steam price tracking."""
    if not email or "@" not in email:
        raise McpError(ErrorData(code=INVALID_PARAMS, message="Invalid email address provided"))
        
    async with db_manager.pool.acquire() as conn:
        try:
            await conn.execute("""
                INSERT INTO steam_users (email) VALUES ($1)
                ON CONFLICT (email) DO NOTHING
            """, email)
            
            return f"✅ User registered successfully: {email}\nYou can now start tracking Steam game prices!"
        except Exception as e:
            raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"Error registering user: {str(e)}"))

@mcp.tool(description="Quick search in cached popular games for instant price response")
async def quick_game_price(
    query: Annotated[str, Field(description="Game name to search for in popular games cache (e.g., 'GTA', 'Witcher', 'Cyberpunk')")]
) -> str:
    """Search cached popular games for instant price response."""
    try:
        # Load popular games cache if not loaded
        if not popular_games_cache["games"]:
            try:
                if os.path.exists(popular_games_cache["cache_file"]):
                    with open(popular_games_cache["cache_file"], 'r') as f:
                        popular_games_cache["games"] = json.load(f)
            except:
                pass
        
        games = popular_games_cache["games"]
        if not games:
            return "❌ Popular games cache not available. Try refreshing the cache."
        
        # Search in cached games
        query_lower = query.lower()
        matches = []
        
        for game in games:
            if query_lower in game['name'].lower():
                matches.append(game)
        
        if not matches:
            return f"🔍 No matches found in popular games cache for '{query}'. Try using the full search_steam_games tool."
        
        # Format results
        result = f"🎮 **POPULAR GAMES PRICE CHECK FOR '{query.upper()}'**\n\n"
        
        for i, game in enumerate(matches[:5], 1):  # Show top 5 matches
            if game['discount'] > 0:
                result += f"{i}. **{game['name']}** 🔥\n"
                result += f"   💰 Price: ₹{game['current_price']:.2f} (was ₹{game['original_price']:.2f}) -{game['discount']}% OFF\n"
                result += f"   🆔 App ID: {game['app_id']}\n\n"
            else:
                result += f"{i}. **{game['name']}**\n"
                result += f"   💰 Price: ₹{game['current_price']:.2f}\n"
                result += f"   🆔 App ID: {game['app_id']}\n\n"
        
        if len(matches) > 5:
            result += f"... and {len(matches) - 5} more matches\n\n"
        
        result += "💡 **FOR PRICE TRACKING:**\n"
        result += "Use `setup_price_alert_by_appid(app_id=XXXXX, email=\"your@email.com\", target_price=XXX)`"
        
        return result
        
    except Exception as e:
        logger.error(f"Error in quick game price search: {e}")
        return f"❌ Error searching games: {str(e)}"

@mcp.tool(description="Search Steam games and display all matching games with their current prices and App IDs. Shows complete information without requiring selection.")
async def search_steam_games(
    query: Annotated[str, Field(description="Game name to search for (e.g., 'PEAK', 'Cyberpunk 2077')")]
) -> str:
    """Search Steam games and display all matches with prices and App IDs."""
    try:
        logger.info(f"Searching for games matching: {query}")
        
        # Find matching games
        matches = await find_steam_game(query)
        if not matches:
            return f"❌ No Steam games found matching '{query}'. Try different keywords or check spelling."
        
        # Display up to 15 games with full details
        result = f"🔍 **STEAM SEARCH RESULTS FOR '{query.upper()}'**\n\n"
        
        for i, game in enumerate(matches[:15], 1):
            name = game['name']
            app_id = game['appid']
            
            # Get current price
            price = "₹???"
            discount_info = ""
            try:
                price_data = await get_steam_price(app_id)
                if price_data:
                    price_overview = price_data.get('price_overview')
                    is_free = price_data.get('is_free', False)
                    
                    if is_free:
                        price = "FREE"
                    elif price_overview:
                        current_price = price_overview.get('final', 0) / 100.0
                        original_price = price_overview.get('initial', 0) / 100.0
                        discount = price_overview.get('discount_percent', 0)
                        
                        if discount > 0:
                            price = f"₹{current_price:.2f} (was ₹{original_price:.2f}) -{discount}% OFF"
                            discount_info = " 🔥"
                        else:
                            price = f"₹{current_price:.2f}"
                    else:
                        price = "Not available in India"
            except Exception as e:
                price = "Error loading price"
            
            result += f"{i:2d}. **{name}**{discount_info}\n"
            result += f"    💰 Price: {price}\n"
            result += f"    🆔 App ID: {app_id}\n\n"
        
        if len(matches) > 15:
            result += f"... and {len(matches) - 15} more games found.\n\n"
        
        result += "💡 **FOR PRICE TRACKING:**\n"
        result += "Use `setup_price_alert_by_appid(app_id=XXXXX, email=\"your@email.com\", target_price=XXX)`\n"
        result += "Example: `setup_price_alert_by_appid(app_id=1245620, email=\"user@example.com\", target_price=500)`"
        
        return result
        
    except Exception as e:
        logger.error(f"Error in steam search: {str(e)}")
        return f"❌ Error searching Steam: {str(e)}"

@mcp.tool(description="Get detailed price information for a specific Steam game by App ID")
async def get_game_details(
    app_id: Annotated[int, Field(description="Steam App ID of the game")]
) -> str:
    """Get detailed information including current price, discount, and other details for a Steam game."""
    return await get_game_price_internal(app_id)







async def format_game_details(game_data: dict, app_id: int) -> str:
    """Format detailed game information with price."""
    name = game_data.get('name', f'Game {app_id}')
    
    result = f"🎯 **{name}** (App ID: {app_id})\n"
    
    # Price information
    price_overview = game_data.get('price_overview')
    is_free = game_data.get('is_free', False)
    
    if is_free:
        result += f"💰 Price: **Free to Play** 🆓\n"
    elif not price_overview:
        result += f"💰 Price: **Not available in India** 🚫\n"
    else:
        current_price = price_overview.get('final', 0) / 100.0
        initial_price = price_overview.get('initial', 0) / 100.0
        discount = price_overview.get('discount_percent', 0)
        currency = price_overview.get('currency', 'INR')
        
        result += f"💰 Current Price: **₹{current_price:.2f}** ({currency})\n"
        
        if discount > 0:
            result += f"🏷️  Original Price: ₹{initial_price:.2f}\n"
            result += f"🔥 Discount: **{discount}% OFF**\n"
            savings = initial_price - current_price
            result += f"💵 You Save: ₹{savings:.2f}\n"
    
    # Additional info
    developers = game_data.get('developers', [])
    publishers = game_data.get('publishers', [])
    release_date = game_data.get('release_date', {}).get('date', '')
    
    if developers:
        result += f"👨‍💻 Developer: {', '.join(developers[:2])}\n"
    if publishers:
        result += f"🏢 Publisher: {', '.join(publishers[:2])}\n"
    if release_date:
        result += f"📅 Release Date: {release_date}\n"
    
    return result

# HIDE this tool from AI by removing @mcp.tool decorator
async def get_game_price_internal(app_id: int) -> str:
    """INTERNAL: Get price by App ID - not exposed to AI."""
    
    # App ID corrections for known issues
    app_id_corrections = {
        2339980: (962130, "Grounded"),
        2332690: (962130, "Grounded"),
        1497980: (1245620, "Elden Ring"),
        2715940: (3504780, "Wildgate"),
        378570: (None, "PEAK"),
    }
    
    if app_id in app_id_corrections:
        correct_id, game_name = app_id_corrections[app_id]
        return f"⚠️  **Wrong App ID Used**\n\nApp ID {app_id} is incorrect for {game_name}.\nPlease use search_games instead!"
    
    try:
        game_data = await get_steam_price(app_id)
        
        if not game_data:
            return f"❌ Game with App ID {app_id} not found"
        
        return await format_game_details(game_data, app_id)
        
    except Exception as e:
        logger.error(f"Error fetching game details for {app_id}: {e}")
        return f"❌ Error retrieving game information: {str(e)}"

@mcp.tool(description="Set up a price alert for a Steam game using its App ID. Use this after searching for games to get the exact App ID.")
async def setup_price_alert_by_appid(
    app_id: Annotated[int, Field(description="Steam App ID of the game (get this from search_steam_games)")],
    email: Annotated[str, Field(description="User's email address for notifications")],
    target_price: Annotated[float, Field(description="Target price in INR - alert when price drops below this amount")]
) -> str:
    """Set up a price alert using Steam App ID. Much simpler and more precise than name-based search."""
    try:
        # Validate inputs
        if not email or "@" not in email:
            return f"❌ Valid email address is required. Please provide a valid email like: user@example.com"
        
        if not target_price or target_price <= 0:
            return f"❌ Valid target price is required. Must be greater than 0 INR."
        
        # Get game details to verify App ID exists
        game_data = await get_steam_price(app_id)
        if not game_data:
            return f"❌ Invalid App ID {app_id}. Game not found on Steam or not available in India."
        
        game_name = game_data.get('name', f'Game {app_id}')
        
        # Create the price alert
        result = await create_price_alert_internal(email, app_id, target_price)
        
        return result
        
    except Exception as e:
        logger.error(f"Error setting up price alert: {e}")
        return f"❌ Error setting up price alert: {str(e)}"

# Removed confirm_price_alert_game - using setup_price_alert_by_appid instead

async def create_price_alert_internal(email: str, app_id: int, target_price: float) -> str:
    """Internal function to create price alert."""
    # Check if database is available
    if not db_manager.pool:
        return "❌ Database not available. Price alerts require database connection."
    
    try:
        async with db_manager.pool.acquire() as conn:
            # Auto-register user if not exists
            await conn.execute("""
                INSERT INTO steam_users (email) VALUES ($1)
                ON CONFLICT (email) DO NOTHING
            """, email)
            
            # Get user ID
            user = await conn.fetchrow("SELECT id FROM steam_users WHERE email = $1", email)
            if not user:
                raise McpError(ErrorData(code=INTERNAL_ERROR, message="Failed to register user"))
                
            # Get current game info
            game_details = await get_steam_price(app_id)
            if not game_details:
                raise McpError(ErrorData(code=INVALID_PARAMS, message=f"Game with App ID {app_id} not found on Steam"))
                
            game_name = game_details.get('name', f'Game {app_id}')
            current_price = 0.0
            
            # Get current price
            price_overview = game_details.get('price_overview')
            is_free = game_details.get('is_free', False)
            
            if is_free:
                current_price = 0.0
            elif price_overview:
                current_price = price_overview.get('final', 0) / 100.0
            
            # Update game in database
            await conn.execute("""
                INSERT INTO steam_games (app_id, name, current_price) 
                VALUES ($1, $2, $3)
                ON CONFLICT (app_id) DO UPDATE SET 
                    name = EXCLUDED.name, 
                    current_price = EXCLUDED.current_price, 
                    last_updated = CURRENT_TIMESTAMP
            """, app_id, game_name, current_price)
            
            # Add price alert
            await conn.execute("""
                INSERT INTO price_alerts (user_id, app_id, target_price, alert_type)
                VALUES ($1, $2, $3, 'below_target')
                ON CONFLICT (user_id, app_id, alert_type) DO UPDATE SET
                    target_price = EXCLUDED.target_price,
                    is_active = TRUE,
                    triggered_at = NULL
            """, user['id'], app_id, target_price)
            
            # Determine if already below target
            status_msg = ""
            if current_price > 0 and current_price <= target_price:
                status_msg = "\n🎉 GOOD NEWS: The game is already at or below your target price!"
            else:
                status_msg = f"\n🔔 You'll be notified when the price drops from ₹{current_price:.2f} to ₹{target_price:.2f} or below."
            
            return (f"✅ Price alert created successfully!\n\n"
                   f"🎮 Game: {game_name}\n"
                   f"🎯 Target Price: ₹{target_price:.2f}\n"
                   f"💰 Current Price: ₹{current_price:.2f}\n"
                   f"📧 Email: {email}\n"
                   f"{status_msg}\n\n"
                   f"📱 Our backend checks prices daily and will email you when the price drops!")
                                                      
    except McpError:
        raise
    except Exception as e:
        logger.error(f"Database error: {e}")
        raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"Failed to add price alert: {str(e)}"))

@mcp.tool
async def remove_price_alert(
    email: Annotated[str, Field(description="User's email address")],
    app_id: Annotated[int, Field(description="Steam app ID of the game")]
) -> str:
    """Remove a price alert for a specific game."""
    async with db_manager.pool.acquire() as conn:
        try:
            result = await conn.execute("""
                UPDATE price_alerts 
                SET is_active = FALSE 
                WHERE user_id = (SELECT id FROM steam_users WHERE email = $1)
                AND app_id = $2 AND is_active = TRUE
            """, email, app_id)
            
            if result == "UPDATE 0":
                return "❌ No active alert found for this game"
                
            return "✅ Price alert removed successfully"
            
        except Exception as e:
            raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"Error removing alert: {str(e)}"))

@mcp.tool
async def list_user_alerts(
    email: Annotated[str, Field(description="User's email address")]
) -> str:
    """List all active price alerts for a user."""
    async with db_manager.pool.acquire() as conn:
        try:
            alerts = await conn.fetch("""
                SELECT sg.name, sg.app_id, pa.target_price, pa.alert_type, sg.current_price
                FROM price_alerts pa
                JOIN steam_games sg ON pa.app_id = sg.app_id
                WHERE pa.user_id = (SELECT id FROM steam_users WHERE email = $1)
                AND pa.is_active = TRUE
                ORDER BY sg.name
            """, email)
            
            if not alerts:
                return "📋 No active price alerts found"
                
            result = f"📋 **Active Price Alerts for {email}:**\n\n"
            for alert in alerts:
                result += f"🎮 **{alert['name']}** (ID: {alert['app_id']})\n"
                result += f"   Target: ₹{alert['target_price']} | Current: ₹{alert['current_price'] or 'N/A'}\n"
                result += f"   Type: {alert['alert_type']}\n\n"
                
            return result
            
        except Exception as e:
            raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"Error fetching alerts: {str(e)}"))

@mcp.tool(description="Subscribe to daily Steam deals notifications at 10:30 PM. Requires valid email address.")
async def subscribe_daily_deals(
    email: Annotated[str, Field(description="User's email address for notifications")]
) -> str:
    """Subscribe user to daily deals notifications."""
    # Validate email
    if not email or "@" not in email:
        return "❌ Valid email address is required. Please use: subscribe_daily_deals(email=\"your@email.com\")"
    
    # Check if database is available
    if not db_manager.pool:
        return "❌ Database not available. Daily deals require database connection."
    
    async with db_manager.pool.acquire() as conn:
        try:
            user = await conn.fetchrow("SELECT id FROM steam_users WHERE email = $1", email)
            if not user:
                return "❌ User not found. Please register first using register_user(email=\"your@email.com\")"
                
            await conn.execute("""
                INSERT INTO daily_deals_subscriptions (user_id)
                VALUES ($1)
                ON CONFLICT (user_id) DO UPDATE SET is_active = TRUE
            """, user['id'])
            
            return f"✅ {email} subscribed to daily deals! You'll receive deals at 10:30 PM every day."
            
        except Exception as e:
            logger.error(f"Error subscribing to daily deals: {e}")
            return f"❌ Error subscribing to daily deals: {str(e)}"

@mcp.tool(description="Get today's top Steam deals and send them via email")
async def send_top_deals_today(
    email: Annotated[str, Field(description="Email address to send the deals to")]
) -> str:
    """Get today's curated Steam deals and send via email immediately."""
    # Validate email
    if not email or "@" not in email:
        return "❌ Valid email address is required. Please use: send_top_deals_today(email=\"your@email.com\")"
    
    try:
        logger.info(f"Sending curated deals to {email}")
        
        # Get cached deals - instant response!
        top_deals = await get_cached_deals()
        
        if not top_deals:
            logger.warning("No deals in cache, trying emergency fallback")
            top_deals = await get_emergency_deals()
        
        if not top_deals:
            return "❌ No deals available right now. Try refreshing the cache or try again later."
        
        # Ensure we have valid deals before sending email
        valid_deals = [deal for deal in top_deals if deal.get('name') and deal.get('discount', 0) >= 0]
        
        if not valid_deals:
            return "❌ Found deals but they have invalid data. Try refreshing the cache."
        
        # Send email with deals
        email_sent = await send_deals_email(email, valid_deals, is_immediate=True)
        
        if email_sent:
            max_discount = max(deal['discount'] for deal in valid_deals) if valid_deals else 0
            return f"📧 ✅ Top Steam deals sent to {email}!\n\nFound {len(valid_deals)} curated games with discounts up to {max_discount}% OFF!"
        else:
            return f"❌ Failed to send email to {email}. Please check the email address and try again."
            
    except Exception as e:
        logger.error(f"Error sending top deals: {e}")
        return f"❌ Error getting top deals: {str(e)}"

# =============================================================================
# DEPRECATED FUNCTIONS - NO LONGER USED (kept for reference)
# =============================================================================

async def get_customized_top_deals_DEPRECATED(genre: str, age_preference: str) -> list:
    """DEPRECATED - Get customized Steam deals based on genre and age preferences, filtering for popular games."""
    try:
        logger.info(f"Searching for {genre} deals in {age_preference} games...")
        deals = []
        
        # Define App ID ranges based on age preferences (more accurate ranges)
        age_ranges = {
            "old": [
                range(10, 100000),       # Very old Steam games (2003-2012)
                range(100000, 300000),   # Classic games (2012-2016)
            ],
            "middle": [
                range(300000, 800000),   # Mid-era games (2016-2019)
                range(800000, 1200000),  # Late middle games (2019-2021)
            ],
            "recent": [
                range(1200000, 1800000), # Recent games (2021-2023)
                range(1800000, 2500000), # Very recent games (2023+)
            ],
            "any": [
                range(10, 800000),       # Mix of old and middle
                range(800000, 2500000),  # Mix of middle and recent
            ]
        }
        
        # Get genre-specific search terms
        genre_terms = {
            "Action": ["action", "shooter", "combat", "fighting"],
            "Adventure": ["adventure", "story", "narrative", "exploration"],
            "RPG": ["rpg", "role playing", "fantasy", "character"],
            "Strategy": ["strategy", "tactical", "management", "civilization"],
            "Simulation": ["simulation", "simulator", "farming", "city"],
            "Racing": ["racing", "driving", "car", "formula"],
            "Sports": ["sports", "football", "soccer", "basketball"],
            "Indie": ["indie", "independent", "pixel", "retro"],
            "Multiplayer": ["multiplayer", "online", "coop", "pvp"],
            "Puzzle": ["puzzle", "logic", "brain", "match"],
            "Horror": ["horror", "survival", "zombie", "scary"],
            "Fighting": ["fighting", "martial", "combat", "tekken"],
            "Any": ["grand", "call", "the", "steam", "game"]
        }
        
        # Method 1: Genre-based search (skip for "Any" genre as it's not effective)
        if genre in genre_terms and genre != "Any":
            for term in genre_terms[genre][:2]:  # Limit search terms
                try:
                    search_results = await find_steam_game(term)
                    for game in search_results[:20]:  # Check more games per search
                        app_id = game.get('appid')
                        if app_id and is_in_age_range(app_id, age_ranges[age_preference]):
                            deal = await check_popular_app_for_deal(app_id)
                            if deal:
                                deals.append(deal)
                except Exception as e:
                    logger.debug(f"Error searching {term}: {e}")
                    continue
        
        # Method 2: Fast targeted sampling (much smaller samples for speed)
        import random
        import asyncio
        
        for app_range in age_ranges[age_preference]:
            # Much smaller, faster samples
            if genre == "Any":
                sample_size = min(30, max(15, len(app_range) // 20000))  # Reduced dramatically
            else:
                sample_size = min(20, max(10, len(app_range) // 30000))  # Much smaller samples
            
            sample_app_ids = random.sample(list(app_range), sample_size)
            
            # Process in batches with timeout to prevent hanging
            batch_size = 5
            for i in range(0, len(sample_app_ids), batch_size):
                batch = sample_app_ids[i:i + batch_size]
                
                try:
                    # Process batch with timeout
                    async def process_batch():
                        batch_deals = []
                        for app_id in batch:
                            try:
                                # Skip genre check for "Any" to save time
                                if genre != "Any":
                                    # Quick genre check without full API call
                                    if not quick_genre_check(app_id, genre):
                                        continue
                                
                                deal = await check_popular_app_for_deal(app_id)
                                if deal:
                                    batch_deals.append(deal)
                            except:
                                continue
                        return batch_deals
                    
                    # Use timeout to prevent hanging
                    batch_deals = await asyncio.wait_for(process_batch(), timeout=5.0)
                    deals.extend(batch_deals)
                    
                    # Early exit if we have enough deals
                    if len(deals) >= 20:
                        break
                        
                except asyncio.TimeoutError:
                    logger.debug(f"Batch processing timed out, continuing...")
                    continue
                except Exception as e:
                    logger.debug(f"Batch processing error: {e}")
                    continue
            
            # Stop if we have enough deals
            if len(deals) >= 20:
                break
        
        # Method 3: Check Steam featured for genre matches
        try:
            featured_deals = await search_steam_featured_deals()
            for deal in featured_deals:
                app_id = deal['app_id']
                if is_in_age_range(app_id, age_ranges[age_preference]):
                    if genre == "Any" or await game_matches_genre(app_id, genre):
                        deals.append(deal)
        except Exception as e:
            logger.debug(f"Error checking featured deals: {e}")
        
        # Method 4: Check Steam specials for more deals
        try:
            special_deals = await search_steam_specials()
            for deal in special_deals:
                app_id = deal['app_id']
                if is_in_age_range(app_id, age_ranges[age_preference]):
                    if genre == "Any" or await game_matches_genre(app_id, genre):
                        deals.append(deal)
        except Exception as e:
            logger.debug(f"Error checking special deals: {e}")
        
        # Method 5: Search for popular games from each genre
        if genre != "Any" and genre in genre_terms:
            popular_game_searches = [
                "best", "top rated", "popular", "award winning", "indie hit"
            ]
            
            for search_term in popular_game_searches:
                try:
                    combined_query = f"{search_term} {genre_terms[genre][0]}"
                    search_results = await find_steam_game(combined_query)
                    
                    for game in search_results[:10]:
                        app_id = game.get('appid')
                        if app_id and is_in_age_range(app_id, age_ranges[age_preference]):
                            deal = await check_popular_app_for_deal(app_id)
                            if deal:
                                deals.append(deal)
                except Exception as e:
                    logger.debug(f"Error searching {combined_query}: {e}")
                    continue
        
        # Remove duplicates and filter for popular games
        unique_deals = {}
        for deal in deals:
            app_id = deal['app_id']
            if app_id not in unique_deals or deal['discount'] > unique_deals[app_id]['discount']:
                unique_deals[app_id] = deal
        
        # Filter for games with good popularity metrics and minimum discount
        popular_deals = [
            deal for deal in unique_deals.values() 
            if deal['discount'] >= 20 and deal.get('is_popular', True)  # Reduced minimum discount
        ]
        
        # If we don't have enough deals, try with lower discount threshold
        if len(popular_deals) < 5:
            popular_deals = [
                deal for deal in unique_deals.values() 
                if deal['discount'] >= 15 and deal.get('is_popular', True)
            ]
        
        # Sort by discount percentage (highest first)
        popular_deals.sort(key=lambda x: x['discount'], reverse=True)
        
        logger.info(f"Found {len(popular_deals)} popular {genre} deals for {age_preference} games")
        
        # If we didn't find enough deals and this is "Any" genre, fallback to general method
        if len(popular_deals) < 3 and genre == "Any":
            logger.info(f"Too few deals found ({len(popular_deals)}), falling back to general method")
            try:
                fallback_deals = await get_todays_top_deals()
                if fallback_deals:
                    # Filter fallback deals by age preference if specified
                    if age_preference != "any":
                        filtered_deals = []
                        for deal in fallback_deals:
                            if is_in_age_range(deal['app_id'], age_ranges[age_preference]):
                                filtered_deals.append(deal)
                        return filtered_deals[:15] if filtered_deals else fallback_deals[:15]
                    return fallback_deals[:15]
            except Exception as fallback_error:
                logger.error(f"Fallback method also failed: {fallback_error}")
        
        return popular_deals[:15]  # Return top 15 deals
        
    except Exception as e:
        logger.error(f"Error getting customized deals: {e}")
        # Try fallback for "Any" genre even on error
        if genre == "Any":
            try:
                logger.info("Error occurred, trying fallback method for Any genre")
                fallback_deals = await get_todays_top_deals()
                return fallback_deals[:15] if fallback_deals else []
            except:
                pass
        return []

async def load_deals_cache() -> dict:
    """Load deals from cache file."""
    try:
        if os.path.exists(deals_cache["cache_file"]):
            with open(deals_cache["cache_file"], 'r') as f:
                cache_data = json.load(f)
                logger.info(f"Loaded {len(cache_data.get('deals', []))} deals from cache")
                return cache_data
    except Exception as e:
        logger.error(f"Error loading cache: {e}")
    
    return {"last_updated": None, "deals": []}

async def save_deals_cache(deals: list):
    """Save deals to cache file."""
    try:
        cache_data = {
            "last_updated": datetime.now().isoformat(),
            "deals": deals
        }
        
        with open(deals_cache["cache_file"], 'w') as f:
            json.dump(cache_data, f, indent=2)
        
        # Update global cache
        deals_cache["last_updated"] = cache_data["last_updated"]
        deals_cache["deals"] = deals
        
        logger.info(f"Saved {len(deals)} deals to cache")
        
    except Exception as e:
        logger.error(f"Error saving cache: {e}")

async def fetch_and_cache_deals():
    """
    Fetch curated deals from Steam and cache them.
    
    NEW SIMPLIFIED SYSTEM:
    - Checks 45+ popular games that frequently have deals
    - Creates curated mix of exactly 10 deals:
      * 3 popular games with deals
      * 2 old games with deals (App ID < 500k)
      * 2 huge discounts (50%+ off)
      * 3 highest discounts overall
    - Caches for 6 hours for instant responses
    - No more genre/age filtering complexity
    """
    logger.info("🔍 Fetching curated Steam deals...")
    
    # Curated list of popular games that frequently have good deals
    popular_games = [
        271590,   # GTA V
        292030,   # The Witcher 3
        377160,   # Fallout 4
        1174180,  # Red Dead Redemption 2
        489830,   # The Elder Scrolls V: Skyrim Special Edition
        1091500,  # Cyberpunk 2077
        1245620,  # ELDEN RING
        1086940,  # Baldur's Gate 3
        413150,   # Stardew Valley
        594650,   # Hunt: Showdown
        252490,   # Rust
        322330,   # Don't Starve Together
        394360,   # Hearts of Iron IV
        236850,   # Europa Universalis IV
        730,      # Counter-Strike 2
        570,      # Dota 2
        578080,   # PUBG: BATTLEGROUNDS
        813780,   # Age of Empires II: Definitive Edition
        431960,   # Wallpaper Engine
        524220,   # NieR:Automata
        359550,   # Tom Clancy's Rainbow Six Siege
        646570,   # Slay the Spire
        1151640,  # Horizon Zero Dawn
        435150,   # Divinity: Original Sin 2
        261550,   # Mount & Blade II: Bannerlord
        1938090,  # Call of Duty: Modern Warfare III
        1517290,  # Battlefield 2042
        975370,   # Deep Rock Galactic
        1145360,  # Hades
        892970,   # Valheim
        381210,   # Dead by Daylight
        582010,   # Monster Hunter: World
        1794680,  # Vampire Survivors
        1237970,  # Titanfall 2
        418370,   # Ori and the Blind Forest
        1172620,  # Sea of Thieves
        1466860,  # It Takes Two
        444090,   # Payday 2
        582010,   # Monster Hunter: World
        1238840,  # Crusader Kings III
        1273350,  # A Plague Tale: Innocence
        1174180,  # Red Dead Redemption 2
        1113560,  # NieR Replicant
        1599340,  # Inscryption
        1938090,  # Call of Duty: Modern Warfare III
    ]
    
    all_deals = []
    
    # Check each popular game for deals
    logger.info(f"Checking {len(popular_games)} popular games for deals...")
    for app_id in popular_games:
        try:
            deal = await check_app_for_deal(app_id)
            if deal:
                all_deals.append(deal)
        except Exception as e:
            logger.debug(f"Error checking app {app_id}: {e}")
            continue
    
    # Also check Steam featured deals
    try:
        featured_deals = await search_steam_featured_deals()
        all_deals.extend(featured_deals)
    except Exception as e:
        logger.debug(f"Error fetching featured deals: {e}")
    
    # Remove duplicates
    unique_deals = {}
    for deal in all_deals:
        app_id = deal['app_id']
        if app_id not in unique_deals or deal['discount'] > unique_deals[app_id]['discount']:
            unique_deals[app_id] = deal
    
    # Get all deals
    all_unique_deals = list(unique_deals.values())
    
    # Create curated mix of 10 deals
    final_deals = []
    
    # Sort by different criteria for variety
    by_discount = sorted(all_unique_deals, key=lambda x: x['discount'], reverse=True)
    by_popularity = [deal for deal in all_unique_deals if deal['app_id'] in popular_games[:20]]
    old_games = [deal for deal in all_unique_deals if deal['app_id'] < 500000]  # Older games
    huge_discounts = [deal for deal in all_unique_deals if deal['discount'] >= 50]
    
    # Mix: 3 popular, 2 old, 2 huge discounts, 3 highest discounts overall
    added_app_ids = set()
    
    # 3 popular games with deals
    for deal in by_popularity[:3]:
        if deal['app_id'] not in added_app_ids:
            final_deals.append(deal)
            added_app_ids.add(deal['app_id'])
    
    # 2 old games with deals
    for deal in old_games[:2]:
        if deal['app_id'] not in added_app_ids and len(final_deals) < 10:
            final_deals.append(deal)
            added_app_ids.add(deal['app_id'])
    
    # 2 huge discounts (50%+)
    for deal in huge_discounts[:2]:
        if deal['app_id'] not in added_app_ids and len(final_deals) < 10:
            final_deals.append(deal)
            added_app_ids.add(deal['app_id'])
    
    # Fill remaining slots with highest discounts
    for deal in by_discount:
        if deal['app_id'] not in added_app_ids and len(final_deals) < 10:
            final_deals.append(deal)
            added_app_ids.add(deal['app_id'])
    
    logger.info(f"✅ Curated {len(final_deals)} deals for cache")
    
    # Save to cache
    await save_deals_cache(final_deals)
    
    # Also cache popular games for instant price lookup
    await cache_popular_games(popular_games)
    
    return final_deals

async def cache_popular_games(popular_games: list):
    """Cache popular games data for instant responses."""
    try:
        games_data = []
        logger.info(f"Caching data for {len(popular_games)} popular games...")
        
        for app_id in popular_games:
            try:
                game_data = await get_steam_price(app_id)
                if game_data and game_data.get('name'):
                    price_overview = game_data.get('price_overview', {})
                    games_data.append({
                        'app_id': app_id,
                        'name': game_data['name'],
                        'current_price': price_overview.get('final', 0) / 100.0 if price_overview else 0,
                        'original_price': price_overview.get('initial', 0) / 100.0 if price_overview else 0,
                        'discount': price_overview.get('discount_percent', 0) if price_overview else 0,
                        'currency': 'INR'
                    })
            except Exception as e:
                logger.debug(f"Error caching game {app_id}: {e}")
                continue
        
        # Save to cache file
        with open(popular_games_cache["cache_file"], 'w') as f:
            json.dump(games_data, f, indent=2)
        
        popular_games_cache["games"] = games_data
        logger.info(f"Cached {len(games_data)} popular games")
        
    except Exception as e:
        logger.error(f"Error caching popular games: {e}")

async def get_cached_deals() -> list:
    """Get the curated deals from cache."""
    # Load cache if not already loaded
    if not deals_cache["deals"]:
        cache_data = await load_deals_cache()
        deals_cache.update(cache_data)
    
    deals = deals_cache["deals"]
    
    if not deals:
        logger.warning("No deals in cache, trying emergency fallback")
        return await get_emergency_deals()
    
    logger.info(f"Returning {len(deals)} curated deals from cache")
    return deals

async def get_emergency_deals() -> list:
    """Emergency fast deals - hardcoded popular games to check quickly."""
    emergency_app_ids = [
        271590,  # GTA V
        1086940, # Baldur's Gate 3
        1174180, # Red Dead Redemption 2
        292030,  # The Witcher 3
        570,     # Dota 2
        730,     # Counter-Strike 2
        440,     # Team Fortress 2
        1938090, # Call of Duty: Modern Warfare III
        524220,  # NieR:Automata
        1245620, # ELDEN RING
        377160,  # Fallout 4
        413150,  # Stardew Valley
        431960,  # Wallpaper Engine
        252490,  # Rust
        578080,  # PUBG: BATTLEGROUNDS
    ]
    
    logger.info(f"Emergency deals: Checking {len(emergency_app_ids)} popular games...")
    deals = []
    
    for app_id in emergency_app_ids:
        try:
            logger.debug(f"Checking emergency app {app_id}")
            deal = await check_app_for_deal(app_id)
            if deal:
                logger.info(f"Emergency deal found: {deal['name']} - {deal['discount']}% off")
                deals.append(deal)
            # Skip games with no discount for now - we want real deals only
        except Exception as e:
            logger.debug(f"Error checking emergency app {app_id}: {e}")
            continue
    
    logger.info(f"Emergency deals found: {len(deals)} games")
    return deals[:8]  # Return up to 8 emergency deals

async def get_todays_top_deals() -> list:
    """Get top Steam deals with highest discounts by searching dynamically (optimized for speed)."""
    try:
        logger.info("Searching for top Steam deals dynamically...")
        deals = []
        import asyncio
        
        # Method 1: Search Steam Store Featured deals (with timeout)
        try:
            featured_deals = await asyncio.wait_for(search_steam_featured_deals(), timeout=8.0)
            deals.extend(featured_deals)
        except (Exception, asyncio.TimeoutError) as e:
            logger.debug(f"Featured deals timed out or failed: {e}")
        
        # Method 2: Search Steam's special offers page (with timeout)
        try:
            special_deals = await asyncio.wait_for(search_steam_specials(), timeout=8.0)
            deals.extend(special_deals)
        except (Exception, asyncio.TimeoutError) as e:
            logger.debug(f"Special deals timed out or failed: {e}")
        
        # Early return if we have enough deals
        if len(deals) >= 8:
            # Remove duplicates quickly
            unique_deals = {}
            for deal in deals:
                app_id = deal['app_id']
                if app_id not in unique_deals or deal['discount'] > unique_deals[app_id]['discount']:
                    unique_deals[app_id] = deal
            
            # Filter for minimum discount (reduced threshold)
            filtered_deals = [deal for deal in unique_deals.values() if deal['discount'] >= 10]
            
            # Sort by discount percentage (highest first)
            filtered_deals.sort(key=lambda x: x['discount'], reverse=True)
            
            logger.info(f"Found {len(filtered_deals)} deals with 20%+ discounts (fast path)")
            return filtered_deals[:10]
        
        # If we need more deals, try one more quick category search
        try:
            action_deals = await asyncio.wait_for(search_category_deals("Action"), timeout=5.0)
            deals.extend(action_deals)
        except (Exception, asyncio.TimeoutError) as e:
            logger.debug(f"Action deals timed out or failed: {e}")
        
        # Remove duplicates based on app_id
        unique_deals = {}
        for deal in deals:
            app_id = deal['app_id']
            if app_id not in unique_deals or deal['discount'] > unique_deals[app_id]['discount']:
                unique_deals[app_id] = deal
        
        # Convert back to list and filter for minimum discount
        filtered_deals = [deal for deal in unique_deals.values() if deal['discount'] >= 10]
        
        # Sort by discount percentage (highest first)
        filtered_deals.sort(key=lambda x: x['discount'], reverse=True)
        
        # Return top deals
        logger.info(f"Found {len(filtered_deals)} deals with 15%+ discounts")
        return filtered_deals[:10]
        
    except Exception as e:
        logger.error(f"Error getting dynamic top deals: {e}")
        return []

async def search_steam_featured_deals() -> list:
    """Search Steam's featured deals section."""
    deals = []
    try:
        # Use Steam Store API to get featured items
        async with aiohttp.ClientSession() as session:
            # Steam's featured page often has deals
            featured_url = "https://store.steampowered.com/api/featured/"
            logger.info("Searching Steam featured deals...")
            
            async with session.get(featured_url) as response:
                if response.status == 200:
                    data = await response.json()
                    logger.info(f"Featured API response keys: {list(data.keys()) if data else 'None'}")
                    
                    # Check featured items for deals
                    if 'large_capsules' in data:
                        logger.info(f"Found {len(data['large_capsules'])} large capsules")
                        for item in data['large_capsules'][:15]:  # Check more items
                            app_id = item.get('id')
                            if app_id:
                                deal = await check_app_for_deal(app_id)
                                if deal:
                                    logger.info(f"Featured deal found: {deal['name']} - {deal['discount']}% off")
                                    deals.append(deal)
                    
                    # Check specials
                    if 'specials' in data:
                        logger.info(f"Found {len(data['specials'])} specials")
                        for item in data['specials'][:15]:
                            app_id = item.get('id')
                            if app_id:
                                deal = await check_app_for_deal(app_id)
                                if deal:
                                    logger.info(f"Special deal found: {deal['name']} - {deal['discount']}% off")
                                    deals.append(deal)
                                    
                    # Check featured categories if available
                    if 'featured_win' in data:
                        featured_win = data['featured_win']
                        for item in featured_win[:10]:
                            app_id = item.get('id')
                            if app_id:
                                deal = await check_app_for_deal(app_id)
                                if deal:
                                    deals.append(deal)
                else:
                    logger.warning(f"Featured deals API returned {response.status}")
                                    
    except Exception as e:
        logger.error(f"Error searching featured deals: {e}")
    
    logger.info(f"Featured deals found: {len(deals)}")
    return deals

async def search_category_deals(category: str) -> list:
    """Search for deals in a specific category."""
    deals = []
    try:
        # Search for games in category and check for deals
        search_results = await find_steam_game(category)
        
        # Check first 15 results for deals to avoid too many API calls
        for game in search_results[:15]:
            app_id = game.get('appid')
            if app_id:
                deal = await check_app_for_deal(app_id)
                if deal:
                    deals.append(deal)
                    
    except Exception as e:
        logger.debug(f"Error searching category {category}: {e}")
    
    return deals

async def search_steam_specials() -> list:
    """Search Steam's special offers using better targeting."""
    deals = []
    try:
        logger.info("Searching Steam specials...")
        import random
        
        # Use popular games that frequently go on sale
        popular_sale_games = [
            # Popular AAA games that often have sales
            271590,   # GTA V
            292030,   # The Witcher 3
            377160,   # Fallout 4
            1174180,  # Red Dead Redemption 2
            489830,   # The Elder Scrolls V: Skyrim Special Edition
            1091500,  # Cyberpunk 2077
            1245620,  # ELDEN RING
            1086940,  # Baldur's Gate 3
            
            # Popular indie games that go on sale
            413150,   # Stardew Valley
            594650,   # Hunt: Showdown
            252490,   # Rust
            322330,   # Don't Starve Together
            394360,   # Hearts of Iron IV
            236850,   # Europa Universalis IV
            
            # Popular multiplayer games
            730,      # Counter-Strike 2
            570,      # Dota 2
            578080,   # PUBG: BATTLEGROUNDS
            813780,   # Age of Empires II: Definitive Edition
        ]
        
        # Check these known popular games first
        for app_id in popular_sale_games[:20]:  # Limit to avoid timeout
            try:
                deal = await check_app_for_deal(app_id)
                if deal:
                    logger.info(f"Popular game deal: {deal['name']} - {deal['discount']}% off")
                    deals.append(deal)
            except:
                continue
        
        # Also check some random ranges, but smarter ones
        if len(deals) < 5:  # Only if we need more deals
            random_ranges = [
                range(300000, 400000),    # 2016-2017 games
                range(800000, 900000),    # 2019-2020 games  
                range(1200000, 1300000),  # 2021-2022 games
            ]
            
            sample_app_ids = []
            for r in random_ranges:
                sample_app_ids.extend(random.sample(list(r), 8))  # 8 from each range
            
            for app_id in sample_app_ids:
                try:
                    deal = await check_app_for_deal(app_id)
                    if deal:
                        deals.append(deal)
                        if len(deals) >= 15:  # Stop when we have enough
                            break
                except:
                    continue
                
    except Exception as e:
        logger.error(f"Error searching specials: {e}")
    
    logger.info(f"Specials found: {len(deals)}")
    return deals

async def check_app_for_deal(app_id: int) -> dict | None:
    """Check if a specific app has a good deal."""
    try:
        game_data = await get_steam_price(app_id)
        if game_data:
            price_overview = game_data.get('price_overview')
            name = game_data.get('name', f'Game {app_id}')
            
            if price_overview:
                discount = price_overview.get('discount_percent', 0)
                
                # Only return if discount is significant (10% or more)
                if discount >= 10:
                    current_price = price_overview.get('final', 0) / 100.0
                    original_price = price_overview.get('initial', 0) / 100.0
                    
                    return {
                        'name': name,
                        'app_id': app_id,
                        'discount': discount,
                        'current_price': current_price,
                        'original_price': original_price,
                        'currency': 'INR'
                    }
    except Exception as e:
        logger.debug(f"Error checking app {app_id}: {e}")
    
    return None

async def check_popular_app_for_deal(app_id: int) -> dict | None:
    """Check if a specific app has a good deal and is popular."""
    try:
        game_data = await get_steam_price(app_id)
        if game_data:
            price_overview = game_data.get('price_overview')
            name = game_data.get('name', f'Game {app_id}')
            
            # Check for popularity indicators
            is_popular = await is_game_popular(game_data, app_id)
            
            if price_overview and is_popular:
                discount = price_overview.get('discount_percent', 0)
                
                # Only return if discount is significant (20% or more for popular games)
                if discount >= 20:
                    current_price = price_overview.get('final', 0) / 100.0
                    original_price = price_overview.get('initial', 0) / 100.0
                    
                    return {
                        'name': name,
                        'app_id': app_id,
                        'discount': discount,
                        'current_price': current_price,
                        'original_price': original_price,
                        'currency': 'INR',
                        'is_popular': True
                    }
    except Exception as e:
        logger.debug(f"Error checking popular app {app_id}: {e}")
    
    return None

def is_in_age_range(app_id: int, age_ranges: list) -> bool:
    """Check if app_id falls within the specified age ranges."""
    for age_range in age_ranges:
        if app_id in age_range:
            return True
    return False

def quick_genre_check(app_id: int, genre: str) -> bool:
    """Quick genre check based on App ID patterns (heuristic, fast)."""
    # This is a fast heuristic check to avoid API calls
    # Based on common App ID patterns for different genres
    if genre == "Any":
        return True
    
    # Simple heuristic based on app_id ranges where certain genres are more common
    genre_patterns = {
        "Action": [range(200000, 800000), range(1000000, 1500000)],
        "RPG": [range(50000, 400000), range(800000, 1200000)],
        "Strategy": [range(10000, 300000), range(600000, 1000000)],
        "Indie": [range(300000, 1200000), range(1500000, 2000000)],
        "Adventure": [range(100000, 600000), range(1200000, 1800000)],
        "Simulation": [range(50000, 500000), range(800000, 1400000)],
        "Racing": [range(10000, 200000), range(400000, 800000)],
        "Sports": [range(10000, 300000), range(600000, 1000000)],
    }
    
    if genre in genre_patterns:
        for pattern_range in genre_patterns[genre]:
            if app_id in pattern_range:
                return True
    
    # Default to True for other genres to avoid filtering too aggressively
    return True

async def game_matches_genre(app_id: int, genre: str) -> bool:
    """Check if game matches the specified genre based on its details."""
    try:
        game_data = await get_steam_price(app_id)
        if game_data:
            # Check game name and description for genre keywords
            name = game_data.get('name', '').lower()
            short_description = game_data.get('short_description', '').lower()
            
            genre_keywords = {
                "Action": ["action", "shooter", "combat", "fighting", "fps", "beat", "battle"],
                "Adventure": ["adventure", "story", "narrative", "quest", "journey"],
                "RPG": ["rpg", "role", "fantasy", "magic", "character", "level", "dungeon"],
                "Strategy": ["strategy", "tactical", "rts", "civilization", "empire", "war"],
                "Simulation": ["simulation", "simulator", "farming", "city", "building", "management"],
                "Racing": ["racing", "driving", "car", "speed", "formula", "rally"],
                "Sports": ["sports", "football", "soccer", "basketball", "baseball", "tennis"],
                "Indie": ["indie", "independent", "pixel", "retro", "artistic"],
                "Multiplayer": ["multiplayer", "online", "coop", "mmo", "pvp", "co-op"],
                "Puzzle": ["puzzle", "logic", "brain", "match", "solve", "thinking"],
                "Horror": ["horror", "survival", "zombie", "scary", "fear", "dark"],
                "Fighting": ["fighting", "martial", "combat", "fighter", "tekken", "street"]
            }
            
            if genre in genre_keywords:
                keywords = genre_keywords[genre]
                text_to_check = f"{name} {short_description}"
                return any(keyword in text_to_check for keyword in keywords)
    except:
        pass
    
    # Default to True if we can't determine (to avoid filtering too aggressively)
    return True

async def is_game_popular(game_data: dict, app_id: int) -> bool:
    """Determine if a game is popular based on various indicators."""
    try:
        # Check for popularity indicators in the game data
        name = game_data.get('name', '')
        
        # Skip games with very generic or placeholder names
        skip_keywords = ['test', 'demo', 'beta', 'alpha', 'sample', 'placeholder', 'sdk', 'tool']
        if any(keyword in name.lower() for keyword in skip_keywords):
            return False
        
        # Check if game has proper description (indicates it's a real game)
        short_description = game_data.get('short_description', '')
        if len(short_description) < 30:  # Reduced from 50 to 30
            return False
        
        # Check if game has price (free games can be popular too, but prefer paid games with deals)
        price_overview = game_data.get('price_overview')
        if price_overview:
            original_price = price_overview.get('initial', 0)
            if original_price < 50:  # Reduced from ₹1 to ₹0.50 original price
                return False
        
        # Check metacritic score if available (more lenient)
        metacritic = game_data.get('metacritic')
        if metacritic and metacritic.get('score', 0) < 50:  # Reduced from 60 to 50
            return False
        
        # If we reach here, assume it's popular enough
        return True
        
    except Exception as e:
        logger.debug(f"Error checking popularity for {app_id}: {e}")
        return True  # Default to popular if we can't determine

async def send_deals_email(email: str, deals: list, is_immediate: bool = False) -> bool:
    """Send deals email using Resend API."""
    try:
        import aiohttp
        
        if not deals:
            return False
        
        # Create email content
        if is_immediate:
            max_discount = max(deal['discount'] for deal in deals) if deals else 0
            subject = f"🔥 TOP STEAM DEALS TODAY - Up to {max_discount}% OFF!"
            greeting = "Here are today's hottest curated Steam deals with the biggest discounts!"
        else:
            subject = "🎮 Daily Steam Deals - Your Gaming Bargains"
            greeting = "Here are today's best Steam deals!"
        
        # Build HTML email content
        html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
            <h2 style="color: #1b2838;">🎮 {subject.split(' - ')[0]}</h2>
            <p style="color: #666;">{greeting}</p>
            
            <div style="background: #f5f5f5; padding: 20px; border-radius: 8px;">
        """
        
        for i, deal in enumerate(deals, 1):
            savings = deal['original_price'] - deal['current_price']
            steam_url = f"https://store.steampowered.com/app/{deal['app_id']}"
            html_content += f"""
                <div style="background: white; margin: 10px 0; padding: 15px; border-radius: 5px; border-left: 4px solid #4CAF50;">
                    <h3 style="margin: 0 0 10px 0; color: #1b2838;">
                        <a href="{steam_url}" style="color: #1b2838; text-decoration: none;">{i}. {deal['name']}</a>
                    </h3>
                    <p style="margin: 5px 0; font-size: 18px;">
                        <span style="color: #4CAF50; font-weight: bold;">₹{deal['current_price']:.2f}</span>
                        <span style="text-decoration: line-through; color: #666; margin-left: 10px;">₹{deal['original_price']:.2f}</span>
                        <span style="background: #ff6b35; color: white; padding: 2px 8px; border-radius: 3px; margin-left: 10px; font-size: 14px;">-{deal['discount']}%</span>
                    </p>
                    <p style="margin: 5px 0; color: #4CAF50;">💰 You save: ₹{savings:.2f}</p>
                    <p style="margin: 5px 0;">
                        <a href="{steam_url}" style="background: #1b2838; color: white; padding: 8px 16px; text-decoration: none; border-radius: 4px; display: inline-block; font-size: 14px;">
                            🛒 View on Steam
                        </a>
                    </p>
                    <p style="margin: 5px 0; font-size: 12px; color: #666;">App ID: {deal['app_id']}</p>
                </div>
            """
        
        html_content += """
            </div>
            <p style="color: #666; margin-top: 20px;">
                🎯 Want price alerts for specific games? Use our Steam Price Tracker!<br>
                📧 This email was sent by Steam Price Tracker MCP
            </p>
        </body>
        </html>
        """
        
        # Debug environment variables
        logger.info(f"RESEND_API_KEY configured: {'Yes' if RESEND_API_KEY and RESEND_API_KEY != 'your_resend_api_key_here' else 'No'}")
        logger.info(f"SENDER_EMAIL configured: {SENDER_EMAIL}")
        logger.info(f"Number of deals to send: {len(deals)}")
        
        # Check if environment variables are properly configured
        if not RESEND_API_KEY or RESEND_API_KEY == "your_resend_api_key_here":
            logger.error("❌ RESEND_API_KEY not configured properly!")
            return False
            
        if not SENDER_EMAIL or SENDER_EMAIL == "alerts@steamtracker.com":
            logger.error("❌ SENDER_EMAIL not configured properly!")
            return False
        
        # Send email using Resend
        email_payload = {
            "from": SENDER_EMAIL,
            "to": [email],
            "subject": subject,
            "html": html_content
        }
        
        logger.info(f"Sending email to {email} with subject: {subject}")
        
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type": "application/json"
                },
                json=email_payload
            ) as response:
                response_text = await response.text()
                logger.info(f"Resend API response: {response.status} - {response_text}")
                
                if response.status == 200:
                    logger.info(f"✅ Deals email sent successfully to {email}")
                    return True
                else:
                    logger.error(f"❌ Failed to send email: {response.status} - {response_text}")
                    return False
                    
    except Exception as e:
        logger.error(f"Error sending deals email: {e}")
        return False

async def initialize_services():
    """Initialize all services on startup."""
    logger.info("Initializing services...")
    
    # Initialize deals cache first (most important for performance)
    try:
        logger.info("🔍 Initializing deals cache...")
        
        # Load existing cache
        cache_data = await load_deals_cache()
        deals_cache.update(cache_data)
        
        # Check if cache is recent (less than 6 hours old)
        cache_age_limit = 6 * 60 * 60  # 6 hours in seconds
        cache_is_fresh = False
        
        if deals_cache["last_updated"]:
            try:
                last_updated = datetime.fromisoformat(deals_cache["last_updated"])
                age = (datetime.now() - last_updated).total_seconds()
                cache_is_fresh = age < cache_age_limit
                logger.info(f"Cache age: {age/3600:.1f} hours")
            except:
                pass
        
        if not cache_is_fresh or not deals_cache["deals"]:
            logger.info("Cache is stale or empty, fetching fresh deals...")
            # Fetch deals in background to avoid blocking startup
            asyncio.create_task(fetch_and_cache_deals())
        else:
            logger.info(f"✅ Using cached deals: {len(deals_cache['deals'])} deals available")
            
    except Exception as e:
        logger.error(f"Cache initialization failed: {e}")
        # Start background fetch as fallback
        asyncio.create_task(fetch_and_cache_deals())
    
    # Try to initialize database, but don't fail if it doesn't work
    try:
        logger.info("Attempting database connection...")
        await db_manager.initialize()
        logger.info("✅ Database initialized successfully")
        
        # Start background scheduler only if database works
        try:
            scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
            scheduler_thread.start()
            logger.info("✅ Background price checker started")
        except Exception as e:
            logger.warning(f"Background scheduler failed: {e}")
        
    except Exception as e:
        logger.warning(f"⚠️  Database initialization failed: {e}")
        logger.info("📱 Continuing in SEARCH-ONLY mode (price alerts disabled)")
        # Set pool to None so tools can check
        db_manager.pool = None
    
    logger.info("✅ Services initialization completed")

async def main():
    """Initialize and run the Steam tracker MCP server."""
    try:
        await initialize_services()
        
        # Use Render's PORT environment variable or default to 8091
        port = int(os.environ.get("PORT", 8091))
        host = "0.0.0.0"
        
        logger.info(f"🎮 Starting Steam Price Tracker MCP Server on http://{host}:{port}")
        logger.info("🔗 Connect with: Bearer Token Authentication")
        logger.info("📧 Server ready for Steam game price tracking!")
        
        await mcp.run_async("streamable-http", host=host, port=port)
        
    except KeyboardInterrupt:
        logger.info("🛑 Server shutdown requested")
    except Exception as e:
        logger.error(f"💥 Server error: {e}")
        logger.info("🔄 Server will attempt to continue...")
        # Try to continue running even with errors
        try:
            port = int(os.environ.get("PORT", 8091))
            await mcp.run_async("streamable-http", host="0.0.0.0", port=port)
        except Exception as e2:
            logger.error(f"💀 Fatal error: {e2}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Server stopped by user")
    except Exception as e:
        print(f"💥 Fatal error: {e}")
        print("🔄 Try restarting the server") 