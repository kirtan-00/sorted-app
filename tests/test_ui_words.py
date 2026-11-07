"""The main window's words and its shape: the plain labels, the rail in order, the filters in one fold, none of the
design tells. Static reads of the page; the behaviour behind them is clicked through by hand with headless Chromium."""
import re
from pathlib import Path

UI = Path(__file__).resolve().parents[1] / "photosort" / "ui"
HTML = (UI / "index.html").read_text("utf-8")
JS = (UI / "app.js").read_text("utf-8")
CSS = (UI / "style.css").read_text("utf-8")


def _text(html):
    """What a reader sees: tags, comments and scripts out, attribute values with them."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"<!--.*?-->", " ", html, flags=re.S)))


def test_old_labels_are_gone_from_the_page():
    text = _text(HTML)
    for word in ("the pile", "Project file", "Open project", "Save project", "Sharp ", "Hide soft", "Hide out of focus", "Framing",
                 "Sequences", "Log, needs grading", "Rec.709 and others", "Light flicker", "Pulsing", "Banding", "Neither",
                 "Show selected", "Select all", "Export picks", "Write XMP", "Links (need the disk plugged in)", "Client web set (2048 px JPEG)", "Editor selects (links)",
                 "Tightness", "Categorise", "Discovered in this shoot", "Less sure", "Reorganise disk", "Inspector"):
        assert word not in text, word


def test_new_labels_are_on_the_page():
    text = _text(HTML)
    for word in ("Sharpest only", "Hide blurry", "Shot size", "Bursts and brackets", "Flat, needs grading", "Ready to use", "Flickering",
                 "Bands", "No flicker", "Not rated", "Save ratings for Lightroom", "Shortcuts to the originals", "For the client (web size)",
                 "For the editor", "Grouping strictness", "How close a match", "Sort into categories", "Also found",
                 "Include possible matches", "Move files into folders", "Shoot file", "More filters", "Finish this shoot:", "Pick all shown",
                 "Pick all matching", "Show picked", "Add to Picked",
                 "P pick, X reject, 1 to 5 stars"):
        assert word in text, word


def test_api_values_kept_their_names():
    for hook in ('value="pulsing"', 'value="banding"', 'value="none"', 'value="symlink"', 'name="hide_bad"', 'name="hide_soft"',
                 'id="pile-filter"', 'data-view="index"'):
        assert hook in HTML, hook


def test_the_rail_runs_shoot_first_and_the_keys_follow_it():
    rail = re.findall(r'<button type="button" data-view="(\w+)"', HTML)
    assert rail == ["index", "search", "people", "categories", "places", "products"]
    views = re.search(r"var VIEWS = \[(.*?)\]", JS).group(1)
    assert re.findall(r'"(\w+)"', views) == rail
    assert HTML.index('id="filters"') < HTML.index('id="nav-live"')    # Live is the rail's last slot, past the filters


def test_folded_filters_sit_inside_the_fold_and_the_rest_above_it():
    fold = HTML.index('id="more-filters"')
    for group in ("shot-group", "dur-group", "look-group", "flicker-group", "camera-group", "grp-group", "exif-group"):
        assert HTML.index(f'id="{group}"') > fold, group
    for top in ("marks-filter", "days-list", "kind-select", "focus-row"):
        assert HTML.index(f'id="{top}"') < fold, top


def test_no_design_tells():
    assert "text-transform" not in CSS
    assert not re.search(r'join\("\s*·\s*"\)', JS)
    assert '"badge mono"' not in JS and '"grid-divider mono"' not in JS
    assert "→" not in JS
    assert "—" not in _text(HTML)


def test_one_list_in_the_words_and_no_second_way_out():
    assert '"Nothing picked yet"' in JS and '" picked"' in JS and '"Show picked"' in JS
    assert "Nothing selected yet" not in JS and '" selected"' not in JS and "Show selected" not in JS
    assert "marks-export" not in HTML + JS
    assert 'name="marks-flag" value="pick"' in HTML                    # the Ratings segment, which is the same filter
    assert "state.selected.clear" not in JS and "s.clear" not in JS    # nothing but the Clear button empties the list
    assert 'id="status-undo-link"' in HTML
    assert "the pile" not in _text(HTML).lower() and "selected" not in _text(HTML).lower()


def test_the_keys_start_on_the_grid_and_the_ratings_button_is_there_once():
    assert "autofocus" not in re.search(r'<input type="search"[^>]*>', HTML).group(0)
    assert _text(HTML).count("Save ratings for Lightroom") == 2         # the button, and its line in How it works
    assert 'id="xmp-sel"' not in HTML and "xmp-sel" not in JS


def test_the_tiles_and_the_full_size_view_say_what_they_are():
    assert 'id="grid" class="grid" role="listbox"' in HTML and '"role", "option"' in JS and 'tileLabel(r)' in JS
    assert 'id="lightbox" role="dialog" aria-modal="true"' in HTML and "trapTab" in JS
    assert 'src="/ui/loupe.js"' in HTML and HTML.index("/ui/loupe.js") < HTML.index("/ui/app.js")
