# -*- coding: utf-8 -*-
"""
自動剪片 —— 主流程（v2：貼圖/音樂改從 Google Drive 抓）

流程：
  1. 掃 Drive「01_待處理」找批次資料夾（一個資料夾＝一批）
  2. 開機時先把 Drive 的音樂＋三類貼圖下載到本機（整輪共用，只抓一次）
  3. 由舊到新，一次一批：下載素材 -> 正規化 -> 套10個模板 -> 上傳成品到「02_完成」
  4. 該批原素材搬到「03_已處理素材」
  5. 每批處理完立刻上傳；全部結束發 Discord 通知

單次最多 MAX_BATCHES_PER_RUN 批（GitHub Actions 單次6小時上限的保險）。

素材放 Drive（手機就能加，不用碰 GitHub）：
  自動剪片/音樂/           所有音樂，隨機抽，檔名隨便
  自動剪片/貼圖/浮誇/       愛心星星爆炸
  自動剪片/貼圖/一般/       箭頭標籤
  自動剪片/貼圖/簡約/       低調文青
  空資料夾不會出錯，抽到該類就自動不放貼圖。

環境變數：
  GOOGLE_SHEET_KEY_PATH   服務帳戶 json 金鑰路徑
  DRIVE_ROOT_FOLDER_ID    「自動剪片」資料夾 ID
  DISCORD_WEBHOOK_URL     Discord webhook
  OAUTH_CLIENT_ID / OAUTH_CLIENT_SECRET / OAUTH_REFRESH_TOKEN  你本人上傳授權（成品吃5TB）
"""

import json
import os
import random
import socket
import shutil
import sys
import tempfile
import time
import traceback

import requests
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as UserCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

import render

# ⚠️ 關鍵：Google API 套件預設「沒有逾時」，連線卡住會無限等下去
# （實際發生過：卡在連 Drive 34分鐘完全沒有任何輸出）。
# 設一個全域上限，超過就丟錯誤，不會再無聲卡死。
socket.setdefaulttimeout(90)

_T0 = time.time()


def log(msg):
    """帶經過時間的即時輸出。flush=True 很重要——
    GitHub Actions 的畫面是靠即時輸出更新的，沒有 flush 會全部卡在緩衝區裡，
    看起來就像「程式沒動」，其實只是訊息還沒被吐出來。"""
    print(f"[{time.time()-_T0:6.1f}s] {msg}", flush=True)

GOOGLE_KEY_PATH = (os.environ.get("GOOGLE_SHEET_KEY_PATH") or "").strip()
DISCORD_WEBHOOK_URL = (os.environ.get("DISCORD_WEBHOOK_URL") or "").strip()
OAUTH_CLIENT_ID = (os.environ.get("OAUTH_CLIENT_ID") or "").strip()
OAUTH_CLIENT_SECRET = (os.environ.get("OAUTH_CLIENT_SECRET") or "").strip()
OAUTH_REFRESH_TOKEN = (os.environ.get("OAUTH_REFRESH_TOKEN") or "").strip()
OAUTH_TOKEN_URI = "https://oauth2.googleapis.com/token"


def _clean_folder_id(raw):
    raw = (raw or "").strip().strip('"').strip("'")
    if "/folders/" in raw:
        raw = raw.split("/folders/")[1]
    if "?" in raw:
        raw = raw.split("?")[0]
    return raw.strip("/ ").strip()


DRIVE_ROOT_FOLDER_ID = _clean_folder_id(os.environ.get("DRIVE_ROOT_FOLDER_ID"))

SCOPES = ["https://www.googleapis.com/auth/drive"]
FOLDER_MIME = "application/vnd.google-apps.folder"

INBOX_NAME = "01_待處理"
DONE_NAME = "02_完成"
ARCHIVE_NAME = "03_已處理素材"
MUSIC_NAME = "音樂"
STICKER_NAME = "貼圖"
STICKER_CATEGORIES = ["浮誇", "一般", "簡約"]

MAX_BATCHES_PER_RUN = 8

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")


# ============ 通知 ============

def notify(message):
    print(message)
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": message[:1900]}, timeout=15)
    except Exception as e:
        print(f"⚠️ Discord 通知發送失敗（不影響主流程）：{e}")


# ============ Drive ============

