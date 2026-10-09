"""Edits a 10-second piece in Google Flow (flow.google.com) by driving the website, for when
there's no Vertex credit. It does what you'd do by hand, in a visible Chrome window that
has its own profile (data/flow_profile), signed in once by you:

    upload the piece -> right-click its tile -> Add to prompt -> Agent on ->
    paste the prompt -> Generate -> wait -> fetch the result

A clip's pieces all run at once (edit_many): each is started a few seconds apart, they
generate together, then each result is fetched; the next clip starts flow: clip_gap_s (20 s)
later. Google has no API for Flow and its terms restrict automated use, so there's a daily
cap, and it stops (and waits for you) on anything unusual: signed out, a rights notice,
"unusual activity", or running low on credits. It never tries to get around a check.
"""

import json
import logging
import os
import random
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import requests

from . import config, media

log = logging.getLogger(__name__)

FLOW = "https://flow.google.com"
PROFILE_DIR = config.DATA_DIR / "flow_profile"
STATE_PATH = config.DATA_DIR / "flow_state.json"
UPLOAD_DIR = config.DATA_DIR / "flow_uploads"
EDIT_CREDITS = 40  # most an edit costs: Google lists 40; measured on AI Pro 2026-10-06: about 20
CHROME_PATHS = (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe")

DEFAULTS = {"chrome_path": "", "debug_port": 9333, "project_url": "", "min_gap_s": 60, "max_gap_s": 180,
            "daily_max_edits": 15, "cooldown_minutes": 60, "timeout_minutes": 12, "upload_timeout_minutes": 6,
            "start_timeout_minutes": 3,
            "clip_gap_s": 20,    # pause before starting the next clip (all its pieces run at once)
            "piece_gap_s": 3}    # pause between starting one piece and the next within a clip

# Phrases Flow shows when something went wrong, by what Clipper should do about it.
# Kept specific: a bare word like "policy" also appears in links and notices.
RIGHTS_WORDS = ("rights to use", "confirm you have", "necessary rights")
CREDIT_WORDS = ("enough credits", "out of credits", "insufficient credits")
BLOCKED_WORDS = ("unusual activity", "unusual_activity", "too many requests", "try again later",
                 "rate limit", "reached your limit", "daily limit")
FAILED_WORDS = ("failed to generate",)
REFUSED_WORDS = ("violates our", "violate our", "against our policies", "against our policy",
                 "was blocked", "can't generate this", "cannot generate this", "unable to generate this",
                 "not able to generate this", "safety filters", "content policy", "isn't allowed by")

_lock = threading.Lock()         # one edit at a time
_state_lock = threading.RLock()  # the state file is shared by the worker and the dashboard


class FlowError(RuntimeError):
    """The robot couldn't do its job. Stops the clip (keeping the pieces already done)."""


class NeedsYou(FlowError):
    """Something only you can fix in the robot window (sign in, a notice, credits)."""


class Paused(FlowError):
    """Flow pushed back (unusual activity, limits) or the daily cap is reached."""


class Refused(FlowError):
    """Google refused this edit (content). The adapter treats it like Omni's refusal."""


class Failed(FlowError):
    """'Sorry, this video failed to generate' (not charged). Worth one more try."""


class TimedOut(FlowError):
    """Flow didn't finish in time. It may still finish (and be charged): never retried blindly."""


# --------------------------------------------------------------------- settings / state

def settings(cfg: dict) -> dict:
    return {**DEFAULTS, **(cfg.get("flow") or {})}


def load_state() -> dict:
    with _state_lock:
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}


def save_state(**fields) -> dict:
    with _state_lock:
        state = {**load_state(), **fields}
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        os.replace(tmp, STATE_PATH)  # never leaves a half-written file
        return state


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def edits_today(state: dict = None) -> int:
    state = state if state is not None else load_state()
    return state.get("edits_today", 0) if state.get("day") == _today() else 0


def count_edit() -> None:
    """One generation Flow (probably) charged for: counts towards the daily cap and the gap."""
    with _state_lock:
        save_state(day=_today(), edits_today=edits_today() + 1, last_edit_at=time.time())


def pause(reason: str, minutes: float) -> None:
    save_state(paused_until=time.time() + minutes * 60, pause_reason=reason)


def clear_pause() -> None:
    """You fixed whatever it was (or want it to try again now)."""
    save_state(paused_until=0, pause_reason="", needs_you="")


