# -*- coding: utf-8 -*-
"""
模板匯入工具:把剪輯軟體專案檔轉成本系統的「時間軸模板」json。
支援:威力導演 .pdrproj(或整個匯出 zip,自動找裡面的 pdrproj)

用法:python template_import.py 專案.pdrproj(或.zip) templates/template_P01.json

特效對照表 FX_MAP:左=威力導演內部名稱(OverlayFx 用 名稱#scriptLocation尾碼),
右=render.py TIMELINE_FX 的鍵。特殊值 "@stickers" 表示該特效是粒子雨
(彩帶/表情飛落),威力導演的內建素材檔不在匯出包裡拿不到,
改成「整支影片用使用者自己 Drive 的浮誇貼圖庫加強飄落」近似,該段畫面照原片播。
遇到沒見過的特效 → 標記 unsupported,執行時跳過該段並在通知點名。
"""

import json
import os
import struct
import sys
import tempfile
import zipfile

FX_MAP = {
    "CircleOpenFx": "circle_open",         # 圓圈擴大聚焦
    "TvWall": "grid",                      # 電視牆(參數決定幾格)
    "MultiScreenFx_01": "grid3",           # 多分割 → 3×3
    "FourScreenSlideFx": "grid2",          # 四分割 → 2×2
    "Rocking02Fx": "rocking",              # 搖晃
    "ContinuousShooting": "stutter",       # 連拍頓格
    "SpinInCircleFx": "spin_in",           # 旋轉入場
    # 依實際截圖確認(2026-07-23):
    "OverlayFx#pdrm_VideoEffect_Atmosphere_06": "@stickers",  # 彩帶紙屑灑落
    "OverlayFx#pdrm_VideoEffect_Light_14": "light_leak",      # 粉紫光暈罩畫面
    "OverlayFx#pdrm_VideoEffect_Variety_03": "@stickers",     # 表情/愛心飛過
    "OverlayFx#pdrm_VideoEffect_Variety_08": "light_rays",    # 白色光芒射線
}


def _load_pdrproj(path):
    if path.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            cands = [n for n in z.namelist() if n.lower().endswith(".pdrproj")]
            if not cands:
                raise RuntimeError("zip 裡沒有 .pdrproj 檔")
            return json.loads(z.read(cands[0]).decode("utf-8"))
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _fx_key(glfx):
    name = glfx.get("name", "")
    loc = glfx.get("scriptLocation", "")
    return f"{name}#{os.path.basename(loc)}" if loc else name


def _decode_color(color_int):
    """威力導演顏色是 int32 ARGB,轉成 ffmpeg 的 0xRRGGBB 字串"""
    b = struct.pack(">i", color_int)
    _, r, g, bv = b[0], b[1], b[2], b[3]
    return f"0x{r:02x}{g:02x}{bv:02x}"


def _parse_texts(d, zip_path=None):
    """掃所有軌道找文字方塊,回傳 list of dict。
    座標 cx/cy 是畫面中心點的 0~1 相對值(直接從 coordinates 取)。
    font_path 是本機可用路徑(zip 裡附的字體就複製出來),找不到就 None。
    """
    texts = []

    # 先把 zip 裡的字體解壓到暫存資料夾
    font_dir = None
    if zip_path and zip_path.lower().endswith(".zip"):
        font_dir = tempfile.mkdtemp(prefix="pdr_fonts_")
        with zipfile.ZipFile(zip_path) as z:
            for name in z.namelist():
                if name.lower().endswith((".otf", ".ttf")):
                    target = os.path.join(font_dir, os.path.basename(name))
                    with open(target, "wb") as f:
                        f.write(z.read(name))

    for tr in d.get("tracks", []):
        for u in tr.get("timelineUnit", []):
            b, e = u.get("beginUs", 0) / 1e6, u.get("endUs", 0) / 1e6
            if e <= b:
                continue
            clip = u.get("timelineClip", {}) or {}
            text = clip.get("text", "").strip()
            if not text:
                continue
            coords = clip.get("coordinates", {})
            if not coords:
                continue

            cx = coords.get("x", 0.5)
            cy = coords.get("y", 0.5)
            norm_size = clip.get("normFontSize", 0.08)
            font_size_px = max(24, int(norm_size * 1920))

            # 顏色從 faceColorSetting(威力導演的 int32 ARGB)
            color_hex = "0xFFFFFF"
            face = clip.get("faceColorSetting", {})
            colors = face.get("colors", [])
            if colors:
                try:
                    color_hex = _decode_color(colors[0])
                except Exception:
                    pass

            # 字體:優先用 zip 裡附的字體檔
            font_name = clip.get("fontName", "")
            font_path = None
            if font_dir and font_name:
                for fname in os.listdir(font_dir):
                    if font_name.lower() in fname.lower():
                        font_path = os.path.join(font_dir, fname)
                        break

            texts.append({
                "text": text,
                "cx": round(cx, 4),
                "cy": round(cy, 4),
                "font_size": font_size_px,
                "color": color_hex,
                "start": round(b, 2),
                "end": round(e, 2),
                "font_name": font_name,
                "font_path": font_path,
            })
    return texts


