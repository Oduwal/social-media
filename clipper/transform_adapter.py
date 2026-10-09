"""Run the user's existing transform_video.py on one highlight clip.

That script caches its work by file name next to itself (albino_work/segments/
segment_001.mp4, ...), so running it on a second video would silently reuse the
first video's segments. Instead of editing it, this imports it as a module and
points WORK_PATH / OUTPUT_PATH at a fresh folder inside each job. Its functions
read those module globals at call time, so the override takes effect.
"""

import base64
import hashlib
import importlib.util
import logging
import re
import shutil
from pathlib import Path

from . import flow_robot, media

log = logging.getLogger(__name__)

_modules = {}
MIN_EDIT_S = 1.0


def load_script(script_path: str):
    path = Path(script_path).resolve()
    if path not in _modules:
        if not path.exists():
            raise FileNotFoundError(f"Transform script not found: {path}")
        spec = importlib.util.spec_from_file_location("user_transform_video", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for name in ("split_video", "edit_segment", "reassemble", "make_client",
                     "EDIT_INSTRUCTION_VARIANTS"):
            if not hasattr(module, name):
                raise AttributeError(f"{path.name} has no {name}(); the adapter needs it")
        _watch_for_refusals(module)
        _modules[path] = module
    return _modules[path]


class ContentBlocked(RuntimeError):
    """Google refused the edit (error code content_blocked). Retrying gets the same answer."""


def _watch_for_refusals(mod) -> None:
    """The script retries every failed segment 3 times with long waits, which is right for
    Google's temporary errors but pointless (and possibly billed) for a refusal. Without
    editing the script: note the error code it dumps, and turn a refusal into an error its
    retry loop doesn't catch, so it stops at once."""
    needed = ("dump_failed_interaction", "_attempt_edit", "SegmentEditFailed")
    if not all(hasattr(mod, name) for name in needed):
        return
    seen = {"blocked": None}
    original_dump, original_attempt = mod.dump_failed_interaction, mod._attempt_edit

    def dump(interaction, index):
        for err in getattr(interaction, "errors", None) or []:
            code = err.get("code") if isinstance(err, dict) else getattr(err, "code", None)
            message = err.get("message") if isinstance(err, dict) else getattr(err, "message", "")
            if code == "content_blocked":
                seen["blocked"] = message or "content blocked"
        return original_dump(interaction, index)

    def attempt(*args, **kwargs):
        seen["blocked"] = None
        try:
            return original_attempt(*args, **kwargs)
        except mod.SegmentEditFailed:
            if seen["blocked"]:
                raise ContentBlocked(seen["blocked"]) from None
            raise

    mod.dump_failed_interaction = dump
    mod._attempt_edit = attempt


def clean_target(target: str) -> str:
    """The 'who to change' description, tidied: one line, at most 300 characters."""
    return re.sub(r"\s+", " ", target or "").strip()[:300]


# Each 10 s piece is edited on its own, so without guidance the model re-imagines the look
# every time (freckles in one piece, darker skin or a different haircut in the next).
STRICT_LOOK = ("The skin must be evenly pale pinkish-white everywhere, with no freckles, spots, blotches, "
               "moles or tan. Keep the person's exact face, age, facial hair and haircut: do not add "
               "stubble, wrinkles or grey hair, and do not remove any hair. The look must be identical "
               "in every frame.")
REFERENCE_NOTE = ("The attached image shows exactly how the person must look after the change: match its "
                  "skin tone, hair colour and eye colour exactly, in every frame.")


def instruction_for(base: str, target: str, strict: bool = False, reference: bool = False) -> str:
    """The script's own instruction, plus (optionally) the stricter look, the reference
    picture note, and who to change when several people appear."""
    parts = [base]
    if strict:
        parts.append(STRICT_LOOK)
    if reference:
        parts.append(REFERENCE_NOTE)
    target = clean_target(target).rstrip(" .")
    if target:
        parts.append(f"There is more than one person in this video. Apply the change ONLY to this "
                     f"person: {target}. Every other person in the video must stay exactly as they are, "
                     f"with the same skin, hair, eyes and clothing.")
    return " ".join(parts)


def variant_key(variant: str, target: str, *extra) -> str:
    """The script names cached edits after EDIT_VARIANT, so anything that changes the
    request (target, model, wording, reference picture) gets a different name: an edit
    made one way is never reused for another."""
    signature = " | ".join([clean_target(target).lower(), *(str(e) for e in extra if e)])
    if not signature.strip(" |"):
        return variant
    return f"{variant}-{hashlib.sha1(signature.encode('utf-8')).hexdigest()[:8]}"


def _prepare(mod, cfg: dict, work: Path, job_dir: Path, target: str, reference_image: Path = None,
             cache_tag: str = "") -> None:
    """Point the script's module globals at this job and this request."""
    tcfg = cfg["transform"]
    variant = tcfg["variant"]
    if variant not in mod.EDIT_INSTRUCTION_VARIANTS:
        raise ValueError(f"Unknown transform variant {variant!r}; "
                         f"choose from {sorted(mod.EDIT_INSTRUCTION_VARIANTS)}")
    if not hasattr(mod, "_clipper_script_model"):
        mod._clipper_script_model = getattr(mod, "VIDEO_MODEL_NAME", "")
        mod._clipper_original_build = getattr(mod, "build_edit_input", None)
    flow = uses_flow(cfg)
    model = "flow" if flow else (resolve_model(mod, tcfg.get("model")) or mod._clipper_script_model)
    if hasattr(mod, "VIDEO_MODEL_NAME"):
        mod.VIDEO_MODEL_NAME = model
    strict = bool(tcfg.get("strict_look"))
    ref_sig = hashlib.sha1(reference_image.read_bytes()).hexdigest()[:8] if reference_image else ""
    # transform.instruction (optional) replaces the script's wording, e.g. a prompt that
    # worked well elsewhere; it's part of the cache name so its edits are kept apart.
    custom = " ".join(str(tcfg.get("instruction") or "").split())
    base = custom or mod.EDIT_INSTRUCTION_VARIANTS[variant]
    mod.WORK_PATH = work
    mod.OUTPUT_PATH = job_dir
    mod.EDIT_VARIANT = variant_key(variant, target, model if (tcfg.get("model") or flow) else "",
                                   "strict" if strict else "", ref_sig, cache_tag,
                                   hashlib.sha1(custom.encode()).hexdigest()[:8] if custom else "",
                                   "" if flow else str(tcfg.get("resolution") or ""))
    mod.EDIT_INSTRUCTION = instruction_for(base, target, strict=strict, reference=bool(reference_image))
    original = mod._clipper_original_build
    if reference_image and original:
        encoded = base64.b64encode(reference_image.read_bytes()).decode("utf-8")

        def build_with_reference(segment_path, instruction):
            parts = list(original(segment_path, instruction))
            # same part shape the script already uses for images: {type, mime_type, data}
            parts.insert(1, {"type": "image", "mime_type": "image/jpeg", "data": encoded})
            return parts

        mod.build_edit_input = build_with_reference
    elif original:
        mod.build_edit_input = original


class _Interactions:
    """Passes everything through, adding the output resolution to each new edit request."""

    def __init__(self, inner, resolution: str):
        self._inner, self._resolution = inner, resolution

    def create(self, **kwargs):
        response_format = dict(kwargs.get("response_format") or {})
        response_format.setdefault("resolution", self._resolution)
        return self._inner.create(**{**kwargs, "response_format": response_format})

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _ClientWithResolution:
    """The script asks Omni for an aspect ratio only, so results come back at 720p. Without
    editing the script, wrap its client so every edit also asks for e.g. 1080p."""

    def __init__(self, client, resolution: str):
        self._client = client
        self.interactions = _Interactions(client.interactions, resolution)

    def __getattr__(self, name):
        return getattr(self._client, name)


def client_for(mod, cfg: dict):
    client = mod.make_client()
    resolution = str(cfg["transform"].get("resolution") or "").strip()
    return _ClientWithResolution(client, resolution) if resolution else client


_resolved_models = {}


def resolve_model(mod, wanted) -> str:
    """The first model in `wanted` (a name or a list) that this Google project actually has.
    Asking (models.get) is free; the answer is remembered until Clipper restarts, so a
    GA model is picked up on the first restart after Google enables it for the project."""
    candidates = [wanted] if isinstance(wanted, str) else list(wanted or [])
    candidates = [c.strip() for c in candidates if c and c.strip()]
    if not candidates:
        return ""
    key = tuple(candidates)
    if key not in _resolved_models:
        chosen = candidates[-1]
        try:
            client = mod.make_client()
            for name in candidates:
                try:
                    client.models.get(model=name)
                    chosen = name
                    break
                except Exception as err:  # noqa: BLE001
                    log.info("Omni model %s isn't available to this project: %s", name, str(err)[:120])
        except Exception as err:  # noqa: BLE001
            log.warning("Couldn't check which Omni models are available (%s); using %s", err, chosen)
        log.info("Using Omni model %s", chosen)
        _resolved_models[key] = chosen
    return _resolved_models[key]


def _rejected_input(err: Exception) -> bool:
    """A 400 / INVALID_ARGUMENT from Google: the request shape (e.g. an image next to the
    video) isn't accepted, as opposed to a content refusal or a temporary failure."""
    text = str(err)
    return getattr(err, "code", None) == 400 or "INVALID_ARGUMENT" in text or "400" in text[:20]


BACKENDS = ("vertex", "flow")


def backend(cfg: dict) -> str:
    """Where the albino edit runs: "vertex" (Omni API) or "flow" (the Flow robot)."""
    name = str(cfg["transform"].get("backend") or "vertex").lower()
    if name not in BACKENDS:
        raise ValueError(f"transform: backend must be one of {', '.join(BACKENDS)}, not {name!r}")
    return name


def uses_flow(cfg: dict) -> bool:
    return backend(cfg) == "flow"


def _flow_paths(mod, index: int, job_dir: Path) -> tuple:
    """(cached edit, raw Flow download, tile name) for one piece. The tile name is the same
    for the same piece and request, so a retry finds its upload (or a result Flow already
    made); the robot adds -2, -3... once a result has been used."""
    cache = Path(mod.WORK_PATH) / "edited" / f"edited_{index:03d}_{mod.EDIT_VARIANT}.mp4"
    raw = cache.with_name(cache.stem + "_flow_raw.mp4")
    base = f"clipper_{job_dir.name}_p{index:02d}_{hashlib.sha1(mod.EDIT_VARIANT.encode()).hexdigest()[:6]}"
    return cache, raw, base


def flow_batch(mod, cfg: dict, segments: list, job_dir: Path) -> dict:
    """Start every piece of the clip on Flow at once (flow_robot.edit_many) and download the
    results; returns {index: error} for pieces Flow couldn't do. Pieces already edited or
    downloaded are skipped, so nothing is paid for twice. Paused/NeedsYou are raised."""
    todo = []
    for index, segment in enumerate(segments, start=1):
        cache, raw, base = _flow_paths(mod, index, job_dir)
        if cache.exists() or _playable(raw) or media.duration(segment) < MIN_EDIT_S:
            continue
        cache.parent.mkdir(parents=True, exist_ok=True)
        todo.append((index, segment, raw, base))
    if not todo:
        return {}
    results = flow_robot.edit_many(todo, mod.EDIT_INSTRUCTION, cfg)
    return {index: r for index, r in results.items() if isinstance(r, Exception)}


def flow_edit(mod, cfg: dict, segment: Path, index: int, job_dir: Path, batch_errors: dict = None) -> Path:
    """One piece's Flow result, conformed and saved where the script keeps its own edits
    (edited_NNN_<variant>.mp4), so caching, redo-a-piece and colour matching work the same
    as with Vertex. Normally flow_batch already downloaded it."""
    cache, raw, base = _flow_paths(mod, index, job_dir)
    if cache.exists():
        return cache
    cache.parent.mkdir(parents=True, exist_ok=True)
    if not _playable(raw):  # a result already downloaded (e.g. conform failed last time) isn't paid for twice
        try:
            if batch_errors and index in batch_errors:
                raise batch_errors[index]
            flow_robot.edit(segment, mod.EDIT_INSTRUCTION, raw, base, cfg)
        except flow_robot.Refused as err:
            raise ContentBlocked(str(err)) from err
    try:
        conform(raw, segment, cache)
    except flow_robot.Failed:
        raw.unlink(missing_ok=True)  # wrong length: the next try asks Flow again
        raise
    raw.unlink(missing_ok=True)
    return cache


def _playable(path: Path) -> bool:
    try:
        return path.exists() and media.duration(path) >= 1
    except Exception:  # noqa: BLE001
        return False


MAX_LENGTH_GAP_S = 0.3  # more than this and the original voice would drift off the lips


def conform(edited: Path, segment: Path, out: Path) -> Path:
    """Make Flow's piece (720p, 24 fps) match the original piece: same size and frame rate,
    with the original piece's sound. reassemble() sizes the whole clip after the first
    piece, so one 720p piece first would otherwise shrink everything. The timing is never
    stretched (that would put the lips out of step with the real voice): a small gap is
    trimmed or padded with the last frame; a big one means Flow cut the piece short."""
    width, height = media.video_size(segment)
    fps = _fps(segment)
    seg_len, ed_len = media.duration(segment), _video_length(edited)
    if abs(seg_len - ed_len) > MAX_LENGTH_GAP_S:
        raise flow_robot.Failed(f"The edit came back {ed_len:.1f} s long for a {seg_len:.1f} s piece")
    pad = f",tpad=stop_mode=clone:stop_duration={seg_len - ed_len + 0.1:.3f}" if ed_len < seg_len else ""
    tmp = out.with_name(out.stem + ".part.mp4")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", edited, "-i", segment,
           "-vf", f"scale={width}:{height}:flags=lanczos,setsar=1,fps={fps}{pad}", "-map", "0:v:0"]
    cmd += ["-map", "1:a:0", "-c:a", "aac", "-b:a", "192k"] if media.has_audio(segment) else ["-an"]
    cmd += ["-t", f"{seg_len:.3f}", "-c:v", "libx264", "-preset", "fast", "-crf", "16", "-pix_fmt", "yuv420p", tmp]
    try:
        media.run(cmd)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(out)  # only a finished file ever becomes the cached edit
    return out


