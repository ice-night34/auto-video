# -*- coding: utf-8 -*-
"""
自動剪片 —— 渲染核心

設計成「兩段式」：
  第一段 normalize_material()：把使用者丟進來的素材整理成一支「乾淨的無聲影片」
      （統一 1080x1920 / 30fps / 靜音 / 長度已符合規則）
  第二段 apply_template()：拿這支乾淨影片，套上某個模板的效果 + 音樂 + 字幕，輸出成品

為什麼要拆兩段：一批素材要出10支影片，長度計算、重複、加速這些工作完全一樣，
只做一次就好，10個模板共用同一支中間檔，速度差很多（實測快3-4倍）。

長度規則（依討論定案）：
  影片 < 6秒        -> 重複接續，直到超過6秒
  影片 6-15秒       -> 原樣
  影片 15-30秒      -> 加速到15秒（僅在「沒有語音」時生效；有語音不加速）
  影片 > 30秒       -> 直接切前15秒
  有語音且語音較長  -> 影片繼續重複，直到超過語音長度（此時不受15秒上限限制）
  照片              -> 一張2.5秒，不足6秒就從第一張再輪一遍
  影片原聲          -> 一律靜音
  音樂              -> 依成品長度裁切，結尾0.5秒淡出；有語音時降到20%
"""

import json
import os
import subprocess

W, H, FPS = 1080, 1920, 30
MIN_DURATION = 6.0
MAX_DURATION = 15.0
SPEEDUP_LIMIT = 30.0
PHOTO_SECONDS = 2.5
MUSIC_FADE_OUT = 0.5
MUSIC_VOLUME_WITH_VOICE = 0.20

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".bmp"}
AUDIO_EXT = {".m4a", ".mp3", ".wav", ".aac", ".ogg", ".flac"}
TEXT_EXT = {".txt"}


def run(cmd):
    """執行 ffmpeg 指令，失敗時把 ffmpeg 的錯誤訊息完整印出來（不然很難查問題）。"""
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg 執行失敗：\n{' '.join(cmd)}\n\n{result.stderr[-3000:]}")
    return result


def probe_duration(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", path],
        capture_output=True, text=True,
    )
    try:
        return float(out.stdout.strip())
    except ValueError:
        raise RuntimeError(f"無法讀取檔案長度，可能不是有效的影音檔：{path}")


# ============ 第一段：素材正規化 ============

def plan_video(src_duration: float, voice_duration: float = 0.0) -> dict:
    """依規則決定：重複幾次、要不要加速、最後成品幾秒。"""
    if src_duration > SPEEDUP_LIMIT:
        return {"loops": 1, "speed": 1.0, "cut_to": MAX_DURATION}

    if src_duration > MAX_DURATION:
        if voice_duration > 0:
            # 有語音就不加速（加速會讓畫面跟口白節奏對不上）
            return {"loops": 1, "speed": 1.0, "cut_to": src_duration}
        return {"loops": 1, "speed": src_duration / MAX_DURATION, "cut_to": MAX_DURATION}

    target = max(MIN_DURATION, voice_duration)
    loops = 1
    while src_duration * loops <= target:
        loops += 1
    return {"loops": loops, "speed": 1.0, "cut_to": src_duration * loops}


