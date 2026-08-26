"""Standalone CLI: probe a media file and print quality findings + the prune decision.

Usable on its own (`quality-check FILE`); the orchestrator imports :func:`quality_check.rules.check`
and :func:`quality_check.prune.decide` directly instead of shelling out to this.
"""

from __future__ import annotations

import json as _json
from pathlib import Path

import typer
from mediacore.models import PolicyConfig
from mediacore.probe import probe
from rich.console import Console

from quality_check.prune import decide
from quality_check.rules import check

app = typer.Typer(
    help="Probe a media file and report audio/subtitle quality findings.",
    add_completion=False,
)
console = Console()


@app.command()
def main(
    file: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    policy = PolicyConfig()
    pr = probe(str(file))
    findings = check(pr, policy)
    decision = decide(pr, policy)

    if as_json:
        console.print_json(
            _json.dumps(
                {
                    "file": str(file),
                    "findings": [
                        {"rule": f.rule, "title": f.title, "details": f.details}
                        for f in findings
                    ],
                    "prune": {
                        "needs_work": decision.needs_work,
                        "audio_arg": decision.audio_arg,
                        "subs_arg": decision.subs_arg,
                        "summary": decision.summary(),
                    },
                }
            )
        )
        raise typer.Exit(0)

    console.print(f"[bold]{file}[/bold]")
    if not findings:
        console.print("[green]OK[/green] — no findings")
    for f in findings:
        console.print(f"[yellow]Rule {f.rule}[/yellow]: {f.title}")
        for d in f.details:
            console.print(f"  • {d}")
    console.print(
        f"\n[bold]Prune:[/bold] {'work needed' if decision.needs_work else 'skip'} — "
        f"{decision.summary()}"
    )
    console.print(f"  mkvprune --audio {decision.audio_arg} --subs {decision.subs_arg}")
