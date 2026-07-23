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
import time

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


# 單一 ffmpeg 指令的最長容許時間。超過就強制中止那一步。
# 用意：任何一個效果出問題時，只會讓「那一支影片」失敗，
# 不會像之前那樣一支卡住就吃掉整個執行（曾經一支卡25分鐘）。
FFMPEG_TIMEOUT = 180


VERBOSE = True   # 印出每個 ffmpeg 步驟，卡住時才知道死在哪一步


def run(cmd, timeout=FFMPEG_TIMEOUT, step=""):
    if VERBOSE and step:
        print(f"        · {step} ...", flush=True)
    _t = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"ffmpeg 執行超過 {timeout} 秒被中止（可能是某個效果參數有問題卡住）：\n"
            f"{' '.join(cmd[:12])} ...")
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg 執行失敗：\n{' '.join(cmd)}\n\n{r.stderr[-3000:]}")
    if VERBOSE and step:
        print(f"        · {step} 完成 ({time.time()-_t:.1f}s)", flush=True)
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
    # 有語音且語音比影片長:影片重複到超過語音長度,不加速、不受15秒限制
    if voice_duration > src_duration:
        loops = 1
        while src_duration * loops < voice_duration:
            loops += 1
        return {"loops": loops, "speed": 1.0, "cut_to": src_duration * loops}
    if src_duration > SPEEDUP_LIMIT:
        return {"loops": 1, "speed": 1.0, "cut_to": MAX_DURATION}
    if src_duration > MAX_DURATION:
        if voice_duration > 0:
            return {"loops": 1, "speed": 1.0, "cut_to": src_duration}
        # cut_to 是「來源時間」:給全長,除以速度後輸出正好 15 秒
        # (之前給 15 會被再除一次速度,24.6秒素材只出 9.1 秒——實測抓到的 bug)
        return {"loops": 1, "speed": src_duration / MAX_DURATION, "cut_to": src_duration}
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
         "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", out_path], step="正規化影片")
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
         "-pix_fmt", "yuv420p", out_path], step="正規化影片")
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
    """前0.5秒畫面從140%快速縮回100%，像「咻」一下對焦。

    ⚠️ 這裡踩過坑：原本寫 1.4-0.8*(進度)，跑到最後會變成 0.6——
    zoompan 的縮放倍率不能小於 1.0，跌破 1 會產生未定義行為，
    在某些機器上會直接卡死不動（實際發生過，一支卡了25分鐘）。
    正確是 1.4-0.4*(進度)，剛好從 1.4 收到 1.0，並用 max(...,1.0) 再保險一層。
    """
    n = int(FPS * 0.5)
    c = (f"[0:v]scale={W}:{H},"
         f"zoompan=z='max(if(lte(on,{n}),1.4-0.4*(on/{n}),1.0),1.0)':"
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


def _concat_files(paths, out, workdir, tag):
    """把多段影片接起來。

    ⚠️ 刻意「重新編碼」而不是用 -c copy 直接複製接合。
    -c copy 要求每段的編碼參數完全一致，只要有一點不同（例如某段經過半解析度處理），
    接出來就會出現大面積綠色破塊——這就是先前 T03/T06 綠屏的真正原因。
    重新編碼慢一點點，但保證畫面正確。
    """
    listf = os.path.join(workdir, f"_cc_{tag}.txt")
    with open(listf, "w") as f:
        for p in paths:
            f.write(f"file '{os.path.abspath(p)}'\n")
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", listf,
         "-vf", f"scale={W}:{H},fps={FPS},format=yuv420p", "-an",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", out])
    return out


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
    _concat_files([head, tail], out, workdir, f"rev_{tid}")
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
    _concat_files(parts, out, workdir, "shuf")
    return order


def _concat_opening_with_rest(normalized, opening_clip, seg, duration, workdir, tid):
    if seg >= duration:
        return opening_clip
    rest = os.path.join(workdir, f"_rest_{tid}.mp4")
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", normalized,
         "-vf", f"trim={seg}:{duration},setpts=PTS-STARTPTS,scale={W}:{H}", "-an",
         "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", rest])
    joined = os.path.join(workdir, f"_joined_{tid}.mp4")
    _concat_files([opening_clip, rest], joined, workdir, f"ol_{tid}")
    return joined


