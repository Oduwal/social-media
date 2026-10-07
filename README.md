# Clipper

Watches one TikTok creator. When they post, Clipper:

1. downloads the video without the watermark, at the best quality TikTok has
2. has Gemini (`gemini-3.5-flash`) watch it and find **every** highlight worth posting (15–45 s each)
3. cuts all of them into free previews in your **Library**
4. you pick which ones to make albino. Only those go through your `transform_video.py` (the paid Omni edit)
5. you finish each clip in the **editor**: trim, text, music, filters, zoom. Optionally, do heavier edits in **Shotcut**
6. posts it to TikTok, Instagram Reels and X through Upload-Post

Nothing costs Omni money until you pick a clip, and nothing is posted until you click **Approve & Post**.

## One-time setup

1. **Install** (already done on this PC):
   `py -m pip install -r requirements.txt`
2. **Pick the creator.** In `config.yaml`, set `creator.handle` (no @). You can also set `credit` and `hashtags` to match the clipping campaign's rules.
3. **Posting.** Clipper posts by itself through each platform's official API, with no subscription. Open the **Connections** page in the dashboard and follow the steps for each platform:
   - **TikTok (free):** create a free TikTok developer app, paste its two keys, then click **Connect TikTok**.
     - By default each clip is **sent to your TikTok inbox as a draft**. You tap the notification, add a trending sound and the caption, and post. Without TikTok approving your app, fully automatic TikTok posts can only be private.
   - **Instagram Reels (free):** your Instagram must be a **Professional account linked to a Facebook Page**. Create a free Meta developer app, paste its ID and secret, then click **Connect Instagram**. After that, posting is fully automatic.
   - **X (about $0.015 per post):** create an X developer app with a few dollars of credit, paste its four keys, then click **Check X keys**. After that, posting is fully automatic.
   - **Upload-Post still works as an alternative.** To use it, set `posting: service: "upload_post"` in `config.yaml` and put the key in `.env`.
4. **Music.** Put royalty-free tracks (mp3/wav/m4a) in the `music` folder.
   - Copyrighted songs get muted or taken down on Instagram and X.
   - Optional: tag tracks with a mood in `music/library.json` so the right one is pre-selected. Moods are hype, chill, funny, emotional, dramatic, inspirational and suspense.
5. **Optional: Shotcut** for heavy edits (free and open source): `winget install --id Meltytech.Shotcut -e`
6. **Check everything:** `python -m clipper check`. Every line should say `[OK]`.

## Daily use

Double-click **`start_clipper.bat`**. It opens http://127.0.0.1:8000.

**Videos** (home page)
- New posts from the creator appear on their own. The **first run skips the creator's existing videos**; click *Process* on any of them if you want it anyway.
- To clip a video by hand, paste any TikTok link into the box.
- Gemini watches each video. You get a Windows notification ("12 highlights found"), and the clips are cut into the Library.

**Library**
- While Gemini works and clips are cut, a progress bar shows how far along it is, e.g. *Cutting highlights: 7 of 16 ready*. New previews appear by themselves. If you're watching one or have clips ticked, a **Show new previews** button appears instead, so nothing jumps under you.
- The **To review** tab shows every highlight as a free preview. **Click a preview to watch it with sound right there**; click again to pause. Hovering plays it silently. Click the title for details: why Gemini picked it, and what's said.
- Tick the ones you want. The bar shows the cost, e.g. *3 clips · ≈ 8 Omni edits*. Then choose:
  - **Make albino:** runs your transform script on just those clips.
  - **Use as is:** skips the transform.
  - **Discard:** hides it. You can bring it back later.
- Finished clips move to **Ready to post**.

