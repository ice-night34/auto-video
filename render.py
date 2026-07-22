# -*- coding: utf-8 -*-
"""
自動剪片 —— 渲染核心（v2：10 種開頭效果 + Drive 貼圖系統）

兩段式：normalize_material() 把素材整理成乾淨無聲影片；apply_template() 套模板效果+音樂+字幕+貼圖。

長度規則：<6秒重複到過6秒；6-15原樣；15-30沒語音時加速到15；>30切前15；
有語音且較長則影片重複到過語音長度（不加速）；照片一張2.5秒；影片一律靜音；
音樂依成品裁切+結尾0.5秒淡出，有語音降到20%。

10種開頭（模板 "opening" 欄指定）：zoom_in柔和放大 / grid九宮格 / circle圓圈 /
time_shuffle時間錯位 / punch_in快速推近 / reverse倒放 / wave波浪 / warm暖色 /
cool冷色 / sticker_boom貼圖爆炸。
"""

import json
import os
import random
import subprocess

W, H, FPS = 1080, 1920, 30
MIN_DURATION = 6.0
MAX_DURATION = 15.0
SPEEDUP_LIMIT = 30.0
PHOTO_SECONDS = 2.5
MUSIC_FADE_OUT = 0.5
MUSIC_VOLUME_WITH_VOICE = 0.20
OPENING_SECONDS = 2.5

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".bmp"}
AUDIO_EXT = {".m4a", ".mp3", ".wav", ".aac", ".ogg", ".flac"}
TEXT_EXT = {".txt"}


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg 執行失敗：\n{' '.join(cmd)}\n\n{r.stderr[-3000:]}")
    return r


def probe_duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", path], capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        raise RuntimeError(f"無法讀取檔案長度，可能不是有效影音檔：{path}")


def is_readable(path):
    if not os.path.exists(path):
        return False
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=format_name",
                        "-of", "csv=p=0", path], capture_output=True, text=True)
    return r.returncode == 0 and bool(r.stdout.strip())


# ============ 正規化 ============

def plan_video(src_duration, voice_duration=0.0):
    if src_duration > SPEEDUP_LIMIT:
        return {"loops": 1, "speed": 1.0, "cut_to": MAX_DURATION}
    if src_duration > MAX_DURATION:
        if voice_duration > 0:
            return {"loops": 1, "speed": 1.0, "cut_to": src_duration}
        return {"loops": 1, "speed": src_duration / MAX_DURATION, "cut_to": MAX_DURATION}
    target = max(MIN_DURATION, voice_duration)
    loops = 1
    while src_duration * loops <= target:
        loops += 1
    return {"loops": loops, "speed": 1.0, "cut_to": src_duration * loops}


def normalize_video(src, out_path, voice_duration=0.0):
    plan = plan_video(probe_duration(src), voice_duration)
    final = round(plan["cut_to"] / plan["speed"], 3)
    vf = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS}"
    if plan["speed"] != 1.0:
        vf += f",setpts=PTS/{plan['speed']:.6f}"
    run(["ffmpeg", "-y", "-loglevel", "error", "-stream_loop", str(plan["loops"] - 1),
         "-i", src, "-an", "-vf", vf, "-t", str(final), "-c:v", "libx264",
         "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", out_path])
    return final


def normalize_photos(photos, out_path, voice_duration=0.0):
    target = max(MIN_DURATION, voice_duration)
    seq, i = [], 0
    while len(seq) * PHOTO_SECONDS <= target:
        seq.append(photos[i % len(photos)])
        i += 1
    final = round(len(seq) * PHOTO_SECONDS, 3)
    listf = out_path + ".txt"
    with open(listf, "w", encoding="utf-8") as f:
        for p in seq:
            f.write(f"file '{os.path.abspath(p)}'\nduration {PHOTO_SECONDS}\n")
        f.write(f"file '{os.path.abspath(seq[-1])}'\n")
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", listf,
         "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS}",
         "-t", str(final), "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-pix_fmt", "yuv420p", out_path])
    return final


