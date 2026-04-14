import json, os, sys, threading, time, base64, hashlib
from datetime import datetime
from pathlib import Path
from io import BytesIO

import pyperclip
from PIL import Image, ImageDraw, ImageGrab
import pystray
import win32clipboard

import firebase_admin
from firebase_admin import credentials, db

# === 設定読み込み ===
BASE = Path(__file__).resolve().parent
CFG  = BASE / "config.json"
KEY  = BASE / "serviceAccountKey.json"
LOGF = BASE / "tray_app.log"

def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOGF.open("a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")

if not CFG.exists():
    print("config.json が見つかりません。")
    sys.exit(1)

conf = json.loads(CFG.read_text(encoding="utf-8"))
DATABASE_URL = conf["database_url"].rstrip("/") + "/"
TARGET_UID   = conf["target_uid"]

# === Firebase 初期化 ===
if not firebase_admin._apps:
    cred = credentials.Certificate(str(KEY))
    firebase_admin.initialize_app(cred, {"databaseURL": DATABASE_URL})

# パスの定義を正確に
ref_phone_to_pc = db.reference(f"clips/{TARGET_UID}/latest")
ref_pc_to_phone = db.reference(f"clips/{TARGET_UID}/pcLatest")
ref_pc_history  = db.reference(f"clips/{TARGET_UID}/pcHistory")

running = True
sync_enabled = True

last_phone_ts = 0
last_text_content = None
last_image_hash = None

def get_image_hash(img):
    return hashlib.md5(img.tobytes()).hexdigest()

def set_image_to_clipboard(img_bytes):
    output = BytesIO()
    img = Image.open(BytesIO(img_bytes))
    img.convert("RGB").save(output, "BMP")
    data = output.getvalue()[14:]
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_DIB, data)
    finally:
        win32clipboard.CloseClipboard()

def poll_phone_to_pc():
    global last_phone_ts
    log(f"[START] Monitoring: clips/{TARGET_UID}/latest")
    while running:
        try:
            if sync_enabled:
                data = ref_phone_to_pc.get()
                if isinstance(data, dict):
                    ts = int(data.get("ts", 0) or 0)
                    if ts > last_phone_ts:
                        last_phone_ts = ts
                        dtype = data.get("type", "text")
                        content = data.get("text", "")
                        if dtype == "image" and content:
                            set_image_to_clipboard(base64.b64decode(content))
                            log(f"PHONE->PC IMAGE synced (ts={ts})")
                        elif dtype == "text" and content:
                            pyperclip.copy(content)
                            log(f"PHONE->PC TEXT synced (ts={ts})")
            time.sleep(1.5)
        except Exception as e:
            log(f"[ERR] phone->pc: {e}")
            time.sleep(2)

def poll_pc_clipboard():
    global last_text_content, last_image_hash
    while running:
        try:
            if sync_enabled:
                img = ImageGrab.grabclipboard()
                if isinstance(img, Image.Image):
                    current_hash = get_image_hash(img)
                    if current_hash != last_image_hash:
                        buf = BytesIO()
                        img.convert("RGB").save(buf, format="JPEG", quality=80) # 画質を少し落として節約
                        b64_str = base64.b64encode(buf.getvalue()).decode("utf-8")
                        ts = int(time.time() * 1000)
                        
                        # 画像は最新(pcLatest)のみにセットし、Historyには入れない！
                        payload = {"type": "image", "text": b64_str, "ts": ts}
                        ref_pc_to_phone.set(payload)
                        
                        last_image_hash = current_hash
                        log(f"PC->PHONE IMAGE sent (ts={ts})")
                else:
                    text = pyperclip.paste()
                    if text and text != last_text_content:
                        ts = int(time.time() * 1000)
                        payload = {"type": "text", "text": text, "ts": ts}
                        
                        # テキストは軽いので履歴(History)にも残す
                        ref_pc_to_phone.set(payload)
                        ref_pc_history.push(payload)
                        
                        last_text_content = text
                        log(f"PC->PHONE TEXT sent (ts={ts})")
            time.sleep(1.0)
        except Exception as e:
            log(f"[ERR] pc->phone: {e}")
            time.sleep(2)

def make_icon_image():
    img = Image.new("RGBA", (64, 64), (0,0,0,0))
    d = ImageDraw.Draw(img)
    d.ellipse((5,5,59,59), outline=(0,120,215,255), width=4)
    d.text((15,15), "CS", fill=(0,120,215,255))
    return img

def main():
    threading.Thread(target=poll_phone_to_pc, daemon=True).start()
    threading.Thread(target=poll_pc_clipboard, daemon=True).start()
    icon = pystray.Icon("clipshare", make_icon_image(), "ClipShare", menu=pystray.Menu(
        pystray.MenuItem("終了", lambda i, n: i.stop())
    ))
    icon.run()

if __name__ == "__main__":
    main()
