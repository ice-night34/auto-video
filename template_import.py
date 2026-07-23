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
import sys
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


def parse_pdrproj(path):
    """回傳 (segments, media, unsupported, particle_hits)"""
    d = _load_pdrproj(path)
    segments, media, unsupported, particle_hits = [], set(), [], []
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
    return segments, sorted(media), unsupported, particle_hits


def convert(src_path, out_path, template_id=None):
    segments, media, unsupported, particles = parse_pdrproj(src_path)
    tid = template_id or os.path.splitext(os.path.basename(out_path))[0].replace("template_", "").upper()
    tpl = {
        "id": tid,
        "name": f"匯入:{os.path.splitext(os.path.basename(src_path))[0]}",
        "type": "timeline",
        "source_duration": max((s["end"] for s in segments), default=0),
        "segments": segments,
        "unsupported": [u["fx"] for u in unsupported],
        "end_fade_out": 0.5,
        "sticker_pool": "浮誇" if particles else "一般",
        "sticker_count": [3, 6] if particles else [0, 2],
        "sticker_motion": ["float", "cross"] if particles else ["enter"],
        "subtitle_style": {"font": "Noto Sans CJK TC", "size": 64, "bold": 1,
                           "primary_colour": "&H00FFFFFF", "outline_colour": "&H00000000",
                           "outline": 4, "margin_v": 260, "alignment": 2},
    }
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(tpl, f, ensure_ascii=False, indent=2)

    print(f"✅ 已轉出模板:{out_path}")
    print(f"   特效段 {len(segments)} 段:")
    for s in segments:
        print(f"     {s['start']:5.1f}→{s['end']:5.1f}s  {s['raw']} → {s['fx']}  {s['params'] or ''}")
    if particles:
        print(f"   🎊 粒子特效 {len(particles)} 段(彩帶/表情飛落)→ 改用你 Drive 的「浮誇」貼圖庫近似")
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