def normalize_material(material_paths, out_path, voice_duration=0.0):
    videos = [p for p in material_paths if os.path.splitext(p)[1].lower() in VIDEO_EXT]
    photos = [p for p in material_paths if os.path.splitext(p)[1].lower() in PHOTO_EXT]
    if videos:
        if len(videos) > 1:
            print(f"⚠️ 這批有 {len(videos)} 支影片，只用第一支（{os.path.basename(sorted(videos)[0])}）")
        return normalize_video(sorted(videos)[0], out_path, voice_duration)
    if photos:
        return normalize_photos(sorted(photos), out_path, voice_duration)
    raise RuntimeError("這批資料夾裡沒有找到任何影片或照片素材")


# ============ 簡單開頭效果（純濾鏡）============

def _fx_zoom_in(t, dur):
    z = t.get("zoom", {"from": 1.0, "to": 1.15})
    span = z["to"] - z["from"]
    c = (f"[0:v]zoompan=z='{z['from']}+{span}*(on/({FPS}*{dur}))':"
         f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={FPS}")
    cl = t.get("corner_light")
    if cl:
        # 角落打光：用 vignette 反向 + 一塊柔光。近似做法：在角落加亮
        pos = {"top_left": "x0=0:y0=0", "top_right": f"x0={W}:y0=0",
               "bottom_left": f"x0=0:y0={H}", "bottom_right": f"x0={W}:y0={H}"}.get(cl, "x0=0:y0=0")
        c += f",vignette=a=PI/4:{pos}:mode=backward"
    return c + "[vbase]", "[vbase]"


def _fx_punch_in(t, dur):
    c = (f"[0:v]scale={W}:{H},zoompan=z='if(lte(on,{int(FPS*0.5)}),1.4-0.8*(on/{int(FPS*0.5)}),1.0)':"
         f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={FPS}[vbase]")
    return c, "[vbase]"


def _fx_warm(t, dur):
    z = t.get("zoom", {"from": 1.0, "to": 1.1})
    span = z["to"] - z["from"]
    # 用 curves 做偏橘的復古暖調，比 colorbalance+vignette 快約2.7倍、效果更明顯
    c = (f"[0:v]curves=r='0/0 0.5/0.58 1/1':g='0/0 0.5/0.5 1/0.96':b='0/0.05 0.5/0.42 1/0.9',"
         f"eq=saturation=1.1,"
         f"zoompan=z='{z['from']}+{span}*(on/({FPS}*{dur}))':"
         f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={FPS}[vbase]")
    return c, "[vbase]"


def _fx_cool(t, dur):
    # 偏藍的電影冷調，同樣用 curves 加速
    return (f"[0:v]curves=r='0/0 0.5/0.42 1/0.92':g='0/0 0.5/0.5 1/1':b='0/0.08 0.5/0.6 1/1',"
            f"eq=saturation=0.92:contrast=1.08[vbase]"), "[vbase]"


def _fx_sticker_boom(t, dur):
    z = t.get("zoom", {"from": 1.05, "to": 1.2})
    span = z["to"] - z["from"]
    c = (f"[0:v]zoompan=z='{z['from']}+{span}*(on/({FPS}*{dur}))':"
         f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={FPS}[vbase]")
    return c, "[vbase]"


SIMPLE_FX = {"zoom_in": _fx_zoom_in, "punch_in": _fx_punch_in, "warm": _fx_warm,
             "cool": _fx_cool, "sticker_boom": _fx_sticker_boom}
SPECIAL_OPENINGS = {"grid", "circle", "time_shuffle", "reverse", "wave"}


# ============ 特殊開頭：生成獨立前段影片 ============

def _make_grid_opening(normalized, dur, out):
    seg = min(OPENING_SECONDS, dur)
    cw, ch = W // 3, H // 3          # 先在 Python 算好格子尺寸，ffmpeg filter 不認 // 運算
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", normalized, "-filter_complex",
         f"[0:v]trim=0:{seg},setpts=PTS-STARTPTS,scale={cw}:{ch}[c];"
         f"[c]split=9[a][b][d][e][f][g][h][i][j];color=c=black:size={W}x{H}:d={seg}:r={FPS}[bg];"
         f"[bg][a]overlay=0:0[p1];[p1][b]overlay={cw}:0[p2];[p2][d]overlay={2*cw}:0[p3];"
         f"[p3][e]overlay=0:{ch}[p4];[p4][f]overlay={cw}:{ch}[p5];[p5][g]overlay={2*cw}:{ch}[p6];"
         f"[p6][h]overlay=0:{2*ch}[p7];[p7][i]overlay={cw}:{2*ch}[p8];[p8][j]overlay={2*cw}:{2*ch}[out]",
         "-map", "[out]", "-t", str(seg), "-an", "-c:v", "libx264", "-preset", "veryfast",
         "-pix_fmt", "yuv420p", out])
    return seg


def _make_circle_opening(normalized, dur, out):
    """模糊的畫面上，清晰的圓從中間慢慢擴大到全屏（對焦感）。圓外是模糊的同一畫面。

    速度優化：geq 是逐像素運算、在 GitHub 免費機器上很慢，改成先縮到半解析度
    (540x960) 做完 geq 再放大回 1080x1920。成本降約4倍，畫質幾乎看不出差
    （因為圓外本來就是模糊的，放大的柔邊反而更自然）。
    """
    seg = min(OPENING_SECONDS, dur)
    hw, hh = W // 2, H // 2
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", normalized, "-filter_complex",
         f"[0:v]trim=0:{seg},setpts=PTS-STARTPTS,scale={hw}:{hh},split[sharp][forblur];"
         f"[forblur]boxblur=15:2[blur];"
         f"[sharp]format=yuva420p,geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
         f"a='if(lte((X-{hw}/2)*(X-{hw}/2)+(Y-{hh}/2)*(Y-{hh}/2),(125+90*T)*(125+90*T)),255,0)'[sharpmask];"
         f"[blur][sharpmask]overlay,scale={W}:{H}[out]",
         "-map", "[out]", "-t", str(seg), "-an", "-c:v", "libx264", "-preset", "veryfast",
         "-pix_fmt", "yuv420p", out])
    return seg