def check_allowed(cfg: dict, state: dict = None) -> None:
    """Raise if the robot shouldn't start an edit now. The worker also uses this to leave
    clips waiting (instead of failing them) until the robot can go on."""
    fcfg = settings(cfg)
    state = state if state is not None else load_state()
    if state.get("needs_you"):
        raise NeedsYou(f"{state['needs_you']} (then click Check on the Connections page)")
    until = state.get("paused_until") or 0
    if until > time.time():
        raise Paused(f"Flow robot is paused until {time.strftime('%H:%M', time.localtime(until))}: "
                     f"{state.get('pause_reason') or 'Flow pushed back'}. Waiting clips carry on after that, "
                     f"or clear the pause on the Connections page to try sooner.")
    if edits_today(state) >= int(fcfg["daily_max_edits"]):
        raise Paused(f"Daily cap reached ({fcfg['daily_max_edits']} Flow edits today, flow: daily_max_edits). "
                     f"Waiting clips carry on tomorrow, or raise the cap.")
    credits = state.get("credits")
    if credits is not None and credits < EDIT_CREDITS and time.time() - state.get("credits_at", 0) < 6 * 3600:
        raise NeedsYou(f"Only {credits} Flow credits left (an edit costs about 20-{EDIT_CREDITS}). "
                       f"They refill daily/monthly, or you can buy more in Flow.")


def can_edit(cfg: dict) -> bool:
    try:
        check_allowed(cfg)
        return True
    except FlowError:
        return False


def gap_wait(cfg: dict, state: dict = None) -> float:
    """Seconds still to wait since the last edit, so edits don't come back-to-back."""
    fcfg = settings(cfg)
    state = state if state is not None else load_state()
    last = state.get("last_edit_at") or 0
    gap = random.uniform(float(fcfg["min_gap_s"]), max(float(fcfg["min_gap_s"]), float(fcfg["max_gap_s"])))
    return max(0.0, last + gap - time.time())


# Flow's Agent narrates what it's doing in long prose ("I'm going to edit the attached image…"),
# which can happen to contain a trouble word. Real notices (a rights prompt, "unusual activity",
# "failed to generate", a policy refusal) are short banners/toasts, so skip long narration lines.
AGENT_PROSE_LEN = 160


def classify(new_text: str) -> type:
    """The FlowError class for text that appeared on the page, or None."""
    low = "\n".join(ln for ln in new_text.lower().splitlines() if len(ln) <= AGENT_PROSE_LEN)
    if any(w in low for w in RIGHTS_WORDS) or any(w in low for w in CREDIT_WORDS):
        return NeedsYou
    if any(w in low for w in BLOCKED_WORDS):
        return Paused
    if any(w in low for w in FAILED_WORDS):
        return Failed
    if any(w in low for w in REFUSED_WORDS):
        return Refused
    return None


def new_lines(before: str, after: str, prompt: str = "") -> str:
    """Lines on the page now that weren't there before (so old messages don't count),
    leaving out our own prompt, which Flow shows back as a (shortened) caption."""
    seen = {}
    for line in before.splitlines():
        seen[line] = seen.get(line, 0) + 1
    ours = " ".join(prompt.split()).lower()
    out = []
    for line in after.splitlines():
        if seen.get(line):
            seen[line] -= 1
            continue
        clean = " ".join(line.split())
        if clean and not (ours and clean.lower().rstrip("…. ") in ours):
            out.append(clean)
    return "\n".join(out)


def credits_from(body: str):
    """Flow's credit balance, from its 'nzlxg' answer: [["wrb.fr","nzlxg","[886,1,..."""
    m = re.search(r'"nzlxg","\[(\d+),', body or "")
    return int(m.group(1)) if m else None


RUNNING, DONE = 2, 3  # Flow's generation status codes (1 = queued; anything above 3 = failed)


def generations_from(body: str) -> list:
    """[(result_id, source_ids_text, status), ...] from Flow's 'jwpduf' status answer, which
    it polls while a generation runs: [null, credits, [[result_id, project, workflow, null,
    null, spec(with the source media id), null, [status], ...], ...]]."""
    out = []
    for line in (body or "").splitlines():
        if '"jwpduf"' not in line:
            continue
        try:
            inner = json.loads(json.loads(line)[0][2])
            for entry in inner[2] or []:
                status = entry[7]
                while isinstance(status, list) and status:  # [3], and sometimes [[3]]
                    status = status[0]
                out.append((entry[0], json.dumps(entry[5]), status if isinstance(status, int) else None))
        except (ValueError, TypeError, IndexError, KeyError):
            continue
    return out


def length_note(seconds: float) -> str:
    return f" The edited video must be the full length of the attached video ({round(seconds)} seconds)."


