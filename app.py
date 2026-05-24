# -*- coding: utf-8 -*-
"""
Stream-resolver microservice – Flask + yt-dlp
Resolves YouTube (and other) URLs into direct audio/video stream URLs.

Key fixes vs original:
  - UTF-8 encoding declaration (fixes SyntaxError from stray non-ASCII char)
  - Correct player_client names (tv_embedded -> android_vr,web_safari, the new default)
  - js_runtimes configured so Node/Deno on the host are picked up automatically
  - Multi-client fallback list for resilience
  - format= left unset so yt-dlp picks the best available (avoids "format not available")
  - gunicorn-friendly (no app.run() side-effects at import time)
"""

import os
import sys
import base64
import logging
import tempfile

import yt_dlp
from flask import Flask, request, jsonify

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Cookie helper
# ---------------------------------------------------------------------------
def _write_cookie_file() -> str | None:
    """Decode the base-64 cookie blob from the environment and write it to a
    temporary file.  Returns the file path, or None if the env-var is absent."""
    b64 = os.environ.get("YT_COOKIES_B64")
    if not b64:
        log.info("No YT_COOKIES_B64 found – proceeding without cookies")
        return None
    try:
        data = base64.b64decode(b64)
        tmp = tempfile.NamedTemporaryFile(
            delete=False, suffix=".txt", mode="wb"
        )
        tmp.write(data)
        tmp.flush()
        log.info("Cookies written to %s", tmp.name)
        return tmp.name
    except Exception as exc:
        log.error("Failed to decode/write cookies: %s", exc)
        return None


COOKIE_FILE: str | None = _write_cookie_file()


# ---------------------------------------------------------------------------
# yt-dlp option builder
# ---------------------------------------------------------------------------
def _build_ydl_opts() -> dict:
    """
    Return a yt-dlp options dict tuned for stream-URL resolution only
    (no actual download).

    Player-client strategy (2025-11+):
      - yt-dlp now requires a JS runtime for YouTube; the default clients
        android_vr + web_safari cover most cases without needing cookies.
      - With cookies, tv_downgraded + web_safari (free) or web_creator (premium)
        are used automatically by yt-dlp internally.
      - We explicitly list a robust fallback chain to maximise format availability.

    JS runtime:
      - yt-dlp[default] ships yt-dlp-ejs.
      - We enable Node as a fallback runtime in case Deno isn't on $PATH.
        Render.com Ubuntu images include Node; adjust if using a different host.
    """
    opts: dict = {
        # ---- extraction flags ------------------------------------------------
        "skip_download": True,
        "noplaylist": True,

        # ---- silence most chatter (our logging captures what we need) --------
        "quiet": True,
        "no_warnings": False,

        # ---- player clients: default chain works for most public videos ------
        # Valid client names (yt-dlp 2025.11+):
        #   web, web_safari, web_embedded, web_music, web_creator,
        #   mweb, ios, android, android_vr, tv, tv_downgraded, tv_simply
        "extractor_args": {
            "youtube": {
                "player_client": ["android_vr", "web_safari", "ios", "tv"],
            }
        },

        # ---- JS runtime (required for YouTube since 2025.11.12) --------------
        # yt-dlp-ejs is included when you install with `pip install yt-dlp[default]`
        # Deno is the default runtime; Node is a common fallback on Linux hosts.
        "js_runtimes": {
            "deno": {},           # use system deno if on PATH
            "node": {},           # fall back to node if deno is absent
        },

        # ---- format: let yt-dlp decide what's available ---------------------
        # Do NOT hard-code a format string here; that's what caused
        # "Requested format is not available" in the original logs.
    }

    if COOKIE_FILE:
        opts["cookiefile"] = COOKIE_FILE

    return opts


# ---------------------------------------------------------------------------
# Format helpers
# ---------------------------------------------------------------------------
def _best_audio(formats: list) -> dict | None:
    audio = [
        f for f in formats
        if f.get("vcodec") == "none"
        and f.get("acodec") not in (None, "none")
        and f.get("url")
    ]
    if not audio:
        return None
    return max(audio, key=lambda f: f.get("abr") or f.get("tbr") or 0)