def normalize_video(src: str, out_path: str, voice_duration: float = 0.0) -> float:
    plan = plan_video(probe_duration(src), voice_duration)
    final = round(plan["cut_to"] / plan["speed"], 3)

    vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,"
          f"crop={W}:{H},fps={FPS}")
    if plan["speed"] != 1.0:
        vf += f",setpts=PTS/{plan['speed']:.6f}"

    run(["ffmpeg", "-y", "-loglevel", "error",
         "-stream_loop", str(plan["loops"] - 1), "-i", src,
         "-an", "-vf", vf, "-t", str(final),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-pix_fmt", "yuv420p", out_path])
    return final


def normalize_photos(photos: list, out_path: str, voice_duration: float = 0.0) -> float:
    """照片每張2.5秒，不足門檻就從第一張再輪一遍。"""
    target = max(MIN_DURATION, voice_duration)
    seq = []
    i = 0
    while len(seq) * PHOTO_SECONDS <= target:
        seq.append(photos[i % len(photos)])
        i += 1
    final = round(len(seq) * PHOTO_SECONDS, 3)

    list_path = out_path + ".txt"
    with open(list_path, "w", encoding="utf-8") as f:
        for p in seq:
            f.write(f"file '{os.path.abspath(p)}'\nduration {PHOTO_SECONDS}\n")
        f.write(f"file '{os.path.abspath(seq[-1])}'\n")  # concat 規格要求最後一張再列一次

    run(["ffmpeg", "-y", "-loglevel", "error",
         "-f", "concat", "-safe", "0", "-i", list_path,
         "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                f"crop={W}:{H},fps={FPS}",
         "-t", str(final),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-pix_fmt", "yuv420p", out_path])
    return final


def normalize_material(material_paths: list, out_path: str, voice_duration: float = 0.0) -> float:
    """自動判斷是影片還是照片，回傳正規化後的長度（秒）。"""
    videos = [p for p in material_paths if os.path.splitext(p)[1].lower() in VIDEO_EXT]
    photos = [p for p in material_paths if os.path.splitext(p)[1].lower() in PHOTO_EXT]

    if videos:
        if len(videos) > 1:
            print(f"⚠️ 這批有 {len(videos)} 支影片，目前只用第一支（{os.path.basename(videos[0])}）")
        return normalize_video(sorted(videos)[0], out_path, voice_duration)
    if photos:
        return normalize_photos(sorted(photos), out_path, voice_duration)
    raise RuntimeError("這批資料夾裡沒有找到任何影片或照片素材")


# ============ 字幕 ============

def build_ass(ass_path: str, lines: list, total: float, style: dict):
    """產生 ASS 字幕檔。

    刻意不用 SRT：ffmpeg 把 SRT 轉字幕時會套 384x288 的預設座標系，
    字級跟位置會整個跑掉（實測字被放大約6倍、跑到畫面最上方）。
    直接寫 ASS 可以指定 PlayRes，字級/位置就是所見即所得。

    ⚠️ 目前時間軸是「平均分配」，還不是真的對上語音。
    之後接 WhisperX 強制對齊後，這個函式改成吃 [(start, end, text), ...] 即可，
    其他地方都不用動。
    """
    def ts(sec):
        h, rem = divmod(sec, 3600)
        m, s = divmod(rem, 60)
        return f"{int(h)}:{int(m):02d}:{s:05.2f}"

    per = total / max(len(lines), 1)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{style['font']},{style['size']},{style['primary_colour']},&H000000FF,{style['outline_colour']},&H00000000,{style.get('bold', 1)},0,0,0,100,100,0,0,1,{style['outline']},0,2,80,80,{style['margin_v']},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, Effect, Text
"""
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(header)
        for i, line in enumerate(lines):
            f.write(f"Dialogue: 0,{ts(i * per)},{ts((i + 1) * per)},Default,,0,0,,{line}\n")


def read_script_lines(path: str) -> list:
    """讀逐字稿。一行一句；空行忽略。"""
    with open(path, encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip()]


# ============ 第二段：套模板 ============

def apply_template(normalized: str, duration: float, template: dict, assets_dir: str,
                   out_path: str, voice_path: str = None, subtitle_lines: list = None):
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", normalized]
    cmd += ["-stream_loop", "-1", "-i", os.path.join(assets_dir, template["music"])]
    idx = 2

    sticker = template.get("sticker") or {}
    sticker_idx = None
    if sticker.get("enabled"):
        # 靜態PNG只有一格畫面，要用 -loop 1 讓它變成連續影格，
        # 否則淡入效果沒有時間軸可走，會整張透明（實際踩過這個坑）
        cmd += ["-loop", "1", "-i", os.path.join(assets_dir, sticker["file"])]
        sticker_idx = idx
        idx += 1

    voice_idx = None
    if voice_path:
        cmd += ["-i", voice_path]
        voice_idx = idx
        idx += 1

    # ---- 影像 ----
    v = "[0:v]"
    z = template.get("zoom") or {}
    if z.get("enabled"):
        span = z["to"] - z["from"]
        # 「按比例」：不管成品幾秒，都是整支片從 from 均勻放大到 to
        v += (f"zoompan=z='{z['from']}+{span}*(on/({FPS}*{duration}))':"
              f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={FPS},")

    flash = template.get("opening_flash") or {}
    if flash.get("enabled"):
        # 「絕對秒數」：開頭固定幾秒的顏色淡入
        v += f"fade=t=in:st=0:d={flash['duration']}:color={flash['color']},"

    if template.get("end_fade_out"):
        v += f"fade=t=out:st={max(duration - template['end_fade_out'], 0)}:d={template['end_fade_out']},"

    v = v.rstrip(",") + "[base]"
    if v == "[0:v][base]":            # 沒有任何影像效果時，補一個不做事的濾鏡避免語法錯誤
        v = "[0:v]null[base]"
    filters = [v]
    last = "[base]"

    if sticker_idx is not None:
        m = sticker.get("margin", 60)
        scale = sticker.get("scale_width", 300)
        pos = {
            "top_right": f"W-w-{m}:{m}",
            "top_left": f"{m}:{m}",
            "bottom_right": f"W-w-{m}:H-h-{m}",
            "bottom_left": f"{m}:H-h-{m}",
            "center": "(W-w)/2:(H-h)/2",
        }[sticker.get("position", "top_right")]
        fade_at = sticker.get("fade_in_at", 0)
        filters.append(
            f"[{sticker_idx}:v]scale={scale}:-1,format=rgba,"
            f"fade=t=in:st={fade_at}:d=0.5:alpha=1[stk]")
        filters.append(f"{last}[stk]overlay={pos}:enable='gte(t,{fade_at})'[withstk]")
        last = "[withstk]"

    if subtitle_lines:
        ass = os.path.splitext(out_path)[0] + ".ass"
        build_ass(ass, subtitle_lines, duration, template["subtitle_style"])
        filters.append(f"{last}ass={ass}[vout]")
        last = "[vout]"

    # ---- 聲音（素材原聲已在正規化階段丟掉）----
    music_vol = MUSIC_VOLUME_WITH_VOICE if voice_path else 1.0
    fade_st = max(duration - MUSIC_FADE_OUT, 0)
    filters.append(
        f"[1:a]atrim=duration={duration},asetpts=PTS-STARTPTS,volume={music_vol},"
        f"afade=t=out:st={fade_st}:d={MUSIC_FADE_OUT}[music]")
    if voice_idx is not None:
        filters.append(f"[{voice_idx}:a]aresample=44100[voice]")
        filters.append("[music][voice]amix=inputs=2:duration=first:"
                       "dropout_transition=0:normalize=0[aout]")
        aout = "[aout]"
    else:
        aout = "[music]"

    cmd += ["-filter_complex", ";".join(filters),
            "-map", last, "-map", aout, "-t", str(duration),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", out_path]
    run(cmd)


def load_templates(templates_dir: str) -> list:
    files = sorted(f for f in os.listdir(templates_dir) if f.endswith(".json"))
    out = []
    for f in files:
        with open(os.path.join(templates_dir, f), encoding="utf-8") as fp:
            out.append(json.load(fp))
    return out