def tile_name(base: str, state: dict = None) -> str:
    """The first of base, base-2, base-3... not already used for a finished edit, so a retry
    reuses an upload (or a result Flow already made) but a redo makes a new one."""
    done = set((state if state is not None else load_state()).get("done_tiles") or [])
    name, n = base, 1
    while name in done:
        n += 1
        name = f"{base}-{n}"
    return name


def mark_done(name: str) -> None:
    with _state_lock:
        done = [t for t in (load_state().get("done_tiles") or []) if t != name] + [name]
        save_state(done_tiles=done[-500:])


def remember_upload(name: str, media_id: str) -> None:
    with _state_lock:
        uploads = dict(load_state().get("uploads") or {})
        uploads[name] = media_id
        save_state(uploads=dict(list(uploads.items())[-500:]))


# --------------------------------------------------------------------- the browser

def chrome_path(cfg: dict) -> str:
    path = settings(cfg)["chrome_path"] or next((p for p in CHROME_PATHS if Path(p).exists()), "")
    if not path or not Path(path).exists():
        raise NeedsYou("Google Chrome isn't installed (or set flow: chrome_path in config.yaml)")
    return path


def browser_running(cfg: dict) -> bool:
    try:
        resp = requests.get(f"http://127.0.0.1:{settings(cfg)['debug_port']}/json/version", timeout=2)
        return resp.ok and "webSocketDebuggerUrl" in resp.json()
    except (requests.RequestException, ValueError):
        return False


def open_window(cfg: dict, url: str = "") -> None:
    """Start the robot's own Chrome (normal Chrome, so Google lets you sign in) if it isn't running."""
    if browser_running(cfg):
        return
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    fcfg = settings(cfg)
    subprocess.Popen([chrome_path(cfg), f"--user-data-dir={PROFILE_DIR}",
                      f"--remote-debugging-port={fcfg['debug_port']}", "--no-first-run",
                      "--no-default-browser-check", url or fcfg["project_url"] or load_state().get("project_url") or FLOW])
    for _ in range(30):
        time.sleep(1)
        if browser_running(cfg):
            return
    raise FlowError("The Flow robot's Chrome window didn't start")


def status(cfg: dict) -> dict:
    state = load_state()
    fcfg = settings(cfg)
    until = state.get("paused_until") or 0
    return {"backend": cfg["transform"].get("backend", "vertex"), "window_open": browser_running(cfg),
            "account": state.get("account", ""), "signed_in": state.get("signed_in"),
            "project_url": fcfg["project_url"] or state.get("project_url", ""),
            "credits": state.get("credits"), "edits_today": edits_today(state),
            "daily_max": int(fcfg["daily_max_edits"]), "needs_you": state.get("needs_you", ""),
            "paused_until": until if until > time.time() else None, "pause_reason": state.get("pause_reason", ""),
            "last_error": state.get("last_error", ""), "last_edit_at": state.get("last_edit_at")}


# --------------------------------------------------------------------- one edit

def _tile_info(page, name: str):
    return page.evaluate("""name => {
        const t = [...document.querySelectorAll('flow-grid-tile-container')].find(x => x.getAttribute('aria-label') === name);
        if (!t) return null;
        const srcs = [...t.querySelectorAll('video')].map(v => v.currentSrc || v.src || '').filter(Boolean);
        return {text: t.innerText.replace(/\\s+/g, ' ').trim(), src: srcs[0] || '', srcs,
                stacked: /\\bstacks\\b/.test(t.innerText)};
    }""", name)


def _media_id(src: str) -> str:
    m = re.search(r"/video/([0-9a-f-]{36})", src or "")
    return m.group(1) if m else ""


