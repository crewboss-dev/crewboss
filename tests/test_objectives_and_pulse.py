from app import pulse, repo


def test_create_and_list_objective(project):
    repo.create_knowledge(project["id"], "objective", "Grow monthly invoicing to $40k", "context",
                           [], "", False, "owner", target_value=40000, target_period="monthly",
                           summary="Monthly invoicing is the income measure.")
    objectives = repo.list_objectives(project["id"])
    assert len(objectives) == 1
    assert objectives[0]["target_value"] == 40000
    assert objectives[0]["target_period"] == "monthly"


def test_objectives_excluded_from_other_kinds(project):
    repo.create_knowledge(project["id"], "objective", "An objective", "", [], "", False, "owner", summary="s")
    repo.create_knowledge(project["id"], "decision", "A decision", "", [], "", False, "owner", summary="s")
    assert len(repo.list_knowledge(project["id"], kind="decision")) == 1
    assert len(repo.list_objectives(project["id"])) == 1


def test_pulse_is_none_stub():
    assert pulse.get_pulse() is None


def test_ground_includes_objectives_and_pulse(project):
    repo.create_knowledge(project["id"], "objective", "Ship more, bill more", "", [], "", False,
                           "owner", target_value=10, target_period="weekly", summary="s")
    g = repo.build_ground("bugs", project["slug"])
    assert any(o["title"] == "Ship more, bill more" for o in g["objectives"])
    assert g["pulse"] is None
    text = repo.render_ground_text(g)
    assert "Ship more, bill more" in text
    assert "phase 3" in text


def test_dashboard_kpis_include_pulse_stub(project):
    kpis = repo.dashboard_kpis(project["id"])
    assert kpis["pulse"] is None
