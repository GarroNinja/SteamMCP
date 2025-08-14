
"""
Background Jobs for Epic Games Store Monitoring
Handles price checking, free games monitoring, and automated notifications
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any
import asyncpg
from epic_games_api import EpicGamesAPI
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os
import json
import schedule
import time
from threading import Thread

logger = logging.getLogger(__name__)

class EpicGamesMonitor:
    """
    Background monitoring system for Epic Games Store
    Handles automated price checking, free games alerts, and deal notifications
    """

    def __init__(self):
        self.epic_api = EpicGamesAPI()
        self.db_pool = None
        self.database_url = os.getenv('DATABASE_URL')

        # Email configuration
        self.smtp_host = os.getenv('SMTP_HOST', 'smtp.gmail.com')
        self.smtp_port = int(os.getenv('SMTP_PORT', '587'))
        self.sender_email = os.getenv('SENDER_EMAIL')
        self.sender_password = os.getenv('SENDER_PASSWORD')

        # Monitoring configuration
        self.price_check_interval = int(os.getenv('EPIC_PRICE_CHECK_HOURS', '12'))  # Check every 12 hours
        self.free_games_check_interval = int(os.getenv('EPIC_FREE_GAMES_CHECK_HOURS', '6'))  # Check every 6 hours
        self.deals_notification_time = os.getenv('EPIC_DEALS_TIME', '22:30')  # Daily deals at 10:30 PM

    async def initialize(self):
        """Initialize database connection and setup"""
        if self.database_url:
            self.db_pool = await asyncpg.create_pool(self.database_url)
            logger.info("Epic Games Monitor: Database connection established")
        else:
            raise Exception("Database URL not provided")

    async def check_epic_price_alerts(self):
        """Check all active Epic Games price alerts and send notifications"""
        try:
            logger.info("Starting Epic Games price alerts check")

            async with self.db_pool.acquire() as conn:
                # Get all active price alerts
                alerts = await conn.fetch("""
                    SELECT epa.*, egu.email
                    FROM epic_price_alerts epa
                    JOIN epic_games_users egu ON epa.user_id = egu.id
                    WHERE epa.is_active = TRUE AND epa.alert_sent = FALSE
                """)

                notifications_sent = 0

                for alert in alerts:
                    try:
                        # Get current price for the game
                        current_price_info = await self.epic_api.get_game_price(
                            alert['epic_namespace'], 
                            alert['epic_offer_id']
                        )

                        if current_price_info:
                            current_price = current_price_info['current_price']
                            target_price = float(alert['target_price'])

                            # Update current price in database
                            await conn.execute("""
                                UPDATE epic_price_alerts 
                                SET current_price = $1, updated_at = CURRENT_TIMESTAMP
                                WHERE id = $2
                            """, current_price, alert['id'])

                            # Check if price dropped below target
                            if current_price <= target_price:
                                # Send notification
                                success = await self._send_price_alert_email(
                                    alert['email'],
                                    alert['game_title'],
                                    current_price,
                                    target_price,
                                    alert['currency'],
                                    alert['epic_namespace'],
                                    alert['epic_offer_id']
                                )

                                if success:
                                    # Mark alert as sent
                                    await conn.execute("""
                                        UPDATE epic_price_alerts 
                                        SET alert_sent = TRUE, updated_at = CURRENT_TIMESTAMP
                                        WHERE id = $1
                                    """, alert['id'])
                                    notifications_sent += 1
                                    logger.info(f"Price alert sent for {alert['game_title']} to {alert['email']}")

                        # Add small delay to avoid rate limiting
                        await asyncio.sleep(1)

                    except Exception as e:
                        logger.error(f"Error checking price alert for {alert['game_title']}: {str(e)}")
                        continue

                logger.info(f"Epic price alerts check completed. Sent {notifications_sent} notifications")

        except Exception as e:
            logger.error(f"Error in Epic price alerts check: {str(e)}")

    async def check_epic_free_games(self):
        """Check for new Epic Games free games and notify subscribers"""
        try:
            logger.info("Starting Epic Games free games check")

            # Get current free games
            free_games = await self.epic_api.get_free_games()
            current_free = free_games.get('current', [])

            if not current_free:
                logger.info("No current free games found")
                return

            notifications_sent = 0

            async with self.db_pool.acquire() as conn:
                # Get all subscribers
                subscribers = await conn.fetch("""
                    SELECT DISTINCT email FROM epic_daily_deals_subscriptions 
                    WHERE is_active = TRUE
                """)

                # Check each free game
                for game in current_free:
                    # Check if we've already notified about this free game
                    existing_alert = await conn.fetchrow("""
                        SELECT id FROM epic_free_games_alerts
                        WHERE epic_namespace = $1 AND epic_offer_id = $2
                        AND alert_sent = TRUE
                    """, game['epic_namespace'], game['epic_id'])

                    if existing_alert:
                        continue  # Already notified about this game

                    # Send notifications to all subscribers
                    for subscriber in subscribers:
                        try:
                            success = await self._send_free_game_notification(
                                subscriber['email'],
                                game
                            )

                            if success:
                                notifications_sent += 1

                        except Exception as e:
                            logger.error(f"Error sending free game notification to {subscriber['email']}: {str(e)}")

                    # Mark as notified
                    await conn.execute("""
                        INSERT INTO epic_free_games_alerts 
                        (user_id, epic_namespace, epic_offer_id, game_title, start_date, end_date, alert_sent)
                        VALUES (1, $1, $2, $3, $4, $5, TRUE)
                        ON CONFLICT (user_id, epic_namespace, epic_offer_id) 
                        DO UPDATE SET alert_sent = TRUE
                    """, game['epic_namespace'], game['epic_id'], game['title'],
                         game.get('start_date'), game.get('end_date'))

            logger.info(f"Epic free games check completed. Sent {notifications_sent} notifications")

        except Exception as e:
            logger.error(f"Error in Epic free games check: {str(e)}")

    async def send_daily_epic_deals(self):
        """Send daily Epic Games deals to subscribers"""
        try:
            logger.info("Starting daily Epic Games deals notification")

            # Get trending deals
            deals = await self.epic_api.get_trending_deals(limit=20)

            if not deals:
                logger.info("No Epic deals found for today")
                return

            notifications_sent = 0

            async with self.db_pool.acquire() as conn:
                # Get all subscribers who haven't received today's deals
                subscribers = await conn.fetch("""
                    SELECT email FROM epic_daily_deals_subscriptions 
                    WHERE is_active = TRUE 
                    AND (last_sent IS NULL OR last_sent < CURRENT_DATE)
                """)

                for subscriber in subscribers:
                    try:
                        success = await self._send_daily_deals_email(
                            subscriber['email'],
                            deals
                        )

                        if success:
                            # Update last_sent timestamp
                            await conn.execute("""
                                UPDATE epic_daily_deals_subscriptions 
                                SET last_sent = CURRENT_TIMESTAMP
                                WHERE email = $1
                            """, subscriber['email'])
                            notifications_sent += 1

                    except Exception as e:
                        logger.error(f"Error sending daily deals to {subscriber['email']}: {str(e)}")

            logger.info(f"Daily Epic deals notification completed. Sent {notifications_sent} emails")

        except Exception as e:
            logger.error(f"Error in daily Epic deals notification: {str(e)}")

    async def _send_price_alert_email(
        self, 
        email: str, 
        game_title: str, 
        current_price: float, 
        target_price: float, 
        currency: str,
        namespace: str,
        offer_id: str
    ) -> bool:
        """Send price alert email notification"""
        try:
            if not self.sender_email or not self.sender_password:
                return False

            subject = f"🎮 Price Alert: {game_title} is now {currency} {current_price:.2f}!"

            html_content = f"""
            <html>
            <head>
                <style>
                    body {{ font-family: Arial, sans-serif; margin: 20px; }}
                    .header {{ color: #2c3e50; font-size: 24px; margin-bottom: 20px; }}
                    .game-info {{ background: #ecf0f1; padding: 15px; border-radius: 5px; margin: 20px 0; }}
                    .price {{ color: #e74c3c; font-size: 20px; font-weight: bold; }}
                    .target {{ color: #27ae60; }}
                    .button {{ background: #3498db; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px; }}
                </style>
            </head>
            <body>
                <div class="header">🎮 Epic Games Price Alert!</div>
                <div class="game-info">
                    <h3>{game_title}</h3>
                    <div class="price">Current Price: {currency} {current_price:.2f}</div>
                    <div class="target">Your Target: {currency} {target_price:.2f}</div>
                    <p>Great news! The price has dropped to your target price or below.</p>
                </div>
                <p><a href="https://store.epicgames.com/en-US/p/{namespace}" class="button">Buy Now on Epic Games Store</a></p>
                <hr>
                <p><small>This alert was sent by Enhanced Game Tracker MCP Server.</small></p>
            </body>
            </html>
            """

            return await self._send_email(email, subject, html_content)

        except Exception as e:
            logger.error(f"Error sending price alert email: {str(e)}")
            return False

    async def _send_free_game_notification(self, email: str, game: Dict[str, Any]) -> bool:
        """Send free game notification email"""
        try:
            if not self.sender_email or not self.sender_password:
                return False

            subject = f"🆓 Free Game Alert: {game['title']} is now free on Epic Games!"

            html_content = f"""
            <html>
            <head>
                <style>
                    body {{ font-family: Arial, sans-serif; margin: 20px; }}
                    .header {{ color: #2c3e50; font-size: 24px; margin-bottom: 20px; }}
                    .game-info {{ background: #ecf0f1; padding: 15px; border-radius: 5px; margin: 20px 0; }}
                    .free {{ color: #27ae60; font-size: 20px; font-weight: bold; }}
                    .button {{ background: #3498db; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px; }}
                </style>
            </head>
            <body>
                <div class="header">🆓 Epic Games Free Game!</div>
                <div class="game-info">
                    <h3>{game['title']}</h3>
                    <div>Developer: {game['developer']}</div>
                    <div class="free">FREE until {game.get('end_date', 'TBA')}</div>
                    <p>{game.get('description', '')}</p>
                </div>
                <p><a href="{game['url']}" class="button">Claim Free Game Now</a></p>
                <p><strong>Hurry!</strong> This offer is only available for a limited time.</p>
                <hr>
                <p><small>This alert was sent by Enhanced Game Tracker MCP Server.</small></p>
            </body>
            </html>
            """

            return await self._send_email(email, subject, html_content)

        except Exception as e:
            logger.error(f"Error sending free game notification: {str(e)}")
            return False

    async def _send_daily_deals_email(self, email: str, deals: List[Dict[str, Any]]) -> bool:
        """Send daily deals email"""
        try:
            if not self.sender_email or not self.sender_password:
                return False

            subject = f"🎮 Epic Games Daily Deals - {len(deals)} Great Offers Today!"

            html_content = """
            <html>
            <head>
                <style>
                    body { font-family: Arial, sans-serif; margin: 20px; }
                    .header { color: #2c3e50; font-size: 24px; margin-bottom: 20px; }
                    .deal { margin-bottom: 20px; padding: 15px; border: 1px solid #ddd; border-radius: 5px; }
                    .title { color: #2c3e50; font-size: 18px; font-weight: bold; }
                    .price { color: #e74c3c; font-size: 16px; font-weight: bold; }
                    .discount { color: #27ae60; font-weight: bold; }
                    .developer { color: #7f8c8d; }
                </style>
            </head>
            <body>
                <div class="header">🎮 Epic Games Store - Today's Best Deals</div>
            """

            for deal in deals:
                price_info = deal['price']
                discount_text = f"{price_info['discount_percentage']}% OFF" if price_info['discount_percentage'] > 0 else ""

                html_content += f"""
                <div class="deal">
                    <div class="title">{deal['title']}</div>
                    <div class="developer">by {deal['developer']}</div>
                    <div class="price">
                        {price_info['currency']} {price_info['current']:.2f}
                """

                if price_info['original'] != price_info['current']:
                    html_content += f" <strike>{price_info['currency']} {price_info['original']:.2f}</strike>"
                    if discount_text:
                        html_content += f' <span class="discount">{discount_text}</span>'

                html_content += f"""
                    </div>
                    <div><a href="{deal['url']}" target="_blank">View on Epic Games Store</a></div>
                </div>
                """

            html_content += """
                <hr>
                <p><small>This email was sent by Enhanced Game Tracker MCP Server.</small></p>
            </body>
            </html>
            """

            return await self._send_email(email, subject, html_content)

        except Exception as e:
            logger.error(f"Error sending daily deals email: {str(e)}")
            return False

    async def _send_email(self, recipient_email: str, subject: str, html_content: str) -> bool:
        """Send email using SMTP"""
        try:
            msg = MIMEMultipart('alternative')
            msg['Subject'] = subject
            msg['From'] = self.sender_email
            msg['To'] = recipient_email

            msg.attach(MIMEText(html_content, 'html'))

            loop = asyncio.get_event_loop()

            def send_email_sync():
                with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                    server.starttls()
                    server.login(self.sender_email, self.sender_password)
                    server.send_message(msg)

            await loop.run_in_executor(None, send_email_sync)
            return True

        except Exception as e:
            logger.error(f"Error sending email: {str(e)}")
            return False

    def start_scheduler(self):
        """Start the background job scheduler"""
        def run_async_job(coro):
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(coro)
            finally:
                loop.close()

        # Schedule jobs
        schedule.every(self.price_check_interval).hours.do(
            lambda: Thread(target=run_async_job, args=(self.check_epic_price_alerts(),)).start()
        )

        schedule.every(self.free_games_check_interval).hours.do(
            lambda: Thread(target=run_async_job, args=(self.check_epic_free_games(),)).start()
        )

        schedule.every().day.at(self.deals_notification_time).do(
            lambda: Thread(target=run_async_job, args=(self.send_daily_epic_deals(),)).start()
        )

        logger.info("Epic Games background jobs scheduled")
        logger.info(f"- Price alerts: Every {self.price_check_interval} hours")
        logger.info(f"- Free games: Every {self.free_games_check_interval} hours")
        logger.info(f"- Daily deals: Daily at {self.deals_notification_time}")

        # Run scheduler
        while True:
            schedule.run_pending()
            time.sleep(60)  # Check every minute

# Main execution
async def main():
    """Main function to run the Epic Games monitor"""
    monitor = EpicGamesMonitor()
    await monitor.initialize()

    # Run scheduler in a separate thread
    scheduler_thread = Thread(target=monitor.start_scheduler)
    scheduler_thread.daemon = True
    scheduler_thread.start()

    logger.info("Epic Games Monitor started successfully")

    # Keep the main thread alive
    try:
        while True:
            await asyncio.sleep(3600)  # Sleep for an hour
    except KeyboardInterrupt:
        logger.info("Epic Games Monitor shutting down")

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    asyncio.run(main())
