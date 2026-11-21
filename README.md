
# MATHAZZ AI — Railway Worker

This project runs as a Railway Worker (Telegram long polling).

## Deploy
1. Upload all files in this folder to the root of your GitHub repository.
2. In Railway, use a Worker service connected to that repository.
3. Set the start command to `python main.py`.
4. Add environment variables in Railway → Variables:
   - BOT_TOKEN (required)
   - GEMINI_API_KEY (optional)
   - GROQ_API_KEY (optional)
   - TWELVE_DATA_API_KEY (optional)
   - DATA_PROVIDER=auto
5. Deploy with one replica for Telegram polling.
6. 
