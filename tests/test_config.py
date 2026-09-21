from photosort import config
def test_exts():
    assert ".jpg" in config.IMAGE_EXTS and ".arw" in config.RAW_EXTS
    assert config.PREVIEW_EDGE == 1024


def test_settings_round_trip_and_unreadable_file(tmp_path):
    from photosort import settings
    from photosort.config import settings_path, app_home
    assert settings_path() == app_home() / "settings.json"
    assert settings.load() == {} and settings.get_export_base() is None
    settings.set_export_base(tmp_path / "disk")
    assert settings.get_export_base() == tmp_path / "disk"
    assert settings.load()["export_base"] == str(tmp_path / "disk")
    settings.set_export_base(None)
    assert settings.get_export_base() is None and "export_base" not in settings.load()
    settings_path().write_text("{not json")
    assert settings.load() == {} and settings.get_export_base() is None


def test_settings_save_is_atomic(tmp_path, monkeypatch):
    import os
    from photosort import settings
    from photosort.config import settings_path
    settings.save({"export_base": "/a"})
    assert settings.load() == {"export_base": "/a"}
    written = []
    real_replace = os.replace
    def spy(src, dst):
        written.append((str(src), str(dst))); real_replace(src, dst)
    monkeypatch.setattr(settings.os, "replace", spy)
    settings.save({"export_base": "/b"})
    assert written and written[-1][1] == str(settings_path()) and written[-1][0] != str(settings_path())
    assert os.path.dirname(written[-1][0]) == str(settings_path().parent)
    assert settings.load() == {"export_base": "/b"}
    assert sorted(os.listdir(settings_path().parent)) == ["settings.json"]     # no temp file left behind


def test_thumbnail_pool_is_sized_to_the_mac():
    """Cores minus one for the thumbnail pass (the main process stores rows), never under 2, capped at
    JPEG_WORKERS_MAX; RAW and video pools keep their memory-bound sizes."""
    import os
    assert config.thumb_workers(8) == 7 and config.thumb_workers(10) == 8 and config.thumb_workers(16) == 8
    assert config.thumb_workers(2) == 2 and config.thumb_workers(1) == 2 and config.thumb_workers(None) == 3
    assert config.JPEG_WORKERS == config.thumb_workers(os.cpu_count())
    assert config.RAW_WORKERS == 2 and config.VIDEO_WORKERS == 3
