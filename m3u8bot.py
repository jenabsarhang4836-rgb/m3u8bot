import json, time, subprocess, os, re, sys, urllib.request, urllib.parse
from pathlib import Path
import gemini_sub as gs

BASE_DIR = Path(__file__).resolve().parent

def clean_srt(text):
    text = re.sub(r"\{[^}]*\}", "", text)
    text = re.sub(r"</?[a-zA-Z][^>]*>", "", text)
    lines = []
    for ln in text.split("\n"):
        s = ln.strip()
        if re.match(r"^\d+$", s) or "-->" in s or s == "":
            lines.append(ln)
        else:
            lines.append(s)
    text = "\n".join(lines)
    fixed = []
    for ln in text.split("\n"):
        if "-->" in ln:
            a, b = ln.split("-->")
            try:
                ln = "%s --> %s" % (gs._to_ts(gs._to_ms(a)),
                                    gs._to_ts(gs._to_ms(b)))
            except (AttributeError, ValueError):
                pass
        fixed.append(ln)
    text = "\n".join(fixed)
    blocks = re.split(r"\n\s*\n", text.strip())
    out = []
    n = 1
    for b in blocks:
        bl = [l for l in b.strip().split("\n") if l.strip() != ""]
        if not bl or not any("-->" in l for l in bl):
            continue
        if not re.match(r"^\d+$", bl[0].strip()):
            bl.insert(0, str(n))
        else:
            bl[0] = str(n)
        n += 1
        out.append("\n".join(bl))
    return "\n\n".join(out) + "\n" if out else text

TOKEN = os.environ.get("BOT_TOKEN", "")
if not TOKEN:
    raise SystemExit("BOT_TOKEN env is required")
ADMIN = {a.strip() for a in os.environ.get("ADMIN_IDS", "").split(",") if a.strip()}
REQUIRED_CHANNEL = os.environ.get("REQUIRED_CHANNEL", "").strip()  # e.g. @YourChannel or -1001234567890
API = f"https://api.telegram.org/bot{TOKEN}"
WORKDIR = os.environ.get("WORKDIR", "/tmp/m3ubot")
YTDL = [sys.executable, "-m", "yt_dlp"]
COOKIES = os.environ.get("COOKIES_PATH", str(BASE_DIR / "yt_cookies.txt"))
CHUNK_SCRIPT = str(BASE_DIR / "chunk_gemini_sub.py")
os.makedirs(WORKDIR, exist_ok=True)

def tg(method, payload=None, timeout=60):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{API}/{method}", data=data,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)

def is_member(user_id):
    """Return True if REQUIRED_CHANNEL is set and the user is a member/subscriber."""
    if not REQUIRED_CHANNEL or str(user_id) in ADMIN:
        return True
    try:
        r = tg("getChatMember", {"chat_id": REQUIRED_CHANNEL, "user_id": int(user_id)})
        if not r.get("ok"):
            print(f"membership check API error: {r}", flush=True)
            # If bot is not admin in channel, don't permanently lock out users
            if "administrator rights" in str(r).lower() or "chat_admin_required" in str(r).lower():
                return True
            return False
        status = (r.get("result") or {}).get("status", "")
        return status in ("creator", "administrator", "member", "restricted")
    except Exception as e:
        print("membership check failed:", e, flush=True)
        return False

def join_channel_kb():
    ch_url = f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}" if REQUIRED_CHANNEL.startswith('@') else "https://t.me/ArzanVpns"
    return {
        "inline_keyboard": [
            [{"text": "📢 عضویت در کانال", "url": ch_url}],
            [{"text": "🔄 بررسی مجدد / شروع", "callback_data": "check_join"}]
        ]
    }

def send(chat, text, kb=None):
    try:
        p = {"chat_id": chat, "text": text,
             "disable_web_page_preview": True}
        if kb:
            p["reply_markup"] = kb
        r = tg("sendMessage", p)
        return (r.get("result") or {}).get("message_id")
    except Exception as e:
        print("send fail:", e, flush=True)
        return None

def answer(cb_id):
    try:
        tg("answerCallbackQuery", {"callback_query_id": cb_id})
    except Exception:
        pass

