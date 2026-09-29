"""Contracts the interface must keep, checked without starting GTK.

The interface is not importable in CI - it needs gi, GTK 4 and a display - so
these read the source instead. That is a weak form of test and it is used
sparingly, for the cases where the cost of a silent regression is high and the
mistake is easy to make.

This one earned its place. The handler, the dialog and the gating for an NVMe
controller refusing Sanitize were all written and all correct, but the button's
visibility is decided in the row widget from device.ata alone. The result was
the worst of both: the drive's firmware methods were withdrawn, the operator
was shown a reduced list with no explanation, and there was nothing to press.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_UI = Path(__file__).resolve().parent.parent / "zeroize" / "ui"


def _source(name: str) -> str:
    path = _UI / name
    if not path.is_file():
        pytest.skip(f"{name} is not present")
    return path.read_text(encoding="utf-8")


def test_device_row_offers_unfreeze_for_a_blocked_nvme_controller():
    """The button must not be gated on ATA state alone.

    An NVMe controller answering Access Denied to Sanitize is exactly as
    actionable as an ATA drive frozen at POST, and from the operator's side it
    looks identical: the good methods are gone. If only the ATA case raises a
    button, the NVMe case is a dead end on screen.
    """
    source = _source("device_row.py")
    assert "sanitize_blocked" in source, (
        "device_row does not consider an NVMe controller that is refusing "
        "Sanitize, so no unfreeze button appears for one"
    )
    assert "ata.frozen" in source or "frozen" in source, "the ATA case was lost"


def test_the_nvme_button_does_not_promise_to_leave_the_machine_alone():
    """The two remedies differ, and the wording beside them must differ too.

    Detaching one ATA drive leaves the rest of the machine running. The NVMe
    remedy suspends the whole machine. Presenting the second with the first's
    description would mislead the operator at the one moment the tool reaches
    past the drive they selected.
    """
    source = _source("device_row.py")
    tree = ast.parse(source)

    strings = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    reassuring = [text for text in strings if "rest of the machine" in text]
    assert reassuring, "the ATA tooltip's wording has changed; re-check this test"

    for text in reassuring:
        assert "suspend" not in text.lower(), (
            "a tooltip promising the machine is left alone is attached to the "
            "suspend path"
        )

    assert any(
        "suspend" in text.lower() and "machine" in text.lower() for text in strings
    ), "the NVMe path does not say anywhere that it suspends the machine"


def test_suspend_is_confirmed_before_it_happens():
    """Nothing may suspend the machine without asking first."""
    source = _source("main_window.py")
    tree = ast.parse(source)

    prompt = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_prompt_suspend"
        ),
        None,
    )
    assert prompt is not None, "the shared suspend prompt is gone"

    calls = [
        node
        for node in ast.walk(prompt)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "confirm_action"
    ]
    assert calls, "_prompt_suspend no longer asks for confirmation"

    # And it must say the blast radius, because this is the only action in the
    # tool that reaches past the selected drive.
    body = " ".join(
        node.value
        for node in ast.walk(prompt)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )
    assert "Every drive" in body, (
        "the prompt does not tell the operator that every drive in the machine "
        "is affected"
    )
