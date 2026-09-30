import time
import webbrowser
import threading
import uvicorn

from config import settings

def open_browser():
    time.sleep(1.5)
    url = f"http://{settings.HOST}:{settings.PORT}"
    print(f"\n[+] Opening web interface at {url} in your browser...\n")
    webbrowser.open(url)

if __name__ == "__main__":
    print("=" * 65)
    print("   LeadGen Pro — Lead Discovery & Email Outreach")
    print("=" * 65)
    print(f"Server starting on http://{settings.HOST}:{settings.PORT}")
    print(f"Gemini AI: {'Configured (' + str(len(settings.get_gemini_keys())) + ' keys)' if settings.is_gemini_configured else 'Not configured'}")
    print(f"SMTP: {'Configured' if settings.is_smtp_configured else 'Not configured'}")
    print("Press Ctrl+C to stop the server.\n")

    threading.Thread(target=open_browser, daemon=True).start()
    uvicorn.run("app:app", host=settings.HOST, port=settings.PORT, log_level=settings.LOG_LEVEL.lower())