class _Run:
    """One connection to the robot's Chrome for one edit."""

    def __init__(self, cfg: dict):
        self.cfg, self.fcfg = cfg, settings(cfg)
        self.pushed_back = ""
        self.baseline = ""
        self.prompt_text = ""
        self.generations = {}  # result id -> (source ids text, status, when), from Flow's own status polls

    def __enter__(self):
        from playwright.sync_api import sync_playwright

        open_window(self.cfg)
        self._pw = sync_playwright().start()
        try:
            self.browser = self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{self.fcfg['debug_port']}")
            if not self.browser.contexts:
                raise FlowError("The robot's Chrome has no window to use")
            context = self.browser.contexts[0]
            flows = [p for p in context.pages if p.url.startswith(FLOW)]
            self.page = flows[-1] if flows else context.new_page()
            self.page.on("response", self._on_response)
        except BaseException as err:
            # __exit__ doesn't run when __enter__ fails: stop Playwright here, or every later
            # start in this thread fails ("Sync API inside the asyncio loop").
            try:
                self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
            if isinstance(err, Exception) and not isinstance(err, FlowError):
                raise FlowError(f"Couldn't connect to the robot's Chrome window: {err}") from err
            raise
        return self

    def __exit__(self, *exc):
        try:
            self._pw.stop()  # disconnects; the window stays open for next time
        except Exception:  # noqa: BLE001
            pass

    def _on_response(self, resp):
        if "batchexecute" not in resp.url:
            return
        try:
            body = resp.text()
        except Exception:  # noqa: BLE001
            return
        credits = credits_from(body)
        if credits is not None:
            save_state(credits=credits, credits_at=time.time())
        for result, sources, status in generations_from(body):
            self.generations[result] = (sources, status, time.time())
        if resp.status in (403, 429) or "UNUSUAL_ACTIVITY" in body:
            self.pushed_back = f"HTTP {resp.status}: {body[:160]}"

    def _text(self) -> str:
        return self.page.evaluate("() => document.body.innerText")

    def _tile(self, name: str):
        return self.page.locator(f'flow-grid-tile-container[aria-label="{name}"]').first

    def _hover(self, name: str) -> None:
        # bring_to_front restores a minimised window: hidden pages don't animate, which
        # makes normal (actionability-checked) mouse actions hang
        self.page.bring_to_front()
        self._tile(name).hover(force=True)

    # -- steps
    def open_project(self) -> None:
        page = self.page
        pinned = self.fcfg["project_url"]
        current = page.url.split("/edit/")[0].split("?")[0] if "/project/" in page.url else ""
        stored = load_state().get("project_url") or ""
        # config pins a project; otherwise the one open in the robot window; otherwise the last one
        target = pinned or current or stored
        page.bring_to_front()
        if target and page.url.split("?")[0] != target:
            page.goto(target, wait_until="domcontentloaded")
        elif not target and not page.url.startswith(FLOW):
            page.goto(FLOW, wait_until="domcontentloaded")
        button = page.get_by_label("Add media menu", exact=True).first
        try:
            button.wait_for(state="visible", timeout=45000)
        except Exception:  # noqa: BLE001 - decided below
            pass
        if "accounts.google.com" in page.url:
            save_state(signed_in=False)
            raise NeedsYou("The Flow robot is signed out: click 'Open robot window' on the Connections page "
                           "and sign in with your AI Pro account")
        if not button.is_visible():
            if target and target == stored and not pinned:
                save_state(project_url="")  # gone or not loading: use whatever you open next
            raise NeedsYou("No Flow project is open in the robot window (or it didn't load). Open or make "
                           "one there")
        account = page.evaluate("""() => document.querySelector('a[aria-label^="Google Account"]')
                                   ?.getAttribute('aria-label') || ''""")
        acct = re.search(r"\(([^)]+@[^)]+)\)", account or "")
        save_state(signed_in=True, project_url=page.url.split("/edit/")[0].split("?")[0],
                   account=acct.group(1) if acct else "")
        self.baseline = self._text()

    def clear_prompt(self) -> None:
        page = self.page
        page.keyboard.press("Escape")
        for _ in range(5):
            clear = page.get_by_label("Clear prompt", exact=True)
            if not clear.count():
                break
            clear.first.click()
            time.sleep(0.6)
        if page.get_by_label("Ingredient", exact=True).count():
            raise FlowError("Couldn't clear what's attached to Flow's prompt box")

    def _check_rights(self) -> None:
        if self.page.get_by_text("Rights to use this video").count():
            raise NeedsYou("Flow is asking you to confirm you have the rights to the video. Answer it in the "
                           "robot window yourself (Clipper never agrees for you)")

    def _check_pushback(self) -> None:
        """Network signs of being pushed back, or an 'unusual activity' message that wasn't
        on the page when this edit started (an old one in the grid doesn't count)."""
        if self.pushed_back:
            raise Paused(f"Flow reported unusual activity ({self.pushed_back})")
        try:
            fresh = new_lines(self.baseline, self._text(), self.prompt_text)
        except Exception:  # noqa: BLE001
            return
        if classify(fresh) is Paused:
            raise Paused(f"Flow: {' '.join(fresh.split())[:200]}")

    def upload(self, file: Path, name: str) -> None:
        page = self.page
        page.bring_to_front()
        page.get_by_label("Add media menu", exact=True).click()
        with page.expect_file_chooser(timeout=15000) as chooser:
            page.get_by_role("menuitem", name="Upload").click()
        chooser.value.set_files(str(file))
        deadline = time.time() + float(self.fcfg["upload_timeout_minutes"]) * 60
        while time.time() < deadline:
            time.sleep(3)
            self._check_rights()
            self._check_pushback()
            info = _tile_info(page, name)
            if info and _media_id(info["src"]):
                return
            if info:
                self._hover(name)
        raise FlowError(f"Flow didn't finish processing the upload within {self.fcfg['upload_timeout_minutes']} minutes")

    def upload_id(self, name: str) -> str:
        for _ in range(4):
            info = _tile_info(self.page, name)
            if info and _media_id(info["src"]):
                return _media_id(info["src"])
            self._hover(name)
            time.sleep(1.5)
        raise FlowError("Couldn't read the uploaded piece's id in Flow")

    def attach(self, name: str) -> None:
        """Right-click the tile -> Add to prompt."""
        page = self.page
        page.bring_to_front()
        self._tile(name).click(button="right")
        page.get_by_role("menuitem", name="Add to prompt").click()
        time.sleep(1.5)
        if page.get_by_label("Ingredient", exact=True).count() != 1:
            raise FlowError("The piece didn't attach to Flow's prompt box")

    def agent_on(self) -> None:
        agent = self.page.locator("button.agent-mode-chip, button:has-text('Agent')").first
        if agent.get_attribute("aria-pressed") != "true":
            agent.click()
            time.sleep(1.2)
        if agent.get_attribute("aria-pressed") != "true":
            raise FlowError("Couldn't switch on Flow's Agent")

    def prompt(self, text: str) -> None:
        page = self.page
        self.prompt_text = text
        box = page.locator("div.ProseMirror[contenteditable=true]").first
        box.click()
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        page.keyboard.type(text, delay=5)
        time.sleep(0.8)
        if " ".join(box.inner_text().split()) != " ".join(text.split()):
            raise FlowError("The prompt didn't go into Flow's box correctly")
        if page.get_by_label("Ingredient", exact=True).count() != 1:
            raise FlowError("The piece came off Flow's prompt box while typing")

    def click_generate(self) -> str:
        before = self._text()
        self.page.get_by_label("Start generation", exact=True).click()
        return before

    def wait_result(self, name: str, upload_id: str, before: str) -> str:
        """Wait for the result to stack onto the tile; returns its media id."""
        page = self.page
        started = time.time()
        deadline = started + float(self.fcfg["timeout_minutes"]) * 60
        start_deadline = started + float(self.fcfg["start_timeout_minutes"]) * 60
        last_progress, seen_progress, last_activity = "", False, started
        while time.time() < deadline:
            time.sleep(8)
            self._check_pushback()
            fresh = new_lines(before, self._text(), self.prompt_text)
            info = _tile_info(page, name) or {}
            progress = re.findall(r"\b(\d{1,3})%", fresh + " " + info.get("text", ""))
            if progress:
                seen_progress = True
                if progress[-1] != last_progress:
                    last_progress, last_activity = progress[-1], time.time()
                    log.info("Flow: %s at %s%%", name, last_progress)
            problem = classify(fresh)
            if problem:
                raise problem(f"Flow: {' '.join(fresh.split())[:300]}")
            # Flow's status polls say how our generation is doing (it polls a running one
            # every few seconds and stops when it ends)
            ours = {r: (st, at) for r, (src, st, at) in self.generations.items() if upload_id in src and r != upload_id}
            if ours:
                seen_progress = True
                last_activity = max(last_activity, max(at for _, at in ours.values()))
                if any(st == DONE for st, _ in ours.values()):
                    return next(r for r, (st, _) in ours.items() if st == DONE)
                if all(st is not None and st > DONE for st, _ in ours.values()):
                    raise Failed("Flow: Sorry, this video failed to generate (not charged)")
            if info.get("stacked") and "%" not in info.get("text", ""):
                result = self.result_id(name, upload_id)
                if result:
                    return result
            # Nothing moving for a while: a failure only shows in the clip's history, not on the grid
            quiet = time.time() - last_activity > 120
            if quiet or (not seen_progress and time.time() > start_deadline):
                if self.history_says_failed(name):
                    raise Failed("Flow: Sorry, this video failed to generate (not charged)")
                if not seen_progress and time.time() > start_deadline:
                    if fresh.strip():
                        raise NeedsYou(f"Flow's agent replied instead of editing: {' '.join(fresh.split())[:300]}")
                    raise Failed("Flow didn't start the edit")
                last_activity = time.time()  # not failed: look again in a couple of minutes
        raise TimedOut(f"Flow didn't finish within {self.fcfg['timeout_minutes']} minutes; it may still finish "
                       f"in the project, and a retry picks the result up from there")

    def history_says_failed(self, name: str) -> bool:
        """Open the tile (its history lists each edit and whether it failed), then go back."""
        page = self.page
        project = page.url.split("/edit/")[0].split("?")[0]
        try:
            page.bring_to_front()
            self._tile(name).click()
            time.sleep(5)
            text = new_lines(self.baseline, self._text(), self.prompt_text).lower()
        finally:
            page.goto(project, wait_until="domcontentloaded")
            page.get_by_label("Add media menu", exact=True).first.wait_for(state="visible", timeout=45000)
        return any(w in text for w in FAILED_WORDS)

    def result_id(self, name: str, upload_id: str) -> str:
        """The media id on the tile that isn't the uploaded original, or ''."""
        for _ in range(3):
            info = _tile_info(self.page, name) or {}
            ids = [_media_id(s) for s in info.get("srcs", [])]
            ids = [i for i in ids if i and i != upload_id]
            if ids:
                return ids[0]
            self._hover(name)
            time.sleep(1.5)
        return ""

    def poll(self, p: dict):
        """One look at a piece that's generating alongside others: its result id once done,
        None while it runs. Raises Failed/Refused/TimedOut for this piece only, and
        Paused/NeedsYou (they concern the whole robot) for everything."""
        now = time.time()
        fresh = new_lines(p["before"], self._text(), self.prompt_text)
        problem = classify(fresh)
        if problem in (Paused, NeedsYou):
            raise problem(f"Flow: {' '.join(fresh.split())[:300]}")
        info = _tile_info(self.page, p["name"]) or {}
        progress = re.findall(r"\b(\d{1,3})%", info.get("text", ""))
        if progress and progress[-1] != p["progress"]:
            p["progress"], p["activity"], p["seen"] = progress[-1], now, True
            log.info("Flow: %s at %s%%", p["name"], progress[-1])
        # Flow's status polls cover every generation that's running or queued
        ours = {r: (st, at) for r, (src, st, at) in self.generations.items()
                if p["upload_id"] in src and r != p["upload_id"]}
        if ours:
            p["seen"] = True
            p["activity"] = max(p["activity"], max(at for _, at in ours.values()))
            if any(st == DONE for st, _ in ours.values()):
                return next(r for r, (st, _) in ours.items() if st == DONE)
            if all(st is not None and st > DONE for st, _ in ours.values()):
                raise Failed("Flow: Sorry, this video failed to generate (not charged)")
        if info.get("stacked") and "%" not in info.get("text", ""):
            result = self.result_id(p["name"], p["upload_id"])
            if result:
                return result
        start_late = not p["seen"] and now > p["submitted"] + float(self.fcfg["start_timeout_minutes"]) * 60
        if now - p["activity"] > 120 or start_late:
            if self.history_says_failed(p["name"]):
                raise Failed("Flow: Sorry, this video failed to generate (not charged)")
            if start_late:
                if problem is Refused:
                    raise Refused(f"Flow: {' '.join(fresh.split())[:300]}")
                raise Failed("Flow didn't start the edit")
            p["activity"] = now  # not failed: look again in a couple of minutes
        # several run at once and Flow may queue some, so later pieces get a little longer
        if now > p["submitted"] + float(self.fcfg["timeout_minutes"]) * 60 + 120 * p["position"]:
            raise TimedOut(f"Flow didn't finish {p['name']} within {self.fcfg['timeout_minutes']} minutes; it may "
                           f"still finish in the project, and a retry picks the result up from there")
        return None

    def fetch(self, name: str, result_id: str, out: Path) -> None:
        src = ""
        for _ in range(4):
            info = _tile_info(self.page, name) or {}
            src = next((s for s in info.get("srcs", []) if _media_id(s) == result_id), "")
            if src:
                break
            self._hover(name)
            time.sleep(1.5)
        if not src:
            raise FlowError("Flow's tile isn't showing the edited result (it's still in the project)")
        resp = self.page.request.get(src, timeout=180000)
        if resp.status != 200 or not resp.body():
            raise FlowError(f"Couldn't download Flow's result (HTTP {resp.status})")
        tmp = out.with_name(out.stem + ".part.mp4")
        tmp.write_bytes(resp.body())
        try:
            playable = media.duration(tmp) >= 1
        except Exception:  # noqa: BLE001
            playable = False
        if not playable:
            tmp.unlink(missing_ok=True)
            raise FlowError("Flow's result isn't a playable video")
        tmp.replace(out)


