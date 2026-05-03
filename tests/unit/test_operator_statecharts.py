from __future__ import annotations

import html
import re
import subprocess
import sys
from importlib import util
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from contracts.operator import copy as operator_copy

ROOT = Path(__file__).resolve().parents[2]
FEATURES_DIR = ROOT / "tests" / "acceptance" / "features"
STATECHARTS_CONTRACT = ROOT / "contracts" / "operator" / "statecharts.yaml"
STATECHARTS_SCHEMA = ROOT / "contracts" / "operator" / "statecharts.schema.json"
MERMAID_GENERATOR = ROOT / "scripts" / "fsm_to_mermaid.py"
MERMAID_PUPPETEER_CONFIG = ROOT / "scripts" / "mermaid-puppeteer-config.json"
MERMAID_CLI_PACKAGE = "@mermaid-js/mermaid-cli@8.14.0"
MERMAID_RENDER_ERROR = re.compile(
    r"Syntax error|Parse error|Lexical error|Unsupported markdown",
    flags=re.IGNORECASE,
)


def _contract() -> dict[str, Any]:
    contract = yaml.safe_load(STATECHARTS_CONTRACT.read_text(encoding="utf-8"))
    assert isinstance(contract, dict)
    assert contract["version"] == 1
    return contract


def _statecharts() -> dict[str, dict[str, Any]]:
    contract = _contract()
    statecharts = contract["statecharts"]
    assert isinstance(statecharts, dict)
    return statecharts


def _schema() -> dict[str, Any]:
    schema = yaml.safe_load(STATECHARTS_SCHEMA.read_text(encoding="utf-8"))
    assert isinstance(schema, dict)
    return schema


def _handoffs() -> list[dict[str, Any]]:
    handoffs = _contract()["handoffs"]
    assert isinstance(handoffs, list)
    return handoffs


def _endpoint(handoff: dict[str, Any], key: str) -> tuple[str, str]:
    endpoint = handoff[key]
    assert isinstance(endpoint, dict)
    return str(endpoint["statechart"]), str(endpoint["state"])


def _operator_copy_references() -> set[str]:
    reference_pattern = re.compile(r'operator (?:notification )?copy "([^"]+)"')
    references: set[str] = set()
    for path in FEATURES_DIR.glob("*.feature"):
        references.update(reference_pattern.findall(path.read_text(encoding="utf-8")))
    return references


def _statechart_references() -> set[tuple[str, str]]:
    reference_pattern = re.compile(
        r'statechart "([^"]+)" state "([^"]+)" is the accepted operator contract'
    )
    references: set[tuple[str, str]] = set()
    for path in FEATURES_DIR.glob("*.feature"):
        references.update(reference_pattern.findall(path.read_text(encoding="utf-8")))
    return references


def _scenario_blocks() -> list[tuple[Path, str]]:
    blocks: list[tuple[Path, str]] = []
    for path in FEATURES_DIR.glob("*.feature"):
        text = path.read_text(encoding="utf-8")
        matches = list(re.finditer(r"(?m)^\s*Scenario:", text))
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            blocks.append((path, text[match.start() : end]))
    return blocks


def _view_references(statecharts: dict[str, dict[str, Any]]) -> set[str]:
    views: set[str] = set()
    for statechart in statecharts.values():
        states = statechart["states"]
        assert isinstance(states, dict)
        for state in states.values():
            assert isinstance(state, dict)
            view = state.get("view")
            if view:
                views.add(str(view))
    return views


def _mermaid_generator_module() -> Any:
    spec = util.spec_from_file_location("fsm_to_mermaid", MERMAID_GENERATOR)
    assert spec is not None
    assert spec.loader is not None
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _mermaid_display_text(value: str) -> str:
    return "<br/>".join(
        html.escape(line, quote=False).replace('"', "'") for line in value.splitlines()
    )


def _combined_mermaid_markdown(paths: list[Path]) -> str:
    blocks: list[str] = []
    for path in paths:
        blocks.append(
            "\n".join(
                (
                    f"## {path.name}",
                    "",
                    "```mermaid",
                    path.read_text(encoding="utf-8").rstrip(),
                    "```",
                )
            )
        )
    return "\n\n".join(blocks) + "\n"


def _assert_mmdc_rendered_without_error(output_markdown: Path, expected_count: int) -> None:
    rendered_markdown = output_markdown.read_text(encoding="utf-8")
    svg_paths = [
        output_markdown.parent / match.removeprefix("./")
        for match in re.findall(r"!\[diagram\]\(([^)]+\.svg)\)", rendered_markdown)
    ]
    assert len(svg_paths) == expected_count
    for svg_path in svg_paths:
        assert svg_path.exists()
        svg = svg_path.read_text(encoding="utf-8")
        assert not MERMAID_RENDER_ERROR.search(svg), svg_path.name


