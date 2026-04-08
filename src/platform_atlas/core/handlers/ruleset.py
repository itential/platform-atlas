# pylint: disable=line-too-long
"""
Dispatch Handler ::: Rulesets
"""

import json
import logging
from argparse import Namespace

import questionary
from rich.console import Console
from rich.table import Table
from rich.text import Text

# ATLAS Core
from platform_atlas.core import ui

# ATLAS Management
from platform_atlas.core.registry import registry
from platform_atlas.core.ruleset_manager import get_ruleset_manager

theme = ui.theme
console = Console()
logger = logging.getLogger(__name__)

@registry.register("ruleset", "list", description="List available rulesets")
def handle_list_rulesets(args: Namespace) -> int:
    """List available rulesets"""
    manager = get_ruleset_manager()
    rulesets = manager.discover_rulesets()
    active_id = manager.get_active_ruleset_id()
    active_profile = manager.get_active_profile_id()

    if not rulesets:
        console.print(f"[yellow]No rulesets in {manager.RULESETS_DIR}[/yellow]")
        return 1

    table = Table(title="Available Rulesets", title_style="bold cyan")
    table.add_column("", width=2)
    table.add_column("ID", style="cyan")
    table.add_column("Name", style="white")
    table.add_column("Version", style="yellow")
    table.add_column("Profile", style="magenta")
    table.add_column("Rules", justify="right", style="green")

    for rs in rulesets:
        is_active = rs.id == active_id
        mark = "✓" if rs.id == active_id else ""
        profile = active_profile or "-" if is_active else "-"
        table.add_row(mark, rs.id, rs.name, rs.version, profile, str(rs.rule_count))
    console.print(table)
    return 0

@registry.register("ruleset", "profiles", description="List available profiles")
def handle_list_profiles(args: Namespace) -> int:
    manager = get_ruleset_manager()
    profiles = manager.discover_profiles()
    active_profile = manager.get_active_profile_id()

    if not profiles:
        console.print(f"[yellow]No Profiles in {manager.PROFILES_DIR}[/yellow]")
        return 1

    table = Table(title="Available Profiles", title_style="bold cyan")
    table.add_column("", width=2)
    table.add_column("ID", style="cyan")
    table.add_column("Name", style="white")
    table.add_column("Description", style="dim")
    table.add_column("Overrides", justify="right", style="yellow")

    for p in profiles:
        mark = "✓" if p.id == active_profile else ""
        table.add_row(mark, p.id, p.name, p.description, str(p.override_count))
    console.print(table)
    return 0

@registry.register("ruleset", "load", description="Load and active a ruleset")
def handle_load_ruleset(args: Namespace) -> int:
    """Load and active a ruleset"""

    # Grab ruleset_id from args
    ruleset_id = args.ruleset_id
    profile_id = getattr(args, "profile", None)

    try:
        get_ruleset_manager().set_active_ruleset(ruleset_id, profile_id)
        #console.print(f"[{theme.success}]✓[/{theme.success}] Activated: [bold]{ruleset_id}[/bold]")
        msg = f"Activated: [bold]{ruleset_id}[/bold]"
        if profile_id:
            msg += f" with profile [bold]{profile_id}[/bold]"
        console.print(f"[{theme.success}]✓[/{theme.success}] {msg}")
        return 0
    except FileNotFoundError as e:
        console.print(f"[{theme.error}]✘[/{theme.error}] {e}")
        return 1

@registry.register("ruleset", "active", description="Show currently active ruleset")
def handle_active_ruleset(args: Namespace) -> int:
    """Show currently active ruleset"""
    manager = get_ruleset_manager()
    active_id = manager.get_active_ruleset_id()

    if not active_id:
        console.print(f"[{theme.warning}]No active ruleset has been set.[/{theme.warning}]")
        console.print(f"Use: [{theme.primary}]platform-atlas --load-ruleset <id>[/{theme.primary}]")
        return 1

    try:
        m = manager.get_metadata(active_id)
        table = Table(title=f"{m.name} ({m.id})", title_style=f"bold {theme.primary}", show_header=False)
        table.add_column("Field", style="dim")
        table.add_column("Value")

        table.add_row("Version", m.version)
        table.add_row("Rules", str(m.rule_count))
        table.add_row("Target", m.target_product)
        table.add_row("Author", m.author)
        table.add_row("Description", m.description)

        console.print(table)
    except FileNotFoundError:
        console.print(f"[{theme.warning}]⚠[/{theme.warning}] '{active_id}' missing, clearing...")
        manager.clear_active_ruleset()

