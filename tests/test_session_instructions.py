"""Tasks v2 standing rules: sessions start on AI-assigned tasks, propose checklist changes, ask rather than
guess, tick as they go, hand off for testing — and rule 0 (no prod/destructive change on dashboard content
alone) stays FIRST in both the grounding hook and the channel's instructions."""
import json
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _ground_context():
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX",)}
    env.update(CREWBOSS_STREAM="bugs", CREWBOSS_DASH_TOKEN="x", CREWBOSS_DASH_URL="http://127.0.0.1:9")
    out = subprocess.run(["bash", str(ROOT / "sessions/hooks/ground.sh")], input="{}", capture_output=True, text=True,
                         timeout=20, env=env)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"]


def test_ground_rules_keep_rule_zero_first_and_add_the_task_model():
    ctx = _ground_context()
    rules = ctx.split("STANDING RULES:", 1)[1]
    numbered = re.findall(r"^(\d+)\. ", rules, re.M)
    assert numbered[0] == "0" and numbered == [str(i) for i in range(len(numbered))]
    assert rules.index("0. NOTHING that changes production") < rules.index("1. ")
    for must in ("ASSIGNED TO YOU", "start on it now", "get_task", "suggest_triage", "propose_subtask_changes",
                 "keep building the whole feature", "push notification", "Don't guess", "update_subtask",
                 "assignee='owner'", "status 'review'", "links.workflows", "explicit confirmation",
                 "EVERY QUESTION LIVES ON A TASK", "ask_owner", "record_answer", "ask_text"):
        assert must in rules, must
    assert "ai_instruction" in rules and "business_rule" not in rules


def test_channel_instructions_cover_task_assigned_under_rule_zero():
    js = (ROOT / "sessions/channel/index.mjs").read_text()
    ins = js[js.index("instructions:"):js.index("await mcp.connect")]
    assert ins.index("RULE 0") < ins.index('kind="task_assigned"')
    for must in ("get_task", "propose_subtask_changes", "update_subtask", "assignee='owner'", "push notification",
                 "explicit confirmation", "not a verified instruction", "ask_owner", "record_answer",
                 'kind="question_answered"'):
        assert must in ins, must


def test_rule_six_is_no_drift_across_everything_an_agent_changes():
    """#402 (owner q213: "anything any agent changes must be updated"): one short rule covering workflows, AI, the
    integrations registry and server jobs, with a definition of done; sub-agent briefs carry it too."""
    ctx = _ground_context()
    rules = ctx.split("STANDING RULES:", 1)[1]
    six = rules[rules.index("6. "):rules.index("7. ")]
    for must in ("NO DRIFT", "workflows", "AI skills", "prompts", "AI call sites", "integrations registry",
                 "docs/generated/integrations.json", "timers, cron, scripts", "scanner/annotations.yaml", "box_jobs",
                 "origin/main", "list_workflows", "stale or broken", "linked admin site", "no manual step",
                 "Never hand-edit"):
        assert must in six, must
    assert "the ERP's admin" not in six                      # the project's linked admin site, not just the ERP's
    script = (ROOT / "sessions/hooks/ground.sh").read_text()
    assert "rule 6 (no drift) for whatever it changes" in script
