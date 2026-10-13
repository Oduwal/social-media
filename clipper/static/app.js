// X character counter
document.querySelectorAll(".counter").forEach((el) => {
  const box = document.getElementById(el.dataset.for);
  const update = () => {
    el.textContent = `${box.value.length}/280`;
    el.classList.toggle("bad-text", box.value.length > 280);
  };
  box.addEventListener("input", update);
  update();
});

// Music preview + volume readout
const track = document.getElementById("track");
const player = document.getElementById("player");
const preview = document.getElementById("preview");
if (track && player && preview) {
  preview.addEventListener("click", () => {
    if (!player.paused) { player.pause(); preview.textContent = "▶ Preview"; return; }
    if (!track.value) return;
    player.src = "/music/" + encodeURIComponent(track.value);
    player.play();
    preview.textContent = "■ Stop";
  });
  track.addEventListener("change", () => { player.pause(); preview.textContent = "▶ Preview"; });
}
const vol = document.getElementById("vol");
const volout = document.getElementById("volout");
if (vol && volout) vol.addEventListener("input", () => { volout.textContent = Number(vol.value).toFixed(2); });

// Don't auto-refresh away from a caption the user is typing
document.querySelectorAll("textarea, input, select").forEach((el) =>
  el.addEventListener("focus", () => document.querySelector('meta[http-equiv="refresh"]')?.remove()));

// Library: hover for a silent peek; click to watch with sound (one at a time)
let watching = null;
document.querySelectorAll(".thumb.playable").forEach((box) => {
  const v = box.querySelector("video");
  v.addEventListener("mouseenter", () => { if (watching !== v) { v.preload = "auto"; v.play().catch(() => {}); } });
  v.addEventListener("mouseleave", () => { if (watching !== v) v.pause(); });
  box.addEventListener("click", () => {
    if (watching === v) {                       // second click: pause
      v.pause(); v.muted = true; box.classList.remove("watching"); watching = null;
      return;
    }
    if (watching) { watching.pause(); watching.muted = true; watching.parentElement.classList.remove("watching"); }
    watching = v;
    box.classList.add("watching");
    v.muted = false;
    v.currentTime = 0;
    v.play().catch(() => {});
  });
});
window.isWatching = () => watching !== null && !watching.paused;

// Library / video page: live progress while highlights are found and cut. New previews
// appear on their own, except while you're watching one or have clips ticked: then a
// "Show new previews" button appears instead, so nothing jumps under you.
const progressBox = document.getElementById("progress");
if (progressBox) {
  const baseline = progressBox.dataset.signature;
  const text = document.getElementById("progresstext");
  const bar = document.getElementById("progressbar");
  const showBtn = document.getElementById("progressshow");
  showBtn.addEventListener("click", () => location.reload());
  const busyUser = () => window.isWatching() || picks.some((p) => p.checked)
    || /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName) && document.activeElement.type !== "checkbox";
  async function poll() {
    let p = null;
    try {
      const video = progressBox.dataset.video;
      const resp = await fetch("/api/progress" + (video ? `?video=${encodeURIComponent(video)}` : ""));
      if (resp.ok) p = await resp.json();
    } catch (err) { /* server restarting: try again later */ }
    if (p) {
      const active = p.cutting > 0 || p.finding > 0;
      progressBox.hidden = !active && p.signature === baseline;
      text.textContent = p.finding && !p.total ? "🔎 Gemini is watching the video for highlights…"
        : active ? `✂ Cutting highlights: ${p.cut} of ${p.total} ready` : `✅ All ${p.total} highlights are ready`;
      bar.style.width = p.total ? `${Math.round(100 * p.cut / p.total)}%` : "0%";
      if (p.signature !== baseline) {
        if (!busyUser()) { location.reload(); return; }
        showBtn.hidden = false;
      }
      setTimeout(poll, active || p.working ? 4000 : 30000);
    } else {
      setTimeout(poll, 10000);
    }
  }
  setTimeout(poll, 3000);
}