@registry.register("ruleset", "profile", "set", description="Set the active profile")
def handle_set_profile(args: Namespace) -> int:
    manager = get_ruleset_manager()
    profile_id = args.profile_id
    ruleset_id = manager.get_active_ruleset_id()

    if not ruleset_id:
        console.print(f"[{theme.warning}]No active ruleset. Load one first.[/{theme.warning}]")
        return 1

    try:
        manager.set_active_ruleset(ruleset_id, profile_id)
        console.print(f"[{theme.success}]✓[/{theme.success}] Profile set: [bold]{profile_id}[/bold]")
        return 0
    except FileNotFoundError as e:
        console.print(f"[{theme.error}]✘[/{theme.error}] {e}")
        return 1

@registry.register("ruleset", "profile", "clear", description="Clear the active profile")
def handle_clear_profile(args: Namespace) -> int:
    manager = get_ruleset_manager()
    ruleset_id = manager.get_active_ruleset_id()

    if not ruleset_id:
        console.print(f"[{theme.warning}]No active ruleset[/{theme.warning}]")
        return 1

    manager.set_active_ruleset(ruleset_id, None)
    console.print(f"[{theme.success}]✓[/{theme.success}] Profile cleared")
    return 0

@registry.register("ruleset", "profile", "list", description="List available profiles")
def handle_profile_list(args: Namespace) -> int:
    manager = get_ruleset_manager()
    profiles = manager.discover_profiles()
    active_profile = manager.get_active_profile_id()

    if not profiles:
        console.print(f"[yellow]No Profiles in {manager.PROFILES_DIR}[/yellow]")
        return 1

    table = Table(title="Available Profiles", title_style="bold cyan")
    table.add_column("", width=2)
    table.add_column("ID", style="cyan")
    table.add_column("Name", style="white")
    table.add_column("Description", style="dim")
    table.add_column("Overrides", justify="right", style="yellow")

    for p in profiles:
        mark = "✓" if p.id == active_profile else ""
        table.add_row(mark, p.id, p.name, p.description, str(p.override_count))
    console.print(table)
    return 0

@registry.register("ruleset", "profile", "active", description="List available profiles")
def handle_profile_active(args: Namespace) -> int:
    """Show the currently active profile"""
    manager = get_ruleset_manager()
    active_profile = manager.get_active_profile_id()

    if not active_profile:
        console.print(f"[{theme.warning}]No active profile[/{theme.warning}]")
        return 0

    try:
        profile_data = manager._load_profile(active_profile)
        console.print(f"[{theme.success}]✓[/{theme.success}] Active profile: [bold]{active_profile}[/bold]")
        console.print(f"  [dim]Name:[/dim] {profile_data.get('profile_name', '-')}")
        console.print(f"  [dim]Description:[/dim] {profile_data.get('description', '-')}")
        console.print(f"  [dim]Overrides:[/dim] {len(profile_data.get('rules', {}))}")
        return 0
    except FileNotFoundError:
        console.print(f"[{theme.warning}]⚠[/{theme.warning}] Profile '{active_profile}' missing, clearing...")
        ruleset_id = manager.get_active_ruleset_id()
        if ruleset_id:
            manager.set_active_ruleset(ruleset_id, None)
        return 1