# ============ 時間軸模板(從剪輯軟體匯入)============
# 每個特效函式吃 (該段長度d, 參數dict),回傳:
#   ("vf", 濾鏡字串) 或 ("fc", filter_complex字串, 輸出標籤)
# 都是對「已裁出的那一段」操作,段內時間 t 從 0 開始。

def _tfx_circle_open(d, p):
    hw, hh = W // 2, H // 2
    grow = max((max(hw, hh) * 0.75) / max(d, 0.1), 30)   # 段長內長到接近全屏
    fc = (f"[0:v]scale={hw}:{hh},split[sharp][forblur];"
          f"[forblur]boxblur=15:2[blur];"
          f"[sharp]format=yuva420p,geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
          f"a='if(lte((X-{hw}/2)*(X-{hw}/2)+(Y-{hh}/2)*(Y-{hh}/2),(60+{grow:.0f}*T)*(60+{grow:.0f}*T)),255,0)'[m];"
          f"[blur][m]overlay,scale={W}:{H}[out]")
    return ("fc", fc, "[out]")


def _grid_fc(n):
    cw, ch = W // n, H // n
    labels = "abcdefghijklmnop"[: n * n]
    fc = f"[0:v]scale={cw}:{ch}[c];[c]split={n*n}" + "".join(f"[{l}]" for l in labels) + ";"
    fc += f"color=c=black:size={W}x{H}:r={FPS}[bg];"
    prev = "[bg]"
    for i, l in enumerate(labels):
        x, y = (i % n) * cw, (i // n) * ch
        out = f"[p{i}]" if i < n * n - 1 else "[out]"
        fc += f"{prev}[{l}]overlay={x}:{y}:shortest=1{out};"
        prev = out
    return ("fc", fc.rstrip(";"), "[out]")


def _tfx_grid(d, p):
    # TvWall 的 Hori_N/Vert_N 參數大約是 3 → 3×3;取整數、限制 2~3
    n = int(round(float(p.get("Hori_N", 3))))
    return _grid_fc(min(max(n, 2), 3))


def _tfx_grid3(d, p):
    return _grid_fc(3)


def _tfx_grid2(d, p):
    return _grid_fc(2)


def _tfx_rocking(d, p):
    # 放大 12% 再用正弦位移裁切,畫面左右上下晃動
    sw, sh = int(W * 1.12), int(H * 1.12)
    return ("vf", f"scale={sw}:{sh},"
                  f"crop={W}:{H}:x='({sw}-{W})/2+{int(W*0.04)}*sin(t*9)':"
                  f"y='({sh}-{H})/2+{int(H*0.02)}*sin(t*7+1)'")


def _tfx_stutter(d, p):
    # 連拍感:降到低幀率產生頓格(Segment 參數越大越碎)
    seg = int(p.get("Segment", 2))
    return ("vf", f"fps={max(3, 2 + seg * 2)}")


def _tfx_spin_in(d, p):
    # 旋轉入場:開頭轉一整圈逐漸停住,同時從放大收回
    sw, sh = int(W * 1.6), int(H * 1.6)
    return ("vf", f"scale={sw}:{sh},"
                  f"rotate='6.2832*pow(max(1-t/{max(d,0.1):.2f},0),2)':ow={sw}:oh={sh}:c=black,"
                  f"crop={W}:{H}")


def _sparkle_chain(d, density, rm, gm, bm, label):
    """閃爍光點產生鏈:黑底上用每像素亂數直接控制光點密度(density=0.003 即 0.3%),
    每一格重抽所以自帶閃爍感;放大到全解析度時邊緣自然柔化。
    rm/gm/bm 是顏色比例(1,1,1=白、1,0.65,0.8=粉紅)。
    注意:blend 只有平面 gbrp 色彩正確,packed rgb24 會色板錯置(實測踩坑)。"""
    return (f"color=c=black:s=540x960:r={FPS}:d={d:.2f},format=gray,"
            f"geq=lum='255*lt(random(1),{density})',"
            f"scale={W}:{H},format=rgb24,"
            f"colorchannelmixer=rr={rm}:gg={gm}:bb={bm},"   # 上色要在轉 gbrp 之前(lutrgb 對 gbrp 不作用,實測)
            f"format=gbrp{label}")


def _tfx_sparkle_pink(d, p):     # Variety_03:粉紅亮片
    fc = (f"[0:v]format=gbrp[base];{_sparkle_chain(d, 0.013, 1.0, 0.65, 0.8, '[sp]')};"
          f"[base][sp]blend=all_mode=screen,format=yuv420p[out]")
    return ("fc", fc, "[out]")


def _tfx_sparkle_white(d, p):    # Variety_08:白色亮片
    fc = (f"[0:v]format=gbrp[base];{_sparkle_chain(d, 0.009, 1.0, 1.0, 1.0, '[sp]')};"
          f"[base][sp]blend=all_mode=screen,format=yuv420p[out]")
    return ("fc", fc, "[out]")


def _tfx_glow_burst(d, p):       # Atmosphere_06:開場紫紅閃光→白色光塵
    fc = (f"[0:v]format=gbrp[base];"
          f"color=c=0x9B4FD0:s={W}x{H}:r={FPS}:d={d:.2f},"
          f"vignette=PI/3.5,fade=t=out:st=0.05:d=0.7,format=gbrp[fl];"
          f"{_sparkle_chain(d, 0.003, 1.0, 1.0, 1.0, '[sp]')};"
          f"[base][fl]blend=all_mode=screen[b1];"
          f"[b1][sp]blend=all_mode=screen,format=yuv420p[out]")
    return ("fc", fc, "[out]")


def _tfx_light_sweep(d, p):      # Light_14:藍紫漏光從左上掃向右上、漸強
    dd = max(d, 0.1)
    fc = (f"[0:v]format=gbrp[base];"
          f"color=c=black:s=270x480:r={FPS}:d={d:.2f},format=gray,"
          f"geq=lum='min(T/{dd:.2f},1)*235*exp(-(pow(X-270*(0.12+0.62*T/{dd:.2f}),2)"
          f"+pow(Y-90,2))/16200)',"
          f"boxblur=6:2,scale={W}:{H},format=gbrp,"
          f"lutrgb=r=val*0.8:g=val*0.6:b=val[lk];"
          f"[base][lk]blend=all_mode=screen,format=yuv420p[out]")
    return ("fc", fc, "[out]")


TIMELINE_FX = {"circle_open": _tfx_circle_open, "grid": _tfx_grid, "grid3": _tfx_grid3,
               "grid2": _tfx_grid2, "rocking": _tfx_rocking, "stutter": _tfx_stutter,
               "spin_in": _tfx_spin_in,
               "glow_burst": _tfx_glow_burst, "light_sweep": _tfx_light_sweep,
               "sparkle_pink": _tfx_sparkle_pink, "sparkle_white": _tfx_sparkle_white}


def render_timeline(normalized, duration, template, out, workdir):
    """把匯入模板的特效段套到正規化影片上。

    模板時間軸和實際素材長度多半不同(模板22秒、素材可能8秒),
    這裡把段落邊界「等比例縮放」到實際長度——每個特效都會出現,只是各自變短/變長。
    沒有近似版的段落照原片播(不會失敗,只是那段沒特效)。
    """
    src_dur = template.get("source_duration") or duration
    scale = duration / max(src_dur, 0.1)
    parts = []
    cursor = 0.0
    segs = sorted(template.get("segments", []), key=lambda s: s["start"])

    def cut_plain(a, b, tag):
        pth = os.path.join(workdir, f"_tl_plain_{tag}.mp4")
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", normalized,
             "-vf", f"trim={a:.3f}:{b:.3f},setpts=PTS-STARTPTS,scale={W}:{H}", "-an",
             "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", pth])
        return pth

    for i, s in enumerate(segs):
        a, b = s["start"] * scale, min(s["end"] * scale, duration)
        if b - a < 0.15 or a >= duration:
            continue
        if a > cursor + 0.05:                      # 段落間的空隙照原片播
            parts.append(cut_plain(cursor, a, f"g{i}"))
        fx = TIMELINE_FX.get(s["fx"])
        pth = os.path.join(workdir, f"_tl_{i}_{s['fx']}.mp4")
        if fx is None:
            parts.append(cut_plain(a, b, f"u{i}"))
        else:
            kind, *rest = fx(b - a, s.get("params", {}))
            base = ["ffmpeg", "-y", "-loglevel", "error", "-i", normalized]
            pre_vf = f"trim={a:.3f}:{b:.3f},setpts=PTS-STARTPTS,scale={W}:{H}"
            if kind == "vf":
                cmd = base + ["-vf", f"{pre_vf},{rest[0]}", "-an",
                              "-c:v", "libx264", "-preset", "veryfast",
                              "-pix_fmt", "yuv420p", pth]
            else:
                fc = rest[0].replace("[0:v]", f"[0:v]{pre_vf},", 1)
                cmd = base + ["-filter_complex", fc, "-map", rest[1],
                              "-t", f"{b-a:.3f}", "-an", "-c:v", "libx264",
                              "-preset", "veryfast", "-pix_fmt", "yuv420p", pth]
            run(cmd, step=f"時間軸段 {s['fx']}")
            parts.append(pth)
        cursor = b
    if cursor < duration - 0.1:
        parts.append(cut_plain(cursor, duration, "tail"))
    if not parts:
        return normalized
    _concat_files(parts, out, workdir, "tl")   # 重新編碼接合(綠幕教訓)
    return out




