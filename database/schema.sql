CREATE TABLE IF NOT EXISTS users (
    id BIGINT PRIMARY KEY,
    created_at TIMESTAMP DEFAULT NOW(),
    is_active BOOLEAN DEFAULT TRUE,
    max_concurrent_recordings INT DEFAULT 2
);

CREATE TABLE IF NOT EXISTS meetings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMP DEFAULT NOW(),
    user_id BIGINT REFERENCES users(id),
    meeting_url TEXT,
    status TEXT DEFAULT 'started',
    transcript TEXT,
    summary TEXT,
    tags TEXT[],
    topic TEXT,
    participants TEXT[],
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_meetings_user_id ON meetings(user_id);
CREATE INDEX IF NOT EXISTS idx_meetings_tags ON meetings USING GIN(tags);
CREATE INDEX IF NOT EXISTS idx_meetings_status ON meetings(status);

-- migrations
ALTER TABLE meetings ADD COLUMN IF NOT EXISTS meeting_type TEXT;
ALTER TABLE meetings ADD COLUMN IF NOT EXISTS calendar_title TEXT;

-- Google Calendar integration
CREATE TABLE IF NOT EXISTS google_tokens (
    user_id BIGINT PRIMARY KEY REFERENCES users(id),
    access_token TEXT NOT NULL,
    refresh_token TEXT,
    token_expiry TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS calendar_settings (
    user_id BIGINT PRIMARY KEY REFERENCES users(id),
    enabled BOOLEAN DEFAULT TRUE,
    auto_join_all BOOLEAN DEFAULT FALSE,
    join_minutes_before INT DEFAULT 1
);

CREATE TABLE IF NOT EXISTS calendar_events (
    user_id BIGINT REFERENCES users(id),
    google_id TEXT NOT NULL,
    title TEXT,
    start_time TIMESTAMP WITH TIME ZONE,
    meeting_url TEXT,
    selected BOOLEAN DEFAULT FALSE,
    joined BOOLEAN DEFAULT FALSE,
    event_date DATE NOT NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (user_id, google_id)
);

CREATE INDEX IF NOT EXISTS idx_calendar_events_user_date ON calendar_events(user_id, event_date);

-- migrations
ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS calendar_name TEXT;