def _video_length(path: Path) -> float:
    """The video stream's own length (the file's can be longer when the audio runs on)."""
    for stream in media.probe(path)["streams"]:
        if stream.get("codec_type") == "video" and stream.get("duration"):
            return float(stream["duration"])
    return media.duration(path)


def _fps(path: Path) -> str:
    for stream in media.probe(path)["streams"]:
        if stream.get("codec_type") == "video":
            return stream.get("r_frame_rate") or "30/1"
    return "30/1"


def make_look(highlight: Path, job_dir: Path, cfg: dict, target: str = "") -> Path:
    """The reference picture: Omni edits a 2-second still of one frame from the clip, and
    a frame of its answer becomes look.jpg, which every piece is then asked to match.
    Uses the same video-in, video-out path as the pieces, so nothing new is assumed."""
    if backend(cfg) != "vertex":
        raise ValueError("The reference look only works with Vertex (set transform: reference: false)")
    mod = load_script(cfg["transform"]["script_path"])
    work = job_dir / "look_work"
    work.mkdir(parents=True, exist_ok=True)
    source = job_dir / "look_source.jpg"
    at = max(0.5, media.duration(highlight) / 3)  # a third in: past any intro, usually facing the camera
    media.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{at:.2f}", "-i", highlight,
               "-frames:v", "1", "-q:v", "2", source])
    still = work / "look_input.mp4"
    media.run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-i", source,
               "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
               "-t", "2", "-r", "30", "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
               "-c:v", "libx264", "-c:a", "aac", "-shortest", still])
    _prepare(mod, cfg, work, job_dir, target, cache_tag="look")
    edited = Path(mod.edit_segment(client_for(mod, cfg), still, 0, verbose=False))
    look = job_dir / "look.jpg"
    media.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{media.duration(edited) / 2:.2f}", "-i", edited,
               "-frames:v", "1", "-q:v", "2", look])
    return look


