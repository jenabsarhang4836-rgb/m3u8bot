"""Gemini transcription -> Persian SRT. Key stored in /data/m3u8bot_cfg.json"""
import json, os, re, subprocess, tempfile, time, mimetypes
import urllib.request, urllib.parse

CFG = os.environ.get("M3UBOT_CFG", os.path.join(os.path.dirname(os.path.abspath(__file__)), "m3u8bot_cfg.json"))
BASE = "https://generativelanguage.googleapis.com"
MODEL = "gemini-3.6-flash"

def _cfg():
    try:
        return json.load(open(CFG))
    except OSError:
        return {}

def set_user_key(uid, key_str):
    c = _cfg()
    user_keys = c.get("user_gemini_keys", {})
    key_str = key_str.strip()
    user_keys[str(uid)] = key_str
    c["user_gemini_keys"] = user_keys
    json.dump(c, open(CFG, "w"))
    return bool(key_str)

def get_user_key(uid, is_admin=False):
    c = _cfg()
    user_keys = c.get("user_gemini_keys", {})
    k = user_keys.get(str(uid), "").strip()
    if k:
        return k
    if is_admin:
        return get_key()
    return ""

def set_key(keys_str):
    c = _cfg()
    keys = [k.strip() for k in keys_str.replace(",", " ").split() if k.strip()]
    c["gemini_keys"] = keys
    json.dump(c, open(CFG, "w"))
    return len(keys)

def get_keys():
    c = _cfg()
    keys = c.get("gemini_keys", [])
    if not keys:
        single = os.environ.get("GEMINI_KEY", "")
        if single:
            return [single]
    return keys

def get_key():
    keys = get_keys()
    return keys[0] if keys else ""

def get_scale():
    c = _cfg()
    try:
        val = float(c.get("sub_scale", 0.018))
        return 0.018 if val > 0.025 else val
    except (ValueError, TypeError):
        return 0.018

def set_scale(val):
    c = _cfg()
    c["sub_scale"] = float(val)
    json.dump(c, open(CFG, "w"))

