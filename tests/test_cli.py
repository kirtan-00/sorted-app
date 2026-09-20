from photosort.cli import main
from conftest import make_image

def test_index_cmd(tmp_path, capsys):
    make_image(tmp_path, "a.jpg")
    main(["index", str(tmp_path), "--no-faces", "--workers", "1"])
    out = capsys.readouterr().out
    assert "indexed 1" in out

def test_people_cmd_prints_suggestion_count(tmp_path, capsys, monkeypatch):
    from test_people import _two_close_people
    from photosort import people as pm
    monkeypatch.setattr(pm, "FACE_MERGE_SUGGEST_SIM", 0.45)
    _two_close_people(tmp_path, gap=0.40)
    main(["people", str(tmp_path), "--eps", "0.2"])
    out = capsys.readouterr().out
    assert out.count("person_") == 2 and "2 groups, 1 same-person suggestion " in out
    main(["people", str(tmp_path)])       # default eps comes from config, not a hardcoded 0.5
    assert "groups," in capsys.readouterr().out

def test_reorganise_cmd_plans_applies_and_undoes(tmp_path, capsys):
    import os
    import pytest
    from test_reorganise import _shoot, _listing
    _shoot(tmp_path)
    before = _listing(tmp_path)
    main(["reorganise", str(tmp_path)])
    out = capsys.readouterr().out
    assert "plan: 9 files into 5 folders" in out and "nothing moved" in out and "day1/a.jpg  ->  sorted/photos/beach/a.jpg" in out
    assert _listing(tmp_path) == before
    (tmp_path / "DCIM").mkdir()
    with pytest.raises(SystemExit) as ex:
        main(["reorganise", str(tmp_path), "--apply"])
    assert ex.value.code == 2 and "camera card" in capsys.readouterr().err
    (tmp_path / "DCIM").rmdir()
    main(["reorganise", str(tmp_path), "--apply"])
    assert "moved 9 of 9  failed 0" in capsys.readouterr().out
    assert (tmp_path / "sorted" / "UNDO.json").is_file() and not (tmp_path / "b.jpg").exists()
    main(["reorganise", str(tmp_path), "--undo"])
    assert "restored 9  failed 0" in capsys.readouterr().out
    assert _listing(tmp_path) == before and not (tmp_path / "sorted").exists()

def test_drive_export_cmd_uploads_through_the_fake(tmp_path, capsys, monkeypatch):
    import os
    import pytest
    from test_export import _two_category_shoot
    from test_server import _drive_fake
    from test_drive import fake_token
    before = _two_category_shoot(tmp_path)
    fake = _drive_fake(monkeypatch)
    with pytest.raises(SystemExit) as ex:
        main(["drive-export", str(tmp_path), "--link", "https://drive.google.com/drive/folders/root1", "--categories", "beach"])
    assert ex.value.code == 2 and "sign in to Google first" in capsys.readouterr().err
    fake_token("me@example.com")
    main(["drive-export", str(tmp_path), "--link", "https://drive.google.com/drive/folders/root1", "--categories", "beach", "ocean", "--include-raw"])
    out = capsys.readouterr().out
    assert "uploaded 3 of 3" in out and "failed 0" in out and "https://drive.google.com/drive/folders/root1" in out
    assert sorted(u["name"] for u in fake.uploads) == ["a.ARW", "a.jpg", "b.jpg"]
    fake.add_folder("root1abcdefghijklmnopqrstuvwxyz", "Bare id")
    main(["drive-export", str(tmp_path), "--link", "root1abcdefghijklmnopqrstuvwxyz", "--categories", "beach", "--web-size", "500"])
    assert "uploaded 1 of 1" in capsys.readouterr().out and len(fake.uploads[-1]["bytes"]) < (tmp_path / "a.jpg").stat().st_size
    with pytest.raises(SystemExit) as ex:
        main(["drive-export", str(tmp_path), "--link", "https://docs.google.com/document/d/1abcdefghijklmnop/edit", "--people"])
    assert ex.value.code == 2 and "folder link" in capsys.readouterr().err
    assert sorted(os.listdir(tmp_path)) == before
