#!/usr/bin/env python3
"""
chunk_gemini_sub.py
Transcribe long audio to Persian SRT using Gemini File API with chunking.
Avoids OOM and token limit truncation.
"""
import os
import sys
import json
import time
import re
import subprocess
import urllib.request
from datetime import timedelta
import gemini_sub as gs

MODEL = "gemini-3.6-flash"
BASE = "https://generativelanguage.googleapis.com"
CHUNK_SECONDS = 900 # 15 minutes per chunk

def get_duration(audio_path):
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1", audio_path]
    out = subprocess.check_output(cmd).decode().strip()
    return float(out)

def split_audio(audio_path, out_dir, chunk_len=CHUNK_SECONDS):
    os.makedirs(out_dir, exist_ok=True)
    duration = get_duration(audio_path)
    chunks = []
    start = 0.0
    idx = 0
    while start < duration:
        length = min(chunk_len, duration - start)
        chunk_path = os.path.join(out_dir, f"chunk_{idx:03d}.wav")
        # Place -i before -ss for exact audio frame seeking without drift
        cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-i", audio_path,
            "-ss", str(start),
            "-t", str(length),
            "-ar", "16000",
            "-ac", "1",
            "-c:a", "pcm_s16le",
            chunk_path
        ]
        subprocess.run(cmd, check=True)
        chunks.append((idx, start, chunk_path))
        start += chunk_len
        idx += 1
    return chunks

def parse_time(t_str):
    # Parses 00:01:23,456 or 00:01:23.456
    t_str = t_str.replace(',', '.')
    parts = t_str.split(':')
    if len(parts) == 3:
        h, m, s = parts
        return float(h) * 3600 + float(m) * 60 + float(s)
    elif len(parts) == 2:
        m, s = parts
        return float(m) * 60 + float(s)
    return float(parts[0])

def format_time(seconds):
    td = timedelta(seconds=seconds)
    total_sec = int(td.total_seconds())
    ms = int((seconds - total_sec) * 1000)
    hours = total_sec // 3600
    minutes = (total_sec % 3600) // 60
    secs = total_sec % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"

def adjust_srt_offset(srt_text, offset_sec):
    lines = srt_text.strip().splitlines()
    out = []
    time_pat = re.compile(r'(\d{1,2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,\.]\d{3})')
    for line in lines:
        m = time_pat.search(line)
        if m:
            start_s = parse_time(m.group(1)) + offset_sec
            end_s = parse_time(m.group(2)) + offset_sec
            new_time = f"{format_time(start_s)} --> {format_time(end_s)}"
            out.append(new_time)
        else:
            out.append(line)
    return "\n".join(out)

def transcribe_chunk(chunk_path, key):
    uri, mime = gs.upload_file(chunk_path, key)
    prompt = (
        "You are an expert Turkish to Persian translator and subtitler. "
        "Listen carefully to this Turkish audio track. "
        "Generate a complete, synchronized subtitle in Persian (.srt format) for ALL dialogue spoken in this audio. "
        "Requirements:\n"
        "- Format strictly as valid SRT with numbered cues and exact timestamps (00:00:00,000 --> 00:00:00,000).\n"
        "- Translate naturally, accurately, and colloquially into Persian.\n"
        "- Do not omit any spoken lines.\n"
        "- Output ONLY the raw SRT text, no markdown backticks, no explanations."
    )
    body = {
        "contents": [{
            "parts": [
                {"file_data": {"mime_type": mime, "file_uri": uri}},
                {"text": prompt}
            ]
        }],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 8192
        }
    }
    url = f"{BASE}/v1beta/models/{MODEL}:generateContent?key={key}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        resp = json.loads(r.read().decode())
    raw = resp["candidates"][0]["content"]["parts"][0]["text"]
    raw = re.sub(r"^```(?:srt)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return raw.strip()

def process_full_audio(audio_path, output_srt, key, progress_cb=None):
    temp_dir = "/tmp/audio_chunks_" + str(int(time.time()))
    print(f"Splitting {audio_path} into chunks...")
    chunks = split_audio(audio_path, temp_dir, chunk_len=CHUNK_SECONDS)
    print(f"Total chunks: {len(chunks)}")
    
    all_cues = []
    cue_counter = 1
    
    for idx, start_sec, c_path in chunks:
        msg = f"Transcribing chunk {idx+1}/{len(chunks)} ({int(start_sec//60)}m - {int((start_sec+CHUNK_SECONDS)//60)}m)..."
        print(msg)
        if progress_cb:
            progress_cb(idx + 1, len(chunks), msg)
        
        for attempt in range(3):
            try:
                raw_srt = transcribe_chunk(c_path, key)
                adj_srt = adjust_srt_offset(raw_srt, start_sec)
                
                # Extract cues and renumber
                cues = re.split(r'\n\s*\n', adj_srt.strip())
                for cue in cues:
                    c_lines = cue.strip().splitlines()
                    if len(c_lines) >= 2:
                        # Find time line
                        time_line_idx = -1
                        for i_l, l in enumerate(c_lines):
                            if "-->" in l:
                                time_line_idx = i_l
                                break
                        if time_line_idx != -1:
                            t_line = c_lines[time_line_idx]
                            text_lines = "\n".join(c_lines[time_line_idx+1:])
                            if text_lines.strip():
                                all_cues.append(f"{cue_counter}\n{t_line}\n{text_lines}")
                                cue_counter += 1
                break
            except Exception as e:
                print(f"Error on chunk {idx+1} (attempt {attempt+1}): {e}")
                time.sleep(5)
                if attempt == 2:
                    print(f"Skipping chunk {idx+1} after 3 fails.")
        
        try:
            os.remove(c_path)
        except OSError:
            pass

    full_srt_content = "\n\n".join(all_cues) + "\n"
    with open(output_srt, "w", encoding="utf-8") as f:
        f.write(full_srt_content)
    
    try:
        os.rmdir(temp_dir)
    except OSError:
        pass
    print(f"Finished! Total cues: {len(all_cues)}. Saved to {output_srt}")
    return output_srt

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: chunk_gemini_sub.py <audio_path> <output_srt>")
        sys.exit(1)
    k = gs.get_key()
    process_full_audio(sys.argv[1], sys.argv[2], k)