HELP_MAIN = ("به ArzanMediaBot خوش اومدی 🌹\nیکی رو انتخاب کن:")
HELP = {
    "m3u8": "📥 لینک m3u8 (یا صفحه قسمت) رو بفرست تا mp4 + mp3 با لینک دانلود بدم.",
    "sub": "🎬 لینک مستقیم mp4 (تا ۱۰۰ مگ) یا ویدیو (زیر ۱۹ مگ) بفرست تا mp4 زیرنویس‌چسبیده + mp3 بدم.",
    "aud": "🎧 فایل صوتی یا لینک مستقیمش رو بده تا با Gemini زیرنویس فارسی SRT بسازم.\nاول با /setkey کلیدت رو ست کن.",
}
def main_kb():
    return {"inline_keyboard": [
        [{"text": "🔑 گرفتن کلید Gemini",
          "url": "https://aistudio.google.com/app/apikey"}],
        [{"text": "📥 دانلود m3u8", "callback_data": "h:m3u8"},
         {"text": "🎬 زیرنویس ویدیو", "callback_data": "h:sub"}],
        [{"text": "🎧 زیرنویس صوت", "callback_data": "h:aud"}],
        [{"text": "🔑 ست کردن کلید", "callback_data": "h:setkey"}]]}

PENDING_KEY = set()

def edit(chat, mid, text, kb=None):
    if not mid:
        return
    try:
        p = {"chat_id": chat, "message_id": mid,
             "text": text, "disable_web_page_preview": True}
        if kb:
            p["reply_markup"] = kb
        tg("editMessageText", p)
    except Exception as e:
        print("edit fail:", e, flush=True)

SUBS = os.environ.get("SUBS_PATH", str(BASE_DIR / "subs.srt"))
FONTS = os.environ.get("FONTS_DIR", str(BASE_DIR / "fonts"))
WATERMARK = os.path.join(FONTS, "watermark.png")

def send_video(chat, path, caption=""):
    try:
        subprocess.run(["curl", "-s", "-m", "180", "-o", "/dev/null",
                        f"{API}/sendVideo", "-F", f"chat_id={chat}",
                        "-F", f"video=@{path}", "-F",
                        f"caption={caption}"],
                       capture_output=True)
    except Exception as e:
        print("sendVideo fail:", e, flush=True)

def download_tg_file(file_id, dest):
    info = tg("getFile", {"file_id": file_id})
    fpath = (info.get("result") or {}).get("file_path")
    if not fpath:
        return False
    req = urllib.request.Request(
        f"https://api.telegram.org/file/bot{TOKEN}/{fpath}")
    with urllib.request.urlopen(req, timeout=300) as r, \
            open(dest, "wb") as f:
        f.write(r.read())
    return True

def get_video_res(path):
    try:
        out = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", path])
        w, h = map(int, out.decode().strip().split("x"))
        return w, h
    except:
        return 1280, 720

def get_style_for_res(w, h):
    # Balanced font size: 12 for vertical (clean, sleek, not bulky), 9 for horizontal
    if w < h:
        fs = 12
        margin_v = 30
    else:
        fs = 9
        margin_v = 15
    return f"FontName=Vazirmatn,FontSize={fs},PrimaryColour=&H0000FFFF,OutlineColour=&H00000000,BackColour=&H00000000,BorderStyle=1,Outline=1.2,Shadow=0,MarginV={margin_v},Alignment=2"

def burn_subs(chat, src, tag, srt=SUBS):
    out = f"{WORKDIR}/{tag}_sub.mp4"
    # Clean stale temp files older than 30 minutes to prevent disk-full failures
    try:
        now = time.time()
        for f in os.listdir(WORKDIR):
            fp = os.path.join(WORKDIR, f)
            if os.path.isfile(fp) and (now - os.path.getmtime(fp) > 1800):
                try:
                    os.remove(fp)
                except OSError:
                    pass
    except Exception:
        pass
    send(chat, "🎬 (۳/۳) چسبوندن زیرنویس فارسی...")
    w, h = get_video_res(src)
    style = get_style_for_res(w, h)
    
    # Check if watermark badge exists to embed branding
    if os.path.exists(WATERMARK):
        fc = f"[0:v]subtitles={srt}:fontsdir={FONTS}:force_style='{style}'[v1];[v1][1:v]overlay=x=(W-w)/2:y=H-h-10[vout]"
        cmd = ["ffmpeg", "-y", "-v", "error", "-threads", "2",
               "-i", src, "-i", WATERMARK,
               "-filter_complex", fc, "-map", "[vout]", "-map", "0:a?",
               "-c:v", "libx264", "-crf", "23", "-preset", "fast", "-c:a", "copy", out]
    else:
        cmd = ["ffmpeg", "-y", "-v", "error", "-threads", "2",
               "-i", src, "-vf", f"subtitles={srt}:fontsdir={FONTS}:force_style='{style}'",
               "-c:v", "libx264", "-crf", "23", "-preset", "fast", "-c:a", "copy", out]

    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(out):
        print("FFMPEG ERROR:", r.stderr[-500:], flush=True)
        send(chat, "❌ نشد. ویدیو خرابه یا سنگینه.")
        return
    send_video(chat, out, "✅ زیرنویس چسبید!\n📢 @ArzanVpns")
    for f in (src, out):
        try:
            os.remove(f)
        except OSError:
            pass


