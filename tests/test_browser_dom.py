"""Opt-in integration coverage using real Chromium and synthetic, local DOM."""

import os

import pytest
from playwright.sync_api import sync_playwright

from robin.capabilities.browser import Browser, PlaywrightPage, _parse_refs
from robin.loop import converse
from robin.model import ModelTurn, ToolCall
from robin.ner import UnavailableNer
from robin.policy import Task
from robin.session import Assistant

pytestmark = pytest.mark.skipif(
    os.environ.get("ROBIN_BROWSER_TESTS") != "1",
    reason="set ROBIN_BROWSER_TESTS=1 to run real Chromium fixtures",
)


@pytest.fixture
def page():
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        context = browser.new_context()
        raw = context.new_page()
        yield raw
        context.close()
        browser.close()


def ref(snapshot, label):
    found = [key for key, (_, name) in _parse_refs(snapshot).items() if name == label]
    assert len(found) == 1, (label, snapshot)
    return found[0]


def test_discovery_content_budget_and_stable_refs(page):
    navigation = "".join(f'<a href="#n{i}">Navigation {i}</a><br>' for i in range(130))
    page.set_content(
        navigation + '<main><fieldset><legend>Year</legend><label>From<input type="number"></label>'
        '</fieldset><p>Results are available</p></main>'
    )
    operator = PlaywrightPage(page)
    snapshot, _ = operator.read()
    assert 'spinbutton "Year: From"' in snapshot
    assert "Results are available" in snapshot.split("Content:")[1]
    assert len(snapshot) < 12000
    first = ref(snapshot, "Year: From")
    assert "Controls omitted:" in snapshot
    later, _ = operator.read(cursor=80)
    assert 'Navigation 100' in later
    focused, _ = operator.read(query="Year")
    assert ref(focused, "Year: From") == first
    operator.type_text("Year: From", "2027", ref=first)
    assert page.locator("input").input_value() == "2027"
    page.locator("input").evaluate("(node) => node.replaceWith(node.cloneNode())")
    with pytest.raises(RuntimeError, match="stale"):
        operator.type_text("Year: From", "2026", ref=first)
    fresh, _ = operator.read(query="Year")
    assert ref(fresh, "Year: From") != first
    assert "2027" in fresh
    # New documents must not recycle refs from the old page.
    page.set_content('<label>From<input type="number"></label>')
    renewed, _ = operator.read()
    assert ref(renewed, "From") != first


def test_result_summary_is_not_hidden_behind_dense_filter_labels(page):
    page.set_content(
        '<main style="display:flex"><aside style="width:50%">'
        + "".join(f'<label>Filter {i}<input type="checkbox"></label>' for i in range(200))
        + '</aside><section><h1>Car search</h1><div>18 matches</div>'
        '<p>Visible result details</p></section></main>'
    )
    snapshot, _ = PlaywrightPage(page).read()
    content = snapshot.split("Content:", 1)[1]
    assert "18 matches" in content and "Visible result details" in content


def test_native_custom_dropdowns_and_idempotent_checks(page):
    page.set_content('''
      <div style="position:relative">
        <input id="brand" type="checkbox" style="position:absolute;width:1px;height:1px">
        <label for="brand" style="position:relative;background:white">Brand</label>
      </div>
      <label>Sort<select><option value="desc">Highest price</option>
        <option value="asc">Lowest price</option></select></label>
      <button role="combobox" aria-label="Model" aria-controls="models"
        onclick="document.getElementById('models').hidden=false">Choose model</button>
      <div role="listbox" id="models" hidden>
        <button role="option" onclick="document.querySelector('[role=combobox]').textContent='GLC';this.parentNode.hidden=true">GLC</button>
      </div>
    ''')
    operator = PlaywrightPage(page)
    snapshot, _ = operator.read()
    brand = ref(snapshot, "Brand")
    operator.set_checked("Brand", True, ref=brand)
    operator.set_checked("Brand", True, ref=brand)
    assert page.locator("input").is_checked()
    operator.select_option("Sort", "Lowest price", ref=ref(snapshot, "Sort"))
    assert page.locator("select").input_value() == "asc"
    operator.select_option("Model", "GLC", ref=ref(snapshot, "Model"))
    assert page.get_by_role("combobox", name="Model").inner_text() == "GLC"
    with pytest.raises(RuntimeError, match="stale"):
        operator.select_option("Sort", "Highest price", ref="99999")
    assert page.locator("select").input_value() == "asc"