def build_ass(ass_path, lines, total, style, timed=None):
    """timed 若提供,格式是 [(開始秒, 結束秒, 文字), ...],用真正對齊過的時間;
    沒提供就退回「平均分配」的舊行為。"""
    def ts(sec):
        sec = max(0.0, min(sec, total))
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
        if timed:
            for st, en, text in timed:
                f.write(f"Dialogue: 0,{ts(st)},{ts(en)},Default,,0,0,,{text}\n")
        else:
            for i, line in enumerate(lines):
                f.write(f"Dialogue: 0,{ts(i*per)},{ts((i+1)*per)},Default,,0,0,,{line}\n")


def align_subtitles(voice_path, lines):
    """用 faster-whisper(免費、CPU可跑)聽出語音的實際時間分佈,
    把逐字稿的每一句對到真正說話的時間點。

    做法:辨識出「哪些時間段有人在講話、講了幾個字」,再把你的字幕句
    依字數比例鋪在真實說話區間上(自動跳過開頭結尾的空白/呼吸)。
    比逐字強制對齊輕量得多,對短影音的句級字幕已經足夠準。
    任何一步失敗都回傳 None,外面就自動退回平均分配,不會讓整支失敗。"""
    if not voice_path or not lines:
        return None
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel("base", device="cpu", compute_type="int8")
        segments, _ = model.transcribe(voice_path, language="zh", vad_filter=True)
        segs = [(s.start, s.end) for s in segments]
        if not segs:
            return None
        speech_start, speech_end = segs[0][0], segs[-1][1]
        span = speech_end - speech_start
        if span <= 0.5:
            return None
        total_chars = sum(max(len(ln), 1) for ln in lines)
        timed, cursor = [], speech_start
        for ln in lines:
            dur = span * max(len(ln), 1) / total_chars
            timed.append((round(cursor, 2), round(cursor + dur, 2), ln))
            cursor += dur
        return timed
    except Exception as e:
        print(f"⚠️ 字幕對齊失敗,退回平均分配:{e}", flush=True)
        return None


