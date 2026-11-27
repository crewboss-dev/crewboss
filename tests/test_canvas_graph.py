"""The workflow canvas's edge/trigger rules (frontend/src/workflow-graph.ts) and its layered layout
(frontend/src/workflow-layout.ts), run as node tests. Node >= 22.18 strips the TypeScript types itself."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")


def _node_major() -> int:
    out = subprocess.run([NODE, "--version"], capture_output=True, text=True)
    return int(out.stdout.strip().lstrip("v").split(".")[0])


needs_node = pytest.mark.skipif(not NODE or _node_major() < 23, reason="needs node >= 23 (type stripping)")


@needs_node
def test_workflow_graph_node_tests():
    out = subprocess.run([NODE, "--test", str(ROOT / "tests/workflow_graph.test.mjs")], capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stdout + out.stderr


@needs_node
def test_workflow_structure_node_tests():
    """Power Automate-style nesting (conditions, loops, rejoins) + its coordinates."""
    out = subprocess.run([NODE, "--test", str(ROOT / "tests/workflow_structure.test.mjs")], capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stdout + out.stderr


@needs_node
@pytest.mark.skipif(not (ROOT / "frontend/node_modules/@dagrejs/dagre").exists(),
                    reason="frontend deps not installed (npm install in frontend)")
def test_workflow_layout_node_tests():
    out = subprocess.run([NODE, "--test", str(ROOT / "tests/workflow_layout.test.mjs")], capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stdout + out.stderr


@needs_node
def test_canvas_model_node_tests():
    """The shared canvas's integration rules: step → integrations, systems, the diagram's geometry (#393)."""
    out = subprocess.run([NODE, "--test", str(ROOT / "tests/canvas_model.test.mjs")], capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stdout + out.stderr


def test_canvas_has_no_watermark_layer():
    """Dot grid only: the canvas renders no text layer behind the cards (both kinds of the shared canvas)."""
    for name in ("workflow-view.tsx", "integrations-view.tsx"):
        tsx = (ROOT / "frontend/src/canvas" / name).read_text()
        assert "BackgroundVariant.Dots" in tsx
        assert "<Panel" not in tsx
        assert "<MiniMap" in tsx and "<Controls" in tsx  # infinite canvas: minimap + zoom/fit controls
    css = (ROOT / "frontend/src/canvas/canvas.css").read_text()
    assert ".react-flow__attribution" in css


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_prefill_node_tests():
    out = subprocess.run([NODE, "--test", str(ROOT / "tests/prefill.test.mjs")], capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0, out.stdout + out.stderr


def test_prefill_script_writes_no_html():
    js = (ROOT / "app/static/js/prefill.js").read_text()
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js and "outerHTML" not in js
    assert "document.write" not in js and "eval(" not in js
