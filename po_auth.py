"""
po_auth.py — Extract Pocket Option demo session from a Chrome HAR file.

Robust version:
  * Handles truncated / corrupted HAR files (Chrome sometimes cuts large HARs).
  * First tries json.load(); if that fails, falls back to a state-machine
    that extracts auth messages directly from the raw (JSON-escaped) text.
  * Rejects HAR files older than 24h (likely expired session).

Public API:
    extract_auth(force_reload=False) -> dict
    build_ssid(auth) -> str

Python 3.7+ compatible.
"""
import json
import os
import time

HAR_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session.har")

_CACHE = {"auth": None, "loaded_at": 0.0}
_CACHE_TTL = 3600  # re-read HAR after 1 hour

# ── Freshness thresholds ────────────────────────────────────────────────
HAR_WARN_AGE_HOURS = 12    # تحذير
HAR_REJECT_AGE_HOURS = 24  # رفض قاطع


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════
def _log(msg):
    # type: (str) -> None
    print("[po_auth] " + msg)


def _validate(info):
    # type: (dict) -> dict
    session = info.get("session")
    uid = info.get("uid")
    is_demo = info.get("isDemo")

    if not isinstance(session, str) or len(session) < 20:
        raise ValueError(
            "Invalid session (len={})".format(len(session) if session else 0)
        )
    if uid is None:
        raise ValueError("Missing uid")
    if is_demo is None:
        raise ValueError("Missing isDemo")

    is_demo = int(is_demo)
    if is_demo != 1:
        raise RuntimeError(
            "Real account detected. Log in with a DEMO account, "
            "open Pocket Option in Chrome, export a fresh HAR."
        )

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


# ═══════════════════════════════════════════════════════════════════════════
# Strategy 1: parse whole HAR with json.load (fast path)
# ═══════════════════════════════════════════════════════════════════════════
def _iter_auth_from_parsed_har(har):
    # type: (dict) -> dict
    entries = har.get("log", {}).get("entries", [])
    real_seen = False

    for entry in reversed(entries):
        for msg in reversed(entry.get("_webSocketMessages", [])):
            data = msg.get("data", "")
            if not isinstance(data, str):
                continue
            if not data.startswith('42["auth"'):
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
        raise RuntimeError(
            "Only real-account sessions found in HAR. Switch to DEMO."
        )
    raise RuntimeError(
        "No valid auth message found in HAR. "
        "Open a chart in PO, wait 30s, then re-export."
    )


# ═══════════════════════════════════════════════════════════════════════════
# Strategy 2: state-machine over raw text (fallback for truncated HARs)
# ═══════════════════════════════════════════════════════════════════════════
def _extract_auth_messages_from_raw(text):
    # type: (str) -> list
    """
    Walk the raw HAR text, find every `"data": "42[\\"auth\\", ..."` field,
    unescape it, parse as JSON, and return the list of valid auth dicts.

    Works even when the file is cut off mid-way.
    """
    results = []
    i = 0
    n = len(text)

    while i < n:
        # Locate the next "data" key
        idx = text.find('"data"', i)
        if idx < 0:
            break

        # Skip whitespace + colon
        j = idx + 6  # past the literal `"data"`
        while j < n and text[j] in ' \t\r\n:':
            j += 1
        if j >= n or text[j] != '"':
            i = idx + 6
            continue

        # Opening quote of the string value
        start = j + 1
        k = start
        while k < n:
            c = text[k]
            if c == '\\':
                k += 2            # skip escaped char
                continue
            if c == '"':
                # A real end-of-string must be followed by , } or whitespace
                if k + 1 >= n or text[k + 1] in ',} \t\r\n':
                    break
            k += 1

        if k >= n:
            break  # file truncated inside the string — give up

        raw_value = text[start:k]  # still JSON-escaped

        # Quick check: does it look like an auth frame?
        if (raw_value.startswith('42[\\"auth\\"') or
                raw_value.startswith('42["auth"')):
            try:
                # Un-escape by treating it as a JSON string literal
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
    # type: (str) -> dict
    candidates = _extract_auth_messages_from_raw(text)

    if not candidates:
        raise RuntimeError(
            "Could not find any auth message in HAR, even in fallback mode.\n"
            "  -> Make sure you opened a chart in PO before exporting.\n"
            "  -> Re-export the HAR from Chrome."
        )

    real_seen = False
    # Try newest first (last found in file)
    for info in reversed(candidates):
        try:
            return _validate(info)
        except RuntimeError:
            real_seen = True
            continue
        except ValueError:
            continue

    if real_seen:
        raise RuntimeError(
            "Only real-account sessions found in HAR. Switch to DEMO."
        )
    raise RuntimeError(
        "Found auth entries in HAR but none are valid (bad session/uid)."
    )