# edge-tts 台灣中文聲線(免費、微軟商用級)
TTS_VOICES = {
    "曉臻": "zh-TW-HsiaoChenNeural",   # 女,自然(預設)
    "曉雨": "zh-TW-HsiaoYuNeural",     # 女,較活潑
    "雲哲": "zh-TW-YunJheNeural",      # 男
}
TTS_DEFAULT = "zh-TW-HsiaoChenNeural"


def synthesize_voice(text, out_path, voice_name=None):
    """把文字轉成語音檔(mp3)。voice_name 可以是「曉臻/曉雨/雲哲」或完整聲線代號。
    成功回傳 out_path,失敗丟例外(由呼叫端決定要不要繼續)。"""
    import asyncio
    import edge_tts
    voice = TTS_VOICES.get((voice_name or "").strip(), None) or \
        (voice_name if voice_name and "-" in voice_name else TTS_DEFAULT)

    async def _go():
        await edge_tts.Communicate(text, voice, rate="+5%").save(out_path)
    asyncio.run(_go())
    if not is_readable(out_path):
        raise RuntimeError("edge-tts 產出的語音檔無法讀取")
    return out_path


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


def _prescale_sticker(src, target_w, workdir, tag):
    """把貼圖先縮成目標尺寸存成小檔，只做一次。

    ⚠️ 這是效能關鍵：原本直接把原始大PNG丟進 -loop 1，
    ffmpeg 會對「影片的每一格」都重新解碼＋縮放那張大圖
    （7秒影片=210格，一張3000x3000的圖就被處理210次）。
    實測3張大貼圖會讓單支從3.7秒變成42.6秒，在慢機器上等於卡死。
    先縮一次再用，貼圖成本就幾乎歸零。
    """
    out = os.path.join(workdir, f"_stk_{tag}.png")
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", src,
         "-vf", f"scale={target_w}:-1:flags=bilinear", "-frames:v", "1", out])
    return out


