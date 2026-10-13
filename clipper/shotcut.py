"""'Open in Shotcut' round-trip for heavy edits.

1. write_project() makes data/jobs/<clip>/shotcut/project.mlt: the clip (trimmed as in
   the dashboard editor) on V1 and the chosen music on A1, in a 1080x1920 30 fps profile.
   Text and colour looks from the dashboard editor are not carried over.
2. open_project() launches Shotcut on it. The user edits and saves (Ctrl+S).
3. render_project() renders the saved project with melt.exe, which ships inside
   Shotcut, straight to final.mp4. No exporting by hand.
"""

import math
import os
import shutil
import subprocess
from pathlib import Path
from xml.sax.saxutils import escape

from . import media, render

PROJECT = "project.mlt"
SEARCH = [
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Shotcut",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Shotcut",
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Shotcut",
]
INSTALL_HINT = "winget install --id Meltytech.Shotcut -e"


class ShotcutError(RuntimeError):
    pass


def find(cfg: dict = None) -> Path:
    """Folder holding shotcut.exe and melt.exe, or None."""
    configured = ((cfg or {}).get("shotcut") or {}).get("path")
    candidates = ([Path(configured)] if configured else []) + SEARCH
    on_path = shutil.which("shotcut")
    if on_path:
        candidates.append(Path(on_path).parent)
    for folder in candidates:
        if (folder / "shotcut.exe").exists() and (folder / "melt.exe").exists():
            return folder
    return None


def _prop(name: str, value) -> str:
    return f'    <property name="{name}">{escape(str(value))}</property>'


def build_project(base: Path, base_length: float, edit: dict, music_path: Path = None,
                  music_length: float = None) -> str:
    """MLT XML in the layout Shotcut itself saves (background track, main_bin,
    shotcut:* properties), so every track stays editable when Shotcut opens it."""
    # Everything in whole frames: MLT rounds each clock time separately, so summing
    # clock-time pieces drifts and makes the render longer than the clip.
    fps = render.FPS
    total = max(1, round((edit["trim"]["end"] - edit["trim"]["start"]) * fps))
    first = round(edit["trim"]["start"] * fps)
    base_frames = max(total + first, math.floor(base_length * fps))
    last = total - 1
    xml = [
        '<?xml version="1.0" standalone="no"?>',
        '<mlt LC_NUMERIC="C" version="7.41.0" title="Clipper" producer="main_bin">',
        f'  <profile description="Clipper vertical 1080x1920 {fps} fps" width="{render.W}" height="{render.H}" '
        f'progressive="1" sample_aspect_num="1" sample_aspect_den="1" display_aspect_num="9" '
        f'display_aspect_den="16" frame_rate_num="{fps}" frame_rate_den="1" colorspace="709"/>',
        '  <playlist id="main_bin">', _prop("xml_retain", 1), '  </playlist>',
        f'  <producer id="black" in="0" out="{last}">',
        _prop("length", total), _prop("eof", "pause"), _prop("resource", 0),
        _prop("aspect_ratio", 1), _prop("mlt_service", "color"), _prop("mlt_image_format", "rgba"),
        _prop("set.test_audio", 0), '  </producer>',
        '  <playlist id="background">', f'    <entry producer="black" in="0" out="{last}"/>',
        '  </playlist>',
        f'  <producer id="clip" in="0" out="{base_frames - 1}">',
        _prop("length", base_frames), _prop("eof", "pause"), _prop("resource", base),
        _prop("mlt_service", "avformat-novalidate"), _prop("seekable", 1),
        _prop("shotcut:caption", base.name), '  </producer>',
        '  <playlist id="playlist0">', _prop("shotcut:video", 1), _prop("shotcut:name", "V1"),
        f'    <entry producer="clip" in="{first}" out="{first + total - 1}"/>',
        '  </playlist>',
    ]
    tracks = ['    <track producer="background"/>', '    <track producer="playlist0"/>']
    transitions = [
        '    <transition id="mix_v1">', _prop("a_track", 0), _prop("b_track", 1),
        _prop("mlt_service", "mix"), _prop("always_active", 1), _prop("sum", 1), '    </transition>',
        '    <transition id="blend_v1">', _prop("a_track", 0), _prop("b_track", 1), _prop("version", 0.1),
        _prop("mlt_service", "frei0r.cairoblend"), _prop("threads", 0), _prop("disable", 1), '    </transition>',
    ]
    music_frames = math.floor((music_length or 0) * fps) if music_path is not None else 0
    if music_frames >= 1:  # a song shorter than one frame can't be placed
        m = edit["music"]
        db_level = 20 * math.log10(max(m["volume"], 0.001))
        xml += [
            f'  <producer id="music" in="0" out="{music_frames - 1}">',
            _prop("length", music_frames), _prop("eof", "pause"), _prop("resource", music_path),
            _prop("mlt_service", "avformat-novalidate"), _prop("shotcut:caption", music_path.name),
            '    <filter id="music_gain">', _prop("window", 75), _prop("max_gain", "20dB"),
            _prop("level", f"{db_level:.2f}"), _prop("mlt_service", "volume"),
            _prop("shotcut:filter", "audioGain"), '    </filter>',
            '  </producer>',
            '  <playlist id="playlist1">', _prop("shotcut:audio", 1), _prop("shotcut:name", "A1"),
        ]
        # repeat the song if it's shorter than the clip, like the dashboard render does;
        # the pieces add up to exactly `total` frames
        placed, cursor = 0, round(m["offset"] * fps) % music_frames
        while placed < total:
            take = min(total - placed, music_frames - cursor)
            xml.append(f'    <entry producer="music" in="{cursor}" out="{cursor + take - 1}"/>')
            placed += take
            cursor = 0
        xml.append('  </playlist>')
        tracks.append('    <track producer="playlist1" hide="video"/>')
        transitions += ['    <transition id="mix_a1">', _prop("a_track", 0), _prop("b_track", 2),
                        _prop("mlt_service", "mix"), _prop("always_active", 1), _prop("sum", 1),
                        '    </transition>']
    xml += [f'  <tractor id="tractor0" title="Clipper" in="0" out="{last}">',
            _prop("shotcut", 1), _prop("shotcut:projectAudioChannels", 2), _prop("shotcut:projectFolder", 0),
            *tracks, *transitions, '  </tractor>', '</mlt>']
    return "\n".join(xml) + "\n"


