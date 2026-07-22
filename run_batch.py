# -*- coding: utf-8 -*-
"""
自動剪片 —— 主流程

執行一次會做：
  1. 到 Google Drive 的「01_待處理」找批次資料夾（一個資料夾＝一批素材）
  2. 由舊到新，一次處理一批：下載素材 -> 正規化 -> 套用所有模板 -> 上傳成品到「02_完成」
  3. 該批的原素材資料夾搬到「03_已處理素材」
  4. 每批處理完立刻上傳（就算後面被中斷，前面的成果也不會白做）
  5. 全部結束後發 Discord 通知

單次最多處理 MAX_BATCHES_PER_RUN 批，剩下的等下一輪觸發再接手
（GitHub Actions 單次執行有6小時上限，這是避免撞到上限的保險）。

環境變數：
  GOOGLE_SHEET_KEY_PATH   服務帳戶 json 金鑰檔路徑（沿用現有那組服務帳戶即可）
  DRIVE_ROOT_FOLDER_ID    Drive 上「自動剪片」資料夾的 ID
  DISCORD_WEBHOOK_URL     Discord 通知用的 webhook 網址
"""

import json
import os
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

GOOGLE_KEY_PATH = (os.environ.get("GOOGLE_SHEET_KEY_PATH") or "").strip()
DISCORD_WEBHOOK_URL = (os.environ.get("DISCORD_WEBHOOK_URL") or "").strip()

# 上傳成品用「你本人」的 OAuth 授權（吃你的 5TB），其他讀寫維持服務帳戶。
# 這三個值由本機跑一次 get_token.py 取得後，存進 GitHub Secrets。
OAUTH_CLIENT_ID = (os.environ.get("OAUTH_CLIENT_ID") or "").strip()
OAUTH_CLIENT_SECRET = (os.environ.get("OAUTH_CLIENT_SECRET") or "").strip()
OAUTH_REFRESH_TOKEN = (os.environ.get("OAUTH_REFRESH_TOKEN") or "").strip()
OAUTH_TOKEN_URI = "https://oauth2.googleapis.com/token"


def _clean_folder_id(raw: str) -> str:
    """把使用者可能貼錯的格式清成純資料夾ID。

    常見貼錯：整條網址、尾巴帶 ?usp=drive_link、前後有空白或換行
    （GitHub Secrets 會原封不動保留空白，這種錯誤很難從錯誤訊息看出來）。
    """
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

MAX_BATCHES_PER_RUN = 8

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
ASSETS_DIR = os.path.join(BASE_DIR, "assets")


# ============ 通知 ============

def notify(message: str):
    print(message)
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": message[:1900]}, timeout=15)
    except Exception as e:
        print(f"⚠️ Discord 通知發送失敗（不影響主流程）：{e}")


# ============ Google Drive ============

