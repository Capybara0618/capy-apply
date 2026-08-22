from __future__ import annotations

import pytest
import typer

from capybot.cli import commands


def test_start_reuses_running_capybot_without_starting_dependencies(monkeypatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(
        commands,
        "_running_apply_url",
        lambda _port: "http://127.0.0.1:8765",
        raising=False,
    )
    monkeypatch.setattr(
        commands.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("dependencies must not restart"),
    )
    monkeypatch.setattr(commands.webbrowser, "open", opened.append)

    commands.start(
        port=8765,
        no_docker=False,
        no_worker=False,
        open_browser=True,
    )

    assert opened == ["http://127.0.0.1:8765/apply"]


def test_start_rejects_unrelated_port_conflict_before_starting_dependencies(
    monkeypatch,
) -> None:
    monkeypatch.setattr(commands, "_running_apply_url", lambda _port: None, raising=False)
    monkeypatch.setattr(commands, "_port_available", lambda _port: False, raising=False)
    monkeypatch.setattr(
        commands.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("dependencies must not start"),
    )

    with pytest.raises(typer.Exit):
        commands.start(
            port=8765,
            no_docker=False,
            no_worker=False,
            open_browser=False,
        )
