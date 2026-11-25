MATHAZZ AI V25.3 — LYLI AI BRAIN + PROFESSIONAL CHART ENGINE

Files
- MATHAZZ_AI_V25_3_LYLI_AI_BRAIN.py: full single-file bot source, based on the original V25.2 source.
- requirements_v25_3.txt: optional chart renderer dependencies (install with pip).
- V25_3_RAILWAY_VARIABLES.txt: Railway variable template. Fill secrets only in Railway Variables.

Deploy
1. Replace your existing bot source with MATHAZZ_AI_V25_3_LYLI_AI_BRAIN.py (or rename it to your current start command filename).
2. Install: pip install -r requirements_v25_3.txt
3. Add variables from V25_3_RAILWAY_VARIABLES.txt in Railway. Keep your existing BOT_TOKEN, ADMIN_IDS, Twelve Data, Gemini, Groq keys and volume settings.
4. Set CHARTIMG_SYMBOL to a TradingView XAUUSD feed that matches the broker/source you want to compare. OANDA:XAUUSD is only the default example.
5. Deploy and test /chart XAUUSD M15, /ask XAUUSD live now, and /paper.

New Telegram commands
- /chart XAUUSD M5 (default M15 if no timeframe) — creates an annotated chart and routes it to Analyze.
- /paper — paper journal summary.
- /ask ... — existing Lily chat; live market questions attach a Twelve Data/Yahoo source-tagged snapshot, plus optional Alpha Vantage spot cross-check and independent Claude/DeepSeek text reviews.

Important safety / limitations
- Original Instant, Analyze, ICT PRO and ICT engines remain in the file. Internal old category names are aliases to exactly four Telegram destinations: Instant, Analyze, ICT PRO, ICT. Image and Chat route to Analyze; Scan Zone routes to ICT.
- V25.3 live market snapshot requests Twelve Data when allowed by the existing quota/cooldown policy, then uses the existing Yahoo fallback. The latest candle close is not guaranteed broker tick data. Compare broker feed, timestamps and exchange symbol.
- Chart-IMG is optional and sends a separate TradingView reference image. Python/matplotlib draws the annotated chart from the bot's candle feed and Python-detected OB/FVG zones. CHARTIMG_SYMBOL must match the intended feed.
- Claude and DeepSeek run as independent text/vision reviewers when keys are configured. Gemini/Groq original chat/image paths are preserved. A missing provider is reported as unavailable, not treated as agreement.
- Alpha Vantage GOLD_SILVER_SPOT is a spot cross-check only. It never overwrites Twelve Data or broker candles.
- Paper journal records only. M1 OHLC monitor resolves TP1/SL; if both are touched in the same candle it marks AMBIGUOUS. No broker orders are sent. VALIDATOR_LIVE_TRADING is hard-coded False in this version.
- SQLite persistence depends on Railway Volume mounted at /data (or your existing RAILWAY_VOLUME_MOUNT_PATH).
- Test run in this environment: Python compile/import passed; chart renderer produced a PNG; paper monitor test resolved a sample paper trade. External API calls, Telegram delivery, Railway deployment and broker-feed matching were not tested because no credentials were supplied.