@registry.register("ruleset", "info", description="Show detailed ruleset information")
def handle_ruleset_info(args: Namespace) -> int:
    """Show detailed ruleset information"""

    # Grab ruleset_id from args
    ruleset_id = args.ruleset_id

    try:
        m = get_ruleset_manager().get_metadata(ruleset_id)
        is_active = m.id == get_ruleset_manager().get_active_ruleset_id()

        table = Table(title=f"{m.name} ({m.id})", title_style=f"bold {theme.primary}", show_header=False)
        table.add_column("Field", style="dim")
        table.add_column("Value")

        table.add_row("Version", m.version)
        table.add_row("Rules", str(m.rule_count))
        table.add_row("Target", m.target_product)
        table.add_row("Author", m.author)
        table.add_row("Description", m.description)
        table.add_row("File", m.file_path.name)
        table.add_row("Modified", m.last_modified.strftime('%Y-%m-%d %H:%M'))
        table.add_row("Active", "[green]Yes ✓[/green]" if is_active else "No")

        console.print(table)
    except FileNotFoundError:
        console.print(f"[{theme.error}]✘[/{theme.error}] Not found: [bold]{ruleset_id}[/bold]")
        return 1
    return 0

@registry.register("ruleset", "clear", description="Clear the active ruleset")
def handle_clear_ruleset(args: Namespace) -> int:
    """Clear the active ruleset"""
    manager = get_ruleset_manager()
    if active_id := manager.get_active_ruleset_id():
        manager.clear_active_ruleset()
        console.print(f"[green]✓[/green] Cleared: [bold]{active_id}[/bold]")
    else:
        console.print("[yellow]No active ruleset[/yellow]")
    return 0

@registry.register("ruleset", "rules", description="Display all rules in a ruleset")
def handle_ruleset_rules(args: Namespace) -> int:
    """Display all rules in the active of specified ruleset as a Rich table"""
    try:
        rm = get_ruleset_manager()

        # Resolve which ruleset to load
        ruleset_id = getattr(args, "ruleset_id", None) or rm.get_active_ruleset_id()

        if not ruleset_id:
            console.print(
                f"[{theme.warning}]No ruleset specified or active[/{theme.warning}]"
            )
            console.print(
                f"[{theme.text_dim}]Use 'platform-atlas ruleset load <id>' "
                f"or specify one: 'platform-atlas ruleset rules <id>'[/{theme.text_dim}]"
            )
            return 1

        # Get metadata (validates the ruleset exists)
        metadata = rm.get_metadata(ruleset_id)

        # Load the raw JSON to get th rules array
        with open(metadata.file_path, "r", encoding="utf-8") as f:
            ruleset_data = json.load(f)

        rules = ruleset_data.get("rules", [])

        if not rules:
            console.print(f"[{theme.warning}]Ruleset contains no rules[/{theme.warning}]")

        # Apply filters
        category_filter = getattr(args, "category", None)
        severity_filter = getattr(args, "severity", None)

        if category_filter:
            rules = [
                r for r in rules
                if r.get("category", "").lower() == category_filter.lower()
            ]
        if severity_filter:
            rules = [
                r for r in rules
                if r.get("severity", "").lower() == severity_filter.lower()
            ]

        if not rules:
            console.print(
                f"[{theme.warning}]No rules match the applied filters[/{theme.warning}]"
            )
            return 0

        # Severity styling
        severity_styles = {
            "critical": f"bold {theme.error}",
            "warning": theme.warning,
            "info": theme.info
        }

        # Build the table
        table = Table(
            title=f"\n{metadata.name}",
            caption=f"{len(rules)} rule{'s' if len(rules) != 1 else ''}",
            show_lines=False,
            pad_edge=True,
            expand=False,
        )

        table.add_column("Rule ID", style="dim", width=10)
        table.add_column("Enabled", justify="center", width=8)
        table.add_column("Name", style="bold", max_width=60)
        table.add_column("Category", width=12)
        table.add_column("Severity", width=10)
        table.add_column("Type", style="dim", width=12)
        table.add_column("Operator", width=10)
        table.add_column("Target Path", style=f"dim {theme.accent}", max_width=40, overflow="ellipsis")

        for rule in rules:
            severity = rule.get("severity", "info").lower()
            sev_style = severity_styles.get(severity, "")
            enabled = rule.get("enabled", True)
            validation = rule.get("validation", {})

            table.add_row(
                rule.get("rule_number", "-"),
                Text("✓", style=theme.success) if enabled else Text("✖", style=f"bold {theme.error}"),
                rule.get("name", "-"),
                rule.get("category", "-"),
                Text(severity, style=sev_style),
                validation.get("type", "-"),
                validation.get("operator", "-"),
                rule.get("path", "-"),
            )
        console.print(table)
        return 0

    except FileNotFoundError:
        console.print(f"[{theme.error}]✖[/{theme.error}] Ruleset not found: {ruleset_id}")
        return 1
    except Exception as e:
        console.print(f"[{theme.error}]✖[/{theme.error}] {e}")