def test_statechart_contract_has_valid_initials_and_transition_targets() -> None:
    for name, statechart in _statecharts().items():
        states = statechart.get("states")
        assert isinstance(states, dict), name
        assert statechart.get("initial") in states, name

        for state_name, state in states.items():
            assert isinstance(state, dict), f"{name}.{state_name}"
            transitions = state.get("transitions", [])
            assert isinstance(transitions, list), f"{name}.{state_name}"
            for transition in transitions:
                assert isinstance(transition, dict), f"{name}.{state_name}"
                assert len({"event", "guard"} & transition.keys()) <= 1
                assert transition.get("target") in states, f"{name}.{state_name}"


def test_statechart_contract_matches_json_schema() -> None:
    Draft202012Validator.check_schema(_schema())
    validator = Draft202012Validator(_schema())
    errors = sorted(validator.iter_errors(_contract()), key=lambda error: error.json_path)
    assert not errors


def test_statechart_handoffs_resolve_between_contract_states() -> None:
    statecharts = _statecharts()
    handoffs = _handoffs()

    assert handoffs
    for index, handoff in enumerate(handoffs):
        assert set(handoff) <= {"from", "event", "label", "target"}, index
        assert {"from", "label", "target"} <= set(handoff), index
        assert isinstance(handoff.get("label"), str), index
        assert handoff["label"].strip(), index
        endpoints: dict[str, tuple[str, str]] = {}
        for key in ("from", "target"):
            endpoint = handoff[key]
            assert isinstance(endpoint, dict), index
            assert set(endpoint) == {"statechart", "state"}, f"{index}.{key}"
            statechart_name, state_name = _endpoint(handoff, key)
            endpoints[key] = (statechart_name, state_name)
            assert statechart_name in statecharts, f"{index}.{key}"
            states = statecharts[statechart_name]["states"]
            assert isinstance(states, dict)
            assert state_name in states, f"{index}.{key}"
        assert endpoints["from"] != endpoints["target"], index


def test_statechart_choice_states_are_routing_states() -> None:
    for statechart_name, statechart in _statecharts().items():
        states = statechart["states"]
        assert isinstance(states, dict)
        for state_name, state in states.items():
            assert isinstance(state, dict)
            if state.get("type") != "choice":
                continue
            assert state.get("transitions"), f"{statechart_name}.{state_name}"
            assert state.get("final") is not True, f"{statechart_name}.{state_name}"
            assert "event" not in state, f"{statechart_name}.{state_name}"


def test_statechart_terminal_states_are_marked_final() -> None:
    for statechart_name, statechart in _statecharts().items():
        states = statechart["states"]
        assert isinstance(states, dict)
        for state_name, state in states.items():
            assert isinstance(state, dict)
            transitions = state.get("transitions", [])
            assert isinstance(transitions, list), f"{statechart_name}.{state_name}"
            if transitions:
                assert state.get("final") is not True, f"{statechart_name}.{state_name}"
                continue
            assert state.get("final") is True, f"{statechart_name}.{state_name}"


def test_statechart_views_resolve_to_operator_copy_contract() -> None:
    missing = [
        view
        for view in sorted(_view_references(_statecharts()))
        if not callable(getattr(operator_copy, view, None))
    ]

    assert not missing


def test_statechart_views_render_user_copy_from_operator_copy_contract() -> None:
    generator = _mermaid_generator_module()
    missing_renderers: list[str] = []

    for view in sorted(_view_references(_statecharts())):
        try:
            rendered = generator.render_operator_copy(view)
        except generator.OperatorCopyReferenceError:
            missing_renderers.append(view)
            continue
        assert isinstance(rendered, str)
        assert rendered.strip()

    assert not missing_renderers


def test_acceptance_operator_copy_references_are_covered_by_statecharts() -> None:
    missing = sorted(_operator_copy_references() - _view_references(_statecharts()))

    assert not missing


def test_acceptance_scenarios_with_operator_copy_name_statechart_contracts() -> None:
    missing: list[str] = []
    for path, block in _scenario_blocks():
        if (
            'operator copy "' in block
            or 'operator notification copy "' in block
        ) and 'statechart "' not in block:
            header = block.strip().splitlines()[0].strip()
            missing.append(f"{path.relative_to(ROOT)}: {header}")

    assert not missing


def test_acceptance_statechart_references_resolve_to_contract_states() -> None:
    references = _statechart_references()
    statecharts = _statecharts()

    assert references
    missing = [
        f"{statechart_name}.{state_name}"
        for statechart_name, state_name in sorted(references)
        if statechart_name not in statecharts
        or state_name not in statecharts[statechart_name]["states"]
    ]
    assert not missing


def test_notification_workflow_covers_every_notification_copy() -> None:
    statecharts = _statecharts()
    notification_states = statecharts["operator.notifications"]["states"]
    assert isinstance(notification_states, dict)

    notification_views = {
        str(state["view"])
        for state in notification_states.values()
        if isinstance(state, dict) and state.get("view")
    }
    notification_contracts = {
        name
        for name in dir(operator_copy)
        if name.startswith("push_") and callable(getattr(operator_copy, name))
    }

    assert notification_views == notification_contracts