def _make_wave_opening(normalized, dur, out):
    """前段畫面像水波輕微晃動。同樣用半解析度做 geq 再放大，避免全解析度逐像素太慢。"""
    seg = min(OPENING_SECONDS, dur)
    hw, hh = W // 2, H // 2
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", normalized, "-filter_complex",
         f"[0:v]trim=0:{seg},setpts=PTS-STARTPTS,scale={hw}:{hh},"
         f"geq=lum='lum(X+8*sin(Y/25+T*3),Y)':cb='cb(X,Y)':cr='cr(X,Y)',"
         f"scale={W}:{H}[out]",
         "-map", "[out]", "-t", str(seg), "-an", "-c:v", "libx264", "-preset", "veryfast",
         "-pix_fmt", "yuv420p", out])
    return seg


def _make_reverse_opening(normalized, dur, out, workdir, tid):
    """開頭 2.5 秒時間倒轉（影片由後往前播），2.5 秒後接回正常播放。

    原本是整支倒放，但 reverse 要把整支載進記憶體，在慢機器上要40幾秒。
    改成只倒放開頭那段（其他特殊效果也是這個做法），速度快約10倍，
    而且「開頭倒轉、之後正常」對吸睛開場來說反而比整支倒放自然。
    """
    seg = min(OPENING_SECONDS, dur)
    head = os.path.join(workdir, f"_revhead_{tid}.mp4")
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", normalized, "-vf",
         f"trim=0:{seg},setpts=PTS-STARTPTS,reverse,scale={W}:{H}", "-an",
         "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", head])
    if seg >= dur:
        return head
    tail = os.path.join(workdir, f"_revtail_{tid}.mp4")
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", normalized, "-vf",
         f"trim={seg}:{dur},setpts=PTS-STARTPTS,scale={W}:{H}", "-an",
         "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", tail])
    listf = os.path.join(workdir, f"_revlist_{tid}.txt")
    with open(listf, "w") as f:
        f.write(f"file '{os.path.abspath(head)}'\nfile '{os.path.abspath(tail)}'\n")
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", listf, "-c", "copy", out])
    return out


