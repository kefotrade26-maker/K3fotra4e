"""
po_auth.py — Extract Pocket Option demo session from a Chrome HAR file.
"""
import json
import os
import time

HAR_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session.har")

_CACHE = {"auth": None, "loaded_at": 0.0}
_CACHE_TTL = 3600

def _log(msg):
    print("[po_auth] " + msg)

def _validate(info):
    session = info.get("session")
    uid = info.get("uid")
    is_demo = info.get("isDemo")

    if not isinstance(session, str) or len(session) < 20:
        raise ValueError(f"Invalid session (len={len(session) if session else 0})")
    if uid is None:
        raise ValueError("Missing uid")
    if is_demo is None:
        raise ValueError("Missing isDemo")

    is_demo = int(is_demo)
    if is_demo != 1:
        raise RuntimeError("Real account detected. Log in with a DEMO account.")

    try:
        uid_val = int(uid)
    except (ValueError, TypeError):
        uid_val = uid

    return {
        "session": session,
        "uid": uid_val,
        "isDemo": is_demo,
        "platform": int(info.get("platform", 2)),
    }

def _iter_auth_from_parsed_har(har):
    entries = har.get("log", {}).get("entries", [])
    real_seen = False

    for entry in reversed(entries):
        for msg in reversed(entry.get("_webSocketMessages", [])):
            data = msg.get("data", "")
            if not isinstance(data, str) or not data.startswith('42["auth"'):
                continue
            try:
                payload = json.loads(data[2:])
            except Exception:
                continue
            if not (isinstance(payload, list) and len(payload) >= 2):
                continue
            if payload[0] != "auth" or not isinstance(payload[1], dict):
                continue

            info = payload[1]
            if not all(k in info for k in ("session", "uid", "isDemo")):
                continue

            try:
                return _validate(info)
            except RuntimeError:
                real_seen = True
                continue
            except ValueError:
                continue

    if real_seen:
        raise RuntimeError("Only real-account sessions found in HAR. Switch to DEMO.")
    raise RuntimeError("No valid auth message found in HAR.")

def _extract_auth_messages_from_raw(text):
    results = []
    i = 0
    n = len(text)

    while i < n:
        idx = text.find('"data"', i)
        if idx < 0:
            break

        j = idx + 6
        while j < n and text[j] in ' \t\r\n:':
            j += 1
        if j >= n or text[j] != '"':
            i = idx + 6
            continue

        start = j + 1
        k = start
        while k < n:
            c = text[k]
            if c == '\\':
                k += 2
                continue
            if c == '"':
                if k + 1 >= n or text[k + 1] in ',} \t\r\n':
                    break
            k += 1

        if k >= n:
            break

        raw_value = text[start:k]

        if (raw_value.startswith('42[\\"auth\\"') or raw_value.startswith('42["auth"')):
            try:
                unescaped = json.loads('"' + raw_value + '"')
            except Exception:
                i = k + 1
                continue

            if not unescaped.startswith('42["auth",'):
                i = k + 1
                continue

            try:
                payload = json.loads(unescaped[2:])
            except Exception:
                i = k + 1
                continue

            if not (isinstance(payload, list) and len(payload) >= 2):
                i = k + 1
                continue
            if payload[0] != "auth" or not isinstance(payload[1], dict):
                i = k + 1
                continue

            info = payload[1]
            if all(kk in info for kk in ("session", "uid", "isDemo")):
                results.append(info)

        i = k + 1

    return results

def _iter_auth_from_raw_text(text):
    candidates = _extract_auth_messages_from_raw(text)
    if not candidates:
        raise RuntimeError("Could not find any auth message in HAR.")

    real_seen = False
    for info in reversed(candidates):
        try:
            return _validate(info)
        except RuntimeError:
            real_seen = True
            continue
        except ValueError:
            continue

    if real_seen:
        raise RuntimeError("Only real-account sessions found in HAR. Switch to DEMO.")
    raise RuntimeError("Found auth entries in HAR but none are valid.")

def _parse_har(path):
    if not os.path.isfile(path):
        raise FileNotFoundError(f"session.har not found at:\n  {path}")

    size = os.path.getsize(path)
    if size < 1000:
        raise ValueError(f"session.har too small ({size} bytes).")

    try:
        with open(path, "r", encoding="utf-8") as f:
            har = json.load(f)
        return _iter_auth_from_parsed_har(har)
    except json.JSONDecodeError as e:
        _log(f"⚠️  HAR is corrupt ({e.msg}). Trying robust text scan...")
    except (RuntimeError, ValueError):
        raise

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    return _iter_auth_from_raw_text(text)

def extract_auth(force_reload=False):
    now = time.time()
    cached = _CACHE["auth"]
    if cached and not force_reload and (now - _CACHE["loaded_at"] < _CACHE_TTL):
        return cached

    try:
        age_h = (now - os.stat(HAR_FILE).st_mtime) / 3600.0
        if age_h > 12:
            _log(f"⚠️  session.har is {age_h:.1f}h old — may be expired")
    except OSError:
        pass

    auth = _parse_har(HAR_FILE)
    _CACHE["auth"] = auth
    _CACHE["loaded_at"] = now
    return auth