**Clip page and editor**
- **✎ Edit video** opens Clipper's timeline editor, laid out like HyperFrames Studio, rendered by ffmpeg on your PC:
  - **Timeline:** a main video track, plus as many text, caption, image/sticker and sound tracks as you want. Drag clips to reorder, drag edges to trim, **S** splits at the playhead (to cut out a part, split it and delete it). Things snap to edges, the playhead and beat markers (**B**). **Ripple** closes gaps when you delete. Undo and redo with Ctrl+Z and Ctrl+Shift+Z. Drafts save by themselves.
  - **On the video:** click anything to select it, drag it to move it, and use the handles to resize and rotate it. It snaps to the centre. **Safe zones** shows where TikTok and Reels put their buttons.
  - **Design panel:** fonts (Anton, Bebas Neue, Bangers, Poppins and more), colour, outline, shadow, box, size, ALL CAPS, entrance and exit animations, and word effects (highlight the spoken word, word by word, typewriter). ◆ adds a **keyframe** at the playhead for position, size, rotation, opacity, zoom/pan and volume.
  - **Main clips:** speed (0.25×–4×), volume, look, **transitions** between cuts (fade, slide, wipe, circle, zoom and more), camera moves (punch-in, slow zoom, pan, shake). The original (non-albino) cut is in **Media** too, for B-roll.
  - **Catalog:** ready-made titles, caption styles (restyle all captions in one click), social overlays (credit lower-third, follow for part 2, like & share, progress bar, comment below), emoji stickers, shapes, transitions, effects and animations.
  - **Captions tab:** **✨ Auto captions** listens to the clip on this PC (HyperFrames' speech-to-text) and places word-timed captions where each word lands after your cuts. Fix misheard words in the list.
  - **Media tab:** import your own videos, images and sounds (drag them in), and use your music library. **AI tab:** describe a change ("make the captions yellow, add a fire emoji when he laughs") and Gemini makes it.
  - **Export / Save & render:** 1080p or 720p, 24/30/60 fps; a 40-second clip renders in about one to two minutes. The browser preview is close, and the render is exact.
  - The older one-screen editor is still there as **Simple editor**, and **Open in Studio** uses HyperFrames Studio itself (custom web graphics, but its renders take several minutes). `editor:` in `config.yaml` picks which one **Edit video** opens.
- **Trending TikTok sound:**
  - **Posting directly (the default):** Gemini searches the web for this week's trending TikTok sounds in the US and Nigeria, and keeps that list for 12 hours. It then watches the clip and suggests the 3 that fit best, by name. Add the one you like in TikTok when you finish the draft; **Find on TikTok** opens it. Choosing a sound makes Clipper send TikTok a version without your editor music, so the two don't clash. About 1–2 cents.
  - **With Upload-Post:** Gemini **listens** to this week's top trending TikTok sounds from the US and Nigeria charts while watching the clip. It suggests the 3 that fit best and says why. That costs about $0.02 per clip.
  - **▶ Listen** plays a sound. **Preview with clip** plays it under the clip's voice. You can also pick any other sound from the charts, or **Ask Gemini again** for this week's chart.
  - The chosen sound is attached **to the TikTok post only**, through TikTok's own licensed music library, so your clip appears on that sound's page. TikTok gets a version without your editor music, so the two don't clash. Instagram and X keep your editor music, because their apps can't attach library sounds.
  - The TikTok connection on Upload-Post needs the **music** permission. If the sounds don't load, reconnect TikTok there.
  - Change the countries in `config.yaml` under `trending: countries`.
- **Open in Shotcut** sends the clip and its music to Shotcut for keyframes, transitions and extra tracks. Save there with Ctrl+S, then click **Use my Shotcut edit**, and Clipper renders your Shotcut project into the final video. Text and filters from the dashboard editor aren't carried over to Shotcut.
- Edit the caption for each platform, tick the platforms, then **Approve & Post** or **Discard**.

**Start private.** `tiktok_privacy_level` is `SELF_ONLY` (only you can see the post) until you change it to `PUBLIC_TO_EVERYONE` in `config.yaml`. Do one test post first.

### Start automatically when Windows starts
```
schtasks /Create /TN "Clipper" /SC ONLOGON /TR "\"C:\Users\ODUWA\Desktop\Social media\start_clipper.bat\""
```

## TikTok login (cookies), optional but recommended

Some posts are hidden from logged-out viewers ("This post may not be comfortable for some audiences. Log in for access"). yt-dlp can't download those without a TikTok login. Clipper then falls back to TikWM, an unofficial service that works today but could stop at any time. To give yt-dlp a login:

1. In Chrome or Edge, install the **Get cookies.txt LOCALLY** extension. Avoid the similarly named "Get cookies.txt" without LOCALLY: it was reported as malware.
2. Log in at tiktok.com, click the extension and export **only tiktok.com** as `tiktok_cookies.txt` into this folder.
3. In `config.yaml`, set `downloader: cookies_file: "tiktok_cookies.txt"` and restart Clipper.
4. `python -m clipper check` should show the login.

Good to know:
- **The file is your logged-in session.** Anyone who has it can use your TikTok account, so never share it or upload it anywhere.
- **Use a spare TikTok account if you can**, so heavy downloading can't get your main account flagged.
- **Re-export it if downloads start failing again.** TikTok logins expire after a while.
- **Firefox users** can set `cookies_from_browser: "firefox"` instead of a file. On Windows, reading cookies straight from Chrome or Edge usually fails while the browser is open.

## Albino edits through Google Flow (the Flow robot)

When there's no Vertex credit, Clipper can run the albino edit in **Google Flow** on your Google AI plan (`transform: backend: "flow"` in `config.yaml`; `"vertex"` switches back). Flow has no API, so a robot drives flow.google.com in its own Chrome window, the way you'd do it by hand: upload the 10 s piece → right-click it → **Add to prompt** → **Agent** on → your prompt → generate → download. Each piece takes about 4-5 minutes and about 20 Flow credits. The result is matched back to 1080p, 30 fps and the exact length, with the original voice.

Set up once, on the **Connections** page:
1. Click **Open robot window**. It's real Chrome with its own profile (`data/flow_profile`), separate from your normal browser.
2. Sign in there with the Google account that has your AI plan, and open (or make) a Flow project.
3. In Flow's Agent settings, keep **Confirm before generating: Never** and the video default at **9:16, x1, Omni 1.1 Flash**.
4. Click **Check**. The card shows your account and credits.

Good to know:
- **Google's terms restrict automated use, so the robot is slow on purpose.** It makes one edit at a time, waits 1-3 minutes between edits and does at most 15 a day (`flow: daily_max_edits`). It never gets around a check.
- **It stops and waits for you** if it's signed out, if Flow shows a rights notice (only you can answer that), or when credits run low. It pauses for an hour if Flow reports "unusual activity". Clips wait in the queue with the reason shown, then carry on by themselves. Use **I fixed it, carry on** / **Clear pause** to go sooner.
- **Failed edits aren't charged.** The robot tries a failed piece once more, then keeps the original footage for it. A refusal skips the rest of the clip, as with Vertex.
- **Don't click in the robot's window while it's editing.** You can leave it in the background.
- `data/flow_profile` holds your Google login: never share it.

## From the terminal
```
python -m clipper list-new                   # creator's newest posts, no download
python -m clipper run --url <tiktok link>    # find and cut every highlight (Gemini only, no Omni cost)
```
While the terminal works on a video, the dashboard leaves it alone. If you stop early with `--until download` or `--until highlight`, the video stays **held** until you press **Continue** on its page. The terminal never transforms or posts anything.

## Good to know

- **Your transform script is not modified.** It saves its work by file name next to itself, so a second video would reuse the first one's pieces. Clipper loads it and gives every clip its own folder in `data/jobs/<clip id>/transform_work` instead.
  - If Omni fails on some 10-second pieces, the clip uses the original footage for those pieces and the page warns you. **Retry those segments** only re-runs the failed ones; pieces that worked aren't billed again.
  - "Internal error encountered" in the failure files means Google had a server problem. Retrying later usually works.
  - If every piece fails, the AI label is switched off, since nothing was changed.
- **Posts are labelled as AI-generated** on all 3 platforms when Omni changed the video. Once Instagram has a label, it can't be removed.
- **TikTok breaks yt-dlp every few weeks.** Clipper updates yt-dlp daily and backs off when blocked. If the dashboard shows the monitor failing:
  - Add an Apify token to `.env` as a paid fallback, or paste links by hand.
  - If downloads fail, it falls back to TikWM automatically.
- **Captions on X:** links are removed from X captions (an Upload-Post rule). TikTok allows 15 posts a day. If TikTok puts a post in your drafts instead of publishing it, the page says so.
- **Costs:**
  - Gemini highlights: about $0.01 for a short TikTok, or about $0.20 for a 20-minute video.
  - Omni: your usual cost per 10-second piece, only for clips you pick.
  - Editor and Shotcut renders: free; they run on your PC.
  - Upload-Post Basic: $24/month.
- Each video and each clip has a folder in `data/jobs/`: video `<id>`, clips `<id>-01`, `<id>-02`… The log is `data/clipper.log`.
- Run the tests with `python -m pytest`. They don't make any paid calls.
