"""Download a TikTok video without the watermark.

Primary: yt-dlp, choosing a play_addr format (no watermark) over download_addr
(watermarked). Fallback: the TikWM single-video API.
"""

import json
import logging
import re
import subprocess
import sys
from pathlib import Path

import requests

from . import config, media

log = logging.getLogger(__name__)

# Tried in order. "download" formats carry the watermark; bytevc2 is unplayable.
FORMATS = [
    # Highest resolution. TikTok often has its best quality only as H.265 video
    # with separate audio (e.g. 720p H.265 vs 540p H.264), so merge the two.
    "bv*[format_id!*=download][vcodec!*=bytevc2]+ba/b[format_id!*=download][vcodec!*=bytevc2]",
    # Fallback: a single combined file, H.264 first.
    "b[format_id*=h264]/b[format_id!*=download]/b",
]
TIKWM_API = "https://www.tikwm.com/api/"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")


class DownloadError(RuntimeError):
    pass


def cookie_args(cfg: dict = None) -> list:
    """yt-dlp options for a TikTok login, needed for posts TikTok only shows to logged-in
    viewers ("This post may not be comfortable for some audiences. Log in for access")."""
    dcfg = (cfg or config.load()).get("downloader") or {}
    cookies_file = (dcfg.get("cookies_file") or "").strip()
    if cookies_file:
        path = Path(cookies_file)
        if not path.is_absolute():
            path = config.ROOT / path
        if not path.exists():
            log.warning("cookies_file %s doesn't exist; downloading without a login", path)
            return []
        return ["--cookies", str(path)]
    browser = (dcfg.get("cookies_from_browser") or "").strip()
    return ["--cookies-from-browser", browser] if browser else []


def ytdlp(*args, timeout: float = 300) -> subprocess.CompletedProcess:
    """Run yt-dlp as a subprocess so a `pip install -U` takes effect without restarting the app."""
    cmd = [sys.executable, "-m", "yt_dlp", "--impersonate", "chrome", "--no-warnings",
           "--no-progress", *cookie_args(), *args]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def resolve_video_id(url: str) -> tuple:
    """(video_id, canonical_url). Follows vm.tiktok.com / tiktok.com/t/ short links."""
    url = url.strip()
    match = re.search(r"/(?:video|photo)/(\d{8,})", url)
    if not match and re.search(r"(vm|vt)\.tiktok\.com|tiktok\.com/t/", url):
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, allow_redirects=True, timeout=20)
        url = resp.url
        match = re.search(r"/(?:video|photo)/(\d{8,})", url)
    if not match:
        raise DownloadError(f"Couldn't find a TikTok video id in {url!r}")
    return match.group(1), url.split("?")[0]


def download(url: str, job_dir: Path) -> dict:
    """Downloads to job_dir/source.mp4 and returns the video's metadata."""
    dst = job_dir / "source.mp4"
    info = {}
    try:
        info = _download_ytdlp(url, job_dir)
        fmt = str(info.get("format_id", ""))
        if "download" in fmt:
            log.warning("yt-dlp only had a watermarked format (%s); trying TikWM", fmt)
            info = {**info, **_download_tikwm(url, dst), "watermark_fallback": fmt}
    except Exception as err:  # noqa: BLE001
        log.warning("yt-dlp download failed (%s); trying TikWM", err)
        if "Log in for access" in str(err) and not cookie_args():
            log.warning("TikTok only shows this post to logged-in viewers. Set downloader.cookies_file "
                        "in config.yaml so yt-dlp can download posts like this itself (see README)")
        try:
            info = {**info, **_download_tikwm(url, dst)}
        except Exception as err2:  # noqa: BLE001
            raise DownloadError(f"yt-dlp: {err}\nTikWM: {err2}") from err2

    if not dst.exists() or dst.stat().st_size == 0:
        raise DownloadError("Download produced no file")
    width, height = media.video_size(dst)  # raises if the file is not a readable video
    return {**info, "resolution": f"{width}x{height}"}


def _download_ytdlp(url: str, job_dir: Path) -> dict:
    errors = []
    for fmt in FORMATS:
        for old in job_dir.glob("source.*"):
            old.unlink()
        result = ytdlp("-f", fmt, "--merge-output-format", "mp4", "--no-simulate", "-j",
                       "-o", str(job_dir / "source.%(ext)s"), url, timeout=1800)
        if result.returncode == 0:
            break
        errors.append(result.stderr.strip()[-700:] or "yt-dlp failed")
        log.warning("yt-dlp format %r failed, trying the next one", fmt)
    else:
        raise DownloadError("\n".join(errors))
    raw = json.loads(result.stdout.strip().splitlines()[-1])
    files = [p for p in job_dir.glob("source.*")
             if p.suffix not in (".part", ".ytdl") and ".f" not in p.stem and ".temp" not in p.stem]
    if not files:
        raise DownloadError("yt-dlp reported success but wrote no file")
    got = files[0]
    if got.suffix.lower() != ".mp4":
        media.run(["ffmpeg", "-y", "-loglevel", "error", "-i", got, "-c", "copy",
                   job_dir / "source.mp4"])
        got.unlink()
    return {
        "downloader": "yt-dlp",
        "format_id": raw.get("format_id"),
        "resolution": f"{raw.get('width')}x{raw.get('height')}",
        "title": raw.get("title") or raw.get("description") or "",
        "description": raw.get("description") or "",
        "uploader": raw.get("uploader") or "",
        "channel_id": raw.get("channel_id") or "",
        "timestamp": raw.get("timestamp"),
        "duration": raw.get("duration"),
    }


def _download_tikwm(url: str, dst: Path) -> dict:
    resp = requests.get(TIKWM_API, params={"url": url, "hd": 1},
                        headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != 0:
        raise DownloadError(f"TikWM: {body.get('msg')}")
    data = body["data"]
    video_url = data.get("hdplay") or data.get("play")
    if not video_url:
        raise DownloadError("TikWM returned no no-watermark URL")
    if video_url.startswith("/"):
        video_url = "https://www.tikwm.com" + video_url
    with requests.get(video_url, headers={"User-Agent": USER_AGENT}, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dst, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    author = data.get("author") or {}
    return {
        "downloader": "tikwm",
        "title": data.get("title") or "",
        "description": data.get("title") or "",
        "uploader": author.get("unique_id") or "",
        "timestamp": data.get("create_time"),
        "duration": data.get("duration"),
    }
