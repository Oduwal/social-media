"""SQLite job store and the pipeline state machine.

Two kinds of row:
- a VIDEO (kind='video', keyed by the TikTok id): download -> highlight -> clips.
  The "clips" step creates one CLIP row per highlight Gemini found.
- a CLIP (kind='clip', keyed '<video id>-<nn>'): cut -> PREVIEW, where it waits
  for the user to pick it -> transform -> mix -> READY, where it waits for
  approval -> post.

Each state names what has been DONE; NEXT_STEP says what runs next.
"""

import json
import sqlite3
import threading
import time
from pathlib import Path

from . import config

# state -> step that runs next
NEXT_STEP = {
    # videos
    "DETECTED": "download",
    "DOWNLOADED": "highlight",
    "HIGHLIGHTED": "clips",
    # clips
    "NEW": "cut",
    "CUT": "transform",          # only after the user picks the clip in PREVIEW
    "TRANSFORMED": "mix",
    "APPROVED": "post",
}
# step -> state the job lands in when the step succeeds
DONE_STATE = {
    "download": "DOWNLOADED",
    "highlight": "HIGHLIGHTED",
    "clips": "CLIPPED",
    "cut": "PREVIEW",
    "transform": "TRANSFORMED",
    "mix": "READY",
    "post": "POSTED",
}
# step -> state to reset to when retrying that step
RETRY_STATE = {step: state for state, step in NEXT_STEP.items()}
STEPS = ["download", "highlight", "clips", "cut", "transform", "mix", "post"]

WAITING_STATES = {"PREVIEW", "LOOK", "READY"}  # LOOK: approve the albino reference look
TERMINAL_STATES = {"CLIPPED", "POSTED", "REJECTED", "SKIPPED", "FAILED"}
BUSY_STATES = set(NEXT_STEP) | {"POSTING"}

JSON_FIELDS = {"info", "captions", "platforms", "transform_info", "post_result", "cut_range", "clip", "edit",
               "sound"}