def send_doc(chat, path, caption=""):
    try:
        subprocess.run(["curl", "-s", "-m", "180", "-o", "/dev/null",
                        f"{API}/sendDocument", "-F", f"chat_id={chat}",
                        "-F", f"document=@{path}", "-F",
                        f"caption={caption}"],
                       capture_output=True)
    except Exception as e:
        print("sendDoc fail:", e, flush=True)

def do_transcribe(chat, audio_path, tag, burn_video=None, uid=None):
    is_admin = str(uid) in ADMIN if uid else False
    key = gs.get_user_key(uid, is_admin=is_admin) if uid else gs.get_key()
    if not key:
        key = gs.get_key()
    if not key:
        send(chat, "❌ سرویس موقتاً با مشکل مواجه شده است. لطفاً چند دقیقه دیگر امتحان کنید.")
        return
    sp = f"{WORKDIR}/{tag}.srt"
    ok = False

    # 1. If we have a video, use Gemini Multimodal Vision + Audio for perfect lip-sync & speaker recognition
    if burn_video and os.path.exists(burn_video):
        send(chat, "👀 (۱/۲) تحلیل ویدیویی و تطبیق چهره‌ها و صدا با Gemini...")
        compressed_video = f"{WORKDIR}/{tag}_vision.mp4"
        try:
            # Compress video to lightweight 360p fast copy so upload takes ~2 seconds
            subprocess.run([
                "ffmpeg", "-y", "-v", "error", "-i", burn_video,
                "-vf", "scale=-2:360", "-c:v", "libx264", "-crf", "32",
                "-preset", "ultrafast", "-c:a", "aac", "-b:a", "96k",
                compressed_video
            ], timeout=180)
            if os.path.exists(compressed_video):
                uri, mime = gs.upload_file(compressed_video, key)
                srt = gs.transcribe(uri, mime, key)
                if " --> " in srt:
                    open(sp, "w", encoding="utf-8").write(clean_srt(srt))
                    ok = True
        except Exception as e:
            print("visual transcription error:", e, flush=True)
        finally:
            try:
                if os.path.exists(compressed_video):
                    os.remove(compressed_video)
            except OSError:
                pass

    # 2. Fallback to whisper pipeline if video processing failed or only audio provided
    if not ok:
        send(chat, "🧠 تشخیص گفتار و زمان‌بندی دقیق صوتی...")
        try:
            import whisper_pipeline
            ok = whisper_pipeline.run_pipeline(audio_path, sp, key)
        except Exception as e:
            print("whisper pipeline error:", e, flush=True)
            ok = False

    # 3. Last resort fallback to audio-only Gemini
    if not ok or not os.path.exists(sp):
        try:
            uri, mime = gs.upload_file(audio_path, key)
            srt = gs.transcribe(uri, mime, key)
            if " --> " in srt:
                open(sp, "w", encoding="utf-8").write(clean_srt(srt))
                ok = True
        except Exception as e:
            send(chat, f"❌ خطا: {str(e)[:200]}")
            return

    if not ok or not os.path.exists(sp):
        send(chat, "❌ ساخت زیرنویس ناموفق بود.")
        return

    send_doc(chat, sp, "📄 زیرنویس سینک و ترجمه آماده شد!")
    if burn_video:
        burn_subs(chat, burn_video, tag + "b", srt=sp)

def gofile_upload(path):
    srv = json.loads(subprocess.run(
        ["curl", "-s", "-m", "30", "https://api.gofile.io/servers"],
        capture_output=True, text=True).stdout)["data"]["servers"][0]["name"]
    out = subprocess.run(
        ["curl", "-s", "-m", "590", "-F", f"file=@{path}",
         f"https://{srv}.gofile.io/uploadFile"],
        capture_output=True, text=True).stdout
    d = json.loads(out).get("data") or {}
    return d.get("downloadPage")