def _best_video_only(formats: list, preferred_height: int = 720) -> dict | None:
    video = [
        f for f in formats
        if f.get("acodec") in (None, "none")
        and f.get("vcodec") not in (None, "none")
        and f.get("url")
        and f.get("height")
    ]
    if not video:
        return None
    exact = next((f for f in video if f.get("height") == preferred_height), None)
    return exact or max(video, key=lambda f: f.get("height") or 0)


def _best_muxed(formats: list) -> dict | None:
    muxed = [
        f for f in formats
        if f.get("vcodec") not in (None, "none")
        and f.get("acodec") not in (None, "none")
        and f.get("url")
        and f.get("height")
    ]
    if not muxed:
        return None
    return max(muxed, key=lambda f: f.get("height") or 0)


def _error_payload(error: str, title: str = "") -> dict:
    return {
        "success": False,
        "error": error,
        "audioUrl": None,
        "videoUrl": None,
        "muxedVideoUrl": None,
        "duration": 0,
        "title": title,
        "uploaderUrl": None,
        "likeCount": -1,
        "viewCount": -1,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/health", methods=["GET", "HEAD"])
def health():
    return "OK", 200


@app.route("/resolve-stream", methods=["POST"])
def resolve_stream():
    body = request.get_json(silent=True) or {}
    url: str = body.get("url", "").strip()

    if not url:
        return jsonify(_error_payload("Missing url")), 400

    log.info("Resolving: %s", url)

    try:
        opts = _build_ydl_opts()

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        formats: list = info.get("formats", [])
        log.info("Total formats returned: %d", len(formats))

        # Debug: log first 10 formats
        for f in formats[:10]:
            log.info(
                "  id=%-12s acodec=%-10s vcodec=%-15s abr=%-6s height=%s url=%s",
                f.get("format_id"),
                f.get("acodec"),
                f.get("vcodec"),
                f.get("abr"),
                f.get("height"),
                bool(f.get("url")),
            )

        # --- audio ---
        best_audio_fmt = _best_audio(formats)
        audio_url: str | None = (
            best_audio_fmt["url"] if best_audio_fmt else info.get("url")
        )

        if not audio_url:
            log.warning("No direct audio URL found for %s", url)
            return jsonify(
                _error_payload(
                    "No direct audio URL – all streams may be cipher-protected or "
                    "require a PO token. Ensure yt-dlp[default] is installed and a "
                    "JS runtime (Deno/Node) is available.",
                    title=info.get("title", ""),
                )
            )

        # --- video-only (adaptive) ---
        best_video_fmt = _best_video_only(formats)
        video_url: str | None = best_video_fmt["url"] if best_video_fmt else None

        # --- muxed (combined a/v) ---
        best_muxed_fmt = _best_muxed(formats)
        muxed_url: str | None = best_muxed_fmt["url"] if best_muxed_fmt else None

        log.info(
            "Resolved '%s' (%ss) | audio=%s video=%s muxed=%s",
            info.get("title"),
            info.get("duration"),
            bool(audio_url),
            bool(video_url),
            bool(muxed_url),
        )

        return jsonify(
            {
                "success": True,
                "audioUrl": audio_url,
                "videoUrl": video_url,
                "muxedVideoUrl": muxed_url,
                "duration": info.get("duration") or 0,
                "title": info.get("title") or "",
                "uploaderUrl": info.get("uploader_url") or None,
                "likeCount": info.get("like_count") if info.get("like_count") is not None else -1,
                "viewCount": info.get("view_count") if info.get("view_count") is not None else -1,
            }
        )

    except yt_dlp.utils.DownloadError as exc:
        log.error("yt-dlp DownloadError: %s", exc)
        return jsonify(_error_payload(str(exc)))
    except Exception as exc:
        log.exception("Unexpected error resolving %s", url)
        return jsonify(_error_payload(str(exc)))


# ---------------------------------------------------------------------------
# Entry-point (dev only – use gunicorn in production)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    log.info("Starting dev server on port %d", port)
    app.run(host="0.0.0.0", port=port, debug=False)