from clipper import config, downloader


def _cfg(**downloader_settings):
    return config._merge(config.DEFAULTS, {"downloader": downloader_settings})


def test_no_login_by_default():
    assert downloader.cookie_args(_cfg()) == []


def test_cookies_file_relative_to_the_project(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    (tmp_path / "tiktok_cookies.txt").write_text("# Netscape HTTP Cookie File\n")
    assert downloader.cookie_args(_cfg(cookies_file="tiktok_cookies.txt")) == \
        ["--cookies", str(tmp_path / "tiktok_cookies.txt")]


def test_missing_cookies_file_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    assert downloader.cookie_args(_cfg(cookies_file="nope.txt")) == []


def test_browser_cookies():
    assert downloader.cookie_args(_cfg(cookies_from_browser="firefox")) == ["--cookies-from-browser", "firefox"]


def test_file_wins_over_browser(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    (tmp_path / "c.txt").write_text("x")
    assert downloader.cookie_args(_cfg(cookies_file="c.txt", cookies_from_browser="firefox"))[0] == "--cookies"


def test_ytdlp_passes_the_login(monkeypatch, tmp_path):
    monkeypatch.setattr(downloader, "cookie_args", lambda cfg=None: ["--cookies", "c.txt"])
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return None

    monkeypatch.setattr(downloader.subprocess, "run", fake_run)
    downloader.ytdlp("--version")
    assert seen["cmd"][-3:] == ["--cookies", "c.txt", "--version"]