def deliver(chat, mp4path, mp3path):
    links = []
    if mp4path and os.path.exists(mp4path):
        if os.path.getsize(mp4path) <= 45 * 1024 * 1024:
            send_video(chat, mp4path, "✅ ویدیوی زیرنویس‌دار")
        else:
            try:
                links.append("🎬 ویدیو:\n" + gofile_upload(mp4path))
            except Exception as e:
                send(chat, f"❌ آپلود ویدیو نشد: {e}")
                return
    if mp3path and os.path.exists(mp3path):
        if os.path.getsize(mp3path) <= 45 * 1024 * 1024:
            send_doc(chat, mp3path, "🎧 صدا")
        else:
            try:
                links.append("🎧 صدا:\n" + gofile_upload(mp3path))
            except Exception as e:
                send(chat, f"❌ آپلود صدا نشد: {e}")
    if links:
        send(chat, "✅ تمومه!\n\n" + "\n\n".join(links))

def handle_small(chat, src, tag):
    base = f"{WORKDIR}/s{tag}"
    mp3 = base + ".mp3"
    mid = send(chat, "🎧 (۱/۳) جدا کردن صدا...")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-threads", "2",
                    "-i", src, "-vn", "-c:a", "libmp3lame", "-b:a",
                    "128k", mp3])
    if not os.path.exists(mp3):
        edit(chat, mid, "❌ استخراج صدا نشد.")
        return
    sp = base + ".srt"
    if not whisper_srt(chat, mid, mp3, sp, "🧠 (۲/۳) زیرنویس"):
        edit(chat, mid, "❌ زیرنویس ساخته نشد.")
        return
    srt = open(sp).read()
    if " --> " not in srt:
        edit(chat, mid, "❌ زیرنویس ساخته نشد.")
        return
    open(sp, "w").write(clean_srt(srt))
    send_doc(chat, sp, "📄 زیرنویس فارسی")
    w, h = get_video_res(src)
    style = get_style_for_res(w, h)
    edit(chat, mid, "🎬 (۳/۳) چسبوندن زیرنویس...")
    out = base + "_sub.mp4"
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-threads", "2",
                        "-i", src, "-vf",
                        "subtitles=" + sp + ":fontsdir=" + FONTS +
                        ":force_style='" + style + "'",
                        "-c:v", "libx264", "-crf", "23", "-preset",
                        "fast", "-c:a", "copy", out])
    if r.returncode != 0 or not os.path.exists(out):
        edit(chat, mid, "❌ چسبوندن نشد، ولی mp3 و srt رو داری 👆")
        deliver(chat, None, mp3, )
    else:
        edit(chat, mid, "📤 تحویل...")
        deliver(chat, out, mp3)
    for f in (src, mp3, sp, out):
        try:
            os.remove(f)
        except OSError:
            pass

def handle_yt(chat, url, tag):
    out = f"{WORKDIR}/yt{tag}.mp4"
    mid = send(chat, "⏳ دارم از یوتیوب می‌گیرم... ۰٪")
    p = subprocess.Popen(YTDL +
                         ["--no-playlist", "--newline"] +
                         (["--cookies", COOKIES]
                          if os.path.exists(COOKIES) else []) +
                         ["-f", "b[height<=720]/b",
                          "--merge-output-format", "mp4", "-o", out, url],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True)
    last = -1
    for line in p.stdout:
        m = re.search(r"\[download\]\s+(\d+(?:\.\d+)?)%", line)
        if m:
            pct = float(m.group(1))
            if pct - last >= 10:
                last = pct
                edit(chat, mid, "⏳ یوتیوب: %.0f٪" % pct)
    rc = p.wait()
    if rc != 0 or not os.path.exists(out):
        edit(chat, mid, "❌ یوتیوب نداد (معمولاً آی‌پی سرور رو می‌بنده). با لینک مستقیم امتحان کن.")
        return
    send(chat, "📤 دارم می‌فرستم بالا...")
    try:
        link = gofile_upload(out)
        send(chat, f"✅ آماده‌ست:\n{link}")
    except Exception as e:
        send(chat, f"❌ آپلود نشد: {e}")
    finally:
        try:
            os.remove(out)
        except OSError:
            pass