def get_drive():
    creds = service_account.Credentials.from_service_account_file(GOOGLE_KEY_PATH, scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def get_user_drive():
    if not (OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET and OAUTH_REFRESH_TOKEN):
        return None
    creds = UserCredentials(token=None, refresh_token=OAUTH_REFRESH_TOKEN,
                            client_id=OAUTH_CLIENT_ID, client_secret=OAUTH_CLIENT_SECRET,
                            token_uri=OAUTH_TOKEN_URI, scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def find_child(drive, parent_id, name, folder_only=True):
    q = f"'{parent_id}' in parents and name = '{name}' and trashed = false"
    if folder_only:
        q += f" and mimeType = '{FOLDER_MIME}'"
    res = drive.files().list(q=q, fields="files(id,name)", supportsAllDrives=True,
                             includeItemsFromAllDrives=True).execute()
    files = res.get("files", [])
    return files[0]["id"] if files else None


def list_children(drive, parent_id, folder_only=False):
    q = f"'{parent_id}' in parents and trashed = false"
    if folder_only:
        q += f" and mimeType = '{FOLDER_MIME}'"
    res = drive.files().list(q=q, fields="files(id,name,mimeType,createdTime)",
                             orderBy="createdTime", pageSize=200, supportsAllDrives=True,
                             includeItemsFromAllDrives=True).execute()
    return res.get("files", [])


def download_file(drive, file_id, dest_path):
    request = drive.files().get_media(fileId=file_id, supportsAllDrives=True)
    with open(dest_path, "wb") as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()


def create_folder(drive, parent_id, name):
    existing = find_child(drive, parent_id, name)
    if existing:
        return existing
    meta = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
    return drive.files().create(body=meta, fields="id", supportsAllDrives=True).execute()["id"]


def upload_file(drive, parent_id, local_path, mime="video/mp4", using_oauth=False):
    meta = {"name": os.path.basename(local_path), "parents": [parent_id]}
    media = MediaFileUpload(local_path, mimetype=mime, resumable=True)
    try:
        return drive.files().create(body=meta, media_body=media, fields="id",
                                    supportsAllDrives=True).execute()["id"]
    except Exception as e:
        if "storageQuotaExceeded" in str(e) or "quotaExceeded" in str(e):
            if using_oauth:
                raise RuntimeError("Drive 上傳被拒：你本人帳號的儲存空間也滿了。\n"
                                   "解法：清理你 Drive 裡的舊檔案（含清空垃圾桶）。") from e
            raise RuntimeError(
                "Drive 上傳被拒：服務帳戶自己的15GB用完了，而且看起來 OAuth 還沒設好。\n"
                "請確認 OAUTH_CLIENT_ID／OAUTH_CLIENT_SECRET／OAUTH_REFRESH_TOKEN 都設定了。") from e
        raise


def move_folder(drive, folder_id, new_parent_id, old_parent_id):
    drive.files().update(fileId=folder_id, addParents=new_parent_id,
                         removeParents=old_parent_id, fields="id",
                         supportsAllDrives=True).execute()


# ============ 下載音樂與貼圖（整輪共用，只抓一次）============

def download_assets(drive, root_id, dest_dir):
    """下載 Drive 的音樂＋三類貼圖到本機，回傳
    (music_paths, {分類: [貼圖路徑,...]})。
    資料夾不存在或空的都不會報錯，只是那項是空清單。"""
    music_paths = []
    music_id = find_child(drive, root_id, MUSIC_NAME)
    if music_id:
        md = os.path.join(dest_dir, "music")
        os.makedirs(md, exist_ok=True)
        for f in list_children(drive, music_id):
            if f["mimeType"] == FOLDER_MIME:
                continue
            p = os.path.join(md, f["name"])
            try:
                download_file(drive, f["id"], p)
                if render.is_readable(p):
                    music_paths.append(p)
                else:
                    print(f"⚠️ 音樂「{f['name']}」壞檔或格式不對，跳過")
            except Exception as e:
                print(f"⚠️ 下載音樂「{f['name']}」失敗，跳過：{e}")

    sticker_pools = {c: [] for c in STICKER_CATEGORIES}
    sticker_root = find_child(drive, root_id, STICKER_NAME)
    if sticker_root:
        for cat in STICKER_CATEGORIES:
            cat_id = find_child(drive, sticker_root, cat)
            if not cat_id:
                continue
            cd = os.path.join(dest_dir, "stickers", cat)
            os.makedirs(cd, exist_ok=True)
            for f in list_children(drive, cat_id):
                if f["mimeType"] == FOLDER_MIME:
                    continue
                p = os.path.join(cd, f["name"])
                try:
                    download_file(drive, f["id"], p)
                    if render.is_readable(p):
                        sticker_pools[cat].append(p)
                    else:
                        print(f"⚠️ 貼圖「{cat}/{f['name']}」壞檔，跳過")
                except Exception as e:
                    print(f"⚠️ 下載貼圖「{cat}/{f['name']}」失敗，跳過：{e}")

    return music_paths, sticker_pools


# ============ 單一批次 ============

def classify_files(files):
    result = {"material": [], "voice": None, "script": None}
    for f in files:
        if f["mimeType"] == FOLDER_MIME:
            continue
        ext = os.path.splitext(f["name"])[1].lower()
        if ext in render.VIDEO_EXT or ext in render.PHOTO_EXT:
            result["material"].append(f)
        elif ext in render.AUDIO_EXT and result["voice"] is None:
            result["voice"] = f
        elif ext in render.TEXT_EXT and result["script"] is None:
            result["script"] = f
    return result


def process_batch(drive, user_drive, batch, inbox_id, done_id, archive_id,
                  templates, music_paths, sticker_pools):
    name = batch["name"]
    log(f"===== 處理批次「{name}」 =====")
    workdir = tempfile.mkdtemp(prefix="batch_")
    ok, failed = [], []
    up_drive = user_drive if user_drive is not None else drive
    using_oauth = user_drive is not None

    try:
        files = list_children(drive, batch["id"])
        cls = classify_files(files)
        if not cls["material"]:
            raise RuntimeError("資料夾裡沒有影片或照片素材")

        log(f"下載素材 {len(cls['material'])} 個檔案...")
        local_material = []
        for f in cls["material"]:
            p = os.path.join(workdir, f["name"])
            download_file(drive, f["id"], p)
            local_material.append(p)

        voice_path = None
        if cls["voice"]:
            voice_path = os.path.join(workdir, cls["voice"]["name"])
            download_file(drive, cls["voice"]["id"], voice_path)

        subtitle_lines = None
        if cls["script"]:
            sp = os.path.join(workdir, cls["script"]["name"])
            download_file(drive, cls["script"]["id"], sp)
            subtitle_lines = render.read_script_lines(sp)

        log("素材下載完成，開始正規化...")
        voice_dur = render.probe_duration(voice_path) if voice_path else 0.0
        normalized = os.path.join(workdir, "_normalized.mp4")
        duration = render.normalize_material(local_material, normalized, voice_dur)
        log(f"正規化完成：{duration:.2f} 秒"
            f"（語音 {voice_dur:.2f} 秒，字幕 {len(subtitle_lines or [])} 句）")

        out_folder_id = create_folder(up_drive, done_id, name)

        for tpl in templates:
            wd = os.path.join(workdir, f"wd_{tpl['id']}")
            os.makedirs(wd, exist_ok=True)
            out_path = os.path.join(wd, f"{tpl['id']}_{name}.mp4")
            try:
                t0 = time.time()
                log(f"  → 開始 {tpl['id']} {tpl['name']}")
                # 音樂隨機抽（沒音樂就無法做，報明確錯）
                if not music_paths:
                    raise RuntimeError("Drive「音樂」資料夾沒有可用音樂")
                music = random.choice(music_paths)
                # 貼圖：依模板分類抽 0~n 個（空分類自動變沒貼圖）
                pool = sticker_pools.get(tpl.get("sticker_pool"), [])
                stickers = render.pick_stickers(pool, tuple(tpl.get("sticker_count", [0, 0])))

                info = render.apply_template(
                    normalized, duration, tpl, out_path, music,
                    voice_path=voice_path, subtitle_lines=subtitle_lines,
                    sticker_paths=stickers, workdir=wd)
                upload_file(up_drive, out_folder_id, out_path, using_oauth=using_oauth)
                log(f"  ✅ {tpl['id']} 完成並上傳（{info}，{time.time()-t0:.1f}s）")
                ok.append(tpl["id"])
            except Exception as e:
                log(f"  ❌ {tpl['id']} 失敗：{e}")
                failed.append(f"{tpl['id']}({type(e).__name__})")
            finally:
                shutil.rmtree(wd, ignore_errors=True)

        if ok:
            move_folder(drive, batch["id"], archive_id, inbox_id)
        return {"name": name, "ok": ok, "failed": failed, "duration": duration}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ============ 主流程 ============

def main():
    missing = [k for k, v in {"GOOGLE_SHEET_KEY_PATH": GOOGLE_KEY_PATH,
                              "DRIVE_ROOT_FOLDER_ID": DRIVE_ROOT_FOLDER_ID}.items() if not v]
    if missing:
        print(f"❌ 缺少環境變數：{', '.join(missing)}")
        sys.exit(1)

    templates = render.load_templates(TEMPLATES_DIR)
    if not templates:
        print("❌ templates 資料夾裡沒有任何模板 json")
        sys.exit(1)
    log(f"已載入 {len(templates)} 個模板：{'、'.join(t['id'] for t in templates)}")

    log("建立服務帳戶連線...")
    drive = get_drive()
    log("服務帳戶連線完成")

    with open(GOOGLE_KEY_PATH, encoding="utf-8") as f:
        sa_email = json.load(f).get("client_email", "(讀不到)")
    log("查詢根資料夾（若卡在這裡代表連 Drive 有問題）...")
    try:
        root = drive.files().get(fileId=DRIVE_ROOT_FOLDER_ID, fields="id,name,mimeType",
                                 supportsAllDrives=True).execute()
    except Exception as e:
        print("❌ 服務帳戶看不到你設定的根資料夾。")
        print(f"   資料夾ID：{DRIVE_ROOT_FOLDER_ID!r}（長度 {len(DRIVE_ROOT_FOLDER_ID)}）")
        print(f"   服務帳戶：{sa_email}")
        print("   請確認：1.資料夾有共用給服務帳戶(編輯者) 2.ID正確 3.Drive API已啟用")
        print(f"   原始錯誤：{e}")
        sys.exit(1)
    if root["mimeType"] != FOLDER_MIME:
        print(f"❌ 這個ID指到的是檔案不是資料夾：{root['name']}")
        sys.exit(1)
    log(f"根資料夾確認：「{root['name']}」")

    user_drive = get_user_drive()
    if user_drive is not None:
        try:
            user_drive.files().get(fileId=DRIVE_ROOT_FOLDER_ID, fields="id").execute()
            log("上傳身分：你本人帳號（成品算你的 5TB）")
        except Exception as e:
            print(f"⚠️ OAuth 連線建立了但看不到根資料夾，退回服務帳戶上傳。原因：{e}")
            user_drive = None
    else:
        log("上傳身分：服務帳戶（⚠️ 只有15GB，OAuth 尚未設定）")

    inbox_id = find_child(drive, DRIVE_ROOT_FOLDER_ID, INBOX_NAME)
    done_id = find_child(drive, DRIVE_ROOT_FOLDER_ID, DONE_NAME)
    archive_id = find_child(drive, DRIVE_ROOT_FOLDER_ID, ARCHIVE_NAME)
    for nm, fid in [(INBOX_NAME, inbox_id), (DONE_NAME, done_id), (ARCHIVE_NAME, archive_id)]:
        if not fid:
            print(f"❌ 根資料夾底下找不到「{nm}」，請先建好（名稱一字不差）")
            sys.exit(1)

    batches = list_children(drive, inbox_id, folder_only=True)
    if not batches:
        print("待處理資料夾是空的，這次沒事做。")
        return

    todo_count = 0
    asset_dir = tempfile.mkdtemp(prefix="assets_")
    try:
        log("開始下載音樂與貼圖...")
        music_paths, sticker_pools = download_assets(drive, DRIVE_ROOT_FOLDER_ID, asset_dir)
        pool_summary = "、".join(f"{c}:{len(sticker_pools[c])}張" for c in STICKER_CATEGORIES)
        log(f"素材下載完成：音樂 {len(music_paths)} 首｜貼圖 {pool_summary}")
        if not music_paths:
            notify("❌ Drive「音樂」資料夾沒有可用音樂，無法出片。請上傳可商用音檔到「自動剪片/音樂/」。")
            return

        todo = batches[:MAX_BATCHES_PER_RUN]
        todo_count = len(todo)
        log(f"待處理 {len(batches)} 批，本次處理 {todo_count} 批")

        results = []
        for b in todo:
            try:
                results.append(process_batch(drive, user_drive, b, inbox_id, done_id,
                                             archive_id, templates, music_paths, sticker_pools))
            except Exception as e:
                traceback.print_exc()
                results.append({"name": b["name"], "ok": [], "failed": [f"整批失敗：{e}"], "duration": 0})
    finally:
        shutil.rmtree(asset_dir, ignore_errors=True)

    lines = ["🎬 **自動剪片完成**"]
    for r in results:
        status = f"✅ {len(r['ok'])} 支"
        if r["failed"]:
            status += f"　❌ 失敗：{', '.join(r['failed'])}"
        lines.append(f"・`{r['name']}`（{r['duration']:.1f}秒）→ {status}")
    remaining = len(batches) - todo_count
    if remaining > 0:
        lines.append(f"\n還有 {remaining} 批排隊中，下一輪會繼續處理。")
    lines.append("成品在 Drive 的「02_完成」，記得下載完刪掉。")
    notify("\n".join(lines))


if __name__ == "__main__":
    main()
  
