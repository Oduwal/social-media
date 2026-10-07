import pytest

from clipper import config


def test_unquoted_hashtags_give_a_plain_message(tmp_path, monkeypatch):
    bad = tmp_path / "config.yaml"
    bad.write_text('creator:\n  handle: "x"\n  hashtags: [#GehGeh, #fyp]\nmonitor:\n  enabled: true\n',
                   encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", bad)
    with pytest.raises(config.ConfigError, match="line .*quotes"):
        config.load()


def test_quoted_hashtags_load(tmp_path, monkeypatch):
    good = tmp_path / "config.yaml"
    good.write_text('creator:\n  handle: "@x"\n  hashtags: ["#GehGeh", "#fyp"]\n', encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", good)
    cfg = config.load()
    assert cfg["creator"]["hashtags"] == ["#GehGeh", "#fyp"] and cfg["creator"]["handle"] == "x"


def test_real_config_loads():
    cfg = config.load()
    assert cfg["creator"]["handle"] == "official_gehgeh_"
    assert cfg["creator"]["hashtags"] == ["#GehGeh", "#fyp"]