def ytdl_progress(chat, mid, url, dest, label):
    p = subprocess.Popen(YTDL +
                         ["--no-playlist", "--newline", "-o", dest, url],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True)
    last = -1
    for line in p.stdout:
        m = re.search(r"\[download\]\s+(\d+(?:\.\d+)?)%", line)
        if m:
            pct = float(m.group(1))
            if pct - last >= 5:
                last = pct
                edit(chat, mid, "%s: %.0f%%" % (label, pct))
    return p.wait()

def whisper_srt(chat, mid, audio, sp, label):
    p = subprocess.Popen(["python3", "-u", CHUNK_SCRIPT, audio, sp],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True)
    for line in p.stdout:
        line = line.strip()
        if "chunk" in line.lower() or "transcribing" in line.lower() or "finished" in line.lower():
            edit(chat, mid, "%s: %s" % (label, line))
    rc = p.wait()
    return rc == 0 and os.path.exists(sp)

def handle_auto(chat, url, tag, uid=None):
    base = f"{WORKDIR}/auto{tag}"
    mp4 = base + ".mp4"
    mid = send(chat, "⏳ (۱/۴) دانلود...")
    src = url
    dl = base + "_dl.mp4"
    if "drive.google.com" in url:
        rc = ytdl_progress(chat, mid, url, dl, "⏳ (۱/۴) دانلود از درایو")
        if rc != 0 or not os.path.exists(dl):
            edit(chat, mid, "❌ دانلود از درایو نشد. دسترسی لینک باید public باشه.")
            return
        src = dl
    p = subprocess.Popen(
        ["ffmpeg", "-y", "-v", "error", "-i", src, "-c", "copy", mp4])
    while p.poll() is None:
        time.sleep(20)
        if os.path.exists(mp4):
            edit(chat, mid, "⏳ (۱/۴) دانلود: %.0f مگ..." % (os.path.getsize(mp4) / 1048576))
    if p.returncode != 0 or not os.path.exists(mp4):
        edit(chat, mid, "❌ دانلود نشد.")
        return
    is_admin = str(uid) in ADMIN if uid else False
    key = gs.get_user_key(uid, is_admin=is_admin) if uid else gs.get_key()
    if not key:
        key = gs.get_key()
    if not key:
        edit(chat, mid, "❌ سرویس موقتاً با مشکل مواجه شده است.")
        return
    w_pv, h_pv = get_video_res(pv)
    fs_pv = 12 if w_pv < h_pv else 9
    margin_pv = 30 if w_pv < h_pv else 15
    style = f"FontName=Vazirmatn,FontSize={fs_pv},PrimaryColour=&H0000FFFF,OutlineColour=&H00000000,BackColour=&H00000000,BorderStyle=1,Outline=1.2,Shadow=0,MarginV={margin_pv},Alignment=2"
    edit(chat, mid, "👀 (۰/۴) ساخت پیش‌نمایش ۲ دقیقه‌ای...")
    pv = base + "_pv.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "0", "-t", "120",
                    "-i", mp4, "-c", "copy", pv])
    pva = base + "_pv.mp3"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-threads", "2",
                    "-i", pv, "-vn", "-c:a", "libmp3lame", "-b:a",
                    "128k", pva])
    psp = base + "_pv.srt"
    if whisper_srt(chat, mid, pva, psp, "👀 پیش‌نمایش/زیرنویس"):
        psrt = clean_srt(open(psp).read())
        open(psp, "w").write(psrt)
    else:
        psrt = ""
    if " --> " in psrt:
        pout = base + "_pv_sub.mp4"
        w, h = get_video_res(pv)
        style = get_style_for_res(w, h)
        r0 = subprocess.run(["ffmpeg", "-y", "-v", "error", "-threads",
                             "2", "-i", pv, "-vf",
                             "subtitles=" + psp + ":fontsdir=" + FONTS +
                             ":force_style='" + style + "'",
                             "-c:v", "libx264", "-crf", "23", "-preset",
                             "fast", "-c:a", "copy", pout])
        if r0.returncode == 0 and os.path.exists(pout):
            try:
                plink = gofile_upload(pout)
                send(chat, f"👀 پیش‌نمایش ۲ دقیقه‌ای:\n{plink}\nاگه اوکیه صبر کن تا نسخه کامل بیاد.")
            except Exception:
                pass
    for f in (pv, pva):
        try:
            os.remove(f)
        except OSError:
            pass
    edit(chat, mid, "🎧 (۲/۴) جدا کردن صدا...")
    mp3 = base + ".mp3"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-threads", "2",
                    "-i", mp4, "-vn", "-c:a", "libmp3lame", "-b:a",
                    "128k", mp3])
    sp = base + ".srt"
    if not whisper_srt(chat, mid, mp3, sp, "🧠 (۳/۴) زیرنویس دقیق"):
        edit(chat, mid, "❌ زیرنویس ساخته نشد.")
        return
    srt = open(sp).read()
    if " --> " not in srt:
        edit(chat, mid, "❌ زیرنویس ساخته نشد.")
        return
    open(sp, "w").write(clean_srt(srt))
    send_doc(chat, sp, "📄 زیرنویس فارسی")
    edit(chat, mid, "🎬 (۴/۴) چسبوندن زیرنویس...")
    w, h = get_video_res(mp4)
    style = get_style_for_res(w, h)
    out = base + "_sub.mp4"
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-threads", "2",
                        "-i", mp4, "-vf",
                        "subtitles=" + sp + ":fontsdir=" + FONTS +
                        ":force_style='" + style + "'",
                        "-c:v", "libx264", "-crf", "23", "-preset",
                        "fast", "-c:a", "copy", out])
    if r.returncode != 0 or not os.path.exists(out):
        edit(chat, mid, "❌ چسبوندن نشد ولی srt رو داری بالا 👆")
        return
    edit(chat, mid, "📤 آپلود نسخه نهایی...")
    try:
        link = gofile_upload(out)
        edit(chat, mid, f"✅ تمومه! ویدیوی زیرنویس‌دار:\n{link}")
    except Exception as e:
        edit(chat, mid, f"❌ آپلود نشد: {e}")
    finally:
        for f in (mp4, mp3, sp, out, dl):
            try:
                os.remove(f)
            except OSError:
                pass

