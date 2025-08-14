
"""
Integration Test Script - Run this after integrating Epic Games API

This will test your Epic Games integration without affecting your main server
"""

import asyncio
import sys
import os

# Add current directory to path so we can import our modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

async def test_integration():
    """Test Epic Games integration"""

    print("🧪 TESTING EPIC GAMES INTEGRATION")
    print("=" * 50)

    # Test 1: Import working Epic Games API
    print("\n1️⃣ Testing Epic Games API import...")
    try:
        from working_epic_games_api import WorkingEpicGamesAPI
        api = WorkingEpicGamesAPI()
        print("✅ Epic Games API imported successfully")
    except Exception as e:
        print(f"❌ Epic Games API import failed: {e}")
        return False

    # Test 2: Free Games (most important feature)
    print("\n2️⃣ Testing Epic Games free games...")
    try:
        free_games = await api.get_free_games()
        current_count = len(free_games.get('current', []))
        upcoming_count = len(free_games.get('upcoming', []))

        print(f"✅ Free games API working: {current_count} current, {upcoming_count} upcoming")

        if current_count > 0:
            print("   Current free games:")
            for game in free_games['current'][:2]:
                print(f"   - {game['title']}")

    except Exception as e:
        print(f"❌ Free games test failed: {e}")
        return False

    # Test 3: Game Search
    print("\n3️⃣ Testing Epic Games search...")
    try:
        games = await api.search_games("Fortnite", 3)
        print(f"✅ Search working: Found {len(games)} games")

        for game in games[:2]:
            price_info = game['price']
            price_text = "Free" if price_info['is_free'] else f"{price_info['currency']} {price_info['current']}"
            print(f"   - {game['title']}: {price_text}")

    except Exception as e:
        print(f"❌ Search test failed: {e}")
        return False

    print("\n🎉 ALL TESTS PASSED!")
    return True

if __name__ == "__main__":
    print("🚀 Starting Epic Games Integration Test...")

    success = asyncio.run(test_integration())

    if success:
        print("\n🎯 READY TO INTEGRATE! 🎉")
    else:
        print("\n⚠️  PLEASE FIX ISSUES BEFORE INTEGRATING")
