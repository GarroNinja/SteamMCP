"""
Combined Game Tracker MCP Server
- Merges your existing Steam MCP tools with the new Epic Games Store tools
- Single process, single MCP server, shared database pool, unified helpers

Drop this file next to your existing project and run it instead of the separate servers.
It expects your existing Steam code to be importable so we can wrap it cleanly.

Assumptions (adjust imports/names if your paths differ):
- Steam-side async tool functions live in `steam_tracker_mcp` (or similar) and are
  importable as async callables:
    * search_steam_games(query: str) -> str
    * get_game_details(app_id: int) -> str
    * setup_price_alert_by_appid(app_id: int, email: str, target_price: float) -> str
    * list_user_alerts(email: str) -> str
    * remove_price_alert(email: str, app_id: int) -> str
    * subscribe_daily_deals(email: str) -> str
    * send_top_deals_today(email: str) -> str

If your actual function names/modules differ, update the `SteamAdapter` below.

Epic-side functionality is taken from your "Enhanced MCP Server with Epic Games Store Integration" snippet.
"""

import asyncio
import logging
import os
import asyncpg
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import List, Dict, Optional

from mcp.server import Server
import mcp.types as types
from working_epic_games_api import WorkingEpicGamesAPI as EpicGamesAPI
logger = logging.getLogger(__name__)

# ---------- Steam adapter ----------------------------------------------------
# This wraps your existing Steam module so we can call its tools from here
# without rewriting their internals. Adjust the import path/names if needed.

class SteamAdapter:
    def __init__(self):
        try:
            # Try the most likely filename/module name. Change if yours differs.
            import steam_tracker_mcp as steam_mod  # noqa: F401
            self.mod = steam_mod
            logger.info("SteamAdapter: steam_tracker_mcp imported successfully")
        except Exception as e:
            logger.warning(f"SteamAdapter: could not import steam module: {e}")
            self.mod = None

    @property
    def available(self) -> bool:
        return self.mod is not None

    async def search(self, query: str) -> str:
        if not self.available:
            return "❌ Steam tools unavailable (module not found)."
        return await self.mod.search_steam_games(query)

    async def details(self, app_id: int) -> str:
        if not self.available:
            return "❌ Steam tools unavailable (module not found)."
        return await self.mod.get_game_details(app_id)

    async def alert_setup(self, app_id: int, email: str, target_price: float) -> str:
        if not self.available:
            return "❌ Steam tools unavailable (module not found)."
        return await self.mod.setup_price_alert_by_appid(app_id=app_id, email=email, target_price=target_price)

    async def alerts_list(self, email: str) -> str:
        if not self.available:
            return "❌ Steam tools unavailable (module not found)."
        return await self.mod.list_user_alerts(email)

    async def alert_remove(self, email: str, app_id: int) -> str:
        if not self.available:
            return "❌ Steam tools unavailable (module not found)."
        return await self.mod.remove_price_alert(email=email, app_id=app_id)

    async def subscribe_daily(self, email: str) -> str:
        if not self.available:
            return "❌ Steam tools unavailable (module not found)."
        return await self.mod.subscribe_daily_deals(email)

    async def deals_today(self, email: str) -> str:
        if not self.available:
            return "❌ Steam tools unavailable (module not found)."
        return await self.mod.send_top_deals_today(email)


# ---------- Combined server --------------------------------------------------