def run(highlight: Path, job_dir: Path, cfg: dict, target: str = "", reference_image: Path = None) -> dict:
    """Writes job_dir/transformed.mp4 and returns what happened to each segment.
    `target` describes which person to change when more than one is in the clip;
    `reference_image` is the approved look every segment is asked to match."""
    out = job_dir / "transformed.mp4"
    tcfg = cfg["transform"]
    if not tcfg["enabled"]:
        shutil.copyfile(highlight, out)
        return {"enabled": False, "segments": 0, "fallback_segments": [], "refused": False}

    mod = load_script(tcfg["script_path"])
    variant = tcfg["variant"]
    work = job_dir / "transform_work"
    work.mkdir(parents=True, exist_ok=True)
    _prepare(mod, cfg, work, job_dir, target, reference_image)
    reference_rejected = None

    _discard_bad_cache(work, highlight)
    route = backend(cfg)
    flow = route == "flow"
    client = client_for(mod, cfg) if route == "vertex" else None
    segments = mod.split_video(highlight)
    flow_errors = {}
    if flow:
        # every piece of the clip goes to Flow at once; Paused/NeedsYou stop the clip here
        flow_errors = flow_batch(mod, cfg, segments, job_dir)

    def edit_piece(segment, index):
        if flow:
            return flow_edit(mod, cfg, segment, index, job_dir, flow_errors)
        return Path(mod.edit_segment(client, segment, index, verbose=False))

    clips, fallback, tiny = [], [], []
    pieces = []  # one entry per segment: what went into the final clip, for the per-piece view and redo
    offset = max_offset = 0.0
    blocked = None

    def original(index, segment, kind):
        path = _with_audio(segment, work, f"segment_{index:03d}_withaudio.mp4")
        clips.append(path)
        pieces.append({"index": index, "kind": kind, "file": str(path.relative_to(job_dir)), "cache": None})

    for index, segment in enumerate(segments, start=1):
        seg_len = media.duration(segment)
        if blocked and not (flow and (_playable(_flow_paths(mod, index, job_dir)[1])
                                      or _flow_paths(mod, index, job_dir)[0].exists())):
            # Google refused an earlier segment of this clip; it would refuse these too
            # (with Flow they all ran at once: a piece Flow did make is still used)
            original(index, segment, "original")
            fallback.append(index)
            continue
        if seg_len < MIN_EDIT_S:
            # A sliver at the end: not worth a billable edit, and Omni tends to return
            # a full-length clip for it, which would wreck the timing.
            original(index, segment, "sliver")
            tiny.append(index)
            continue
        try:
            try:
                edited = edit_piece(segment, index)
            except Exception as err:  # noqa: BLE001
                # A picture of the person next to a video of them talking is refused as a
                # "speech edit" (and some setups reject image inputs outright): carry on without it.
                if not (reference_image and reference_rejected is None
                        and (isinstance(err, ContentBlocked) or _rejected_input(err))):
                    raise
                reference_rejected = str(err)[:300]
                log.warning("Omni rejected the reference picture (%s); continuing without it", reference_rejected)
                reference_image = None
                _prepare(mod, cfg, work, job_dir, target)
                edited = edit_piece(segment, index)
            cache = edited  # the script's cached edit: deleting it makes just this piece redo
            edited = _with_audio(edited, work, f"edited_{index:03d}_withaudio.mp4")
            offset += media.duration(edited) - seg_len
            max_offset = max(max_offset, abs(offset))
            clips.append(edited)
            pieces.append({"index": index, "kind": "albino", "file": str(edited.relative_to(job_dir)),
                           "cache": str(cache.relative_to(job_dir))})
        except ContentBlocked as err:
            blocked = str(err)
            log.warning("Google refused segment %d (%s); not sending the rest of this clip", index, blocked)
            original(index, segment, "original")
            fallback.append(index)
        except flow_robot.FlowError as err:
            if not isinstance(err, flow_robot.Failed):
                # The robot needs you (sign-in, a notice, credits) or Flow pushed back: stop
                # here rather than post a half-albino clip. Finished pieces stay cached.
                raise
            log.warning("Flow couldn't edit piece %d twice, keeping original footage: %s", index, err)
            original(index, segment, "original")
            fallback.append(index)
        except Exception as err:  # noqa: BLE001 - same fallback as the script's own main()
            log.warning("Transform segment %d failed, keeping original footage: %s", index, err)
            original(index, segment, "original")
            fallback.append(index)

    colour = None
    if tcfg.get("colour_match", True):
        colour = match_colours(pieces, job_dir)
        clips = [job_dir / p["file"] for p in pieces]

    raw_name = "transformed_raw.mp4"
    mod.reassemble(clips, raw_name)
    raw = job_dir / raw_name

    # Put the clip's real audio back on, unless edited segments changed length enough
    # that, added up across the seams, the voice would drift off the lips.
    edited_count = len(segments) - len(fallback) - len(tiny)
    keep_original_audio = max_offset <= 0.3
    if keep_original_audio:
        media.run(["ffmpeg", "-y", "-loglevel", "error", "-i", raw, "-i", highlight,
                   "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                   "-shortest", out])
        raw.unlink()
    else:
        raw.replace(out)

    return {
        "enabled": True,
        "variant": variant,
        "target": clean_target(target),
        "segments": len(segments),
        "fallback_segments": fallback,
        "skipped_tiny_segments": tiny,
        "refused": edited_count == 0,
        "blocked": blocked,  # Google's refusal message, if it refused the edit
        "pieces": pieces,
        "colour_match": colour,
        "model": "Google Flow (Omni 1.1 Flash, Agent)" if flow else getattr(mod, "VIDEO_MODEL_NAME", ""),
        "backend": route,
        "strict_look": bool(tcfg.get("strict_look")),
        "reference": bool(reference_image),
        "reference_rejected": reference_rejected,
        "original_audio": keep_original_audio,
        "max_segment_drift_s": round(max_offset, 2),
    }


MAX_LIGHT_SHIFT, MAX_TINT_SHIFT = 30.0, 12.0  # gentle: evens out, never repaints


def match_colours(pieces: list, job_dir: Path) -> dict:
    """Each piece is a separate Omni run, so one comes out paler, another greyer. Measure
    the skin area's brightness and tint in every albino piece, take the middle value as
    the target, and shift each piece towards it. Brightness and tint only: it can't
    remove freckles or patches (that's what redoing a piece is for). Updates `pieces`
    in place to point at the matched files."""
    albino = [p for p in pieces if p["kind"] == "albino"]
    if len(albino) < 2:
        return None
    for piece in albino:
        piece["before"] = [round(v, 1) for v in media.skin_stats(job_dir / piece["file"])]

    def middle(i):
        values = sorted(p["before"][i] for p in albino)
        return values[len(values) // 2]

    target = [middle(0), middle(1), middle(2)]
    for piece in albino:
        y, u, v = piece["before"]
        dy = max(-MAX_LIGHT_SHIFT, min(MAX_LIGHT_SHIFT, target[0] - y))
        du = max(-MAX_TINT_SHIFT, min(MAX_TINT_SHIFT, target[1] - u))
        dv = max(-MAX_TINT_SHIFT, min(MAX_TINT_SHIFT, target[2] - v))
        piece["shift"] = [round(dy, 1), round(du, 1), round(dv, 1)]
        if max(abs(dy), abs(du), abs(dv)) < 2:
            continue  # already matches
        src = job_dir / piece["file"]
        dst = src.with_name(f"matched_{piece['index']:03d}.mp4")
        media.shift_colour(src, dst, dy, du, dv)
        piece["file"] = str(dst.relative_to(job_dir))
    return {"target": [round(t, 1) for t in target]}


def _with_audio(clip: Path, work: Path, name: str) -> Path:
    """reassemble() maps [i:a] for every clip, so each one needs an audio stream."""
    return clip if media.has_audio(clip) else media.add_silent_audio(clip, work / name)


def _discard_bad_cache(work: Path, highlight: Path) -> None:
    """The script reuses any segment file that exists. If a previous run was killed
    mid-split, a truncated segment would be reused forever, so check the cached
    segments still add up to the highlight and start over if not."""
    seg_dir = work / "segments"
    cached = sorted(seg_dir.glob("segment_*.mp4")) if seg_dir.exists() else []
    if not cached:
        return
    try:
        total = sum(media.duration(p) for p in cached)
        ok = abs(total - media.duration(highlight)) <= 0.5
    except Exception:  # noqa: BLE001 - unreadable file
        ok = False
    if not ok:
        log.warning("Cached transform segments don't match the highlight; re-splitting")
        shutil.rmtree(seg_dir)
        shutil.rmtree(work / "edited", ignore_errors=True)