def check(cfg: dict) -> dict:
    """Open the window if needed and confirm it's signed in with a project open (no credits used)."""
    if not _lock.acquire(timeout=1):
        raise FlowError("The robot is busy editing right now; check again when it's done")
    try:
        with _Run(cfg) as run:
            run.open_project()
            time.sleep(2)  # let Flow report the credit balance
        save_state(needs_you="", last_error="")
        return status(cfg)
    except NeedsYou as err:
        save_state(needs_you=str(err)[:300])
        raise
    finally:
        _lock.release()


def edit(segment: Path, instruction: str, out: Path, base: str, cfg: dict) -> Path:
    """Edit one piece in Flow and save the result to `out`. One at a time, paced.
    `base` names the piece's tile in Flow (stable per clip piece, so a retry can pick up
    an upload or a result that's already there)."""
    with _lock:
        check_allowed(cfg)
        wait = gap_wait(cfg)
        if wait > 0:
            log.info("Flow robot: waiting %.0f s before the next edit (pacing)", wait)
            time.sleep(wait)
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        text = " ".join(instruction.split()) + length_note(media.duration(segment))
        upload = None
        try:
            for attempt in (1, 2):
                name = tile_name(base)
                charged = False
                try:
                    with _Run(cfg) as run:
                        run.open_project()
                        run.clear_prompt()
                        info = _tile_info(run.page, name)
                        known = (load_state().get("uploads") or {}).get(name, "")
                        result = ""
                        if info and known and info.get("stacked"):
                            result = run.result_id(name, known)  # done earlier; only the download failed
                        if not result and info and known:
                            # used before without a result: its history may hold an old failure,
                            # so start clean on a new tile rather than read the old one wrong
                            mark_done(name)
                            name, info = tile_name(base), None
                        if not result:
                            if not info:
                                upload = UPLOAD_DIR / f"{name}.mp4"
                                shutil.copyfile(segment, upload)
                                run.upload(upload, name)
                            upload_id = run.upload_id(name)
                            remember_upload(name, upload_id)
                            run.attach(name)
                            run.agent_on()
                            run.prompt(text)
                            before = run.click_generate()
                            charged = True
                            log.info("Flow: generating %s", name)
                            result = run.wait_result(name, upload_id, before)
                            count_edit()
                            charged = False  # counted
                        run.fetch(name, result, out)
                        mark_done(name)
                        save_state(last_error="")
                        try:
                            run.clear_prompt()
                        except Exception as err:  # noqa: BLE001 - tidying up only; the edit is safe
                            log.warning("Flow: couldn't clear the prompt afterwards: %s", err)
                    return out
                except (Failed, Refused) as err:  # Flow says these aren't charged
                    save_state(last_edit_at=time.time(), last_error=str(err)[:300])
                    mark_done(name)  # leave that tile alone
                    if isinstance(err, Refused) or attempt == 2:
                        raise
                    log.warning("Flow robot: %s; trying once more", err)
                    time.sleep(gap_wait(cfg) or 30)
                finally:
                    if charged:
                        count_edit()  # generation started and then something broke: it likely cost credits
                    if upload:
                        upload.unlink(missing_ok=True)
                        upload = None
        except Paused as err:
            pause(str(err)[:200], float(settings(cfg)["cooldown_minutes"]))
            save_state(last_error=str(err)[:300])
            raise
        except NeedsYou as err:
            save_state(needs_you=str(err)[:300], last_error=str(err)[:300])
            raise
        except FlowError as err:
            save_state(last_error=str(err)[:300], last_edit_at=time.time())
            raise
        except Exception as err:  # noqa: BLE001 - a Playwright error, a changed page...
            save_state(last_error=f"Flow robot: {err}"[:300], last_edit_at=time.time())
            raise FlowError(f"Flow robot stopped: {err}") from err


