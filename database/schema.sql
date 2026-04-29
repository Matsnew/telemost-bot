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
