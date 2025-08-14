"""
Epic Games Store Integration - Usage Examples

This file shows how to use the Epic Games Store features
in the enhanced MCP server.
"""

import asyncio
from enhanced_mcp_server import CombinedGameTrackerMCP

async def example_usage():
    """Example usage of Epic Games Store features"""

    print("🎮 Epic Games Store Integration Examples")
    print("=" * 50)

    # Initialize the combined MCP server
    server = CombinedGameTrackerMCP()

    # Example 1: Search Epic Games Store
    print("
1️⃣ Searching Epic Games Store...")
    epic_results = await server._search_epic_games("Cyberpunk 2077", 5)
    print(f"Found {len(epic_results)} Epic Games results")

    # Example 2: Get free games
    print("
2️⃣ Getting Epic Games free games...")
    free_games = await server._get_epic_free_games()
    current_count = len(free_games.get('current', []))
    upcoming_count = len(free_games.get('upcoming', []))
    print(f"Current free games: {current_count}")
    print(f"Upcoming free games: {upcoming_count}")

    # Example 3: Multi-platform search
    print("
3️⃣ Multi-platform search example...")
    # This would search both Steam and Epic Games Store
    print("Multi-platform search combines results from both platforms")

    # Example 4: Price comparison
    print("
4️⃣ Price comparison example...")  
    # This would compare prices between Steam and Epic Games Store
    print("Price comparison helps find the best deals across platforms")

    print("
✅ Epic Games Store integration examples complete!")

if __name__ == "__main__":
    asyncio.run(example_usage())