def write_project(job_dir: Path, edit: dict, music_dir: Path = None, overwrite: bool = False) -> Path:
    """Write the project unless one already exists (so reopening keeps the user's work)."""
    from . import music as music_lib

    folder = job_dir / "shotcut"
    folder.mkdir(exist_ok=True)
    project = folder / PROJECT
    if project.exists() and not overwrite:
        return project
    base = (job_dir / "transformed.mp4").resolve()
    track = edit["music"].get("track")
    music_path = music_lib.track_path(track, music_dir).resolve() if track else None
    project.write_text(build_project(base, media.duration(base), edit, music_path,
                                     media.duration(music_path) if music_path else None), encoding="utf-8")
    return project


def open_project(project: Path, cfg: dict = None) -> None:
    folder = find(cfg)
    if folder is None:
        raise ShotcutError(f"Shotcut isn't installed. Install it with:  {INSTALL_HINT}")
    subprocess.Popen([str(folder / "shotcut.exe"), str(project)], cwd=str(project.parent))


def render_project(job_dir: Path, cfg: dict = None, out_name: str = "final.mp4") -> Path:
    """Render the user's saved Shotcut project with melt into final.mp4 (via a temp file)."""
    folder = find(cfg)
    if folder is None:
        raise ShotcutError(f"Shotcut isn't installed. Install it with:  {INSTALL_HINT}")
    project = job_dir / "shotcut" / PROJECT
    if not project.exists():
        raise ShotcutError("No Shotcut project for this clip yet; use 'Open in Shotcut' first")
    tmp = job_dir / f"render_tmp_{out_name}"
    media.run([folder / "melt.exe", "-quiet", project, "-consumer", f"avformat:{tmp}",
               "vcodec=libx264", "preset=veryfast", "crf=20", "pix_fmt=yuv420p",
               "acodec=aac", "ab=192k", "ar=44100", "channels=2", "movflags=+faststart", "real_time=-1"],
              timeout=3600, cwd=project.parent)
    if not tmp.exists() or tmp.stat().st_size == 0:
        raise ShotcutError("melt finished without writing a video")
    media.replace(tmp, job_dir / out_name)
    return job_dir / out_name
