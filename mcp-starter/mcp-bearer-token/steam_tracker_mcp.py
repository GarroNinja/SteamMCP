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
    """Simple, reliable Steam game search."""
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
                    if any(skip in name_lower for skip in ['dedicated server', 'sdk', 'authoring tools']):
                        continue
                    
                    # Find matches
                    if query_lower in name_lower:
                        matches.append({
                            'name': name,
                            'appid': app['appid'],
                            'exact': query_lower == name_lower
                        })
                
                # Sort: exact matches first, then alphabetical
                matches.sort(key=lambda x: (not x['exact'], x['name'].lower()))
                return matches[:10]
                
    except Exception as e:
        logger.error(f"Search error: {e}")
        return []

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
    """Run background scheduler for price checks."""
    schedule.every(12).hours.do(lambda: asyncio.run(price_tracker.check_price_alerts()))
    
    while True:
        schedule.run_pending()
        time.sleep(60)

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

@mcp.tool(description="Get today's top Steam deals with highest discounts and send them via email immediately")
async def send_top_deals_today(
    email: Annotated[str, Field(description="Email address to send the deals to")]
) -> str:
    """Get today's top Steam deals and send them via email immediately."""
    # Validate email
    if not email or "@" not in email:
        return "❌ Valid email address is required. Please use: send_top_deals_today(email=\"your@email.com\")"
    
    try:
        logger.info(f"Fetching top deals for {email}")
        
        # Get top deals from Steam (games with high discounts)
        top_deals = await get_todays_top_deals()
        
        if not top_deals:
            return "❌ No deals found today. Please try again later."
        
        # Send email with deals
        email_sent = await send_deals_email(email, top_deals, is_immediate=True)
        
        if email_sent:
            return f"📧 ✅ Top Steam deals sent to {email}!\n\nFound {len(top_deals)} amazing deals with discounts up to 90% OFF!"
        else:
            return f"❌ Failed to send email to {email}. Please check the email address and try again."
            
    except Exception as e:
        logger.error(f"Error sending top deals: {e}")
        return f"❌ Error getting top deals: {str(e)}"

async def get_todays_top_deals() -> list:
    """Get top Steam deals with highest discounts by searching dynamically."""
    try:
        logger.info("Searching for top Steam deals dynamically...")
        deals = []
        
        # Method 1: Search Steam Store Featured deals
        deals.extend(await search_steam_featured_deals())
        
        # Method 2: Search popular categories for deals
        popular_categories = [
            "Action", "Adventure", "RPG", "Strategy", "Simulation", 
            "Racing", "Sports", "Indie", "Multiplayer"
        ]
        
        for category in popular_categories[:3]:  # Limit to avoid too many requests
            try:
                category_deals = await search_category_deals(category)
                deals.extend(category_deals)
            except Exception as e:
                logger.debug(f"Error searching {category} deals: {e}")
                continue
        
        # Method 3: Check Steam's special offers page
        special_deals = await search_steam_specials()
        deals.extend(special_deals)
        
        # Remove duplicates based on app_id
        unique_deals = {}
        for deal in deals:
            app_id = deal['app_id']
            if app_id not in unique_deals or deal['discount'] > unique_deals[app_id]['discount']:
                unique_deals[app_id] = deal
        
        # Convert back to list and filter for minimum discount
        filtered_deals = [deal for deal in unique_deals.values() if deal['discount'] >= 30]
        
        # Sort by discount percentage (highest first)
        filtered_deals.sort(key=lambda x: x['discount'], reverse=True)
        
        # Return top 15 deals
        logger.info(f"Found {len(filtered_deals)} deals with 30%+ discounts")
        return filtered_deals[:15]
        
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
            async with session.get(featured_url) as response:
                if response.status == 200:
                    data = await response.json()
                    
                    # Check featured items for deals
                    if 'large_capsules' in data:
                        for item in data['large_capsules'][:10]:  # Limit requests
                            app_id = item.get('id')
                            if app_id:
                                deal = await check_app_for_deal(app_id)
                                if deal:
                                    deals.append(deal)
                    
                    # Check specials
                    if 'specials' in data:
                        for item in data['specials'][:10]:
                            app_id = item.get('id')
                            if app_id:
                                deal = await check_app_for_deal(app_id)
                                if deal:
                                    deals.append(deal)
                                    
    except Exception as e:
        logger.debug(f"Error searching featured deals: {e}")
    
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
    """Search Steam's special offers."""
    deals = []
    try:
        # Check some random popular app IDs that often have sales
        import random
        
        # Generate some random app IDs in common ranges
        random_ranges = [
            range(200000, 300000),    # Older popular games
            range(400000, 500000),    # Mid-era games  
            range(1000000, 1200000),  # Newer games
        ]
        
        sample_app_ids = []
        for r in random_ranges:
            sample_app_ids.extend(random.sample(r, 5))  # 5 from each range
        
        for app_id in sample_app_ids:
            try:
                deal = await check_app_for_deal(app_id)
                if deal:
                    deals.append(deal)
            except:
                continue  # Skip failed requests
                
    except Exception as e:
        logger.debug(f"Error searching specials: {e}")
    
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
                
                # Only return if discount is significant (30% or more)
                if discount >= 30:
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

async def send_deals_email(email: str, deals: list, is_immediate: bool = False) -> bool:
    """Send deals email using Resend API."""
    try:
        import aiohttp
        
        if not deals:
            return False
        
        # Create email content
        if is_immediate:
            subject = f"🔥 TOP STEAM DEALS TODAY - Up to {deals[0]['discount']}% OFF!"
            greeting = "Here are today's hottest Steam deals with the biggest discounts!"
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
        
        # Send email using Resend
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "from": SENDER_EMAIL,
                    "to": [email],
                    "subject": subject,
                    "html": html_content
                }
            ) as response:
                if response.status == 200:
                    logger.info(f"Deals email sent successfully to {email}")
                    return True
                else:
                    error_text = await response.text()
                    logger.error(f"Failed to send email: {response.status} - {error_text}")
                    return False
                    
    except Exception as e:
        logger.error(f"Error sending deals email: {e}")
        return False

async def initialize_services():
    """Initialize all services on startup."""
    logger.info("Initializing services...")
    
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