def _make_time_shuffle(normalized, dur, out, workdir):
    seg = dur / 4
    order = [0, 1, 2, 3]
    random.shuffle(order)
    parts = []
    for k, i in enumerate(order):
        p = os.path.join(workdir, f"_seg{k}.mp4")
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", normalized,
             "-vf", f"trim={i*seg}:{(i+1)*seg},setpts=PTS-STARTPTS,scale={W}:{H}",
             "-an", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", p])
        parts.append(p)
    listf = os.path.join(workdir, "_shuf.txt")
    with open(listf, "w") as f:
        for p in parts:
            f.write(f"file '{os.path.abspath(p)}'\n")
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", listf, "-c", "copy", out])
    return order


def _concat_opening_with_rest(normalized, opening_clip, seg, duration, workdir, tid):
    if seg >= duration:
        return opening_clip
    rest = os.path.join(workdir, f"_rest_{tid}.mp4")
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", normalized,
         "-vf", f"trim={seg}:{duration},setpts=PTS-STARTPTS,scale={W}:{H}", "-an",
         "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", rest])
    listf = os.path.join(workdir, f"_ol_{tid}.txt")
    with open(listf, "w") as f:
        f.write(f"file '{os.path.abspath(opening_clip)}'\nfile '{os.path.abspath(rest)}'\n")
    joined = os.path.join(workdir, f"_joined_{tid}.mp4")
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", listf, "-c", "copy", joined])
    return joined


# ============ 字幕 ============