NEW_COLUMNS = {
    "hold": "INTEGER NOT NULL DEFAULT 0",
    "kind": "TEXT NOT NULL DEFAULT 'video'",
    "parent_id": "TEXT",
    "clip": "TEXT",
    "transform_mode": "TEXT",
    "edit": "TEXT",
    "target": "TEXT",  # which person to make albino when a clip shows several people
    "sound": "TEXT",   # trending TikTok sound: Gemini's suggestions + the user's choice
    "handle": "TEXT",  # which creator this video (and its clips) came from
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    video_id        TEXT PRIMARY KEY,
    url             TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'monitor',
    state           TEXT NOT NULL,
    failed_step     TEXT,
    error           TEXT,
    info            TEXT,
    candidate_index INTEGER NOT NULL DEFAULT 0,
    cut_range       TEXT,
    transform_info  TEXT,
    music_track     TEXT,
    music_volume    REAL,
    captions        TEXT,
    platforms       TEXT,
    ai_label        INTEGER,
    post_attempt    INTEGER NOT NULL DEFAULT 0,
    post_result     TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

_lock = threading.RLock()
_conn = None


def init(path: Path = None) -> None:
    global _conn
    path = Path(path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = sqlite3.connect(str(path), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(_SCHEMA)
        columns = {row["name"] for row in _conn.execute("PRAGMA table_info(jobs)")}
        for name, ddl in NEW_COLUMNS.items():
            if name not in columns:
                _conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {ddl}")
        _conn.execute("CREATE INDEX IF NOT EXISTS jobs_parent ON jobs(parent_id)")
        _conn.commit()


def _db() -> sqlite3.Connection:
    if _conn is None:
        init()
    return _conn


def _row_to_job(row) -> dict:
    if row is None:
        return None
    job = dict(row)
    for field in JSON_FIELDS:
        if job.get(field):
            job[field] = json.loads(job[field])
    if job.get("ai_label") is not None:
        job["ai_label"] = bool(job["ai_label"])
    job["hold"] = bool(job.get("hold"))
    return job


def insert_if_new(video_id: str, url: str, state: str = "DETECTED", source: str = "monitor",
                  handle: str = "") -> bool:
    """Returns True if the job was created, False if the video was already known."""
    now = time.time()
    with _lock:
        cur = _db().execute(
            "INSERT OR IGNORE INTO jobs (video_id, url, source, state, handle, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (video_id, url, source, state, handle, now, now),
        )
        _db().commit()
        return cur.rowcount == 1


def insert_clip(clip_id: str, parent: dict, clip: dict, **fields) -> bool:
    """One highlight of a video, inserted complete in one statement so the worker
    never sees a half-made row. Returns False if it already exists."""
    now = time.time()
    row = _encode({"video_id": clip_id, "url": parent["url"], "source": parent["source"],
                   "state": "NEW", "kind": "clip", "parent_id": parent["video_id"], "clip": clip,
                   "handle": parent.get("handle", ""), "created_at": now, **fields})
    cols = ", ".join(row)
    with _lock:
        cur = _db().execute(f"INSERT OR IGNORE INTO jobs ({cols}) VALUES ({', '.join('?' for _ in row)})",
                            tuple(row.values()))
        _db().commit()
        return cur.rowcount == 1


def auto_pick_previews() -> int:
    """Move every clip waiting in PREVIEW to CUT as-is (transform_mode 'original'), so it
    flows on to READY without a manual pick. Returns how many were moved."""
    with _lock:
        cur = _db().execute(
            "UPDATE jobs SET state = 'CUT', transform_mode = 'original', updated_at = ? "
            "WHERE kind = 'clip' AND state = 'PREVIEW' AND hold = 0", (time.time(),))
        _db().commit()
        return cur.rowcount


def get(video_id: str) -> dict:
    with _lock:
        row = _db().execute("SELECT * FROM jobs WHERE video_id = ?", (video_id,)).fetchone()
    return _row_to_job(row)


def list_jobs(limit: int = 200, include_skipped: bool = False) -> list:
    """Videos only, newest first."""
    query = "SELECT * FROM jobs WHERE kind = 'video'"
    if not include_skipped:
        query += " AND state != 'SKIPPED'"
    query += " ORDER BY created_at DESC LIMIT ?"
    with _lock:
        rows = _db().execute(query, (limit,)).fetchall()
    return [_row_to_job(r) for r in rows]


def list_clips(parent_id: str = None, states: list = None, limit: int = 500) -> list:
    """Clips, newest video first and in timeline order within a video."""
    query, args = "SELECT * FROM jobs WHERE kind = 'clip'", []
    if parent_id:
        query += " AND parent_id = ?"
        args.append(parent_id)
    if states:
        query += f" AND state IN ({', '.join('?' for _ in states)})"
        args += list(states)
    # TikTok ids grow with time, so parent_id DESC = newest video first
    query += " ORDER BY parent_id DESC, video_id LIMIT ?"
    with _lock:
        rows = _db().execute(query, (*args, limit)).fetchall()
    return [_row_to_job(r) for r in rows]


def clip_counts(parent_id: str = None) -> dict:
    query, args = "SELECT state, COUNT(*) AS n FROM jobs WHERE kind = 'clip'", []
    if parent_id:
        query += " AND parent_id = ?"
        args.append(parent_id)
    with _lock:
        rows = _db().execute(query + " GROUP BY state", args).fetchall()
    return {r["state"]: r["n"] for r in rows}


def _encode(fields: dict) -> dict:
    for key in list(fields):
        if key in JSON_FIELDS and fields[key] is not None:
            fields[key] = json.dumps(fields[key])
        if key in ("ai_label", "hold") and fields[key] is not None:
            fields[key] = int(bool(fields[key]))
    fields["updated_at"] = time.time()
    return fields


def update(video_id: str, **fields) -> None:
    if not fields:
        return
    fields = _encode(fields)
    cols = ", ".join(f"{k} = ?" for k in fields)
    with _lock:
        _db().execute(f"UPDATE jobs SET {cols} WHERE video_id = ?", (*fields.values(), video_id))
        _db().commit()


def next_actionable(skip_transforms: bool = False) -> dict:
    """Oldest job that has work to do. Posting approved clips goes first:
    the user is waiting on those, and they don't cost model time.
    Held jobs (being run from the CLI, or stopped early by it) are skipped, and so are
    clips waiting for an albino edit when `skip_transforms` (the Flow robot is paused)."""
    with _lock:
        row = _db().execute(
            "SELECT * FROM jobs WHERE state = 'APPROVED' AND hold = 0 ORDER BY updated_at LIMIT 1"
        ).fetchone()
        if row is None:
            placeholders = ", ".join("?" for _ in NEXT_STEP)
            # Cutting a preview takes seconds; an albino transform takes minutes. Cut first,
            # so a video's previews all appear without waiting behind clips being transformed.
            waiting = (" AND NOT (state = 'CUT' AND COALESCE(transform_mode, '') != 'original')"
                       if skip_transforms else "")
            row = _db().execute(
                f"SELECT * FROM jobs WHERE state IN ({placeholders}) AND hold = 0{waiting} "
                "ORDER BY CASE state WHEN 'NEW' THEN 0 ELSE 1 END, created_at, video_id LIMIT 1",
                tuple(NEXT_STEP),
            ).fetchone()
    return _row_to_job(row)


def transition(video_id: str, from_state: str, **fields) -> bool:
    """Update only if the job is still in from_state. Returns False when something
    else (the dashboard, another process) changed it in the meantime."""
    fields = _encode(fields)
    cols = ", ".join(f"{k} = ?" for k in fields)
    with _lock:
        cur = _db().execute(f"UPDATE jobs SET {cols} WHERE video_id = ? AND state = ?",
                            (*fields.values(), video_id, from_state))
        _db().commit()
        return cur.rowcount == 1


def mark_failed(video_id: str, step: str, error: str, from_state: str = None) -> bool:
    fields = {"state": "FAILED", "failed_step": step, "error": error[-4000:]}
    if from_state is None:
        update(video_id, **fields)
        return True
    return transition(video_id, from_state, **fields)


def reject(video_id: str) -> bool:
    """Discard, unless it's being posted or already posted (checked in the same
    statement, so the worker starting the upload in between can't be overwritten)."""
    with _lock:
        cur = _db().execute("UPDATE jobs SET state = 'REJECTED', updated_at = ? "
                            "WHERE video_id = ? AND state NOT IN ('POSTING', 'POSTED')",
                            (time.time(), video_id))
        _db().commit()
        return cur.rowcount == 1


def retry(video_id: str) -> None:
    job = get(video_id)
    if job and job["state"] == "FAILED" and job["failed_step"] in RETRY_STATE:
        update(video_id, state=RETRY_STATE[job["failed_step"]], failed_step=None, error=None)


def reset_interrupted() -> None:
    """A crash mid-post leaves a job in POSTING. Put it back to READY so the
    user decides whether to post again, rather than risking a double post."""
    with _lock:
        _db().execute("UPDATE jobs SET state = 'READY', error = 'Posting was interrupted; "
                      "check your accounts before approving again.' WHERE state = 'POSTING'")
        _db().commit()


def kv_get(key: str, default=None):
    with _lock:
        row = _db().execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def kv_delete(key: str) -> None:
    with _lock:
        _db().execute("DELETE FROM kv WHERE key = ?", (key,))
        _db().commit()


def kv_set(key: str, value) -> None:
    with _lock:
        _db().execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, json.dumps(value)))
        _db().commit()