class CombinedGameTrackerMCP:
    def __init__(self) -> None:
        self.server = Server("game-tracker")
        self.db_pool: Optional[asyncpg.Pool] = None
        self.epic_api = EpicGamesAPI()
        self.steam = SteamAdapter()

        # Email configuration for Epic deal emails (uses SMTP)
        self.smtp_host = os.getenv('SMTP_HOST', 'smtp.gmail.com')
        self.smtp_port = int(os.getenv('SMTP_PORT', '587'))
        self.sender_email = os.getenv('SENDER_EMAIL')
        self.sender_password = os.getenv('SENDER_PASSWORD')

        self._register_tools()

    # -------------------- Tool registration ---------------------------------
    def _register_tools(self) -> None:
        # -------------------- STEAM (wrapped) --------------------------------
        @self.server.call_tool()
        async def search_steam_games(query: str) -> List[types.TextContent]:
            """Steam: Search games and show matches with prices and App IDs."""
            text = await self.steam.search(query)
            return [types.TextContent(type="text", text=text)]

        @self.server.call_tool()
        async def get_steam_game_details(app_id: int) -> List[types.TextContent]:
            """Steam: Get detailed price info for a specific App ID."""
            text = await self.steam.details(app_id)
            return [types.TextContent(type="text", text=text)]

        @self.server.call_tool()
        async def setup_steam_price_alert(app_id: int, email: str, target_price: float) -> List[types.TextContent]:
            """Steam: Set a price alert using App ID."""
            text = await self.steam.alert_setup(app_id, email, target_price)
            return [types.TextContent(type="text", text=text)]

        @self.server.call_tool()
        async def list_steam_alerts(email: str) -> List[types.TextContent]:
            text = await self.steam.alerts_list(email)
            return [types.TextContent(type="text", text=text)]

        @self.server.call_tool()
        async def remove_steam_alert(email: str, app_id: int) -> List[types.TextContent]:
            text = await self.steam.alert_remove(email, app_id)
            return [types.TextContent(type="text", text=text)]

        @self.server.call_tool()
        async def subscribe_steam_daily_deals(email: str) -> List[types.TextContent]:
            text = await self.steam.subscribe_daily(email)
            return [types.TextContent(type="text", text=text)]

        @self.server.call_tool()
        async def send_steam_deals_today(email: str) -> List[types.TextContent]:
            text = await self.steam.deals_today(email)
            return [types.TextContent(type="text", text=text)]

        # -------------------- EPIC (native) ----------------------------------
        @self.server.call_tool()
        async def search_epic_games(query: str, limit: int = 10) -> List[types.TextContent]:
            try:
                games = await self.epic_api.search_games(query, limit)
                if not games:
                    return [types.TextContent(type="text", text=f"No Epic Games found for query: {query}")]

                lines = [f"Found {len(games)} Epic Games for '{query}':\n"]
                for i, game in enumerate(games, 1):
                    price = game['price']
                    price_txt = "Free" if price['is_free'] else f"{price['currency']} {price['current']:.2f}"
                    if price['original'] != price['current']:
                        price_txt += f" (was {price['currency']} {price['original']:.2f})"
                    lines.append(
                        f"{i}. **{game['title']}**\n"
                        f"   Developer: {game['developer']}\n"
                        f"   Price: {price_txt}\n"
                        f"   Epic ID: {game['epic_namespace']}/{game['epic_id']}\n"
                        f"   URL: {game['url']}\n"
                    )
                return [types.TextContent(type="text", text="\n".join(lines))]
            except Exception as e:
                logger.exception("search_epic_games failed")
                return [types.TextContent(type="text", text=f"Error searching Epic Games: {e}")]

        @self.server.call_tool()
        async def get_epic_free_games() -> List[types.TextContent]:
            try:
                free_games = await self.epic_api.get_free_games()
                out = ["# Epic Games Store - Free Games\n"]
                current = free_games.get('current', [])
                upcoming = free_games.get('upcoming', [])
                if current:
                    out.append("## Currently Free:\n")
                    for g in current:
                        out.append(
                            f"- **{g['title']}** by {g['developer']}\n"
                            f"  Until: {g['end_date']}\n"
                            f"  {g['url']}\n"
                        )
                else:
                    out.append("## No games currently free\n")
                if upcoming:
                    out.append("\n## Upcoming Free:\n")
                    for g in upcoming:
                        out.append(
                            f"- **{g['title']}** by {g['developer']}\n"
                            f"  From: {g['start_date']}  →  Until: {g['end_date']}\n"
                        )
                else:
                    out.append("\n## No upcoming free games announced\n")
                return [types.TextContent(type="text", text="\n".join(out))]
            except Exception as e:
                logger.exception("get_epic_free_games failed")
                return [types.TextContent(type="text", text=f"Error getting Epic free games: {e}")]

        @self.server.call_tool()
        async def setup_epic_price_alert(epic_namespace: str, epic_offer_id: str, email: str, target_price: float) -> List[types.TextContent]:
            try:
                price = await self.epic_api.get_game_price(epic_namespace, epic_offer_id)
                if not price:
                    return [types.TextContent(type="text", text=f"Game not found: {epic_namespace}/{epic_offer_id}")]
                await self._register_epic_user(email)
                async with self.db_pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO epic_price_alerts 
                        (user_id, epic_namespace, epic_offer_id, game_title, target_price, current_price, currency)
                        VALUES ((SELECT id FROM epic_games_users WHERE email = $1), $2, $3, $4, $5, $6, $7)
                        ON CONFLICT (user_id, epic_namespace, epic_offer_id) DO UPDATE SET 
                            target_price = EXCLUDED.target_price,
                            current_price = EXCLUDED.current_price,
                            is_active = TRUE,
                            alert_sent = FALSE,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        email, epic_namespace, epic_offer_id, price['title'], target_price, price['current_price'], price['currency']
                    )
                cur = price['current_price']; curcy = price['currency']
                if cur <= target_price:
                    msg = f"🎮 ALERT: **{price['title']}** is already at your target! Current: {curcy} {cur:.2f} ≤ Target: {curcy} {target_price:.2f}"
                else:
                    msg = f"✅ Alert set for **{price['title']}** — Current: {curcy} {cur:.2f} → Target: {curcy} {target_price:.2f}"
                return [types.TextContent(type="text", text=msg)]
            except Exception as e:
                logger.exception("setup_epic_price_alert failed")
                return [types.TextContent(type="text", text=f"Error setting up price alert: {e}")]

        @self.server.call_tool()
        async def subscribe_epic_free_games_alerts(email: str) -> List[types.TextContent]:
            try:
                await self._register_epic_user(email)
                async with self.db_pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO epic_daily_deals_subscriptions (user_id, email)
                        VALUES ((SELECT id FROM epic_games_users WHERE email = $1), $1)
                        ON CONFLICT (user_id, email) DO UPDATE SET is_active = TRUE
                        """,
                        email,
                    )
                return [types.TextContent(type="text", text=f"✅ Subscribed {email} to Epic free game alerts")]
            except Exception as e:
                logger.exception("subscribe_epic_free_games_alerts failed")
                return [types.TextContent(type="text", text=f"Error subscribing: {e}")]

        @self.server.call_tool()
        async def get_epic_deals_today(email: str) -> List[types.TextContent]:
            try:
                deals = await self.epic_api.get_trending_deals(limit=15)
                if not deals:
                    return [types.TextContent(type="text", text="No Epic Games deals found today.")]
                sent = await self._send_epic_deals_email(email, deals)
                lines = [f"# Epic Games Store - Today's Deals ({len(deals)} games)\n"]
                for d in deals:
                    p = d['price']
                    disc = f" {p['discount_percentage']}% OFF" if p['discount_percentage'] else ""
                    base = f"- **{d['title']}** by {d['developer']}\n  Price: {p['currency']} {p['current']:.2f}"
                    if p['original'] != p['current']:
                        base += f" (was {p['currency']} {p['original']:.2f}){disc}"
                    lines.append(base + f"\n  URL: {d['url']}\n")
                if sent:
                    lines.append(f"\n📧 Deals also sent to {email}")
                return [types.TextContent(type="text", text="\n".join(lines))]
            except Exception as e:
                logger.exception("get_epic_deals_today failed")
                return [types.TextContent(type="text", text=f"Error getting Epic deals: {e}")]

        # -------------------- UNIFIED ----------------------------------------
        @self.server.call_tool()
        async def search_games_all_platforms(query: str, platforms: str = "steam,epic", limit: int = 10) -> List[types.TextContent]:
            try:
                plats = [p.strip().lower() for p in platforms.split(',')]
                sections: List[str] = [f"# Game Search Results for '{query}'\n"]

                if 'epic' in plats:
                    ep = await self.epic_api.search_games(query, limit)
                    if ep:
                        sections.append("## Epic Games Store:\n")
                        for g in ep[:5]:
                            price = g['price']
                            pt = "Free" if price['is_free'] else f"{price['currency']} {price['current']:.2f}"
                            sections.append(f"- **{g['title']}** — {pt}\n  Developer: {g['developer']}\n  URL: {g['url']}\n")

                if 'steam' in plats:
                    steam_text = await self.steam.search(query)
                    sections.append("## Steam Store:\n")
                    sections.append(steam_text)

                sections.append("\n💡 Use platform-specific search tools for complete details.")
                return [types.TextContent(type="text", text="\n".join(sections))]
            except Exception as e:
                logger.exception("search_games_all_platforms failed")
                return [types.TextContent(type="text", text=f"Error searching games: {e}")]

        @self.server.call_tool()
        async def compare_game_prices(game_title: str) -> List[types.TextContent]:
            try:
                lines = [f"# Price Comparison for '{game_title}'\n"]

                # Epic side
                epic_results = await self.epic_api.search_games(game_title, limit=3)
                if epic_results:
                    lines.append("## Epic Games Store:\n")
                    for g in epic_results:
                        p = g['price']
                        txt = "Free" if p['is_free'] else f"{p['currency']} {p['current']:.2f}"
                        if p['original'] != p['current']:
                            txt += f" (was {p['currency']} {p['original']:.2f})"
                        lines.append(f"- **{g['title']}** — {txt}\n  {g['url']}\n")
                else:
                    lines.append("## Epic Games Store:\nNo matches found\n")

                # Steam side: reuse the search text result (already includes prices)
                steam_block = await self.steam.search(game_title)
                lines.append("\n## Steam Store:\n")
                lines.append(steam_block or "No matches found")

                lines.append("\n💡 Tip: Set alerts on both platforms to catch the lowest price.")
                return [types.TextContent(type="text", text="\n".join(lines))]
            except Exception as e:
                logger.exception("compare_game_prices failed")
                return [types.TextContent(type="text", text=f"Error comparing prices: {e}")]

    # -------------------- Helpers -------------------------------------------
    async def _register_epic_user(self, email: str) -> None:
        if not self.db_pool:
            raise RuntimeError("Database not configured. Set DATABASE_URL env var.")
        async with self.db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO epic_games_users (email) VALUES ($1)
                ON CONFLICT (email) DO NOTHING
                """,
                email,
            )

    async def _send_epic_deals_email(self, recipient_email: str, deals: List[Dict]) -> bool:
        try:
            if not (self.sender_email and self.sender_password):
                logger.warning("SMTP credentials not configured; skip email send")
                return False

            subject = f"Epic Games Store - {len(deals)} Great Deals Today!"
            html = [
                "<html><head><style>body{font-family:Arial} .deal{margin:12px 0;padding:10px;border:1px solid #ddd;border-radius:6px}</style></head><body>",
                "<h2>🎮 Epic Games Store - Today's Best Deals</h2>",
            ]
            for d in deals:
                p = d['price']
                disc = f" {p['discount_percentage']}% OFF" if p['discount_percentage'] else ""
                html.append(
                    f"<div class='deal'><div><b>{d['title']}</b> — by {d['developer']}</div>"
                    f"<div>{p['currency']} {p['current']:.2f}"
                    + (f" <strike>{p['currency']} {p['original']:.2f}</strike>{disc}" if p['original'] != p['current'] else "")
                    + f"</div><div><a href='{d['url']}'>View on Epic Games Store</a></div></div>"
                )
            html.append("<hr><small>Sent by Combined Game Tracker MCP. To unsubscribe, contact admin.</small></body></html>")

            msg = MIMEMultipart('alternative')
            msg['Subject'] = subject
            msg['From'] = self.sender_email
            msg['To'] = recipient_email
            msg.attach(MIMEText("".join(html), 'html'))

            with smtplib.SMTP(self.smtp_host, self.smtp_port) as s:
                s.starttls()
                s.login(self.sender_email, self.sender_password)
                s.send_message(msg)
            return True
        except Exception as e:
            logger.exception("Failed to send Epic deals email")
            return False

    # -------------------- Startup -------------------------------------------
    async def start_server(self) -> None:
        # Database pool (shared by Epic tools; Steam uses its own internals)
        database_url = os.getenv('DATABASE_URL')
        if database_url:
            self.db_pool = await asyncpg.create_pool(database_url)
            logger.info("Database connection established")
        else:
            logger.warning("No DATABASE_URL provided — Epic alert tools that need DB will fail")
        logger.info("Starting Combined Game Tracker MCP (Steam + Epic)")
        # NOTE: If you run via an MCP host (like VS Code or other clients), you typically
        # don't call server.serve_http() here. If you want to run standalone HTTP server,
        # you can wire it up similarly to your existing implementation.


# -------------------- Main ---------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    server = CombinedGameTrackerMCP()

    # If you want to expose an HTTP transport (optional):
    async def _run():
        await server.start_server()
        # Example: start a streamable HTTP MCP transport if desired
        # from mcp.server.stdio import stdio_server  # or an HTTP transport if you use one
        # await server.server.run_async("streamable-http", host="0.0.0.0", port=int(os.getenv("PORT", 8091)))
        # For now we just await forever so the process keeps running.
        while True:
            await asyncio.sleep(3600)

    asyncio.run(_run())