# --------------------------------------------------------------------- a whole clip at once

def edit_many(pieces: list, instruction: str, cfg: dict) -> dict:
    """Edit all of a clip's pieces at the same time: start every one (a few seconds apart),
    wait for them together, then fetch each result. Waits flow: clip_gap_s (20 s) after the
    previous clip first. A piece that fails gets one more go.

    pieces: [(key, segment, out, base)]: `out` is where the result goes, `base` names the
    tile (as in edit()). Returns {key: out, or the FlowError for a piece that couldn't be
    done}. Paused / NeedsYou are raised (after the pieces already running are collected),
    because they stop the robot, not just one piece."""
    results = {}
    if not pieces:
        return results
    with _lock:
        try:
            todo = list(pieces)
            for attempt in (1, 2):
                _batch(todo, instruction, cfg, results)
                todo = [p for p in todo if isinstance(results.get(p[0]), Failed)]
                if not todo or attempt == 2:
                    break
                log.warning("Flow robot: %d piece(s) failed to generate; trying them once more", len(todo))
            stop = next((r for r in results.values() if isinstance(r, (Paused, NeedsYou))), None)
            if stop:
                raise stop
            return results
        except Paused as err:
            pause(str(err)[:200], float(settings(cfg)["cooldown_minutes"]))
            save_state(last_error=str(err)[:300])
            raise
        except NeedsYou as err:
            save_state(needs_you=str(err)[:300], last_error=str(err)[:300])
            raise
        except FlowError as err:
            save_state(last_error=str(err)[:300])
            raise
        except Exception as err:  # noqa: BLE001 - a Playwright error, a changed page...
            save_state(last_error=f"Flow robot: {err}"[:300])
            raise FlowError(f"Flow robot stopped: {err}") from err
        finally:
            save_state(last_batch_at=time.time())


