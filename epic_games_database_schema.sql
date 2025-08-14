-- Epic Games Integration Database Schema Extensions (PostgreSQL compatible)

-- Epic Games Users table
CREATE TABLE IF NOT EXISTS epic_games_users (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    epic_account_id VARCHAR(255),
    preferences JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Epic Games Price Alerts table
CREATE TABLE IF NOT EXISTS epic_price_alerts (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES epic_games_users(id) ON DELETE CASCADE,
    epic_namespace VARCHAR(255) NOT NULL,
    epic_offer_id VARCHAR(255) NOT NULL,
    game_title VARCHAR(255) NOT NULL,
    target_price DECIMAL(10, 2) NOT NULL,
    current_price DECIMAL(10, 2),
    currency VARCHAR(3) DEFAULT 'INR',
    is_active BOOLEAN DEFAULT TRUE,
    alert_sent BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, epic_namespace, epic_offer_id)
);

-- Epic Free Games Alerts table
CREATE TABLE IF NOT EXISTS epic_free_games_alerts (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES epic_games_users(id) ON DELETE CASCADE,
    epic_namespace VARCHAR(255) NOT NULL,
    epic_offer_id VARCHAR(255) NOT NULL,
    game_title VARCHAR(255) NOT NULL,
    start_date TIMESTAMP,
    end_date TIMESTAMP,
    alert_sent BOOLEAN DEFAULT FALSE,
    claimed BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, epic_namespace, epic_offer_id)
);

-- Epic Daily Deals Subscriptions table
CREATE TABLE IF NOT EXISTS epic_daily_deals_subscriptions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES epic_games_users(id) ON DELETE CASCADE,
    email VARCHAR(255) NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    last_sent TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, email)
);

-- Game Platform Mapping table
CREATE TABLE IF NOT EXISTS game_platform_mapping (
    id SERIAL PRIMARY KEY,
    game_title VARCHAR(255) NOT NULL,
    steam_app_id INTEGER,
    epic_namespace VARCHAR(255),
    epic_offer_id VARCHAR(255),
    normalized_title VARCHAR(255) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Price History table
CREATE TABLE IF NOT EXISTS price_history (
    id SERIAL PRIMARY KEY,
    platform VARCHAR(20) NOT NULL, -- 'steam' or 'epic'
    game_identifier VARCHAR(255) NOT NULL, -- steam_app_id or epic_namespace/offer_id
    game_title VARCHAR(255) NOT NULL,
    price DECIMAL(10, 2) NOT NULL,
    currency VARCHAR(3) NOT NULL,
    discount_percentage INTEGER DEFAULT 0,
    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for better performance
CREATE INDEX IF NOT EXISTS idx_epic_price_alerts_user_active 
    ON epic_price_alerts(user_id, is_active);

CREATE INDEX IF NOT EXISTS idx_epic_price_alerts_namespace_offer 
    ON epic_price_alerts(epic_namespace, epic_offer_id);

CREATE INDEX IF NOT EXISTS idx_epic_free_games_dates 
    ON epic_free_games_alerts(start_date, end_date);

CREATE INDEX IF NOT EXISTS idx_game_platform_mapping_title 
    ON game_platform_mapping(normalized_title);

CREATE INDEX IF NOT EXISTS idx_price_history_platform_game 
    ON price_history(platform, game_identifier, recorded_at);

CREATE INDEX IF NOT EXISTS idx_price_history_recorded_at 
    ON price_history(recorded_at);

-- Trigger function to auto-update updated_at
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Triggers for updated_at
CREATE TRIGGER update_epic_games_users_updated_at 
    BEFORE UPDATE ON epic_games_users 
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_epic_price_alerts_updated_at 
    BEFORE UPDATE ON epic_price_alerts 
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_game_platform_mapping_updated_at 
    BEFORE UPDATE ON game_platform_mapping 
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
