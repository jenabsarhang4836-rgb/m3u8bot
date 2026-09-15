"""Whisper timing (Turkish speech -> precise word-level segments) + Gemini translation -> Persian SRT.
Whisper handles exact millisecond sync.
Gemini handles natural, colloquial Persian translation.
"""
import json
import os
import re
import subprocess
import sys
import urllib.request
import gemini_sub as gs

BASE = "https://generativelanguage.googleapis.com"
MODEL = "gemini-3.6-flash"


def whisper_segments(path):
    from faster_whisper import WhisperModel
    m = WhisperModel("base", device="cpu", compute_type="int8", download_root="/tmp/models")
    # By using word_timestamps=False and simply relying on default segmenting (which is VAD-aware), 
    # we get much better and native sync.
    segs, _ = m.transcribe(path, language="tr", vad_filter=True, 
                           vad_parameters={"min_silence_duration_ms": 300})
    out = []
    for s in segs:
        t = s.text.strip()
        if t:
            out.append((s.start, s.end, t))
    return out



def to_ts(sec):
    ms = max(0, int(sec * 1000))
    h, ms = divmod(ms, 3600000)
    mi, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d,%03d" % (h, mi, s, ms)


def translate(texts, key):
    lines = "\n".join("%d. %s" % (i + 1, t) for i, t in enumerate(texts))
    prompt = ("Translate these Turkish subtitle lines to natural, colloquial Iranian Persian (فارسی محاوره‌ای و روان).
"
              "CRITICAL RULES:
"
              "1. You MUST keep the EXACT same line numbers. Do not merge or split lines.
"
              "2. Translate meaning naturally (not literal). Keep it short (max 38 chars).
"
              "3. Reply ONLY with the numbered list, like:
1. سلام
2. چطوری؟

" + lines)
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": 65536}}
    req = urllib.request.Request(
        "%s/v1beta/models/%s:generateContent?key=%s" % (BASE, MODEL, key),
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        resp = json.loads(r.read().decode())
    txt = ""
    for c in resp.get("candidates", []):
        for p in (c.get("content") or {}).get("parts", []):
            txt += p.get("text", "")
    out = {}
    for ln in txt.strip().split("\n"):
        ln = ln.strip().strip("`")
        if not ln or "." not in ln:
            continue
        num, _, rest = ln.partition(".")
        if num.strip().isdigit():
            out[int(num.strip())] = rest.strip().replace("\\n", "\n")
    return [out.get(i + 1, texts[i]) for i in range(len(texts))]


def build_srt(segs, trans):
    blocks = []
    for i, ((a, b, _), t) in enumerate(zip(segs, trans)):
        blocks.append("%d\n%s --> %s\n%s" % (i + 1, to_ts(a), to_ts(b), t))
    return "\n\n".join(blocks) + "\n"


def run_pipeline(audio_path, srt_path, key):
    print("whisper: extracting audio timestamps...", flush=True)
    segs = whisper_segments(audio_path)
    print("whisper found %d speech segments" % len(segs), flush=True)
    if not segs:
        return False
    fa = []
    for i in range(0, len(segs), 40):
        batch = [t for _, _, t in segs[i:i + 40]]
        fa += translate(batch, key)
        print("translated %d/%d" % (min(i + 40, len(segs)), len(segs)), flush=True)
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write(build_srt(segs, fa))
    print("SRT_OK: %d lines" % len(segs), flush=True)
    return True


if __name__ == "__main__":
    if len(sys.argv) >= 4:
        run_pipeline(sys.argv[1], sys.argv[2], sys.argv[3])
    elif len(sys.argv) >= 3:
        k = gs.get_key()
        run_pipeline(sys.argv[1], sys.argv[2], k)
