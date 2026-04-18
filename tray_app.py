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

BASE = Path(__file__).resolve().parent
CFG  = BASE / "config.json"
KEY  = BASE / "serviceAccountKey.json"
LOGF = BASE / "tray_app.log"

MAX_TEXT_HISTORY = 30

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

if not firebase_admin._apps:
    cred = credentials.Certificate(str(KEY))
    firebase_admin.initialize_app(cred, {"databaseURL": DATABASE_URL})

ref_phone_to_pc = db.reference(f"clips/{TARGET_UID}/latest")
ref_pc_to_phone = db.reference(f"clips/{TARGET_UID}/pcLatest")
# [変更] pcHistory → pcText / pcImage に分離
ref_pc_text_history  = db.reference(f"clips/{TARGET_UID}/pcText")
ref_pc_image_history = db.reference(f"clips/{TARGET_UID}/pcImage")

running = True
sync_enabled = True

last_phone_ts = 0
last_text_content = None
last_image_hash = None
listener_registration = None


def get_image_hash(img):
    return hashlib.md5(img.tobytes()).hexdigest()


def set_image_to_clipboard(img_bytes):
    output = BytesIO()
    img = Image.open(BytesIO(img_bytes))
    img.convert("RGB").save(output, "BMP")
    data = output.getvalue()[14:]
    output.close()
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_DIB, data)
    finally:
        win32clipboard.CloseClipboard()


def trim_text_history():
    """テキスト履歴を MAX_TEXT_HISTORY 件に制限する"""
    try:
        snapshot = ref_pc_text_history.order_by_key().get()
        if snapshot and len(snapshot) > MAX_TEXT_HISTORY:
            keys_to_delete = list(snapshot.keys())[: len(snapshot) - MAX_TEXT_HISTORY]
            for k in keys_to_delete:
                ref_pc_text_history.child(k).delete()
            log(f"[TRIM] pcText を {len(keys_to_delete)} 件削除しました")
    except Exception as e:
        log(f"[ERR] trim_text_history: {e}")


def on_phone_update(event):
    global last_phone_ts, last_text_content, last_image_hash
    try:
        if not sync_enabled:
            return
        data = event.data
        if isinstance(data, dict):
            ts = int(data.get("ts", 0) or 0)
            if ts > last_phone_ts:
                last_phone_ts = ts
                dtype = data.get("type", "text")
                content = data.get("text", "")

                if dtype == "image" and content:
                    img_bytes = base64.b64decode(content)
                    with Image.open(BytesIO(img_bytes)) as temp_img:
                        last_image_hash = get_image_hash(temp_img)
                    set_image_to_clipboard(img_bytes)
                    log(f"PHONE->PC IMAGE synced (ts={ts})")
                    # ノード全体を空に置き換え
                    ref_phone_to_pc.set({"type": "text", "text": "", "ts": ts})

                elif dtype == "text" and content:
                    last_text_content = content
                    pyperclip.copy(content)
                    log(f"PHONE->PC TEXT synced (ts={ts})")

    except Exception as e:
        log(f"[ERR] phone->pc listener: {e}")


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
                        img.convert("RGB").save(buf, format="JPEG", quality=95)
                        b64_str = base64.b64encode(buf.getvalue()).decode("utf-8")
                        ts = int(time.time() * 1000)

                        payload = {"type": "image", "text": b64_str, "ts": ts}
                        ref_pc_to_phone.set(payload)

                        # [変更] 画像履歴は pcImage に push（スマホ側でDL後に削除される）
                        ref_pc_image_history.push(payload)

                        last_image_hash = current_hash
                        data_size_kb = len(b64_str) / 1024
                        log(f"PC->PHONE IMAGE sent: size={img.size}, payload={data_size_kb:.2f} KB (ts={ts})")

                else:
                    text = pyperclip.paste()
                    if text and text != last_text_content:
                        ts = int(time.time() * 1000)
                        payload = {"type": "text", "text": text, "ts": ts}

                        ref_pc_to_phone.set(payload)

                        # [変更] テキスト履歴は pcText に push
                        ref_pc_text_history.push(payload)
                        threading.Thread(target=trim_text_history, daemon=True).start()

                        last_text_content = text
                        data_size_kb = len(text) / 1024
                        log(f"PC->PHONE TEXT sent: len={len(text)}, payload={data_size_kb:.2f} KB (ts={ts})")

            time.sleep(1.0)
        except Exception as e:
            log(f"[ERR] pc->phone: {e}")
            time.sleep(2)


def make_icon_image():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((5, 5, 59, 59), outline=(0, 120, 215, 255), width=4)
    d.text((15, 15), "CS", fill=(0, 120, 215, 255))
    return img


def main():
    global listener_registration, running
    log(f"[START] Listening for changes on: clips/{TARGET_UID}/latest")
    listener_registration = ref_phone_to_pc.listen(on_phone_update)
    threading.Thread(target=poll_pc_clipboard, daemon=True).start()

    def on_exit(icon, item):
        global running
        running = False
        if listener_registration:
            listener_registration.close()
        icon.stop()

    icon = pystray.Icon("clipshare", make_icon_image(), "ClipShare", menu=pystray.Menu(
        pystray.MenuItem("終了", on_exit)
    ))
    icon.run()


if __name__ == "__main__":
    main()