// Library: select all + running cost of the picked clips
const picks = [...document.querySelectorAll("input.pick")];
const summary = document.getElementById("picksummary");
function pickedEdits() {
  return picks.filter((p) => p.checked).reduce((n, p) => n + Number(p.dataset.edits || 0), 0);
}
function updatePickSummary() {
  if (!summary) return;
  const n = picks.filter((p) => p.checked).length;
  summary.textContent = n ? `${n} clip${n > 1 ? "s" : ""} selected` : "Tick clips to use or delete.";
}
function confirmDelete() {
  const n = picks.filter((p) => p.checked).length;
  if (!n) { alert("Tick at least one clip first."); return false; }
  return confirm(`Permanently delete ${n} clip${n > 1 ? "s" : ""}? This can't be undone.`);
}
picks.forEach((p) => p.addEventListener("change", updatePickSummary));
const checkall = document.getElementById("checkall");
if (checkall) checkall.addEventListener("change", () => {
  picks.forEach((p) => { p.checked = checkall.checked; });
  updatePickSummary();
});
function confirmEdit() {
  if (!picks.some((p) => p.checked)) { alert("Tick at least one clip first."); return false; }
  return true;
}
function confirmRetry() {
  const n = picks.filter((p) => p.checked).length;
  if (!n) { alert("Tick at least one clip first."); return false; }
  return confirm(`Retry ${n} clip${n > 1 ? "s" : ""}? Each keeps its edit and captions. A clip that failed while posting will be sent again to the platforms it hasn't reached yet.`);
}
function confirmPick() {
  const n = picks.filter((p) => p.checked).length;
  if (!n) { alert("Tick at least one clip first."); return false; }
  return confirm(`Make ${n} clip${n > 1 ? "s" : ""} albino? That's about ${pickedEdits()} paid Omni edits.`);
}

// Copy a caption to the clipboard (for pasting into the TikTok draft or a manual post)
document.querySelectorAll("[data-copy]").forEach((b) => b.addEventListener("click", async () => {
  const box = document.getElementById(b.dataset.copy);
  try { await navigator.clipboard.writeText(box.value); } catch (err) { box.select(); document.execCommand("copy"); }
  const label = b.textContent;
  b.textContent = "Copied ✓";
  setTimeout(() => { b.textContent = label; }, 1500);
}));

// Trending TikTok sounds: listen to a preview, or hear it together with the clip
const soundPlayer = document.getElementById("soundplayer");
let soundButton = null;
let together = null; // {video, src, volume} while previewing with the clip
function stopSound() {
  if (!soundPlayer) return;
  soundPlayer.pause();
  if (soundButton) soundButton.textContent = soundButton.dataset.label;
  soundButton = null;
  if (together) {
    together.video.pause();
    together.video.src = together.src;
    together.video.volume = together.volume;
    together = null;
  }
}
function playSound(button, trackId, start, volume) {
  soundPlayer.src = `/trending/preview/${encodeURIComponent(trackId)}`;
  soundPlayer.volume = Math.min(1, Math.max(0, volume));
  soundPlayer.addEventListener("loadedmetadata", () => { soundPlayer.currentTime = Number(start) || 0; },
                               { once: true });
  soundPlayer.play().catch(() => {});
  button.textContent = "■ Stop";
  soundButton = button;
}
if (soundPlayer) {
  soundPlayer.addEventListener("ended", stopSound);
  document.querySelectorAll("[data-preview], [data-together]").forEach((b) => { b.dataset.label = b.textContent; });
  document.querySelectorAll("[data-preview]").forEach((b) => b.addEventListener("click", () => {
    const again = soundButton === b;
    stopSound();
    if (!again) playSound(b, b.dataset.preview, b.dataset.start, 1);
  }));
  document.querySelectorAll("[data-together]").forEach((b) => b.addEventListener("click", () => {
    const again = soundButton === b;
    stopSound();
    if (again) return;
    // the clip's own audio (without your editor music), like the TikTok version
    const video = document.querySelector("video.vid");
    if (!video) return;
    together = { video, src: video.src, volume: video.volume };
    video.src = `/media/${b.dataset.clip}/transformed.mp4`;
    video.volume = Number(b.dataset.voice) / 100;
    video.play().catch(() => {});
    video.addEventListener("ended", stopSound, { once: true });
    playSound(b, b.dataset.together, b.dataset.start, Number(b.dataset.volume) / 100);
  }));
}