def handle_job(chat, url, tag):
    base = f"{WORKDIR}/{tag}"
    mp4 = base + ".mp4"
    mid = send(chat, "⏳ دانلود شروع شد...")
    src = url
    dl = base + "_dl.mp4"
    if "drive.google.com" in url:
        rc = ytdl_progress(chat, mid, url, dl, "⏳ دانلود از درایو")
        if rc != 0 or not os.path.exists(dl):
            edit(chat, mid, "❌ دانلود از درایو نشد. دسترسی لینک باید public باشه.")
            return
        src = dl
    p = subprocess.Popen(
        ["ffmpeg", "-y", "-v", "error", "-i", src, "-c", "copy", mp4])
    while p.poll() is None:
        time.sleep(15)
        if os.path.exists(mp4):
            mb = os.path.getsize(mp4) / 1048576
            edit(chat, mid, "⏳ دانلود: %.0f مگ تا اینجا..." % mb)
    if p.returncode != 0 or not os.path.exists(mp4):
        edit(chat, mid, "❌ دانلود نشد. لینک خرابه یا فرمتش پشتیبانی نمیشه.")
        return
    edit(chat, mid, "✅ دانلود تموم شد.")
    send(chat, "🎧 دارم صدا رو جدا می‌کنم...")
    mp3 = base + ".mp3"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-threads", "2",
                    "-i", mp4, "-vn", "-c:a", "libmp3lame", "-b:a",
                    "128k", mp3])
    send(chat, "📤 دارم می‌فرستم بالا (فایل بزرگه، طول می‌کشه)...")
    try:
        l1 = gofile_upload(mp4)
        msg = f"✅ ویدیو آماده‌ست:\n{l1}"
        if os.path.exists(mp3):
            l2 = gofile_upload(mp3)
            msg += f"\n\n🎧 صدا:\n{l2}"
        send(chat, msg)
    except Exception as e:
        send(chat, f"❌ آپلود نشد: {e}")
    finally:
        for f in (mp4, mp3, dl):
            try:
                os.remove(f)
            except OSError:
                pass

