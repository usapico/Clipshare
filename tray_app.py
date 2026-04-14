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

# パスの定義
ref_phone_to_pc = db.reference(f"clips/{TARGET_UID}/latest")
ref_pc_to_phone = db.reference(f"clips/{TARGET_UID}/pcLatest")
ref_pc_history  = db.reference(f"clips/{TARGET_UID}/pcHistory")

running = True
sync_enabled = True

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
    """スマホ→PC：スマホ側の更新を監視してPCのクリップボードに反映"""
    global last_phone_ts, last_text_content, last_image_hash
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
                            img_bytes = base64.b64decode(content)
                            # PC側スレッドが「新しい画像だ！」と誤検知して投げ返さないようにハッシュを記録
                            with Image.open(BytesIO(img_bytes)) as temp_img:
                                last_image_hash = get_image_hash(temp_img)
                            
                            set_image_to_clipboard(img_bytes)
                            log(f"PHONE->PC IMAGE synced (ts={ts})")
                        elif dtype == "text" and content:
                            # PC側スレッドが「新しいテキストだ！」と誤検知して投げ返さないように内容を記録
                            last_text_content = content
                            
                            pyperclip.copy(content)
                            log(f"PHONE->PC TEXT synced (ts={ts})")
            time.sleep(1.5)
        except Exception as e:
            log(f"[ERR] phone->pc: {e}")
            time.sleep(2)

def poll_pc_clipboard():
    """PC→スマホ：PCのクリップボードを監視してスマホ側へ送信"""
    global last_text_content, last_image_hash
    while running:
        try:
            if sync_enabled:
                # 1. 画像のチェック
                img = ImageGrab.grabclipboard()
                if isinstance(img, Image.Image):
                    current_hash = get_image_hash(img)
                    if current_hash != last_image_hash:
                        buf = BytesIO()
                        # 高画質設定（quality=95）で JPEG 化
                        img.convert("RGB").save(buf, format="JPEG", quality=95)
                        b64_str = base64.b64encode(buf.getvalue()).decode("utf-8")
                        ts = int(time.time() * 1000)
                        
                        payload = {"type": "image", "text": b64_str, "ts": ts}
                        # 最新情報と履歴の両方にセット
                        ref_pc_to_phone.set(payload)
                        ref_pc_history.push(payload)
                        
                        last_image_hash = current_hash
                        # 送信サイズをログに出力 (KB単位)
                        data_size_kb = len(b64_str) / 1024
                        log(f"PC->PHONE IMAGE sent: size={img.size}, payload={data_size_kb:.2f} KB (ts={ts})")
                
                # 2. テキストのチェック
                else:
                    text = pyperclip.paste()
                    if text and text != last_text_content:
                        ts = int(time.time() * 1000)
                        payload = {"type": "text", "text": text, "ts": ts}
                        
                        ref_pc_to_phone.set(payload)
                        ref_pc_history.push(payload)
                        
                        last_text_content = text
                        # 送信サイズをログに出力 (KB単位)
                        data_size_kb = len(text) / 1024
                        log(f"PC->PHONE TEXT sent: len={len(text)}, payload={data_size_kb:.2f} KB (ts={ts})")
            time.sleep(1.0)
        except Exception as e:
            log(f"[ERR] pc->phone: {e}")
            time.sleep(2)

def make_icon_image():
    """システムトレイ用の簡易アイコン画像を作成"""
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