def test_same_origin_frame_refs_keep_element_identity(page):
    page.set_content('<iframe srcdoc="&lt;label&gt;Year&lt;input type=number&gt;&lt;/label&gt;"></iframe>')
    page.frame_locator("iframe").get_by_role("spinbutton").wait_for()
    operator = PlaywrightPage(page)
    snapshot, _ = operator.read(query="Year")
    target = ref(snapshot, "Year")
    operator.type_text("Year", "2027", ref=target)
    inner = page.frames[1].locator("input")
    assert inner.input_value() == "2027"
    inner.evaluate("(node) => node.replaceWith(node.cloneNode())")
    with pytest.raises(RuntimeError, match="stale"):
        operator.type_text("Year", "2026", ref=target)


def test_custom_dropdown_and_scroll_use_the_owning_frame(page):
    page.set_content("<iframe></iframe>")
    inner = page.frames[1]
    inner.set_content('''
      <button role="combobox" aria-label="Model" aria-controls="models"
        onclick="document.querySelector('#models').hidden=false">Choose</button>
      <div role="listbox" id="models" hidden><button role="option"
        onclick="document.querySelector('[role=combobox]').textContent='GLC';this.parentNode.hidden=true">GLC</button></div>
      <div role="region" aria-label="Filters" tabindex="0" style="height:50px;overflow:auto">
        <div style="height:2000px">Long filter panel</div>
      </div>
    ''')
    operator = PlaywrightPage(page)
    snapshot, _ = operator.read()
    operator.select_option("Model", "GLC", ref=ref(snapshot, "Model"))
    assert inner.get_by_role("combobox").inner_text() == "GLC"
    movement = operator.scroll("down", ref=ref(snapshot, "Filters"))
    assert movement["after"] > movement["before"]


def test_nested_scroll_and_end_of_region(page):
    page.set_content(
        '<div role="region" aria-label="Filters" tabindex="0" style="height:100px;overflow:auto">'
        + "".join(f'<p>Filter {i}</p>' for i in range(100)) + "</div>"
    )
    operator = PlaywrightPage(page)
    snapshot, _ = operator.read()
    target = ref(snapshot, "Filters")
    movement = operator.scroll("down", ref=target)
    assert movement["after"] > movement["before"]
    assert page.get_by_role("region").evaluate("(node) => node.scrollTop") > 0
    with pytest.raises(RuntimeError, match="scroll container"):
        operator.scroll("down", ref="99999")
    with pytest.raises(ValueError, match="direction"):
        operator.scroll("sideways")
    operator.scroll("bottom", ref=target)
    movement = operator.scroll("down", ref=target)
    assert movement["after"] == movement["before"] == movement["maximum"]
    page.get_by_role("region").evaluate("(node) => node.replaceWith(node.cloneNode(true))")
    with pytest.raises(RuntimeError, match="scroll container"):
        operator.scroll("up", ref=target)


def test_discovery_beyond_traversal_limit_and_shadow_range_context(page):
    page.set_content(
        "".join(f'<a href="#n{i}">Navigation {i}</a>' for i in range(2010))
        + '<main><div id="range" aria-label="Model year"></div></main>'
        + '''<script>document.querySelector('#range').attachShadow({mode:'open'}).innerHTML =
          '<label>Min<input type="number" min="1900" max="2030"></label>';</script>'''
    )
    operator = PlaywrightPage(page)
    snapshot, _ = operator.read(query="Model year")
    target = ref(snapshot, "Model year: Min")
    operator.type_text("Min", "2027", ref=target)
    assert page.locator("input").input_value() == "2027"
    with pytest.raises(RuntimeError, match="allowed range"):
        operator.type_text("Min", "2031", ref=target)