def _batch(pieces: list, instruction: str, cfg: dict, results: dict) -> None:
    fcfg = settings(cfg)
    wait = float(load_state().get("last_batch_at") or 0) + float(fcfg["clip_gap_s"]) - time.time()
    if wait > 0:
        log.info("Flow robot: waiting %.0f s before the next clip", wait)
        time.sleep(wait)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    running, uploads = [], []
    try:
        with _Run(cfg) as run:
            run.open_project()
            run.clear_prompt()
            # 1. start every piece
            for position, (key, segment, out, base) in enumerate(pieces):
                try:
                    check_allowed(cfg)  # daily cap / pause / credits, before each new generation
                except (Paused, NeedsYou) as err:
                    results[key] = err  # the rest wait; the ones already started still finish
                    continue
                try:
                    name = tile_name(base)
                    info = _tile_info(run.page, name)
                    known = (load_state().get("uploads") or {}).get(name, "")
                    if info and known and info.get("stacked"):
                        result = run.result_id(name, known)  # done earlier; only the download failed
                        if result:
                            running.append({"key": key, "out": out, "name": name, "result": result})
                            continue
                    if info and known:
                        mark_done(name)  # used before without a result: start clean on a new tile
                        name, info = tile_name(base), None
                    if not info:
                        upload = UPLOAD_DIR / f"{name}.mp4"
                        shutil.copyfile(segment, upload)
                        uploads.append(upload)
                        run.upload(upload, name)
                    try:
                        upload_id = run.upload_id(name)
                    except FlowError:
                        if not info:
                            raise
                        # a tile left half-uploaded by an earlier, interrupted run: upload afresh
                        mark_done(name)
                        name = tile_name(base)
                        upload = UPLOAD_DIR / f"{name}.mp4"
                        shutil.copyfile(segment, upload)
                        uploads.append(upload)
                        run.upload(upload, name)
                        upload_id = run.upload_id(name)
                    remember_upload(name, upload_id)
                    run.attach(name)
                    run.agent_on()
                    run.prompt(" ".join(instruction.split()) + length_note(media.duration(segment)))
                    before = run.click_generate()
                    count_edit()  # started: it likely costs credits whatever happens next
                    now = time.time()
                    log.info("Flow: generating %s (%d of %d at once)", name, position + 1, len(pieces))
                    running.append({"key": key, "out": out, "name": name, "upload_id": upload_id, "before": before,
                                    "submitted": now, "activity": now, "seen": False, "progress": "",
                                    "position": position})
                    time.sleep(float(fcfg["piece_gap_s"]))
                    run.clear_prompt()
                except (Paused, NeedsYou):
                    raise
                except Exception as err:  # noqa: BLE001 - this piece only; the started ones still finish
                    results[key] = err if isinstance(err, FlowError) else FlowError(f"Flow robot: {err}")
                    log.warning("Flow: couldn't start piece %s: %s", key, str(err)[:200])
                    try:
                        run.clear_prompt()
                    except Exception:  # noqa: BLE001
                        pass
            # 2. wait for them together
            pending = [p for p in running if "result" not in p]
            while pending:
                time.sleep(8)
                run._check_pushback()
                for p in list(pending):
                    try:
                        result = run.poll(p)
                    except (Failed, Refused, TimedOut) as err:
                        results[p["key"]] = err
                        mark_done(p["name"])  # leave that tile alone
                        pending.remove(p)
                        log.warning("Flow: %s: %s", p["name"], err)
                        continue
                    if result:
                        p["result"] = result
                        pending.remove(p)
                        log.info("Flow: %s done", p["name"])
            # 3. fetch the results
            for p in running:
                if "result" not in p:
                    continue
                try:
                    run.fetch(p["name"], p["result"], p["out"])
                    mark_done(p["name"])
                    results[p["key"]] = p["out"]
                except FlowError as err:
                    results[p["key"]] = err
            save_state(last_error="")
            try:
                run.clear_prompt()
            except Exception as err:  # noqa: BLE001 - tidying up only
                log.warning("Flow: couldn't clear the prompt afterwards: %s", err)
    finally:
        for upload in uploads:
            upload.unlink(missing_ok=True)