@registry.register("ruleset", "setup", description="Interactive ruleset and profile selection")
def handle_ruleset_setup(args: Namespace) -> int:
    """Interactively select a ruleset and profile."""
    from platform_atlas.core.init_setup import QSTYLE
    manager = get_ruleset_manager()
    rulesets = manager.discover_rulesets()

    if not rulesets:
        console.print(f"\n  [{theme.warning}]No rulesets found.[/{theme.warning}]")
        console.print(f"  [{theme.text_dim}]Add rulesets to {manager.RULESETS_DIR}[/{theme.text_dim}]\n")
        return 1

    active_ruleset = manager.get_active_ruleset_id()
    active_profile = manager.get_active_profile_id()

    # ── Step 1: Select ruleset ──
    # Use file_path.stem as the value — set_active_ruleset resolves
    # the path as RULESETS_DIR / f"{ruleset_id}.json", so the value
    # must match the filename, not the internal JSON id.
    ruleset_choices = []
    for rs in rulesets:
        file_id = rs.file_path.stem
        suffix = " (active)" if file_id == active_ruleset else ""
        label = f"{rs.id}  —  {rs.name} v{rs.version} ({rs.rule_count} rules){suffix}"
        ruleset_choices.append(questionary.Choice(title=label, value=file_id))

    default_ruleset = active_ruleset if active_ruleset else rulesets[0].file_path.stem
    selected_ruleset = questionary.select(
        "Select ruleset:",
        choices=ruleset_choices,
        default=default_ruleset,
        style=QSTYLE,
    ).ask()

    if selected_ruleset is None:
        console.print(f"  [{theme.text_dim}]Cancelled[/{theme.text_dim}]")
        return 1

    # ── Step 2: Select profile ──
    profiles = manager.discover_profiles()

    _NO_PROFILE = "__none__"
    selected_profile = None
    if profiles:
        profile_choices = [
            questionary.Choice(title="None (no profile)", value=_NO_PROFILE),
        ]
        for p in profiles:
            file_id = p.file_path.stem
            suffix = " (active)" if file_id == active_profile else ""
            label = f"{p.id}  —  {p.name} ({p.override_count} overrides){suffix}"
            profile_choices.append(questionary.Choice(title=label, value=file_id))

        default_profile = active_profile if active_profile else _NO_PROFILE
        result = questionary.select(
            "Select profile:",
            choices=profile_choices,
            default=default_profile,
            style=QSTYLE,
        ).ask()

        if result is None:
            console.print(f"  [{theme.text_dim}]Cancelled[/{theme.text_dim}]")
            return 1

        selected_profile = None if result == _NO_PROFILE else result

    # ── Apply ──
    try:
        manager.set_active_ruleset(selected_ruleset, selected_profile)
    except FileNotFoundError as e:
        console.print(f"\n  [{theme.error}]✘[/{theme.error}] {e}\n")
        return 1

    msg = f"Active ruleset: [{theme.accent}]{selected_ruleset}[/{theme.accent}]"
    if selected_profile:
        msg += f" with profile [{theme.accent}]{selected_profile}[/{theme.accent}]"
    console.print(f"\n  [{theme.success}]✓[/{theme.success}] {msg}\n")
    return 0

@registry.register("ruleset", "switch", description="Switch the active ruleset and profile")
def handle_ruleset_switch(args: Namespace) -> int:
    """Switch the active ruleset and profile (alias for setup)"""
    return handle_ruleset_setup(args)