def test_delayed_same_length_results_and_busy_timeout(page):
    page.set_content('''
      <label>Year<input type="number" oninput="
        setTimeout(()=>document.querySelector('p').textContent='Applied 2027',700)"></label>
      <p>Pending 2027</p>
    ''')
    operator = PlaywrightPage(page)
    snapshot, _ = operator.read()
    operator.type_text("Year", "2027", ref=ref(snapshot, "Year"))
    operator.settle()
    snapshot, _ = operator.read()
    assert "Applied 2027" in snapshot
    page.locator("p").evaluate("(node) => node.setAttribute('aria-busy','true')")
    status = operator.settle(timeout_ms=500)
    assert status.get("still_updating") is True
    snapshot, _ = operator.read()
    assert "Still updating" in snapshot
    assert "2027" in snapshot


def test_ambiguous_controls_options_and_obstruction_do_not_choose_first(page):
    page.set_content('''
      <label>Minimum<input></label><label>Minimum<input></label>
      <label>Sort<select><option>Price</option><option>Price</option></select></label>
      <button onclick="window.clicked=true">Apply</button>
      <div style="position:fixed;inset:0;z-index:10;background:white"></div>
    ''')
    operator = PlaywrightPage(page)
    with pytest.raises(RuntimeError, match="matches 2"):
        operator.type_text("Minimum", "2027")
    assert page.locator("input").evaluate_all("nodes => nodes.every(node => !node.value)")
    snapshot, _ = operator.read()
    with pytest.raises(RuntimeError, match="missing or ambiguous"):
        operator.select_option("Sort", "Price", ref=ref(snapshot, "Sort"))
    with pytest.raises(RuntimeError, match="could not be clicked"):
        operator.click("Apply", ref=ref(snapshot, "Apply"))
    assert page.evaluate("window.clicked") is None


class ReadyNer(UnavailableNer):
    def available(self):
        return True


def test_private_group_and_options_round_trip_through_agent(page):
    email = "jane@example.test"
    person = "Jane Doe"
    page.set_content(
        f'<fieldset><legend>{person}</legend><label>Recipient<select><option>None</option>'
        f'<option>{email}</option></select></label></fieldset>'
    )
    assistant = Assistant(ner=ReadyNer())
    assistant.add(Browser("ada", PlaywrightPage(page)))
    vault = assistant.vaults.get("ada", "private")
    reference = vault.token("EMAIL", email)
    vault.token("PERSON", person)
    steps = 0

    class Model:
        def complete(self, *, messages, tools):
            nonlocal steps
            steps += 1
            if steps == 1:
                return ModelTurn("", (ToolCall("browser_read", {}),))
            observations = [message["content"] for message in messages if message["role"] == "tool"]
            assert email not in str(messages) and person not in str(messages)
            assert reference in str(observations)
            if steps == 2:
                controls = _parse_refs(observations[-1])
                target = next(key for key, (role, name) in controls.items() if role == "combobox")
                return ModelTurn("", (ToolCall("browser_select", {"target": target, "value": reference}),))
            return ModelTurn("Selection verified.")

    reply = converse(assistant, Task("ada", "private", "Select the saved recipient"), Model())
    assert reply.text == "Selection verified."
    assert page.locator("select").input_value() == email