def test_mermaid_generator_writes_clear_compatible_mmdc_validated_workflows(
    tmp_path: Path,
) -> None:
    statecharts = _statecharts()
    generator = _mermaid_generator_module()
    mermaid_dir = tmp_path / "mermaid"

    result = subprocess.run(
        [
            sys.executable,
            str(MERMAID_GENERATOR),
            "--out-dir",
            str(mermaid_dir),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    generated_paths = sorted(mermaid_dir.glob("*.mmd"))
    generated = {path.name for path in generated_paths}
    assert generated == {f"{name}.mmd" for name in statecharts}
    assert "operator.notifications.mmd" in result.stdout
    assert "all.mmd" not in result.stdout
    for path in generated_paths:
        rendered = path.read_text(encoding="utf-8")
        statechart = statecharts[path.stem]
        states = statechart["states"]
        assert isinstance(states, dict)
        has_copy_state = any(
            isinstance(state, dict) and state.get("view") for state in states.values()
        )

        assert "flowchart TD" in rendered
        assert "stateDiagram-v2" not in rendered
        assert "Generated from contracts/operator/statecharts.yaml" in rendered
        assert "[*]" not in rendered
        assert "note right" not in rendered
        assert not re.search(r"^\s+view [A-Za-z_]+\s*$", rendered, flags=re.MULTILINE)
        assert "-->|" not in rendered
        assert not re.search(r"-->\s+[A-Za-z0-9_]+\s*:", rendered)
        assert '"guard ' not in rendered
        assert '"event ' not in rendered
        assert not re.search(r"^\s+(event|view):", rendered, flags=re.MULTILINE)
        assert '["<b>' in rendered
        assert "classDef stateNode fill:#f8fafc" in rendered
        assert "classDef eventNode fill:#e0f2fe" in rendered
        assert "classDef guardNode fill:#fef3c7" in rendered
        assert "classDef linkNode fill:#f3e8ff" in rendered
        assert "classDef externalStateNode fill:#faf5ff" in rendered
        if has_copy_state:
            assert "<br/>" in rendered

    assert not (mermaid_dir / "all.mmd").exists()

    hot_recovery = (mermaid_dir / "arc_disc.hot_recovery.mmd").read_text(
        encoding="utf-8"
    )
    hot_storage = (mermaid_dir / "arc.hot_storage.mmd").read_text(encoding="utf-8")
    burn = (mermaid_dir / "arc_disc.burn.mmd").read_text(encoding="utf-8")
    assert _mermaid_display_text(
        generator.render_operator_copy("hot_recovery_insert_disc")
    ) in hot_recovery
    assert (
        _mermaid_display_text(generator.render_operator_copy("burn_label_checkpoint")) in burn
    )
    assert (
        'restore_complete_progress_done_2{"<b>Restore Complete</b>"}:::guardNode'
        in hot_recovery
    )
    assert (
        'operator_inserts_another_disc_retry_other_disc_insert_disc_1'
        '(["<b>Operator Inserts Another Disc</b>"]):::eventNode'
    ) in hot_recovery
    assert "guard_restore_complete" not in hot_recovery
    assert "event_operator_inserts_another_disc" not in hot_recovery
    assert (
        'link_pin_waiting_for_disc_to_arc_disc_guided_scan_backlog'
        '[["<b>Operator Runs Arc Disc</b>"]]:::linkNode'
    ) in hot_storage
    assert (
        'external_arc_disc_guided_scan_backlog["<b>Arc Disc Guided</b><br/>'
        'Scan Backlog"]:::externalStateNode'
    ) in hot_storage

    input_markdown = tmp_path / "operator-statecharts.md"
    output_markdown = tmp_path / "operator-statecharts.validated.md"
    input_markdown.write_text(
        _combined_mermaid_markdown(generated_paths),
        encoding="utf-8",
    )
    subprocess.run(
        [
            "npx",
            "-y",
            MERMAID_CLI_PACKAGE,
            "-p",
            str(MERMAID_PUPPETEER_CONFIG),
            "-i",
            str(input_markdown),
            "-o",
            str(output_markdown),
            "--quiet",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    _assert_mmdc_rendered_without_error(
        output_markdown,
        expected_count=len(generated_paths),
    )


def test_generated_operator_workflows_are_gitignored_and_regenerable() -> None:
    assert "/contracts/operator/statecharts/" in (ROOT / ".gitignore").read_text(
        encoding="utf-8"
    )
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "operator-workflows:" in makefile
    assert "scripts/fsm_to_mermaid.py --out-dir contracts/operator/statecharts" in makefile
    assert "operator-workflows-validate:" not in makefile