def parse_pdrproj(path):
    """回傳 (segments, media, unsupported, particle_hits, fixed_texts)"""
    d = _load_pdrproj(path)
    segments, media, unsupported, particle_hits = [], set(), [], []
    fixed_texts = _parse_texts(d, zip_path=path if path.lower().endswith(".zip") else None)

    for tr in d.get("tracks", []):
        for u in tr.get("timelineUnit", []):
            b, e = u.get("beginUs", 0) / 1e6, u.get("endUs", 0) / 1e6
            if e <= b:
                continue
            clip = u.get("timelineClip", {}) or {}
            fp = clip.get("filePath") or clip.get("srcFilePath")
            if fp:
                media.add(os.path.basename(fp))
            eff = clip.get("Effect")
            glfx = (eff or {}).get("glfx") if isinstance(eff, dict) else None
            if not glfx or not glfx.get("name"):
                continue
            key = _fx_key(glfx)
            params = {}
            for p in (glfx.get("params") or glfx.get("param") or []):
                nm = p.get("name", "").replace("IDS_Vi_Param_", "").replace("_Name", "")
                params[nm] = p.get("value")
            mapped = FX_MAP.get(key)
            if mapped == "@stickers":
                particle_hits.append(key)
                continue
            if mapped is None:
                unsupported.append({"start": b, "end": e, "fx": key})
                continue
            segments.append({"start": round(b, 2), "end": round(e, 2),
                             "fx": mapped, "params": params, "raw": key})
    segments.sort(key=lambda s: s["start"])
    return segments, sorted(media), unsupported, particle_hits, fixed_texts


def convert(src_path, out_path, template_id=None):
    segments, media, unsupported, particles, fixed_texts = parse_pdrproj(src_path)
    tid = template_id or os.path.splitext(os.path.basename(out_path))[0].replace("template_", "").upper()

    # 字體檔:如果有附在 zip 裡就複製到模板 json 旁邊,讓 render 時找得到
    out_dir = os.path.dirname(out_path) or "."
    for t in fixed_texts:
        if t.get("font_path") and os.path.exists(t["font_path"]):
            dest = os.path.join(out_dir, os.path.basename(t["font_path"]))
            if not os.path.exists(dest):
                import shutil
                shutil.copy2(t["font_path"], dest)
            # 改成相對於 out_dir 的路徑,讓 render 時用同一目錄找字體
            t["font_path"] = dest

    tpl = {
        "id": tid,
        "name": f"匯入:{os.path.splitext(os.path.basename(src_path))[0]}",
        "type": "timeline",
        "source_duration": max((s["end"] for s in segments), default=0),
        "segments": segments,
        "unsupported": [u["fx"] for u in unsupported],
        "fixed_texts": fixed_texts,          # ← 新增:固定文字方塊
        "end_fade_out": 0.5,
        "sticker_pool": "浮誇" if particles else "一般",
        "sticker_count": [3, 6] if particles else [0, 2],
        "sticker_motion": ["float", "cross"] if particles else ["enter"],
        "subtitle_style": {"font": "Noto Sans CJK TC", "size": 64, "bold": 1,
                           "primary_colour": "&H00FFFFFF", "outline_colour": "&H00000000",
                           "outline": 4, "margin_v": 260, "alignment": 2},
    }
    os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(tpl, f, ensure_ascii=False, indent=2)

    print(f"✅ 已轉出模板:{out_path}")
    print(f"   特效段 {len(segments)} 段:")
    for s in segments:
        print(f"     {s['start']:5.1f}→{s['end']:5.1f}s  {s['raw']} → {s['fx']}  {s['params'] or ''}")
    if fixed_texts:
        print(f"   📝 固定文字 {len(fixed_texts)} 個:")
        for t in fixed_texts:
            font_ok = "✅" if t.get("font_path") else "⚠️ 用系統字體"
            print(f"     「{t['text']}」cx={t['cx']} cy={t['cy']} sz={t['font_size']}px {t['color']} {font_ok}")
    if particles:
        print(f"   🎊 粒子特效 {len(particles)} 段 → 改用你 Drive 的「浮誇」貼圖庫近似")
    if unsupported:
        print(f"   ⚠️ 尚未支援 {len(unsupported)} 段(執行時跳過、該段播原片):")
        for u in unsupported:
            print(f"     {u['start']:5.1f}→{u['end']:5.1f}s  {u['fx']}(請回報截圖以新增近似版)")
    if media:
        print(f"   專案引用媒體:{'、'.join(media)}")
    return tpl


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("用法:python template_import.py 專案.pdrproj(或.zip) templates/template_P01.json")
        sys.exit(1)
    convert(sys.argv[1], sys.argv[2])