def build_ass(ass_path, lines, total, style):
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
Style: Default,{style.get('font','Noto Sans CJK TC')},{style.get('size',64)},{style.get('primary_colour','&H00FFFFFF')},&H000000FF,{style.get('outline_colour','&H00000000')},&H64000000,{style.get('bold',1)},0,0,0,100,100,{style.get('spacing',0)},0,{style.get('border_style',1)},{style.get('outline',4)},{style.get('shadow',0)},{style.get('alignment',2)},80,80,{style.get('margin_v',260)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, Effect, Text
"""
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(header)
        for i, line in enumerate(lines):
            f.write(f"Dialogue: 0,{ts(i*per)},{ts((i+1)*per)},Default,,0,0,,{line}\n")


def read_script_lines(path):
    with open(path, encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip()]


# ============ 貼圖 ============

def pick_stickers(pool_files, count_range):
    if not pool_files:
        return []
    lo, hi = count_range
    n = random.randint(lo, hi)
    if n <= 0:
        return []
    return [random.choice(pool_files) for _ in range(n)]


def _sticker_filter(idx, motion, slot, total_count):
    base = 320 if total_count <= 2 else (240 if total_count <= 4 else 180)
    edges = [(60, 120), (W - 60, 120), (60, H - 400), (W - 60, H - 400),
             (60, H / 2), (W - 60, H / 2), (W / 2, 120), (W / 2, H - 300)]
    px, py = edges[slot % len(edges)]
    fade_at = round(random.uniform(0.1, 1.2), 2)
    inp = f"[{idx}:v]scale={base}:-1,format=rgba,fade=t=in:st={fade_at}:d=0.4:alpha=1[s{idx}]"
    if motion == "float":
        amp = random.randint(15, 35)
        ov = f"overlay=x='{px}-w/2':y='{py}-h/2+{amp}*sin(t*2+{slot})':enable='gte(t,{fade_at})'"
    elif motion == "cross":
        if slot % 2 == 0:
            xexpr = f"-w+({W}+w)*t/3"
        else:
            xexpr = f"{W}-({W}+w)*t/3"
        ov = f"overlay=x='{xexpr}':y='{py}-h/2':enable='gte(t,{fade_at})'"
    else:  # enter
        ov = f"overlay=x='{px}-w/2':y='{py}-h/2':enable='gte(t,{fade_at})'"
    return inp, ov


# ============ 套模板 ============

def apply_template(normalized, duration, template, out_path, music_path,
                   voice_path=None, subtitle_lines=None, sticker_paths=None, workdir=None):
    opening = template.get("opening", "zoom_in")
    workdir = workdir or os.path.dirname(out_path)
    sticker_paths = sticker_paths or []

    processed = normalized
    if opening in SPECIAL_OPENINGS:
        pre = os.path.join(workdir, f"_pre_{template['id']}.mp4")
        if opening == "grid":
            seg = _make_grid_opening(normalized, duration, pre)
            processed = _concat_opening_with_rest(normalized, pre, seg, duration, workdir, template["id"])
        elif opening == "circle":
            seg = _make_circle_opening(normalized, duration, pre)
            processed = _concat_opening_with_rest(normalized, pre, seg, duration, workdir, template["id"])
        elif opening == "wave":
            seg = _make_wave_opening(normalized, duration, pre)
            processed = _concat_opening_with_rest(normalized, pre, seg, duration, workdir, template["id"])
        elif opening == "reverse":
            processed = _make_reverse_opening(normalized, duration, pre, workdir, template["id"])
        elif opening == "time_shuffle":
            _make_time_shuffle(normalized, duration, pre, workdir)
            processed = pre

    # 音樂隨機起點：從歌曲的隨機位置開始擷取，讓同一首歌每次配出來的段落不同、
    # 也更可能抓到副歌而不是永遠用開頭。起點最多退到「歌長 - 成品長」，
    # 保證從起點還抓得滿；配上 -stream_loop -1，就算歌比成品短也會循環補滿。
    try:
        music_len = probe_duration(music_path)
    except Exception:
        music_len = 0.0
    music_ss = 0.0
    if music_len > duration + 1:
        music_ss = round(random.uniform(0, music_len - duration - 0.5), 2)

    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", processed,
           "-stream_loop", "-1", "-ss", str(music_ss), "-i", music_path]
    idx = 2
    sticker_indices = []
    for sp in sticker_paths:
        cmd += ["-loop", "1", "-i", sp]
        sticker_indices.append(idx)
        idx += 1
    voice_idx = None
    if voice_path:
        cmd += ["-i", voice_path]
        voice_idx = idx
        idx += 1

    filters = []
    if opening in SIMPLE_FX:
        chain, last = SIMPLE_FX[opening](template, duration)
        filters.append(chain)
    else:
        filters.append("[0:v]null[vbase]")
        last = "[vbase]"

    if template.get("end_fade_out"):
        d = template["end_fade_out"]
        filters.append(f"{last}fade=t=out:st={max(duration-d,0)}:d={d}[vfade]")
        last = "[vfade]"

    for n, sidx in enumerate(sticker_indices):
        motion = random.choice(template.get("sticker_motion", ["enter"]))
        inp, ov = _sticker_filter(sidx, motion, n, len(sticker_paths))
        filters.append(inp)
        filters.append(f"{last}[s{sidx}]{ov}[st{n}]")
        last = f"[st{n}]"

    if subtitle_lines:
        ass = os.path.splitext(out_path)[0] + ".ass"
        build_ass(ass, subtitle_lines, duration, template.get("subtitle_style", {}))
        filters.append(f"{last}ass={ass}[vout]")
        last = "[vout]"

    music_vol = MUSIC_VOLUME_WITH_VOICE if voice_path else 1.0
    fade_st = max(duration - MUSIC_FADE_OUT, 0)
    filters.append(f"[1:a]atrim=duration={duration},asetpts=PTS-STARTPTS,volume={music_vol},"
                   f"afade=t=out:st={fade_st}:d={MUSIC_FADE_OUT}[music]")
    if voice_idx is not None:
        filters.append(f"[{voice_idx}:a]aresample=44100[voice]")
        filters.append("[music][voice]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]")
        aout = "[aout]"
    else:
        aout = "[music]"

    cmd += ["-filter_complex", ";".join(filters), "-map", last, "-map", aout,
            "-t", str(duration), "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", out_path]
    run(cmd)

    picked = [f"開頭 {opening}", f"音樂 {os.path.basename(music_path)}"]
    if sticker_paths:
        picked.append(f"貼圖 {len(sticker_paths)}張")
    return "、".join(picked)


def load_templates(templates_dir):
    files = sorted(f for f in os.listdir(templates_dir) if f.endswith(".json"))
    out = []
    for f in files:
        with open(os.path.join(templates_dir, f), encoding="utf-8") as fp:
            out.append(json.load(fp))
    return out