def _sticker_filter(idx, motion, slot, total_count, duration):
    """貼圖的輸入濾鏡 + 疊加指令。

    針對「太小、太晚出現、擠在角落沒存在感」調整：
    - 尺寸放大到畫面寬度的 3-4 成（原本只有 1.7-3 成，在直式影片上幾乎看不到）
    - 0.2~0.8 秒內就淡入完成（原本最晚到 1.2 秒才開始淡入，開頭都看不到）
    - 位置改成「上下三分之一 + 兩側中段」，避開正中央商品但不再只擠在四個死角
    - cross（橫move）改成走完整支片長，才有從頭飄到尾的效果
    """
    # 尺寸：數量越多稍微縮小，但最小也有 300（1080寬的28%）
    base = 520 if total_count <= 2 else (420 if total_count <= 3 else 320)

    # 位置池：避開畫面正中央（商品通常在中間），但比純四角更有存在感
    spots = [
        (W * 0.28, H * 0.20),   # 左上偏中
        (W * 0.74, H * 0.20),   # 右上偏中
        (W * 0.22, H * 0.76),   # 左下
        (W * 0.78, H * 0.76),   # 右下
        (W * 0.16, H * 0.48),   # 左側中段
        (W * 0.84, H * 0.48),   # 右側中段
        (W * 0.50, H * 0.14),   # 正上方
        (W * 0.50, H * 0.84),   # 正下方
    ]
    px, py = spots[slot % len(spots)]
    # 在基準點附近隨機偏移(±8%寬/±6%高),同一位置每次也長得不一樣
    px += random.uniform(-W * 0.08, W * 0.08)
    py += random.uniform(-H * 0.06, H * 0.06)
    px = min(max(px, W * 0.12), W * 0.88)   # 夾住不出界
    py = min(max(py, H * 0.10), H * 0.90)
    fade_at = 0.0                            # 0秒就開始淡入,0.35秒完全現身

    # 貼圖已在外面預先縮好，這裡不再 scale（避免每格重複縮放）
    inp = f"[{idx}:v]format=rgba,fade=t=in:st={fade_at}:d=0.35:alpha=1[s{idx}]"

    if motion == "float":
        amp = random.randint(25, 55)               # 飄動幅度加大才看得出來
        ov = (f"overlay=x='{px:.0f}-w/2':y='{py:.0f}-h/2+{amp}*sin(t*2+{slot})'"
              f":enable='gte(t,{fade_at})'")
    elif motion == "cross":
        span = max(duration, 3)                    # 走完整支片，不是只走3秒就消失
        if slot % 2 == 0:
            xexpr = f"-w+({W}+w)*t/{span:.2f}"
        else:
            xexpr = f"{W}-({W}+w)*t/{span:.2f}"
        ov = f"overlay=x='{xexpr}':y='{py:.0f}-h/2':enable='gte(t,{fade_at})'"
    else:  # enter：淡入後停住
        ov = f"overlay=x='{px:.0f}-w/2':y='{py:.0f}-h/2':enable='gte(t,{fade_at})'"

    return inp, ov, base


