# pylint: disable=line-too-long
"""
Dispatch Handler ::: Customer
"""

from pathlib import Path
from rich.console import Console
from rich.table import Table
from rich import box

from platform_atlas.core import ui
from platform_atlas.core.registry import registry

theme = ui.theme
console = Console()

@registry.register("customer", "import", description="Import a customer capture file")
def handle_import_customer_data(args) -> int:
    """Import a customer capture file"""
    from platform_atlas.sessions.customer_data import import_capture

    capture_file = Path(args.capture_file)
    if not capture_file.exists():
        console.print(f"[red]✗[/red] File not found: {capture_file}")
        return 1

    try:
        dest = import_capture(
            capture_file,
            getattr(args, 'organization', None),  # Changed from hasattr check
            getattr(args, 'session', None)   # Changed from hasattr check
        )

        console.print(f"[green]✓[/green] Imported to: {dest}")
        return 0
    except ValueError as e:
        console.print(f"[red]✗[/red] {e}")
        return 1
    except Exception as e:
        console.print(f"[red]✗[/red] Import failed: {e}")
        return 1

@registry.register("customer", "list", description="List all companies with capture data")
def handle_list_customer_companies(args=None) -> int:  # Added args parameter with default
    """List all companies with capture data"""
    from platform_atlas.sessions.customer_data import list_companies, list_sessions

    companies = list_companies()

    if not companies:
        console.print("[yellow]No customer data imported yet[/yellow]")
        console.print("\nUse: [cyan]platform-atlas customer import <file>[/cyan]")
        return 1

    table = Table(title="Customer Companies", box=box.SIMPLE_HEAVY)
    table.add_column("Company", style="cyan")
    table.add_column("Sessions", justify="right", style="green")

    for company in companies:
        sessions = list_sessions(company)
        table.add_row(company, str(len(sessions)))

    console.print(table)
    return 0

@registry.register("customer", "sessions", description="List all sessions for a company")
def handle_list_customer_sessions(args) -> int:
    """List all sessions for a company"""
    from platform_atlas.sessions.customer_data import list_sessions

    sessions = list_sessions(args.organization)

    if not sessions:
        console.print(f"[yellow]No sessions found for '{args.organization}'[/yellow]")
        return 1

    table = Table(
        title=f"Sessions for {args.organization}",
        box=box.SIMPLE_HEAVY
    )
    table.add_column("Session", style="cyan")
    table.add_column("Hostname", style="white")
    table.add_column("Captured", style="dim")
    table.add_column("Validated", justify="center")
    table.add_column("Report", justify="center")

    for session in sessions:
        session_name = session.filename.replace("-capture.json", "")
        validated = "[green]✓[/green]" if session.has_validation else "[dim]-[/dim]"
        reported = "[green]✓[/green]" if session.has_report else "[dim]-[/dim]"

        table.add_row(
            session_name,
            session.hostname,
            session.captured_at,
            validated,
            reported
        )

    console.print(table)
    return 0

@registry.register("customer", "validate", description="Validate a customer capture session")
def handle_validate_customer_session(args) -> int:
    """Validate a customer capture session"""
    from platform_atlas.sessions.customer_data import get_capture_path, normalize_organization_name
    from platform_atlas.validation.validation_engine import validate_from_files

    capture_file = get_capture_path(args.organization, args.session)

    if not capture_file:
        console.print(
            f"[red]✗[/red] Session not found: {args.organization}/{args.session}"
        )
        return 1

    console.print(f"[cyan]Validating {args.organization}/{args.session}...[/cyan]\n")

    # Run validation
    df = validate_from_files(capture_file)

    # Save validation result
    from platform_atlas.core.paths import ATLAS_CUSTOMER_DATA
    organization_dir = ATLAS_CUSTOMER_DATA / normalize_organization_name(args.organization)
    validation_file = organization_dir / f"{args.session}-validation.parquet"
    df.to_parquet(validation_file, engine="pyarrow", compression="snappy")

    console.print(f"\n[green]✓[/green] Validation saved to: {validation_file}")

    # Optionally generate report immediately
    from rich.prompt import Confirm
    if Confirm.ask("Generate report now?", default=True):
        handle_report_customer_session(args)
    return 0

@registry.register("customer", "report", description="Generate report for a customer capture session")
def handle_report_customer_session(args) -> int:
    """Generate report for a customer capture session"""
    from platform_atlas.sessions.customer_data import (
        normalize_organization_name, get_capture_path,
    )
    from platform_atlas.reporting.reporting_engine import report
    from platform_atlas.core.paths import ATLAS_CUSTOMER_DATA
    import pandas as pd

    organization_dir = ATLAS_CUSTOMER_DATA / normalize_organization_name(args.organization)
    validation_file = organization_dir / f"{args.session}-validation.parquet"

    if not validation_file.exists():
        console.print(
            f"[yellow]No validation found. Run validation first:[/yellow]\n"
            f"  platform-atlas customer validate {args.organization} {args.session}"
        )
        return 1

    # Load validation data
    df = pd.read_parquet(validation_file)

    # Parquet doesn't preserve df.attrs - reload metadata from capture JSON
    capture_path = get_capture_path(args.organization, args.session)
    if capture_path:
        import json
        with open(capture_path, encoding="utf-8") as f:
            capture_data = json.load(f)

        atlas_internal = capture_data.get("_atlas", {})
        metadata = atlas_internal.get("metadata", {})
        system_facts = atlas_internal.get("system_facts", {})
        platform_data = capture_data.get("platform", {})
        health_server = platform_data.get("health_server", {}) if isinstance(platform_data, dict) else {}

        df.attrs["hostname"] = system_facts.get("hostname", "Unknown")
        df.attrs["platform_ver"] = health_server.get("version", "Unknown")
        df.attrs["ruleset_id"] = metadata.get("ruleset_id", "")
        df.attrs["ruleset_version"] = metadata.get("ruleset_version", "")
        df.attrs["modules_ran"] = metadata.get("modules_ran", "")
        df.attrs["captured_at"] = metadata.get("captured_at", "")

    # Generate report
    report_file = organization_dir / f"{args.session}-report.html"
    report_name = f"{args.organization.title()} - {args.session}"

    report(df, report_name, str(report_file))

    console.print(f"[green]✓[/green] Report generated: {report_file}")

    # Auto-open if requested
    if not (hasattr(args, 'no_open') and args.no_open):
        import webbrowser
        webbrowser.open(report_file.as_uri())
        console.print(f"  [{theme.text_dim}]Opened in browser[/{theme.text_dim}]")
    return 0
