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

# === 設定読み込み (既存のまま) ===
BASE = Path(__file__).resolve().parent
CFG  = BASE / "config.json"
KEY  = BASE / "serviceAccountKey.json"
LOGF = BASE / "tray_app.log"

def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOGF.open("a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")

conf = json.loads(CFG.read_text(encoding="utf-8"))
DATABASE_URL = conf["database_url"].rstrip("/") + "/"
TARGET_UID   = conf["target_uid"]

# === Firebase 初期化 ===
if not firebase_admin._apps:
    cred = credentials.Certificate(str(KEY))
    firebase_admin.initialize_app(cred, {"databaseURL": DATABASE_URL})

ref_phone_to_pc = db.reference(f"clips/{TARGET_UID}/latest")
ref_pc_to_phone = db.reference(f"clips/{TARGET_UID}/pcLatest")

running = True
sync_enabled = True
lock = threading.Lock()

# 送受信の重複回避用
last_phone_ts = 0
last_text_content = None
last_image_hash = None

def get_image_hash(img):
    """画像の同一性を判定するための簡易ハッシュ"""
    return hashlib.md5(img.tobytes()).hexdigest()

def set_image_to_clipboard(img_bytes):
    """画像をWindowsクリップボードにセット(DIB形式)"""
    output = BytesIO()
    img = Image.open(BytesIO(img_bytes))
    img.convert("RGB").save(output, "BMP")
    data = output.getvalue()[14:]  # BMPヘッダー(14byte)を削るとDIBになる
    output.close()

    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_DIB, data)
    finally:
        win32clipboard.CloseClipboard()

def poll_phone_to_pc():
    """スマホ→PC：監視状況を詳しくログに出すバージョン"""
    global last_phone_ts, last_text_content, last_image_hash
    
    # 起動時の確認ログ
    log(f"[DEBUG] Monitoring Path: clips/{TARGET_UID}/latest")
    
    while running:
        try:
            if sync_enabled:
                data = ref_phone_to_pc.get()
                
                if data is None:
                    # ここでログが出すぎるのを防ぐため、時々出す
                    pass 
                elif isinstance(data, dict):
                    ts = int(data.get("ts", 0) or 0)
                    
                    # デバッグ用：データは見えてるけど無視してる場合にログを出す
                    if ts <= last_phone_ts and ts != 0:
                        # log(f"[DEBUG] Data ignored: ts({ts}) is not newer than last({last_phone_ts})")
                        pass

                    if ts > last_phone_ts:
                        log(f"[DEBUG] New data detected! ts={ts}, type={data.get('type')}")
                        last_phone_ts = ts
                        
                        dtype = data.get("type", "text")
                        content = data.get("text", "")
                        
                        if dtype == "image" and content:
                            img_data = base64.b64decode(content)
                            set_image_to_clipboard(img_data)
                            log(f"PHONE->PC IMAGE updated ts={ts}")
                        elif dtype == "text" and content:
                            pyperclip.copy(content)
                            log(f"PHONE->PC TEXT updated ts={ts}")
            
            time.sleep(1.5) # 少しゆっくりにする
        except Exception as e:
            log(f"[ERR] phone->pc loop: {e}")
            time.sleep(2)

def poll_pc_clipboard():
    """PC→スマホ：クリップボードを監視して画像かテキストを送る"""
    global last_text_content, last_image_hash
    log("pc->phone thread started")
    while running:
        try:
            if sync_enabled:
                # 1. まず画像をチェック
                img = ImageGrab.grabclipboard()
                if isinstance(img, Image.Image):
                    current_hash = get_image_hash(img)
                    if current_hash != last_image_hash:
                        # 画像をJPEGでBase64化
                        buf = BytesIO()
                        img.convert("RGB").save(buf, format="JPEG", quality=85)
                        b64_str = base64.b64encode(buf.getvalue()).decode("utf-8")
                        
                        ts = int(time.time() * 1000)
                        payload = {"type": "image", "text": b64_str, "ts": ts}
                        ref_pc_to_phone.set(payload)
                        db.reference(f"clips/{TARGET_UID}/pcHistory").push(payload)
                        
                        last_image_hash = current_hash
                        log(f"PC->PHONE IMAGE sent ts={ts}")
                
                # 2. テキストをチェック
                else:
                    text = pyperclip.paste()
                    if text and text != last_text_content:
                        ts = int(time.time() * 1000)
                        payload = {"type": "text", "text": text, "ts": ts}
                        ref_pc_to_phone.set(payload)
                        db.reference(f"clips/{TARGET_UID}/pcHistory").push(payload)
                        
                        last_text_content = text
                        log(f"PC->PHONE TEXT sent ts={ts} len={len(text)}")
            
            time.sleep(0.8)
        except Exception as e:
            log(f"[ERR] pc->phone: {e}")
            time.sleep(2)

# --- 以下、トレイアイコン等のUI部分は既存の tray_app.py と同じ ---
def make_icon_image():
    size = 64
    img = Image.new("RGBA", (size, size), (0,0,0,0))
    d = ImageDraw.Draw(img)
    d.ellipse((2,2,size-2,size-2), outline=(0,0,0,200), width=3)
    d.line((12,32,40,32), fill=(0,0,0,220), width=5)
    d.polygon([(40,32),(32,26),(32,38)], fill=(0,0,0,220))
    d.line((52,20,24,20), fill=(0,0,0,220), width=5)
    d.polygon([(24,20),(32,14),(32,26)], fill=(0,0,0,220))
    return img

def on_toggle(icon, item):
    global sync_enabled
    sync_enabled = not sync_enabled
    icon.title = f"ClipShare: {'ON' if sync_enabled else 'PAUSE'}"

def on_quit(icon, item):
    global running
    running = False
    icon.stop()

def main():
    t1 = threading.Thread(target=poll_phone_to_pc, daemon=True)
    t2 = threading.Thread(target=poll_pc_clipboard, daemon=True)
    t1.start(); t2.start()

    icon = pystray.Icon(
        "clipshare",
        make_icon_image(),
        title="ClipShare: ON",
        menu=pystray.Menu(
            pystray.MenuItem(lambda item: "一時停止" if sync_enabled else "再開", on_toggle),
            pystray.MenuItem("終了", on_quit)
        )
    )
    icon.run()

if __name__ == "__main__":
    main()