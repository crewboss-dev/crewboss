"""#771: the left menu behaves like the ERP's: the same rules as its chrome.css (verbatim, tests/test_chrome_drift.py) and the same code as its
base.html "THE MENU DRAWER". Static checks here; the browser run is in the task report (Playwright at 1440, 1024 and 390)."""
import re
import subprocess

JS = open("app/static/js/chrome.js").read()


def test_peek_is_a_real_mouse_at_any_width_and_pin_is_wide_only_and_remembered():
    assert 'matchMedia("(min-width: 1280px)")' in JS  and 'matchMedia("(hover: hover)")' not in JS                     # never trust the media query (#703)
    assert re.search(r'function isMouse\(e\) \{ return !!e && e\.pointerType === "mouse"; \}', JS)
    assert re.search(r"function peekIn\(e\) \{ if \(!isMouse\(e\) \|\| pinnedNow\(\)\) return;", JS)        # no WIDE test: any width
    assert "WIDE.matches || !HOVER" not in JS
    assert '"pointerenter", peekIn' in JS and '"pointerleave", peekOut' in JS
    assert "if (WIDE.matches && want === undefined) { window.toggleNavPin(); return; }" in JS        # the button pins when wide, opens when not
    assert "if (want === undefined && peeking)" in JS                                                # a click on a narrow hover-peek keeps it open
    assert 'localStorage.setItem(PIN_KEY' in JS and "crewboss-nav-pinned" in JS and "try {" in JS
    assert "function pinnedNow()" in JS and 'classList.contains("nav-pinned") && WIDE.matches' in JS


def test_the_overlay_closes_on_outside_escape_and_choosing_a_page():
    assert 'document.addEventListener("mousedown", closeIfOutside, true)' in JS and 'document.addEventListener("touchstart", closeIfOutside' in JS
    assert '"#nav-list a"' in JS and 'e.key === "Escape"' in JS


def test_the_pin_is_applied_before_paint_from_localstorage_not_a_cookie(client):
    html = client.get("/").text
    assert "localStorage.getItem('crewboss-nav-pinned') === '1'" in html.split("</head>", 1)[0]
    assert 'class="nav-pinned"' not in html and "crewboss_nav_pinned" not in html and "crewboss_nav_pinned" not in JS


def test_the_pushed_page_and_the_peek_rules_are_the_erps_css(client):
    css = open("app/static/css/chrome.css").read()
    assert "html.nav-pinned .nav-push{ margin-left:var(--nav-w); }" in css
    assert "html.nav-open #nav-drawer{ transform:none; visibility:visible;" in css
    assert 'class="nav-push"' in client.get("/").text


def test_chrome_js_is_valid_javascript():
    assert subprocess.run(["node", "--check", "app/static/js/chrome.js"], capture_output=True).returncode == 0


def test_phone_menu_is_a_bottom_sheet_and_the_keyboard_hides_the_bar(client):
    """Owner 11-15: on a phone the Menu tab raises the drawer from the bottom (grab line, swipe down closes), and while the on-screen
    keyboard is up the bar and the assistant's button step aside and the page shrinks above the keyboard. Browser run: Playwright at 390."""
    shell = open("app/static/css/shell.css").read()
    assert "#nav-drawer{ top:auto; left:0; right:0; bottom:var(--tabbar-h, 0px);" in shell and "transform:translateY(105%)" in shell
    assert "html.kb-open #tabbar, html.kb-open #ca-btn{ display:none !important; }" in shell
    assert "html.kb-open:not(.in-app:not(.shell2)){ --tabbar-h:0px; }" in shell and "bottom:var(--kb-h, 0px)" in shell
    html = client.get("/").text
    assert 'class="m-grab nav-grab" data-sheet-grab' in html and "interactive-widget=resizes-content" in html
    assert 'dragSheet(el("nav-drawer")' in JS and 'root.classList.toggle("kb-open", up)' in JS and "window.visualViewport" in JS


def test_the_favourite_star_changes_on_the_tap_and_rolls_back_on_failure():
    """Owner 11-16: tapping the star removes the favourite instantly; the server is told after, and a refusal puts it back."""
    i = JS.index("var favBusy = {};")
    block = JS[i:i + 1400]
    assert block.index("paintFav(slug, want, name, href);") < block.index('fetch("/all/favourites"')   # painted before the request
    assert "paintFav(slug, !want, name, href)" in block and "Could not change that favourite." in block
