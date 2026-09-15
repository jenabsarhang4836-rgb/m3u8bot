import json, os, urllib.request, time

CFG = "/data/gemini_cfg.json"
BASE = "https://generativelanguage.googleapis.com"
MODEL = "gemini-3.6-flash"

def _cfg():
    try:
        return json.load(open(CFG))
    except OSError:
        return {}

def set_key(keys_str):
    c = _cfg()
    # allow comma-separated or space-separated multiple keys
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
    # Backwards compatibility, but whisper_pipeline should use rotate
    keys = get_keys()
    return keys[0] if keys else ""

import random
def get_random_key():
    keys = get_keys()
    return random.choice(keys) if keys else ""
