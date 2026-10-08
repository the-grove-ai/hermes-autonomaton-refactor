"""The portal's left navigation is declared, not hard-coded: groups, plain
labels and existing targets from portal.nav.yaml; two live counts; a flag that
hides scheduled skills; and a landing page. Navigation and labels only."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from grove.api import portal_nav as nav_mod

REPO = Path(__file__).resolve().parents[3]
GOALS = [{"id": "gl-invoice-coding", "name": "Invoice GL coding"}]
LIVE = {"to_sign": 2, "suggestions": 15}


def _text(html: str) -> list:
    return [re.sub(r"<[^>]+>", "", part).strip()
            for part in re.findall(r"<li[^>]*>.*?</li>", html, re.S)
            if re.sub(r"<[^>]+>", "", part).strip()]


def test_the_shipped_nav_reads_as_the_story_in_plain_words():
    nav = nav_mod.load_nav(REPO / "config" / "portal.nav.yaml")
    html = nav_mod.render_nav(nav, goals=GOALS, skills=[], live=LIVE)
    assert _text(html) == [
        # 2026-10-08: the dock first (its work, what to sign, what it returned
        # and the record behind it), then how the node is set up.
        "Dock", "Invoice GL coding", "To sign2",
        "Scorecard", "Reasoning trace", "Audit check",
        "Admin", "Models", "Connected tools", "Skills", "Knowledge", "Memory",
        "Suggestions15",
        "Advanced", "Tool permissions", "System",
    ]
    # None of the internal words a first-time reader would trip on.
    for word in ("Fleet", "Observers", "Admission", "Composition", "substrate", "Dashboard",
                 "forge-jobsearch", "scout-jobsearch", "researcher"):
        assert word not in html, word
    # The demo's pages are one click from the top level, at their existing addresses.
    for target in ("fragments/goal/gl-invoice-coding", "fragments/audit/", "fragments/trace/",
                   "fragments/proposals/pending?type=signature"):
        assert f'href="/portal#{target}"' in html, target
    # The portal opens on the goal's own page, always.
    assert 'data-landing="fragments/goal/gl-invoice-coding"' in html


def test_what_needs_a_signature_is_counted_apart_from_memory_suggestions():
    nav = nav_mod.load_nav(REPO / "config" / "portal.nav.yaml")
    html = nav_mod.render_nav(nav, goals=GOALS, skills=[], live={"to_sign": 1, "suggestions": 9})
    sign = re.search(r'type=signature">To sign<span class="nav-badge hot">(\d+)</span>', html)
    suggestions = re.search(r'type=memory">Suggestions<span class="nav-badge hot">(\d+)</span>', html)
    assert (sign.group(1), suggestions.group(1)) == ("1", "9")


def test_scheduled_skills_appear_only_behind_their_flag_and_never_the_hidden_ones(tmp_path):
    data = yaml.safe_load((REPO / "config" / "portal.nav.yaml").read_text())
    assert data["flags"] == {"scheduled_skills": False}          # off by default
    off = nav_mod.load_nav(REPO / "config" / "portal.nav.yaml")
    listed = ["cultivator", "drafter", "researcher", "scout"]
    assert "On a schedule" not in nav_mod.render_nav(off, goals=GOALS, skills=listed, live=LIVE)
    data["flags"]["scheduled_skills"] = True
    path = tmp_path / "portal.nav.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    on = nav_mod.load_nav(path)
    html = nav_mod.render_nav(on, goals=GOALS, skills=listed, live=LIVE)
    text = _text(html)
    start = text.index("On a schedule")
    assert text[start:start + 5] == ["On a schedule", "cultivator", "drafter", "researcher", "scout"]
    assert 'href="/portal#fragments/fleet/researcher/"' in html
    # The hidden list is applied before the nav ever sees a name.
    assert on["hidden_skills"] == ["forge-jobsearch", "scout-jobsearch"]


def test_the_hidden_skills_are_filtered_out_of_the_live_list(monkeypatch):
    from grove.api import fragments

    monkeypatch.setattr(fragments, "_fleet_index_rows", lambda: [
        {"name": n} for n in ("cultivator", "forge-jobsearch", "researcher", "scout-jobsearch")])
    assert nav_mod.scheduled_skills(["forge-jobsearch", "scout-jobsearch"]) == [
        "cultivator", "researcher"]


def test_the_landing_page_is_the_first_goal_with_work_or_a_declared_page():
    nav = nav_mod.load_nav(REPO / "config" / "portal.nav.yaml")
    assert nav_mod.landing_target(nav, GOALS) == "fragments/goal/gl-invoice-coding"
    assert nav_mod.landing_target(nav, []) == "fragments/dock/goals"
    assert nav_mod.landing_target({**nav, "landing": "fragments/audit/"}, GOALS) == "fragments/audit/"


def test_an_operator_file_replaces_the_default_whole(tmp_path, monkeypatch):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    assert nav_mod.load_nav()["source"].endswith("config/portal.nav.yaml")
    (tmp_path / "portal.nav.yaml").write_text(
        "groups:\n  - {label: Only this, target: fragments/dock/goals}\n")
    mine = nav_mod.load_nav()
    assert mine["source"] == str(tmp_path / "portal.nav.yaml")
    assert _text(nav_mod.render_nav(mine, goals=[], skills=[], live={})) == ["Only this"]


@pytest.mark.parametrize("body,why", [
    ("groups: nope", "needs a 'groups' list"),
    ("groups:\n  - {label: A, target: /etc/passwd}", "begins 'fragments/'"),
    ("groups:\n  - {label: A}", "needs a target"),
    ("groups:\n  - {target: fragments/x}", "needs a label"),
    ("groups:\n  - {label: A, target: fragments/x, badge: total}", "names badge"),
    ("groups:\n  - {label: A, target: fragments/x, flag: ghosts}", "not declared"),
    ("groups:\n  - {label: A, target: fragments/x, colour: red}", "unknown keys"),
    ("flags: {a: maybe}\ngroups: []", "true or false"),
    ("landing: https://example.com\ngroups: []", "landing must be"),
])
def test_a_nav_file_that_cannot_be_read_is_refused(tmp_path, body, why):
    path = tmp_path / "portal.nav.yaml"
    path.write_text(body)
    with pytest.raises(ValueError, match=why):
        nav_mod.load_nav(path)


def test_the_shell_loads_the_declared_nav_and_speaks_plainly():
    shell = (REPO / "gateway" / "assets" / "portal" / "index.html").read_text()
    assert 'hx-get="/portal/fragments/nav/main"' in shell
    assert "Search knowledge, memory and goals…" in shell
    assert "substrate" not in shell.split("<script")[0].split("</style>")[-1]
    assert "Knowledge Browser</h1>" not in shell and "read-only here" not in shell
    # Every target in the shipped nav is a route the portal already serves.
    src = "".join(path.read_text() for path in sorted((REPO / "grove" / "api").glob("*.py")))
    nav = nav_mod.load_nav(REPO / "config" / "portal.nav.yaml")

    def targets(items):
        for entry in items:
            if "items" in entry:
                yield from targets(entry["items"])
            elif entry.get("target"):
                yield entry["target"]

    for target in targets(nav["groups"]):
        # The page family each target belongs to is one the portal serves.
        family = "/portal/fragments/" + target.split("?")[0].split("/")[1]
        assert family in src, family