def main():
    print("m3ubot polling...", flush=True)
    off = 0
    while True:
        try:
            u = tg("getUpdates", {"offset": off, "timeout": 40,
                                  "allowed_updates": ["message",
                                                      "callback_query"]},
                   timeout=70)
        except Exception as e:
            print("poll err:", e, flush=True)
            time.sleep(5)
            continue
        for up in u.get("result", []):
            off = up["update_id"] + 1
            cb = up.get("callback_query")
            if cb:
                cuid = str(((cb.get("from") or {}).get("id")))
                cb_id = cb.get("id")
                answer(cb_id)
                data = cb.get("data") or ""
                m = cb.get("message") or {}
                ch = (m.get("chat") or {}).get("id")
                mid = m.get("message_id")
                
                if data == "check_join":
                    if is_member(cuid):
                        edit(ch, mid, HELP_MAIN, kb=main_kb())
                    else:
                        send(ch, "❌ هنوز در کانال عضو نشدید! لطفاً عضو شوید و سپس دکمه زیر را بزنید:", kb=join_channel_kb())
                    continue

                if data.startswith("h:"):
                    if data == "h:setkey":
                        PENDING_KEY.add(str(cuid))
                        edit(ch, mid, "🔑 لطفاً کلید Gemini خود را بفرستید:\n(اگر کلید ندارید، می‌توانید از دکمه زیر به صورت رایگان از گوگل دریافت کنید)", kb={"inline_keyboard": [[{"text": "🌐 دریافت رایگان کلید Gemini", "url": "https://aistudio.google.com/app/apikey"}]]})
                    else:
                        edit(ch, mid, HELP.get(data[2:], HELP_MAIN))
                continue

            m = up.get("message") or {}
            uid = str((m.get("from") or {}).get("id"))
            chat = (m.get("chat") or {}).get("id")
            text = (m.get("text") or "").strip()

            # Membership gate for ALL non-admin users
            if not is_member(uid):
                send(chat, "⛔️ برای استفاده از ربات، لطفاً ابتدا در کانال زیر عضو شوید و سپس روی «بررسی مجدد» بزنید:", kb=join_channel_kb())
                continue

            # Handle user setting their own Gemini API key
            if uid in PENDING_KEY and text and not text.startswith("/"):
                gs.set_user_key(uid, text)
                PENDING_KEY.discard(uid)
                send(chat, "✅ کلید اختصاصی Gemini شما با موفقیت ذخیره شد!\nحالا می‌توانید هر ویدیویی بفرستید تا زیرنویس شود.")
                continue

            if text.startswith("/setkey"):
                parts = text.split(None, 1)
                if len(parts) < 2:
                    send(chat, "برای ثبت کلید Gemini خود، دستور را به این شکل بفرستید:\n/setkey YOUR_KEY\n\nبرای دریافت کلید رایگان به سایت زیر بروید:\nhttps://aistudio.google.com/app/apikey")
                else:
                    gs.set_user_key(uid, parts[1])
                    send(chat, "✅ کلید اختصاصی Gemini شما با موفقیت ذخیره شد!\nحالا می‌توانید ویدیوهای خود را بفرستید.")
                continue

            if text == "/key":
                k = gs.get_user_key(uid, is_admin=(uid in ADMIN))
                send(chat, "وضعیت کلید Gemini شما: " + (f"فعال ({k[:6]}...{k[-4:]})" if k else "❌ ست نشده! از دستور /setkey استفاده کنید."))
                continue

            # Admin-only commands
            if text.startswith("/font"):
                if uid not in ADMIN:
                    send(chat, "⛔️ این دستور فقط برای ادمین ربات مجاز است.")
                    continue
            if text.startswith("/font"):
                parts = text.split(None, 1)
                try:
                    val = float(parts[1])
                    if 0.01 <= val <= 0.10:
                        gs.set_scale(val)
                        send(chat, f"✅ اندازه فونت تنظیم شد: {val*100:.1f}٪ ارتفاع ویدیو (تغییر بلافاصله اعمال شد)")
                    else:
                        send(chat, "عدد باید بین ۰.۰۱ تا ۰.۱۰ باشه (مثلاً ۰.۰۳ یعنی ۳٪)")
                except (IndexError, ValueError):
                    send(chat, "مثال: /font 0.03")
                continue
            if text == "/start":
                send(chat, HELP_MAIN, main_kb())
                continue
            vid = m.get("video") or {}
            doc = m.get("document") or {}
            dmt = doc.get("mime_type") or ""
            dname = doc.get("file_name") or ""
            if dname == "cookies.txt" and doc.get("file_id"):
                if download_tg_file(doc.get("file_id"), COOKIES):
                    send(chat, "✅ کوکی ذخیره شد. حالا لینک یوتیوب رو بده.")
                else:
                    send(chat, "❌ دانلود کوکی نشد.")
                continue
            fid = vid.get("file_id") or (doc.get("file_id") if dmt.startswith("video") else None)
            if fid:
                fsize = vid.get("file_size") or doc.get("file_size") or 0
                if fsize > 19 * 1024 * 1024:
                    send(chat, "❌ فایل بالای ۱۹ مگه، تلگرام به ربات بیشتر نمیده.")
                    continue
                src = f"{WORKDIR}/in{up['update_id']}.mp4"
                send(chat, "⏳ گرفتمش...")
                try:
                    if download_tg_file(fid, src):
                        ap = f"{WORKDIR}/v{up['update_id']}.mp3"
                        r = subprocess.run(["ffmpeg", "-y", "-v", "error",
                                            "-threads", "2", "-i", src, "-vn",
                                            "-c:a", "libmp3lame", "-b:a",
                                            "128k", ap])
                        if r.returncode == 0 and os.path.exists(ap):
                            send_doc(chat, ap, "🎧 صدا")
                            do_transcribe(chat, ap, f"v{up['update_id']}",
                                          burn_video=src, uid=uid)
                            try:
                                os.remove(ap)
                            except OSError:
                                pass
                        else:
                            send(chat, "❌ استخراج صدا نشد.")
                    else:
                        send(chat, "❌ دانلود فایل از تلگرام نشد.")
                except Exception as e:
                    print(f"Error processing video {up['update_id']}: {e}", flush=True)
                    send(chat, f"❌ خطا در پردازش: {str(e)[:150]}")
                finally:
                    try:
                        if os.path.exists(src):
                            os.remove(src)
                    except OSError:
                        pass
                continue
            aud = m.get("audio") or m.get("voice") or {}
            adoc = m.get("document") or {}
            amt = adoc.get("mime_type") or ""
            afid = aud.get("file_id") or (adoc.get("file_id") if amt.startswith("audio") else None)
            if afid:
                asize = aud.get("file_size") or adoc.get("file_size") or 0
                if asize > 19 * 1024 * 1024:
                    send(chat, "❌ صوت بالای ۱۹ مگه. لینک مستقیمش رو بده.")
                    continue
                ap = f"{WORKDIR}/a{up['update_id']}.bin"
                send(chat, "⏳ گرفتمش...")
                if download_tg_file(afid, ap):
                    do_transcribe(chat, ap, f"s{up['update_id']}", uid=uid)
                else:
                    send(chat, "❌ دانلود نشد.")
                continue
            elif "http" in text:
                if text.startswith("sub:"):
                    url = text[4:].strip().split()[0]
                    handle_auto(chat, url, f"a{up['update_id']}", uid=uid)
                    continue
                url = text.split()[0]
                low = url.lower().split("?")[0]
                if low.endswith((".mp3", ".m4a", ".wav", ".ogg", ".opus", ".flac")):
                    ap = f"{WORKDIR}/u{up['update_id']}.bin"
                    send(chat, "⏳ دارم فایل صوتی رو می‌گیرم...")
                    r = subprocess.run(["curl", "-sL", "-m", "590", "-o", ap, url])
                    if r.returncode == 0 and os.path.exists(ap):
                        do_transcribe(chat, ap, f"u{up['update_id']}", uid=uid)
                    else:
                        send(chat, "❌ دانلود لینک نشد.")
                else:
                    if low.endswith(".mp4"):
                        try:
                            h = subprocess.run(["curl", "-sIL", "-m", "30", url],
                                               capture_output=True, text=True).stdout
                            mm = re.search(r"(?i)content-length:\s*(\d+)", h)
                            size = int(mm.group(1)) if mm else 0
                        except Exception:
                            size = 0
                        if size > 100 * 1024 * 1024:
                            send(chat, "❌ فایل بالای ۱۰۰ مگه. لینک کوتاه‌تر بده.")
                            continue
                        dest = f"{WORKDIR}/d{up['update_id']}.mp4"
                        send(chat, "⏳ دارم دانلود می‌کنم...")
                        r = subprocess.run(["curl", "-sL", "-m", "590",
                                            "--max-filesize", "104857600",
                                            "-o", dest, url])
                        if r.returncode != 0 or not os.path.exists(dest):
                            send(chat, "❌ دانلود نشد.")
                            continue
                        handle_small(chat, dest, f"d{up['update_id']}")
                        continue
                    if "youtube.com" in low or "youtu.be" in low:
                        handle_yt(chat, url, f"job{up['update_id']}")
                    else:
                        u2 = text[4:].strip().split()[0] if text.startswith("sub:") else url
                        handle_auto(chat, u2, f"a{up['update_id']}", uid=uid)

main()