@pytest.mark.parametrize("run", range(5))
def test_car_filters_and_sort_through_agent_airlock(page, run):
    page.set_content('''
      <main>
      <label>Mercedes-Benz<input type="checkbox" id="brand"></label>
      <label>Model<select id="model"><option>Any</option><option>GLC</option></select></label>
      <fieldset><legend>Model year</legend>
        <label>From<input id="from" type="number"></label>
        <label>To<input id="to" type="number"></label>
      </fieldset>
      <label>Sort<select id="sort"><option>Highest price</option><option>Lowest price</option></select></label>
      <p id="results">Filters not applied</p>
      </main>
      <script>
      document.querySelectorAll('input,select').forEach(node => node.addEventListener('change', () => {
        document.querySelector('main').setAttribute('aria-busy', 'true');
        setTimeout(() => {
          const applied = document.querySelector('#brand').checked
            && document.querySelector('#model').value === 'GLC'
            && document.querySelector('#from').value === '2027'
            && document.querySelector('#to').value === '2027';
          document.querySelector('#results').textContent = applied
            ? (document.querySelector('#sort').value === 'Lowest price'
              ? '2 matches: GLC 2027 - 100000; GLC 2027 - 200000'
              : '2 matches: GLC 2027 - 200000; GLC 2027 - 100000')
            : 'Filters not applied';
          document.querySelector('main').setAttribute('aria-busy', 'false');
        }, 100);
      }));
      </script>
    ''')
    assistant = Assistant(ner=ReadyNer())
    assistant.add(Browser("ada", PlaywrightPage(page)))
    steps = [
        ("browser_read", None, {}),
        ("browser_set_checked", "Mercedes-Benz", {"checked": True}),
        ("browser_select", "Model", {"value": "GLC"}),
        ("browser_type", "Model year: From", {"text": "2027"}),
        ("browser_type", "Model year: To", {"text": "2027"}),
        ("browser_select", "Sort", {"value": "Lowest price"}),
    ]

    class Model:
        def complete(self, *, messages, tools):
            if steps:
                name, label, arguments = steps.pop(0)
                if label:
                    arguments["target"] = ref(messages[-1]["content"], label)
                return ModelTurn("", (ToolCall(name, arguments),))
            result = messages[-1]["content"]
            assert "value=2027" in result
            assert "value=Lowest price" in result
            assert "100000" in result and result.index("100000") < result.index("200000")
            return ModelTurn("Verified GLC, exact year 2027, lowest price first.")

    reply = converse(assistant, Task("ada", f"test-{run}", "Find GLC 2027 cars sorted by price"), Model())
    assert reply.text.startswith("Verified GLC")
    assert page.locator("#brand").is_checked()
    assert page.locator("#from").input_value() == page.locator("#to").input_value() == "2027"


def test_two_unchanged_scrolls_prevent_third_execution(page):
    page.set_content("<main><p>Search entry point not reached</p></main>")
    operator = PlaywrightPage(page)
    assistant = Assistant(ner=ReadyNer())
    capability = Browser("ada", operator)
    assistant.add(capability)
    calls = []
    original = operator.scroll

    def scroll(*args, **kwargs):
        calls.append(args)
        original(*args, **kwargs)

    operator.scroll = scroll

    class Model:
        def complete(self, *, messages, tools):
            observations = [message for message in messages if message["role"] == "tool"]
            if not observations:
                return ModelTurn("", (ToolCall("browser_read", {}),))
            if "Action blocked" in observations[-1]["content"]:
                return ModelTurn("I cannot verify the filters yet.")
            return ModelTurn("", (ToolCall("browser_scroll", {"direction": "down"}),))

    reply = converse(assistant, Task("ada", "scroll", "Find filters"), Model())
    assert len(calls) == 2
    assert "cannot verify" in reply.text


