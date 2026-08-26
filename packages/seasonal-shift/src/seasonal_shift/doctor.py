from __future__ import annotations

from rich import print
from rich.table import Table

from .models import FileOperation, ShowConfig
from .planner import detect_collisions, detect_duplicates, plan_operations
from .scanner import scan_show


def run_doctor(shows: list[ShowConfig]) -> None:
    print("[bold blue]Running diagnostics...[/]\n")

    table = Table(title="Show Diagnostics")
    table.add_column("Show")
    table.add_column("Path Exists")
    table.add_column("Episode Files")
    table.add_column("Config Seasons")

    for show in shows:

        path_exists: bool = show.path.exists()

        episode_count: int = 0
        config_seasons: str = ", ".join(map(str, sorted(show.seasons.keys())))

        if path_exists:
            episode_count = sum(1 for _ in scan_show(show.path))

        table.add_row(
            show.name,
            "✔" if path_exists else "✘",
            str(episode_count),
            config_seasons,
        )

    print(table)

    print("\n[bold blue]Planning operations...[/]\n")

    operations: list[FileOperation] = []

    for show in shows:
        operations.extend(plan_operations(show, scan_show))

    print(f"[green]Operations planned:[/] {len(operations)}")

    collisions = detect_collisions(operations)

    if collisions:
        print("\n[red]Collision detected:[/]")
        for c in collisions:
            print(c)
    else:
        print("[green]No collisions detected[/]")

    duplicates = detect_duplicates(operations)

    if duplicates:
        print("\n[yellow]Duplicate destination names:[/]")
        for a, b in duplicates:
            print(f"{a} and {b}")
    else:
        print("[green]No duplicates detected[/]")

    if not collisions and not duplicates:
        print("\n[bold green]All checks passed ✔[/]")