# ═══════════════════════════════════════════════════════════════════════════
# Public entry point
# ═══════════════════════════════════════════════════════════════════════════
def _parse_har(path):
    # type: (str) -> dict
    if not os.path.isfile(path):
        raise FileNotFoundError(
            "session.har not found at:\n  {}\n"
            "Export it from Chrome DevTools -> Network -> WS -> "
            "'Save all as HAR with content'.".format(path)
        )

    size = os.path.getsize(path)
    if size < 1000:
        raise ValueError(
            "session.har too small ({} bytes). Export failed.".format(size)
        )

    # ── Fast path: whole file is valid JSON ─────────────────────────────────
    try:
        with open(path, "r", encoding="utf-8") as f:
            har = json.load(f)
        return _iter_auth_from_parsed_har(har)
    except json.JSONDecodeError as e:
        _log("⚠️  HAR is corrupt ({}). Trying robust text scan...".format(e.msg))
    except (RuntimeError, ValueError):
        # Valid JSON but no usable auth — propagate immediately
        raise

    # ── Fallback: scan raw text ─────────────────────────────────────────────
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    return _iter_auth_from_raw_text(text)


def extract_auth(force_reload=False):
    # type: (bool) -> dict
    """
    Extract auth credentials from session.har.

    Raises:
        FileNotFoundError: HAR missing.
        ValueError: HAR too small / invalid.
        RuntimeError: HAR too old (>24h), or real account, or no auth found.
    """
    now = time.time()
    cached = _CACHE["auth"]
    if cached and not force_reload and (now - _CACHE["loaded_at"] < _CACHE_TTL):
        return cached

    # ── Freshness check — REJECT stale HARs ─────────────────────────────────
    try:
        age_h = (now - os.stat(HAR_FILE).st_mtime) / 3600.0
        if age_h > HAR_REJECT_AGE_HOURS:
            raise RuntimeError(
                "session.har is {:.1f}h old — likely expired.\n"
                "  -> Re-export HAR from Chrome (Network → WS → "
                "'Save all as HAR with content').".format(age_h)
            )
        if age_h > HAR_WARN_AGE_HOURS:
            _log(
                "⚠️  session.har is {:.1f}h old — may be expired soon".format(age_h)
            )
    except OSError:
        pass

    auth = _parse_har(HAR_FILE)
    _CACHE["auth"] = auth
    _CACHE["loaded_at"] = now
    return auth


def build_ssid(auth):
    # type: (dict) -> str
    return (
        '42["auth",'
        + json.dumps(
            {
                "session": auth["session"],
                "isDemo": auth["isDemo"],
                "uid": auth["uid"],
                "platform": auth["platform"],
            },
            separators=(",", ":"),
        )
        + "]"
    )


# ═══════════════════════════════════════════════════════════════════════════
# CLI self-test
# ═══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    a = extract_auth(force_reload=True)
    _log("Pocket Option authentication OK")
    _log("UID      : {} (type={})".format(a["uid"], type(a["uid"]).__name__))
    _log("Account  : DEMO")
    _log("Platform : {}".format(a["platform"]))
    _log("Session  : <redacted, {} chars>".format(len(a["session"])))
