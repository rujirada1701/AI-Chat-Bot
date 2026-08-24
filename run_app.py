#!/usr/bin/env python
"""
Entry point สำหรับสร้าง .exe ของ Chat-Bot
เปิด Flask server และเบราว์เซอร์อัตโนมัติ
"""

import os
import sys
import time
import webbrowser
from pathlib import Path
from threading import Thread

# เพิ่ม path ของ project ให้ import ได้
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

# Import Flask app
from app import app


def open_browser():
    """รอให้ server พร้อม แล้วเปิดเบราว์เซอร์"""
    time.sleep(2)
    webbrowser.open("http://localhost:5000/qa")


if __name__ == "__main__":
    # เปิด browser ใน thread แยก
    browser_thread = Thread(target=open_browser, daemon=True)
    browser_thread.start()
    
    print("🚀 MT Assistant AI - Chat Bot")
    print("=" * 50)
    print("🌐 Opening browser at http://localhost:5000/qa")
    print("📝 กำลังเปิด Chat UI...")
    print("\nกด Ctrl+C เพื่อหยุด server")
    print("=" * 50)
    
    # รัน Flask app
    app.run(host="0.0.0.0", port=5000, debug=False)