def _req(url, data=None, headers=None, timeout=120):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def upload_file(path, key):
    mime, _ = mimetypes.guess_type(path)
    mime = mime or "audio/mpeg"
    size = os.path.getsize(path)
    meta = {"file": {"display_name": os.path.basename(path)}}
    req = urllib.request.Request(
        f"{BASE}/upload/v1beta/files?key={key}", data=json.dumps(meta).encode(),
        headers={"Content-Type": "application/json",
                 "X-Goog-Upload-Protocol": "resumable",
                 "X-Goog-Upload-Command": "start",
                 "X-Goog-Upload-Header-Content-Length": str(size),
                 "X-Goog-Upload-Header-Content-Type": mime},
        method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        up_url = r.headers.get("X-Goog-Upload-URL")
    with open(path, "rb") as f:
        blob = f.read()
    req2 = urllib.request.Request(
        up_url, data=blob,
        headers={"Content-Length": str(len(blob)),
                 "X-Goog-Upload-Offset": "0",
                 "X-Goog-Upload-Command": "upload, finalize"},
        method="PUT")
    with urllib.request.urlopen(req2, timeout=600) as r2:
        info = json.loads(r2.read().decode())
    f = info.get("file") or info
    uri = f.get("uri")
    for _ in range(60):
        st = _req(f"{BASE}/v1beta/{f['name']}?key={key}")
        if (st.get("state") or "").upper() == "ACTIVE":
            break
        time.sleep(10)
    return uri, mime

PROMPT = (
    "You are an expert film & series audiovisual translator specializing in translating ANY spoken language into Persian.\n"
    "Watch the video closely (lips, actions, scene changes, speaker shifts) and listen to all dialogue.\n\n"
    "CRITICAL RULES:\n"
    "1. VISUAL SYNC & ACCURACY: Align the start and end of subtitles strictly with when each character physically speaks on screen (mouth movement / voice onset & cutoff). Never let one character's line bleed into another's speech.\n"
    "2. PROPER NOUNS & NAMES: NEVER translate personal names, surnames, places, or honorifics into Persian meaning. Transliterate them phonetically into natural Persian spelling (e.g., 'Ferit' -> 'فریت', 'Seyran' -> 'سیران', 'John' -> 'جان', 'Maria' -> 'ماریا').\n"
    "3. NATURAL COLLOQUIAL PERSIAN: Translate the spoken dialogue into fluent, authentic, everyday Iranian colloquial Persian (فارسی روان، امروزی، محاوره‌ای و کاملاً طبیعی).\n"
    "4. NO REDUNDANT MERGING: If characters speak back-to-back, create separate short subtitle cues for each line.\n"
    "5. OUTPUT FORMAT: Output ONLY valid, clean SRT formatted subtitles with precise timestamps (HH:MM:SS,mmm --> HH:MM:SS,mmm). No Markdown blocks, no explanations, no introduction."
)

def transcribe(uri, mime, key):
    body = {"contents": [{"parts": [
        {"file_data": {"mime_type": mime, "file_uri": uri}},
        {"text": PROMPT}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 65536}}
    r = _req(f"{BASE}/v1beta/models/{MODEL}:generateContent?key={key}",
             json.dumps(body).encode(),
             {"Content-Type": "application/json"}, timeout=600)
    txt = ""
    for c in r.get("candidates", []):
        for p in (c.get("content") or {}).get("parts", []):
            txt += p.get("text", "")
    txt = txt.strip()
    if txt.startswith("```"):
        txt = txt.split("\n", 1)[1] if "\n" in txt else txt
        txt = txt.strip("`").strip()
        if txt.lower().startswith("srt"):
            txt = txt[3:].strip()
    return txt

_TS = re.compile(r"(?:(\d+):)?(?:(\d+):)?(\d+)[,.](\d+)")

def _to_ms(s):
    m = _TS.match(s.strip())
    if not m:
        raise ValueError("bad ts: %r" % s)
    h, mi, sec, ms = m.groups()
    if mi is None:
        mi, h = h, 0
    h = int(h or 0)
    mi = int(mi or 0)
    sec = int(sec)
    ms = int((ms + "000")[:3])
    return ((h * 3600 + mi * 60 + sec) * 1000 + ms)

def _to_ts(ms):
    ms = max(0, int(ms))
    h, ms = divmod(ms, 3600000)
    mi, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d,%03d" % (h, mi, s, ms)

def shift_srt(srt, offset_ms):
    out = []
    for line in srt.split("\n"):
        if "-->" in line:
            a, b = line.split("-->")
            try:
                out.append("%s --> %s" % (_to_ts(_to_ms(a) + offset_ms),
                                          _to_ts(_to_ms(b) + offset_ms)))
                continue
            except (AttributeError, ValueError):
                pass
        out.append(line)
    return "\n".join(out)

def _renumber(srt, start=1):
    blocks = re.split(r"\n\s*\n", srt.strip())
    out = []
    n = start
    for b in blocks:
        lines = b.strip().split("\n")
        if not lines:
            continue
        if re.match(r"^\d+$", lines[0].strip()):
            lines[0] = str(n)
            n += 1
        else:
            lines.insert(0, str(n))
            n += 1
        out.append("\n".join(lines))
    return "\n\n".join(out) + "\n", n

def _duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration", "-of", "csv=p=0", path],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0

def transcribe_chunked(audio_path, key, chunk_sec=300, progress_cb=None):
    total = _duration(audio_path) or 0
    n = max(1, int(total // chunk_sec) + (1 if total % chunk_sec else 0))
    parts = []
    tmpd = tempfile.mkdtemp(prefix="subch_")
    try:
        for i in range(n):
            start = i * chunk_sec
            ch = os.path.join(tmpd, "ch%d.wav" % i)
            # IMPORTANT: place -i BEFORE -ss so ffmpeg does sample-accurate
            # seeking (no drift). Use pcm_s16le 16k WAV (CBR, no mp3 padding delay)
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", audio_path,
                            "-ss", str(start), "-t", str(chunk_sec),
                            "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", ch],
                           check=True)
            if progress_cb:
                progress_cb(i + 1, n)
            uri, mime = upload_file(ch, key)
            srt = ""
            for _ in range(2):
                try:
                    srt = transcribe(uri, mime, key)
                    if " --> " in srt:
                        break
                except Exception:
                    time.sleep(5)
            if " --> " not in srt:
                continue
            parts.append(shift_srt(srt, int(start * 1000)))
    finally:
        for f in os.listdir(tmpd):
            try:
                os.remove(os.path.join(tmpd, f))
            except OSError:
                pass
        try:
            os.rmdir(tmpd)
        except OSError:
            pass
    merged, _ = _renumber("\n\n".join(parts))
    return merged
