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

import os
import shutil
import sys
import tempfile
import time
import traceback

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

import render

GOOGLE_KEY_PATH = os.environ.get("GOOGLE_SHEET_KEY_PATH")
DRIVE_ROOT_FOLDER_ID = os.environ.get("DRIVE_ROOT_FOLDER_ID")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

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
    creds = service_account.Credentials.from_service_account_file(
        GOOGLE_KEY_PATH, scopes=SCOPES)
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


def upload_file(drive, parent_id: str, local_path: str, mime="video/mp4") -> str:
    meta = {"name": os.path.basename(local_path), "parents": [parent_id]}
    media = MediaFileUpload(local_path, mimetype=mime, resumable=True)
    try:
        return drive.files().create(body=meta, media_body=media, fields="id",
                                    supportsAllDrives=True).execute()["id"]
    except Exception as e:
        if "storageQuotaExceeded" in str(e) or "quotaExceeded" in str(e):
            raise RuntimeError(
                "Drive 上傳被拒：服務帳戶自己的儲存空間額度用完了。\n"
                "（服務帳戶上傳的檔案算在它自己的額度，不是算你的5TB，這是Google的規則）\n"
                "解法：把「02_完成」裡的舊成品刪掉並清空垃圾桶；"
                "如果常常滿，就要改成用你本人帳號的 OAuth 授權。") from e
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


def process_batch(drive, batch, inbox_id, done_id, archive_id, templates) -> dict:
    """處理一批素材，回傳結果摘要。"""
    name = batch["name"]
    print(f"\n===== 處理批次「{name}」 =====")
    workdir = tempfile.mkdtemp(prefix="batch_")
    ok, failed = [], []

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

        out_folder_id = create_folder(drive, done_id, name)

        for tpl in templates:
            out_path = os.path.join(workdir, f"{tpl['id']}_{name}.mp4")
            try:
                t0 = time.time()
                render.apply_template(
                    normalized, duration, tpl, ASSETS_DIR, out_path,
                    voice_path=voice_path, subtitle_lines=subtitle_lines)
                upload_file(drive, out_folder_id, out_path)
                os.remove(out_path)     # 上傳完就刪本地檔，免得暫存空間爆掉
                print(f"  ✅ {tpl['id']} {tpl['name']}（{time.time() - t0:.1f}s）")
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

    drive = get_drive()
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
            results.append(process_batch(drive, b, inbox_id, done_id, archive_id, templates))
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