# ============ 套模板 ============

def apply_template(normalized, duration, template, out_path, music_path,
                   voice_path=None, subtitle_lines=None, subtitle_timed=None,
                   sticker_paths=None, workdir=None):
    opening = template.get("opening", "zoom_in")
    workdir = workdir or os.path.dirname(out_path)
    sticker_paths = sticker_paths or []

    processed = normalized
    if template.get("type") == "timeline":
        pre = os.path.join(workdir, f"_tl_{template['id']}.mp4")
        processed = render_timeline(normalized, duration, template, pre, workdir)
    elif opening in SPECIAL_OPENINGS:
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
    # 先算好每張要多大，預縮一次（見 _prescale_sticker 的說明）
    n_stk = len(sticker_paths)
    stk_w = 520 if n_stk <= 2 else (420 if n_stk <= 3 else 320)
    small_stickers = []
    for i, sp in enumerate(sticker_paths):
        try:
            small_stickers.append(_prescale_sticker(sp, stk_w, workdir, f"{template['id']}_{i}"))
        except Exception as e:
            print(f"⚠️ 貼圖 {os.path.basename(sp)} 預處理失敗，跳過：{e}")
    for sp in small_stickers:
        cmd += ["-loop", "1", "-i", sp]
        sticker_indices.append(idx)
        idx += 1
    voice_idx = None
    if voice_path:
        cmd += ["-i", voice_path]
        voice_idx = idx
        idx += 1

    filters = []
    if template.get("type") != "timeline" and opening in SIMPLE_FX:
        chain, last = SIMPLE_FX[opening](template, duration)
        filters.append(chain)
    else:
        filters.append("[0:v]null[vbase]")
        last = "[vbase]"

    if template.get("end_fade_out"):
        d = template["end_fade_out"]
        filters.append(f"{last}fade=t=out:st={max(duration-d,0)}:d={d}[vfade]")
        last = "[vfade]"

    slot_order = random.sample(range(8), 8)   # 位置順序每支影片重洗
    for n, sidx in enumerate(sticker_indices):
        motion = random.choice(template.get("sticker_motion", ["enter"]))
        inp, ov, _ = _sticker_filter(sidx, motion, slot_order[n % 8],
                                     len(sticker_indices), duration)
        filters.append(inp)
        filters.append(f"{last}[s{sidx}]{ov}[st{n}]")
        last = f"[st{n}]"

    if subtitle_lines:
        ass = os.path.splitext(out_path)[0] + ".ass"
        build_ass(ass, subtitle_lines, duration, template.get("subtitle_style", {}),
                  timed=subtitle_timed)
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
    run(cmd, step=f"{template['id']} 合成輸出")

    picked = [f"開頭 {opening}", f"音樂 {os.path.basename(music_path)}"]
    if template.get("type") == "timeline":
        picked[0] = f"時間軸模板 {len(template.get('segments', []))}段"
        if template.get("unsupported"):
            picked.append(f"⚠️未支援特效:{len(template['unsupported'])}個")
    if sticker_indices:
        picked.append(f"貼圖 {len(sticker_indices)}張")
    return "、".join(picked)


def load_templates(templates_dir):
    files = sorted(f for f in os.listdir(templates_dir) if f.endswith(".json"))
    out = []
    for f in files:
        with open(os.path.join(templates_dir, f), encoding="utf-8") as fp:
            out.append(json.load(fp))
    return out
