"""Server speed test for the cloud plan: times the heavy ffmpeg steps of the pipeline
(480p proxy for Gemini, cutting one clip, a final-style re-encode) with 1, 2 and 3 jobs
at once, so you can see how many videos a server handles per hour.

Needs: Python 3.10+, ffmpeg/ffprobe on PATH, and the `clipper` folder next to `tools/`
(only media.py and cutter.py are used). No API keys, nothing is posted, nothing leaves the machine.

    python tools/vps_loadtest.py --source source.mp4 --highlight highlight.json

Runs at low priority (nice 10) in its own temp folder, so it's safe beside another product.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from clipper import cutter, media  # noqa: E402


def one_job(n: int, source: Path, clip: dict, root: Path) -> dict:
    work = root / f"job{n}"
    work.mkdir(parents=True)
    times = {}
    s = time.monotonic()
    media.make_proxy(source, work / "proxy.mp4")
    times["proxy"] = time.monotonic() - s
    s = time.monotonic()
    cutter.cut(source, work, clip, 15, 45)
    times["cut"] = time.monotonic() - s
    s = time.monotonic()  # stands in for the final render: a re-encode of the cut clip
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(work / "highlight.mp4"), "-c:v", "libx264",
                    "-preset", "medium", "-crf", "20", "-c:a", "aac", str(work / "final.mp4")], check=True)
    times["render"] = time.monotonic() - s
    return times


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, type=Path)
    ap.add_argument("--highlight", required=True, type=Path, help="a highlight.json (its first clip is cut)")
    ap.add_argument("--jobs", type=int, nargs="+", default=[1, 2, 3])
    a = ap.parse_args()
    if hasattr(os, "nice"):
        os.nice(10)
    clip = json.loads(a.highlight.read_text(encoding="utf-8"))["clips"][0]
    mins = media.duration(a.source) / 60
    print(f"CPUs: {os.cpu_count()}   source: {mins:.1f} min   load average: "
          f"{os.getloadavg() if hasattr(os, 'getloadavg') else 'n/a'}")
    root = Path(tempfile.mkdtemp(prefix="vps_loadtest_"))
    try:
        for count in a.jobs:
            shutil.rmtree(root, ignore_errors=True)
            root.mkdir()
            start = time.monotonic()
            with ThreadPoolExecutor(count) as pool:
                results = list(pool.map(lambda n: one_job(n, a.source, clip, root), range(count)))
            wall = time.monotonic() - start
            per = {k: sum(r[k] for r in results) / count for k in results[0]}
            # one source video ~ 1 proxy + 5 clips (cut + render each)
            video_s = per["proxy"] + 5 * (per["cut"] + per["render"])
            print(f"{count} at once: wall {wall:.0f}s | avg proxy {per['proxy']:.0f}s cut {per['cut']:.0f}s "
                  f"render {per['render']:.0f}s | est. one 5-clip video {video_s/60:.1f} min "
                  f"-> about {count * 3600 / video_s:.1f} videos/hour at this load")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