def get_drive():
    """服務帳戶的 Drive 連線：負責讀素材、建資料夾、搬資料夾。"""
    creds = service_account.Credentials.from_service_account_file(
        GOOGLE_KEY_PATH, scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def get_user_drive():
    """你本人 OAuth 的 Drive 連線：只負責上傳成品（吃你的 5TB）。

    沒設定 OAuth 三個環境變數時回傳 None，上傳會退回服務帳戶
    （這樣就算還沒設好 OAuth，其他功能也不會壞）。
    """
    if not (OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET and OAUTH_REFRESH_TOKEN):
        return None
    creds = UserCredentials(
        token=None,
        refresh_token=OAUTH_REFRESH_TOKEN,
        client_id=OAUTH_CLIENT_ID,
        client_secret=OAUTH_CLIENT_SECRET,
        token_uri=OAUTH_TOKEN_URI,
        scopes=SCOPES,
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def find_child(drive, parent_id: str, name: str, folder_only=True):
    q = (f"'{parent_id}' in parents and name = '{name}' and trashed = false")
    if folder_only:
        q += f" and mimeType = '{FOLDER_MIME}'"
    res = drive.files().list(q=q, fields="files(id,name)",
                             supportsAllDrives=True,
                             includeItemsFromAllDrives=True).execute()
    files = res.get("files", [])
    return files[0]["id"] if files else None


def list_children(drive, parent_id: str, folder_only=False):
    q = f"'{parent_id}' in parents and trashed = false"
    if folder_only:
        q += f" and mimeType = '{FOLDER_MIME}'"
    res = drive.files().list(
        q=q, fields="files(id,name,mimeType,createdTime)",
        orderBy="createdTime", pageSize=200,
        supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
    return res.get("files", [])


def download_file(drive, file_id: str, dest_path: str):
    request = drive.files().get_media(fileId=file_id, supportsAllDrives=True)
    with open(dest_path, "wb") as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()


def create_folder(drive, parent_id: str, name: str) -> str:
    existing = find_child(drive, parent_id, name)
    if existing:
        return existing
    meta = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
    return drive.files().create(body=meta, fields="id",
                                supportsAllDrives=True).execute()["id"]


def upload_file(drive, parent_id: str, local_path: str, mime="video/mp4",
                using_oauth=False) -> str:
    meta = {"name": os.path.basename(local_path), "parents": [parent_id]}
    media = MediaFileUpload(local_path, mimetype=mime, resumable=True)
    try:
        return drive.files().create(body=meta, media_body=media, fields="id",
                                    supportsAllDrives=True).execute()["id"]
    except Exception as e:
        if "storageQuotaExceeded" in str(e) or "quotaExceeded" in str(e):
            if using_oauth:
                raise RuntimeError(
                    "Drive 上傳被拒：你本人帳號的儲存空間也滿了。\n"
                    "解法：清理你 Drive 裡的舊檔案（含清空垃圾桶）。") from e
            raise RuntimeError(
                "Drive 上傳被拒：服務帳戶自己的儲存空間額度用完了。\n"
                "（服務帳戶上傳的檔案算它自己的額度，不是你的5TB，這是Google的規則）\n"
                "看起來 OAuth 還沒設定好，所以退回用服務帳戶上傳。\n"
                "請確認 OAUTH_CLIENT_ID／OAUTH_CLIENT_SECRET／OAUTH_REFRESH_TOKEN "
                "三個 Secret 都設好了。") from e
        raise


def move_folder(drive, folder_id: str, new_parent_id: str, old_parent_id: str):
    drive.files().update(fileId=folder_id, addParents=new_parent_id,
                         removeParents=old_parent_id, fields="id",
                         supportsAllDrives=True).execute()


# ============ 單一批次處理 ============

def classify_files(files: list) -> dict:
    """依副檔名分類，使用者不用改檔名。"""
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
                  templates, good_assets) -> dict:
    """處理一批素材，回傳結果摘要。

    drive       ：服務帳戶連線，負責讀素材、搬資料夾
    user_drive  ：你本人 OAuth 連線，負責建立成品資料夾＋上傳成品（吃你的5TB）。
                  沒設定 OAuth 時是 None，會退回用服務帳戶上傳。
    """
    name = batch["name"]
    print(f"\n===== 處理批次「{name}」 =====")
    workdir = tempfile.mkdtemp(prefix="batch_")
    ok, failed = [], []

    # 上傳端：有 OAuth 就用你本人帳號，沒有就退回服務帳戶
    up_drive = user_drive if user_drive is not None else drive
    using_oauth = user_drive is not None

    try:
        files = list_children(drive, batch["id"])
        cls = classify_files(files)
        if not cls["material"]:
            raise RuntimeError("資料夾裡沒有影片或照片素材")

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

        voice_dur = render.probe_duration(voice_path) if voice_path else 0.0
        normalized = os.path.join(workdir, "_normalized.mp4")
        duration = render.normalize_material(local_material, normalized, voice_dur)
        print(f"素材正規化完成：{duration:.2f} 秒"
              f"（語音 {voice_dur:.2f} 秒，字幕 {len(subtitle_lines or [])} 句）")

        # 成品資料夾用「上傳端」建立，讓資料夾跟裡面的檔案同屬一個擁有者，
        # 避免服務帳戶建的資料夾、你本人帳號卻沒權限寫進去的錯亂
        out_folder_id = create_folder(up_drive, done_id, name)

        for tpl in templates:
            out_path = os.path.join(workdir, f"{tpl['id']}_{name}.mp4")
            try:
                t0 = time.time()
                picked = render.apply_template(
                    normalized, duration, tpl, ASSETS_DIR, out_path,
                    voice_path=voice_path, subtitle_lines=subtitle_lines,
                    good_assets=good_assets)
                upload_file(up_drive, out_folder_id, out_path, using_oauth=using_oauth)
                os.remove(out_path)     # 上傳完就刪本地檔，免得暫存空間爆掉
                print(f"  ✅ {tpl['id']} {tpl['name']}（{picked}，{time.time() - t0:.1f}s）")
                ok.append(tpl["id"])
            except Exception as e:
                print(f"  ❌ {tpl['id']} {tpl['name']} 失敗：{e}")
                failed.append(f"{tpl['id']}({type(e).__name__})")

        if ok:
            move_folder(drive, batch["id"], archive_id, inbox_id)

        return {"name": name, "ok": ok, "failed": failed, "duration": duration}

    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ============ 主流程 ============

def main():
    missing = [k for k, v in {
        "GOOGLE_SHEET_KEY_PATH": GOOGLE_KEY_PATH,
        "DRIVE_ROOT_FOLDER_ID": DRIVE_ROOT_FOLDER_ID,
    }.items() if not v]
    if missing:
        print(f"❌ 缺少環境變數：{', '.join(missing)}")
        sys.exit(1)

    templates = render.load_templates(TEMPLATES_DIR)
    if not templates:
        print("❌ templates 資料夾裡沒有任何模板 json")
        sys.exit(1)
    print(f"已載入 {len(templates)} 個模板：{'、'.join(t['id'] for t in templates)}")

    # 開跑前檢查每個模板要用的音樂/貼圖：
    #   1. 檔案在不在
    #   2. ffmpeg 讀不讀得動（擋「副檔名對但檔案壞掉/不是真的該格式」）
    # 分兩種嚴重度：
    #   缺檔／指定的檔案全壞 -> 這個模板沒救，直接停下來（missing）
    #   清單裡有壞檔但還有好的 -> 只警告、把壞檔踢出隨機池，繼續跑（bad_but_ok）
    good_assets = set()
    missing = []          # 完全找不到，或某模板的音樂/某類貼圖「全部」都壞
    bad_files = []        # 個別壞檔（清單裡還有其他好的可以頂替）

    def check_pool(tid, kind, names):
        """檢查一組候選檔案，回傳這組裡「好的」檔名清單。"""
        good_here = []
        for fn in names:
            if not fn:
                continue
            path = os.path.join(ASSETS_DIR, fn)
            if not os.path.exists(path):
                bad_files.append(f"{tid} {kind}：assets/{fn} 不存在")
            elif not render.is_readable(path):
                bad_files.append(f"{tid} {kind}：assets/{fn} 壞檔或格式不對，ffmpeg 讀不動")
            else:
                good_here.append(fn)
                good_assets.add(fn)
        return good_here

    for t in templates:
        music_names = render.asset_candidates(t.get("music")) or [None]
        good_music = check_pool(t["id"], "音樂", music_names)
        if not good_music:
            missing.append(f"{t['id']}：沒有任何一個音樂檔可用（music 欄位）")

        if (t.get("sticker") or {}).get("enabled"):
            sticker_names = render.asset_candidates(t["sticker"]["file"])
            good_sticker = check_pool(t["id"], "貼圖", sticker_names)
            if not good_sticker:
                missing.append(f"{t['id']}：沒有任何一個貼圖檔可用（sticker.file）")

    if missing:
        have = sorted(os.listdir(ASSETS_DIR)) if os.path.isdir(ASSETS_DIR) else []
        msg = ("❌ 有模板缺少必要素材，無法出片，先補齊再跑：\n  "
               + "\n  ".join(missing))
        if bad_files:
            msg += "\n\n（另外偵測到這些壞檔）：\n  " + "\n  ".join(bad_files)
        msg += (f"\n\n  assets 資料夾目前有：{'、'.join(have) if have else '（空的）'}"
                "\n  音樂請準備可商用音檔上傳到 repo 的 assets/；"
                "不想要貼圖就把模板 json 裡 sticker 的 enabled 改成 false。")
        notify(msg)
        sys.exit(1)

    if bad_files:
        # 有壞檔但每個模板都還有好的可用 -> 只提醒，不中斷
        notify("⚠️ 偵測到壞檔，已自動跳過（不影響出片，但建議換掉）：\n  "
               + "\n  ".join(bad_files))

    drive = get_drive()

    # 開跑前先確認「根資料夾看得到」，不然後面的404錯誤訊息完全看不出原因
    with open(GOOGLE_KEY_PATH, encoding="utf-8") as f:
        sa_email = json.load(f).get("client_email", "(讀不到)")
    try:
        root = drive.files().get(fileId=DRIVE_ROOT_FOLDER_ID,
                                 fields="id,name,mimeType",
                                 supportsAllDrives=True).execute()
    except Exception as e:
        print("❌ 服務帳戶看不到你設定的根資料夾。")
        print(f"   收到的資料夾ID：{DRIVE_ROOT_FOLDER_ID!r}（長度 {len(DRIVE_ROOT_FOLDER_ID)}）")
        print(f"   服務帳戶：{sa_email}")
        print("   Drive 對「沒權限」跟「不存在」都回 404，所以請依序檢查：")
        print("   1. 「自動剪片」資料夾有沒有共用給上面那個服務帳戶，權限是不是「編輯者」")
        print("   2. 資料夾ID是不是只有網址 /folders/ 後面那一段（正常長度約33字元）")
        print("   3. Google Drive API 是不是啟用在「服務帳戶所屬的那個專案」")
        print(f"\n   原始錯誤：{e}")
        sys.exit(1)

    if root["mimeType"] != FOLDER_MIME:
        print(f"❌ 這個ID指到的是檔案不是資料夾：{root['name']}")
        sys.exit(1)
    print(f"根資料夾確認：「{root['name']}」")

    # 建立你本人 OAuth 的上傳連線（成品吃你的5TB）。沒設定就 None，退回服務帳戶。
    user_drive = get_user_drive()
    if user_drive is not None:
        try:
            user_drive.files().get(fileId=DRIVE_ROOT_FOLDER_ID, fields="id").execute()
            print("上傳身分：你本人帳號（成品算你的 5TB）")
        except Exception as e:
            print(f"⚠️ OAuth 連線建立了但看不到根資料夾，退回用服務帳戶上傳。原因：{e}")
            user_drive = None
    else:
        print("上傳身分：服務帳戶（⚠️ 只有15GB，OAuth 尚未設定）")

    inbox_id = find_child(drive, DRIVE_ROOT_FOLDER_ID, INBOX_NAME)
    done_id = find_child(drive, DRIVE_ROOT_FOLDER_ID, DONE_NAME)
    archive_id = find_child(drive, DRIVE_ROOT_FOLDER_ID, ARCHIVE_NAME)
    for nm, fid in [(INBOX_NAME, inbox_id), (DONE_NAME, done_id), (ARCHIVE_NAME, archive_id)]:
        if not fid:
            print(f"❌ 在根資料夾底下找不到「{nm}」資料夾，請先建好（名稱要一字不差）")
            sys.exit(1)

    batches = list_children(drive, inbox_id, folder_only=True)
    if not batches:
        print("待處理資料夾是空的，這次沒事做。")
        return

    todo = batches[:MAX_BATCHES_PER_RUN]
    print(f"待處理 {len(batches)} 批，本次處理 {len(todo)} 批。")

    results = []
    for b in todo:
        try:
            results.append(process_batch(drive, user_drive, b, inbox_id, done_id,
                                         archive_id, templates, good_assets))
        except Exception as e:
            traceback.print_exc()
            results.append({"name": b["name"], "ok": [], "failed": [f"整批失敗：{e}"],
                            "duration": 0})

    lines = ["🎬 **自動剪片完成**"]
    for r in results:
        status = f"✅ {len(r['ok'])} 支"
        if r["failed"]:
            status += f"　❌ 失敗：{', '.join(r['failed'])}"
        lines.append(f"・`{r['name']}`（{r['duration']:.1f}秒）→ {status}")
    remaining = len(batches) - len(todo)
    if remaining > 0:
        lines.append(f"\n還有 {remaining} 批排隊中，下一輪會繼續處理。")
    lines.append("成品在 Drive 的「02_完成」，記得下載完刪掉。")
    notify("\n".join(lines))


if __name__ == "__main__":
    main()
