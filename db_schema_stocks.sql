-- ============================================================
-- Stock Advisor DB Schema
-- DB: stock_advisor (im bestehenden bybit_postgres Container)
-- Setup: CREATE DATABASE stock_advisor;
-- ============================================================

-- Portfolio: manuell via Excel gepflegt
CREATE TABLE IF NOT EXISTS holdings (
    id          SERIAL PRIMARY KEY,
    ticker      VARCHAR(10)    NOT NULL,
    shares      NUMERIC(12,4)  NOT NULL,
    avg_buy     NUMERIC(10,4)  NOT NULL,
    broker      VARCHAR(30),
    sektor      VARCHAR(50),
    notizen     TEXT,
    updated     TIMESTAMP DEFAULT NOW(),
    UNIQUE(ticker, broker)
);

-- Watchlist: potenzielle Käufe
CREATE TABLE IF NOT EXISTS watchlist (
    id              SERIAL PRIMARY KEY,
    ticker          VARCHAR(10) NOT NULL UNIQUE,
    sektor          VARCHAR(50),
    prio            CHAR(1) CHECK (prio IN ('H','M','L')),
    max_position    NUMERIC(10,2),
    notizen         TEXT,
    updated         TIMESTAMP DEFAULT NOW()
);

-- Ticker-Mapping: TR/Scalable Ticker → yfinance Ticker
-- Nötig weil z.B. BQ73 (TR) = RCAT (yfinance), PCELL (TR) = PCELL.ST
CREATE TABLE IF NOT EXISTS ticker_map (
    broker_ticker   VARCHAR(10) PRIMARY KEY,
    yf_ticker       VARCHAR(15) NOT NULL,
    name            VARCHAR(80),
    exchange        VARCHAR(20)
);

-- Default Mappings (TR/Scalable → yfinance)
INSERT INTO ticker_map (broker_ticker, yf_ticker, name, exchange) VALUES
    ('BQ73',  'RCAT',     'Red Cat Holdings',   'NASDAQ'),
    ('ITM',   'ITMPF',    'ITM Power',          'OTC'),
    ('PCELL', 'PCELL.ST', 'Powercell Sweden',   'Stockholm'),
    ('BTQ',   'BTQ.V',    'BTQ Technologies',   'TSX-V'),
    ('BRN',   'BRN.AX',   'Brainchip Holdings', 'ASX'),
    ('FCEL',  'FCEL',     'FuelCell Energy',     'NASDAQ'),
    ('LAES',  'LAES',     'SealSQ',             'NASDAQ'),
    ('D7G / NLLSF', 'NLLSF', 'Nel ASA',        'OTC'),
    ('FLT / TAKOF', 'TAKOF', 'Volatus Aerospace','OTC')
ON CONFLICT (broker_ticker) DO NOTHING;

-- Historische OHLCV + Technicals (via yfinance)
CREATE TABLE IF NOT EXISTS stock_history (
    ticker      VARCHAR(15)   NOT NULL,   -- yfinance ticker
    date        DATE          NOT NULL,
    open        NUMERIC(12,4),
    high        NUMERIC(12,4),
    low         NUMERIC(12,4),
    close       NUMERIC(12,4),
    volume      BIGINT,
    rsi_14      NUMERIC(6,2),
    atr_pct     NUMERIC(8,5),
    bb_position NUMERIC(6,3),
    sma_50      NUMERIC(12,4),
    sma_200     NUMERIC(12,4),
    PRIMARY KEY (ticker, date)
);

-- Newsletter Sentiment (von newsletter_digest_imap.py)
CREATE TABLE IF NOT EXISTS email_sentiment (
    id            SERIAL PRIMARY KEY,
    date          DATE          NOT NULL,
    ticker        VARCHAR(15)   NOT NULL,
    sentiment     NUMERIC(4,2)  NOT NULL,   -- -2.0 bis +2.0 (Claude score)
    relevance     SMALLINT,                  -- 1-5
    source        VARCHAR(50) DEFAULT 'newsletter',
    headlines     TEXT[],
    created       TIMESTAMP DEFAULT NOW(),
    UNIQUE(date, ticker, source)
);

-- News Sentiment (von yfinance news + Claude scoring)
CREATE TABLE IF NOT EXISTS news_sentiment (
    id            SERIAL PRIMARY KEY,
    date          DATE          NOT NULL,
    ticker        VARCHAR(15)   NOT NULL,
    title         TEXT          NOT NULL,
    publisher     VARCHAR(100),
    url           TEXT,
    sentiment     NUMERIC(4,2),              -- -2.0 bis +2.0
    relevance     SMALLINT,                  -- 1-5
    created       TIMESTAMP DEFAULT NOW(),
    UNIQUE(date, ticker, title)
);

-- Aggregiertes Daily Sentiment (View für schnellen Zugriff)
CREATE OR REPLACE VIEW v_daily_sentiment AS
SELECT
    date,
    ticker,
    AVG(sentiment)         AS avg_sentiment,
    MAX(relevance)         AS max_relevance,
    COUNT(*)               AS source_count,
    'combined'             AS source
FROM (
    SELECT date, ticker, sentiment, relevance FROM email_sentiment
    UNION ALL
    SELECT date, ticker, sentiment, relevance FROM news_sentiment
) combined
GROUP BY date, ticker;

-- Wöchentliche Scoring-Ergebnisse
CREATE TABLE IF NOT EXISTS weekly_scores (
    id              SERIAL PRIMARY KEY,
    run_date        DATE          NOT NULL,
    ticker          VARCHAR(15)   NOT NULL,
    yf_ticker       VARCHAR(15),
    score_total     NUMERIC(5,3),
    score_tech      NUMERIC(5,3),
    score_sentiment NUMERIC(5,3),
    action          VARCHAR(20),             -- BUY / HOLD / SKIP
    suggested_eur   NUMERIC(10,2),
    reasoning       TEXT,
    created         TIMESTAMP DEFAULT NOW(),
    UNIQUE(run_date, ticker)
);

-- Indizes für Performance
CREATE INDEX IF NOT EXISTS idx_stock_history_date ON stock_history(date);
CREATE INDEX IF NOT EXISTS idx_email_sentiment_date ON email_sentiment(date, ticker);
CREATE INDEX IF NOT EXISTS idx_news_sentiment_date ON news_sentiment(date, ticker);
CREATE INDEX IF NOT EXISTS idx_weekly_scores_date ON weekly_scores(run_date);