def test_recovery_resets_stagnation_only_after_observed_control_change(page):
    page.set_content('<label>Filter<input type="checkbox"></label><p>No matches verified yet</p>')
    operator = PlaywrightPage(page)
    assistant = Assistant(ner=ReadyNer())
    assistant.add(Browser("ada", operator))
    calls = []
    original = operator.scroll

    def scroll(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    operator.scroll = scroll
    steps = 0

    class Model:
        def complete(self, *, messages, tools):
            nonlocal steps
            steps += 1
            observations = [message["content"] for message in messages if message["role"] == "tool"]
            if steps == 1:
                return ModelTurn("", (ToolCall("browser_read", {}),))
            if steps in {2, 3, 4, 6}:
                return ModelTurn("", (ToolCall("browser_scroll", {"direction": "down"}),))
            if steps == 5:
                return ModelTurn("", (ToolCall("browser_find", {"query": "Filter"}),))
            if steps == 7:
                assert "Action blocked" in observations[-1]
                # The blocked call preserved the last refs locally.
                target = ref(observations[-2], "Filter")
                return ModelTurn("", (ToolCall("browser_set_checked", {"target": target, "checked": True}),))
            if steps == 8:
                return ModelTurn("", (ToolCall("browser_scroll", {"direction": "down"}),))
            return ModelTurn("Recovery changed the filter, but results are not verified.")

    reply = converse(assistant, Task("ada", "recover", "Apply filter and find results"), Model())
    assert len(calls) == 3
    assert page.locator("input").is_checked()
    assert "not verified" in reply.text


def test_listing_cards_appear_as_content_lines(page):
    page.set_content(
        '<main><h1>Dairy</h1>'
        '<a href="/milk"><div><span>Helmelk 1L</span><span>18 kr</span></div></a>'
        '<a href="/bread"><div><span>Kneipp</span><span>22 kr</span></div></a>'
        '</main>'
    )
    snapshot, _ = PlaywrightPage(page).read()
    content = snapshot.split("Content:", 1)[1]
    assert "Helmelk 1L" in content and "18 kr" in content
    assert "Kneipp" in content


def test_cross_origin_frame_emits_unread_control(page):
    page.set_content(
        '<main><h1>Times</h1>'
        '<iframe title="Timetable" src="https://widget.entur.example/"></iframe>'
        '</main>'
    )
    snapshot, _ = PlaywrightPage(page).read()
    assert 'frame "Timetable"' in snapshot or 'frame "widget.entur.example"' in snapshot
    assert "unread" in snapshot
    assert "text not readable" in snapshot
    from robin.capabilities.browser import _bot_wall_note
    assert _bot_wall_note(snapshot) == ""


def test_filter_budget_keeps_main_listing_links(page):
    filters = "".join(f'<label>Filter {i}<input type="checkbox"></label>' for i in range(90))
    products = "".join(f'<a href="/p{i}">Helmelk {i}L 18 kr</a><br>' for i in range(12))
    page.set_content(f'<aside>{filters}</aside><main><h1>Shop</h1>{products}</main>')
    snapshot, _ = PlaywrightPage(page).read()
    assert "Helmelk 1L" in snapshot
    assert '[1]' in snapshot


def test_non_blocking_dialog_keeps_main_content(page):
    page.set_content(
        '<main><h1>Shop</h1><a href="/milk"><div>Helmelk 1L 18 kr</div></a></main>'
        '<div role="dialog" aria-label="Get the app" style="position:fixed;right:8px;bottom:8px;'
        'width:160px;height:80px;background:white">'
        '<p>Get the app</p><button>Close</button></div>'
    )
    snapshot, _ = PlaywrightPage(page).read()
    assert "Helmelk 1L" in snapshot.split("Content:", 1)[1]
    assert "Dismissible dialog" in snapshot or "Get the app" in snapshot


def test_overlay_click_reports_blocker_and_keeps_ref(page):
    page.set_content(
        '<button id="buy">Legg i handlekurv</button>'
        '<div id="wall" style="position:fixed;inset:0;z-index:10;background:white">Cookie</div>'
    )
    operator = PlaywrightPage(page)
    snapshot, _ = operator.read()
    target = ref(snapshot, "Legg i handlekurv")
    with pytest.raises(RuntimeError, match="intercepts pointer") as raised:
        operator.click("Legg i handlekurv", ref=target)
    assert f"[{target}]" in str(raised.value)
    assert "still current" in str(raised.value)
    assert page.evaluate("window.clicked") is None
