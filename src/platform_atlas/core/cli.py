# pylint: disable=line-too-long
"""
Platform Atlas CLI Command Structure

This module defines the complete command-line interface using argparse subcommands.
Follows the pattern: platform-atlas <command> <subcommand> [options]

Command Groups:
    - session: Manage audit sessions (create, run, list, show, export, delete)
    - ruleset: Manage rulesets (list, load, info, active, clear)
    - config: Configuration management (init, validate, show)
    - env: Manage deployment environments (list, switch, create, show, remove, edit)
    - preflight: Run preflight connectivity checks
"""

import json
import argparse
from pathlib import Path
from rich_argparse import RichHelpFormatter

from platform_atlas.core import ui
from platform_atlas.core._version import __version__

theme = ui.theme

# =================================================
# Custom Help Formatter
# =================================================

class AtlasHelpFormatter(RichHelpFormatter):
    """Custom help formatter with Atlas branding"""

    def __init__(self, prog):
        super().__init__(prog, max_help_position=40)
        # Atlas color scheme
        self.styles["argparse.args"] = theme.accent
        self.styles["argparse.metavar"] = f"italic {theme.secondary}"
        self.styles["argparse.prog"] = f"italic {theme.primary_glow}"
        self.styles["argparse.groups"] = f"bold {theme.warning_glow}"
        self.styles["argparse.text"] = theme.info

        self.styles["argparse.special"] = f"bold {theme.warning_glow}"


# =================================================
# Main Parser Setup
# =================================================

def create_parser() -> argparse.ArgumentParser:
    """Create the main argument parser with all subcommands"""

    # Main parser
    parser = argparse.ArgumentParser(
        prog="platform-atlas",
        description=f"Platform Atlas {__version__} - Itential Platform Configuration Auditing",
        formatter_class=AtlasHelpFormatter,
        epilog="Run 'platform-atlas <command> --help' for more information on a command."
    )

    parser.add_argument(
        '--version',
        action='version',
        version=f'%(prog)s {__version__}'
    )

    parser.add_argument(
        '--debug',
        action='store_true',
        help='Enable debug mode with verbose logging'
    )

    parser.add_argument(
        '--env',
        dest='env_override',
        metavar='ENV',
        help='Use a specific environment for this command (overrides active environment)'
    )

    parser.add_argument(
        '--whats-new',
        dest='whats_new',
        action='store_true',
        help='Show the "What\'s New" screen for the current version'
    )

    # Create subcommand groups
    subparsers = parser.add_subparsers(
        dest='command',
        title='Commands',
        description='Available command groups',
        help='Command to execute',
        metavar='<command>'
    )

    # Add command groups
    _add_session_commands(subparsers)
    _add_ruleset_commands(subparsers)
    _add_config_commands(subparsers)
    _add_env_commands(subparsers)
    _add_preflight_command(subparsers)
    _add_guide_commands(subparsers)
    _add_customer_commands(subparsers)

    return parser

# =================================================
# SESSION Command Group
# =================================================

def _add_session_commands(subparsers):
    """Add session management commands"""

    session_parser = subparsers.add_parser(
        'session',
        help='Manage audit sessions (create, capture, validate, report)',
        formatter_class=AtlasHelpFormatter,
        description='Create, run, and manage audit sessions'
    )

    session_subparsers = session_parser.add_subparsers(
        dest='session_action',
        title='Session Actions',
        help='Action to perform',
        metavar='<action>',
        required=True
    )

    # session create
    create = session_subparsers.add_parser(
        'create',
        help='Create a new audit session',
        formatter_class=AtlasHelpFormatter,
        description='Initialize a new audit session with bound environment, ruleset, and profile'
    )
    create.add_argument(
        'session_name',
        help='Unique name for this session (e.g., "prod-audit-feb")'
    )
    create.add_argument(
        '--description',
        help='Optional description of this audit session'
    )
    create.add_argument(
        '--target',
        help='Target system identifier'
    )
    create.add_argument(
        '--env',
        help='Environment name (interactive picker if not specified)'
    )
    create.add_argument(
        '--ruleset',
        help='Ruleset ID (interactive picker if not specified)'
    )
    create.add_argument(
        '--profile',
        help='Profile ID (interactive picker if not specified)'
    )

    # session run
    run = session_subparsers.add_parser(
        'run',
        help='Run a workflow stage (capture, validate, report, all)',
        formatter_class=AtlasHelpFormatter,
        description='Execute capture or validation within a session'
    )
    run.add_argument(
        'stage',
        choices=['capture', 'validate', 'report', 'all'],
        help='Workflow stage to execute (all = capture → validate → report)'
    )
    run.add_argument(
        '--session',
        help='Session name (uses active session if not specified)'
    )
    run.add_argument(
        '--modules',
        nargs='+',
        metavar='MODULE',
        help='Specific modules to run during capture (e.g., system mongo redis platform)'
    )
    run.add_argument(
        '--manual',
        action='store_true',
        help='Guided manual collection - walk through providing data files instead of live capture'
    )
    run.add_argument(
        '--import-dir',
        metavar='DIR',
        help='Import capture files from a directory (used with --manual). '
             'Atlas matches files by name and loads them automatically. '
             'Re-runnable — add more files and run again to update.'
    )
    run.add_argument(
        '--skip-architecture',
        action='store_true',
        help='Skip the architecture validation questions'
    )
    run.add_argument(
        '--skip-guided',
        action='store_true',
        help='Skip guided fallback prompts for failed capture modules'
    )
    # Log parser options
    run.add_argument(
        '--log-mode',
        choices=['top', 'heuristics'],
        default='top',
        help='Log analysis mode: top-N frequency ranking or heuristic keyword matching (default: top)'
    )
    run.add_argument(
        '--log-top-n',
        type=int,
        default=25,
        help='Number of top messages per log group (default: 25)'
    )
    run.add_argument(
        '--log-levels',
        nargs='+',
        default=['error', 'warn'],
        metavar='LEVEL',
        help='Log levels to include (default: error warn)'
    )
    run.add_argument(
        '--skip-logs',
        action='store_true',
        help='Skip platform and webserver log collection during capture'
    )
    run.add_argument(
        '--output',
        help='Output file path (for report stage)'
    )
    run.add_argument(
        '--format',
        choices=['html', 'csv', 'json', 'md'],
        default='html',
        help='Output format for report stage (default: html)'
    )
    run.add_argument(
        '--no-open',
        action='store_true',
        help='Do not automatically open generated reports'
    )
    run.add_argument(
        '--headless',
        action='store_true',
        help='Non-interactive mode - skip all prompts, use sensible defaults. '
            'Implies --skip-architecture --skip-guided --no-open'
    )
    run.add_argument(
        '--no-fixes',
        action='store_true',
        help='Disable fix instructions from the knowledge base in the report detail modals'
    )
    run.add_argument(
        '--operational',
        action='store_true',
        help='Generate an operational metrics report from MongoDB aggregation pipelines'
    )

    # session list
    list_sessions = session_subparsers.add_parser(
        'list',
        help='List all audit sessions',
        formatter_class=AtlasHelpFormatter,
        description='Display all available audit sessions'
    )
    list_sessions.add_argument(
        '--limit',
        type=int,
        default=20,
        help='Maximum number of sessions to display (default: 20)'
    )
    list_sessions.add_argument(
        '--sort',
        choices=['date', 'name', 'status'],
        default='date',
        help='Sort sessions by field (default: date)'
    )

    # session show
    show = session_subparsers.add_parser(
        'show',
        help='Show session details',
        formatter_class=AtlasHelpFormatter,
        description='Display detailed information about a specific session'
    )
    show.add_argument(
        'session_name',
        nargs='?',
        help='Session name (uses active session if not specified)'
    )
    show.add_argument(
        '--files',
        action='store_true',
        help='List all files in the session'
    )

    # session active
    active = session_subparsers.add_parser(
        'active',
        help='Show or set the active session',
        formatter_class=AtlasHelpFormatter,
        description='Manage the currently active session'
    )
    active.add_argument(
        'session_name',
        nargs='?',
        help='Session to set as active (shows current if not specified)'
    )

    # session switch (alias for session active)
    switch = session_subparsers.add_parser(
        'switch',
        help='Switch the active session (alias for active)',
        formatter_class=AtlasHelpFormatter,
        description='Interactively switch the active session'
    )
    switch.add_argument(
        'session_name',
        nargs='?',
        help='Session to set as active (interactive picker if not specified)'
    )

    # session edit
    edit = session_subparsers.add_parser(
        'edit',
        help='Edit session bindings (environment, ruleset, profile)',
        formatter_class=AtlasHelpFormatter,
        description='Change a session\'s environment, ruleset, or profile (only before capture)'
    )
    edit.add_argument(
        'session_name',
        nargs='?',
        help='Session to edit (uses active session if not specified)'
    )

    # session export
    export = session_subparsers.add_parser(
        'export',
        help='Export session as ZIP for delivery',
        formatter_class=AtlasHelpFormatter,
        description='Package session data for customer delivery'
    )
    export.add_argument(
        'session_name',
        nargs='?',
        help='Session name (uses active session if not specified)'
    )
    export.add_argument(
        '--output',
        help='Output file path (default: current directory)'
    )
    export.add_argument(
        '--format',
        choices=['zip', 'tar.gz'],
        default='zip',
        help='Archive format (default: zip)'
    )
    export.add_argument(
        '--include-debug',
        action='store_true',
        help='Include debug logs and raw data'
    )
    export.add_argument(
        '--no-redact',
        dest='redact',
        action='store_false',
        default=True,
        help='Include raw capture data in export'
    )

    # session delete
    delete = session_subparsers.add_parser(
        'delete',
        help='Delete an audit session',
        formatter_class=AtlasHelpFormatter,
        description='Permanently remove a session and all its data'
    )
    delete.add_argument(
        'session_name',
        nargs='?',
        help='Session name to delete (interactive if not specified)'
    )
    delete.add_argument(
        '--force',
        action='store_true',
        help='Skip confirmation prompt'
    )

    # session diff
    diff = session_subparsers.add_parser(
        'diff',
        help='Compare two sessions side-by-side',
        formatter_class=AtlasHelpFormatter,
        description='Generate a comparison report between two audit sessions'
    )
    diff.add_argument(
        'baseline_session',
        nargs='?',
        help='Baseline session name (interactive if not specified)'
    )
    diff.add_argument(
        'latest_session',
        nargs='?',
        help='Latest session name (interactive if not specified)'
    )
    diff.add_argument(
        '--output',
        help='Output file path for diff report'
    )
    diff.add_argument(
        '--no-open',
        action='store_true',
        help='Do not automatically open the diff report'
    )

    # session repair
    repair = session_subparsers.add_parser(
        'repair',
        help='Backfill missing metadata on older sessions',
        formatter_class=AtlasHelpFormatter,
        description='Scan sessions and fill in organization name, environment, '
                    'ruleset, and profile from capture data. Safe to run multiple times.'
    )
    repair.add_argument(
        'session_name',
        nargs='?',
        help='Repair a specific session (all sessions if not specified)'
    )
    repair.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would change without writing anything'
    )

# =================================================
# RULESET Command Group
# =================================================

def _add_ruleset_commands(subparsers):
    """Add ruleset management commands"""

    ruleset_parser = subparsers.add_parser(
        'ruleset',
        help='Manage validation rulesets (load, profiles, rules)',
        formatter_class=AtlasHelpFormatter,
        description='Load, view, and manage validation rulesets'
    )

    ruleset_subparsers = ruleset_parser.add_subparsers(
        dest='ruleset_action',
        title='Ruleset Actions',
        help='Action to perform',
        metavar='<action>',
        required=True
    )

    # ruleset list
    list_rulesets = ruleset_subparsers.add_parser(
        'list',
        help='List available rulesets',
        formatter_class=AtlasHelpFormatter,
        description='Display all available validation rulesets'
    )

    # ruleset load
    load = ruleset_subparsers.add_parser(
        'load',
        help='Load and activate a ruleset',
        formatter_class=AtlasHelpFormatter,
        description='Set a ruleset as the active configuration'
    )
    load.add_argument(
        'ruleset_id',
        help='Ruleset identifier to load'
    )
    load.add_argument(
        '--profile',
        help='Apply a profile overlay'
    )

    # ruleset info
    info = ruleset_subparsers.add_parser(
        'info',
        help='Show ruleset details (rule count, categories, version)',
        formatter_class=AtlasHelpFormatter,
        description='Display detailed information about a specific ruleset'
    )
    info.add_argument(
        'ruleset_id',
        nargs='?',
        help='Ruleset identifier (uses active ruleset if not specified)'
    )

    # ruleset active
    active = ruleset_subparsers.add_parser(
        'active',
        help='Show active ruleset',
        formatter_class=AtlasHelpFormatter,
        description='Display the currently active ruleset'
    )

    # ruleset clear
    clear = ruleset_subparsers.add_parser(
        'clear',
        help='Clear active ruleset',
        formatter_class=AtlasHelpFormatter,
        description='Deactivate the current ruleset'
    )

    # ruleset setup
    ruleset_subparsers.add_parser(
        'setup',
        help='Interactive ruleset and profile selection',
        formatter_class=AtlasHelpFormatter,
        description='Interactively select a ruleset and profile in one step'
    )

    # ruleset switch (alias for ruleset setup)
    ruleset_subparsers.add_parser(
        'switch',
        help='Interactive ruleset and profile selection (alias for setup)',
        formatter_class=AtlasHelpFormatter,
        description='Interactively select a ruleset and profile in one step'
    )

    # ruleset rules
    rules = ruleset_subparsers.add_parser(
        'rules',
        help='Display all rules in a ruleset (filterable by category/severity)',
        formatter_class=AtlasHelpFormatter,
        description='List all validation rules in formatted table output'
    )
    rules.add_argument(
        'ruleset_id',
        nargs='?',
        help='Ruleset to display (uses active if not specified)'
    )
    rules.add_argument(
        '--category',
        help='Filter by category (e.g., redis, platform, mongo)'
    )
    rules.add_argument(
        '--severity',
        choices=['critical', 'warning', 'info'],
        help='Filter by severity level'
    )

    # ruleset profile
    profile_parser = ruleset_subparsers.add_parser(
        'profile',
        help='Manage ruleset profiles (set, list, clear)',
        formatter_class=AtlasHelpFormatter,
        description='Set, clear, and list ruleset profile overlays'
    )

    profile_subparsers = profile_parser.add_subparsers(
        dest='profile_action',
        title='Profile Actions',
        help='Action to perform',
        metavar='<action>',
        required=True
    )

    # ruleset profile set
    profile_set = profile_subparsers.add_parser(
        'set',
        help='Set the active profile',
        formatter_class=AtlasHelpFormatter,
        description='Apply a profile overlay to the active ruleset'
    )
    profile_set.add_argument(
        'profile_id',
        help='Profile identifier to activate'
    )

    # ruleset profile clear
    profile_subparsers.add_parser(
        'clear',
        help='Clear the active profile',
        formatter_class=AtlasHelpFormatter,
        description='Remove the profile overlay, reverting to the base ruleset'
    )

    # ruleset profile list
    profile_subparsers.add_parser(
        'list',
        help='List available profiles',
        formatter_class=AtlasHelpFormatter,
        description='Display all available profile overlays'
    )

    # ruleset profile active
    profile_subparsers.add_parser(
        'active',
        help='Show the active profile',
        formatter_class=AtlasHelpFormatter,
        description='Display the currently active profile'
    )

# =================================================
# CONFIG Command Group
# =================================================

def _add_config_commands(subparsers):
    """Add configuration management commands"""

    config_parser = subparsers.add_parser(
        'config',
        help='Manage Atlas configuration (credentials, topology, themes)',
        formatter_class=AtlasHelpFormatter,
        description='Initialize and manage Atlas configuration'
    )

    config_subparsers = config_parser.add_subparsers(
        dest='config_action',
        title='Config Actions',
        help='Action to perform',
        metavar='<action>',
        required=True
    )

    # config init
    init = config_subparsers.add_parser(
        'init',
        help='Run the interactive setup wizard',
        formatter_class=AtlasHelpFormatter,
        description='Run the interactive configuration setup wizard'
    )

    # config show
    show = config_subparsers.add_parser(
        'show',
        help='Display current configuration (secrets masked)',
        formatter_class=AtlasHelpFormatter,
        description='Show current configuration (redacted)'
    )
    show.add_argument(
        '--full',
        action='store_true',
        help='Show complete configuration (WARNING: includes secrets)'
    )

    # config credentials
    credentials = config_subparsers.add_parser(
        'credentials',
        help='View and update stored credentials',
        formatter_class=AtlasHelpFormatter,
        description='Manage credentials'
    )

    # config theme
    config_subparsers.add_parser(
        'theme',
        help='Switch color theme interactively',
        formatter_class=AtlasHelpFormatter,
        description='Interactively select a color theme for Atlas'
    )

    # config deployment
    config_subparsers.add_parser(
        'deployment',
        help='Reconfigure deployment topology',
        formatter_class=AtlasHelpFormatter,
        description='Update the deployment topology without changing credentials'
    )

    # config architecture
    config_subparsers.add_parser(
        'architecture',
        help='Collect or update architecture information',
)


# =================================================
# ENV Command Group
# =================================================

def _add_env_commands(subparsers):
    """Add environment management commands"""

    env_parser = subparsers.add_parser(
        'env',
        help='Manage deployment environments (create, switch, edit, list)',
        formatter_class=AtlasHelpFormatter,
        description='Create, switch, edit, and manage named deployment environments'
    )

    env_subparsers = env_parser.add_subparsers(
        dest='env_action',
        title='Environment Actions',
        help='Action to perform',
        metavar='<action>',
        required=True
    )

    # env list
    env_subparsers.add_parser(
        'list',
        help='List all environments and show which is active',
        formatter_class=AtlasHelpFormatter,
        description='Display all configured environments and show which is active'
    )

    # env switch
    switch = env_subparsers.add_parser(
        'switch',
        help='Switch the active environment',
        formatter_class=AtlasHelpFormatter,
        description='Set a different environment as the active deployment target'
    )
    switch.add_argument(
        'env_name',
        nargs='?',
        help='Environment name to switch to (interactive if not specified)'
    )

    # env show
    show = env_subparsers.add_parser(
        'show',
        help='Show environment details',
        formatter_class=AtlasHelpFormatter,
        description='Display the full configuration for an environment'
    )
    show.add_argument(
        'env_name',
        nargs='?',
        help='Environment name (shows active if not specified)'
    )

    # env create
    create = env_subparsers.add_parser(
        'create',
        help='Create a new environment (interactive wizard)',
        formatter_class=AtlasHelpFormatter,
        description='Run the interactive wizard to create a new environment'
    )
    create.add_argument(
        'env_name',
        nargs='?',
        help='Environment name (prompted if not specified)'
    )
    create.add_argument(
        '--from',
        dest='from_env',
        metavar='ENV',
        help='Copy from an existing environment'
    )

    # env remove
    remove = env_subparsers.add_parser(
        'remove',
        help='Remove an environment',
        formatter_class=AtlasHelpFormatter,
        description='Permanently delete an environment file'
    )
    remove.add_argument(
        'env_name',
        help='Environment name to remove'
    )
    remove.add_argument(
        '--force',
        action='store_true',
        help='Skip confirmation prompt'
    )

    # env edit
    edit = env_subparsers.add_parser(
        'edit',
        help='Edit an environment\'s settings interactively',
        formatter_class=AtlasHelpFormatter,
        description='Modify connection details and settings for an existing environment'
    )
    edit.add_argument(
        'env_name',
        nargs='?',
        help='Environment name to edit (edits active environment if not specified)'
    )

# =================================================
# PREFLIGHT Command
# =================================================

def _add_preflight_command(subparsers):
    """Add preflight connectivity check command"""

    preflight = subparsers.add_parser(
        'preflight',
        help='Run preflight connectivity checks (SSH, API, databases)',
        formatter_class=AtlasHelpFormatter,
        description='Test connectivity to all configured services'
    )

# =================================================
# GUIDE Command
# =================================================

def _add_guide_commands(subparsers):
    """Add guide viewer command"""

    guide = subparsers.add_parser(
        'guide',
        help='View the built-in user guide',
        formatter_class=AtlasHelpFormatter,
        description='Views the README in Rich Markdown viewer'
    )

# =================================================
# CUSTOMER DATA MANAGEMENT (Mult-Tenant Mode Only)
# =================================================

def _add_customer_commands(subparsers):
    """Add customer management commands"""

    customer_parser = subparsers.add_parser(
        'customer',
        help='[argparse.special][Itential] [/argparse.special] Manage customer capture data (import, validate, report)',
        formatter_class=AtlasHelpFormatter,
        description='Import, validate, and report on customer configuration data'
        )

    customer_subparsers = customer_parser.add_subparsers(
        dest="customer_command",
        metavar='<command>',
        title="Customer Commands",
        required=True,
    )

    # customer import
    customer_import = customer_subparsers.add_parser(
        'import',
        help='Import a customer capture JSON file',
        formatter_class=AtlasHelpFormatter,
        description='Import and validate a customer configuration capture file'
    )

    customer_import.add_argument(
        "capture_file",
        type=validate_capture_file,
        help="Path to capture JSON file"
    )

    customer_import.add_argument(
        "--organization",
        help="Organization name (extracted from capture file if not provided)"
    )

    customer_import.add_argument(
        "--session",
        help="Session name (defaults to current quarter: YYYY-QN)"
    )

    # customer list
    customer_subparsers.add_parser(
        'list',
        help="List all customer organizations",
        formatter_class=AtlasHelpFormatter,
        description='Display all organizations with imported capture data'
    )

    # customer sessions
    customer_sessions = customer_subparsers.add_parser(
        'sessions',
        help='List sessions for an organization',
        formatter_class=AtlasHelpFormatter,
        description='Show all capture sessions for a specific organization'
    )

    customer_sessions.add_argument(
        "organization",
        help="Organization name"
    )

    # customer validate
    customer_validate = customer_subparsers.add_parser(
        'validate',
        help='Validate a customer session against the active ruleset',
        formatter_class=AtlasHelpFormatter,
        description='Run validation rules against customer capture data'
    )

    customer_validate.add_argument(
        'organization',
        help='Organization name'
    )

    customer_validate.add_argument(
        'session',
        help='Session name (e.g., 2026-q1)'
    )

    # customer report
    customer_report = customer_subparsers.add_parser(
        'report',
        help='Generate HTML report for a customer session',
        formatter_class=AtlasHelpFormatter,
        description='Create HTML validation report for customer session'
    )

    customer_report.add_argument(
        'organization',
        help='Organization name')

    customer_report.add_argument(
        'session',
        help='Session name (e.g., 2026-q1)'
    )

# =================================================
# Helper: Extract Command Path
# =================================================

def get_command_path(args: argparse.Namespace) -> tuple[str, ...]:
    """
    Extract the command path from parsed arguments.

    Returns:
        Tuple representing the command hierarchy
        e.g., ('session', 'run', 'capture')

    Examples:
        >>> args = parser.parse_args(['session', 'run', 'capture'])
        >>> get_command_path(args)
        ('session', 'run', 'capture')

        >>> args = parser.parse_args(['ruleset', 'list'])
        >>> get_command_path(args)
        ('ruleset', 'list')
    """
    path = []

    # Primary command
    if hasattr(args, 'command') and args.command:
        path.append(args.command)

        # Session subcommand
        if args.command == 'session' and hasattr(args, 'session_action'):
            path.append(args.session_action)
            if args.session_action == 'run' and hasattr(args, 'stage'):
                path.append(args.stage)

        # Ruleset subcommand
        elif args.command == 'ruleset' and hasattr(args, 'ruleset_action'):
            path.append(args.ruleset_action)
            # Profile sub-subcommand
            if args.ruleset_action == 'profile' and hasattr(args, 'profile_action'):
                path.append(args.profile_action)

        # Config subcommand
        elif args.command == 'config' and hasattr(args, 'config_action'):
            path.append(args.config_action)

        # Env subcommand
        elif args.command == 'env' and hasattr(args, 'env_action'):
            path.append(args.env_action)

        # Customer subcommand
        elif args.command == 'customer' and hasattr(args, 'customer_command'):
            path.append(args.customer_command)

    return tuple(path)


# =================================================
# Validation Helpers
# =================================================

def validate_session_name(name: str) -> bool:
    """
    Validate session name format.

    Rules:
        - Alphanumeric, hyphens, underscores only
        - 3-64 characters
        - Cannot start/end with hyphen
    """
    import re
    pattern = r'^[a-zA-Z0-9]([a-zA-Z0-9_-]{1,62}[a-zA-Z0-9])?$'
    return bool(re.match(pattern, name))

def validate_ruleset_id(ruleset_id: str) -> bool:
    """
    Validate ruleset ID format.

    Rules:
        - Alphanumeric and hyphens only
        - Lowercase
    """
    import re
    pattern = r'^[a-z0-9-]+$'
    return bool(re.match(pattern, ruleset_id))

def validate_capture_file(filepath):
    """Validate capture file exists and is readable"""
    path = Path(filepath)
    if not path.exists():
        raise argparse.ArgumentTypeError(f"File not found: {filepath}")
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"Not a file: {filepath}")
    if path.suffix.lower() not in ['.json']:
        raise argparse.ArgumentTypeError(f"Must be .json file: {filepath}")

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise argparse.ArgumentTypeError(
                f"Capture file must contain a JSON object, got {type(data).__name__}"
            )
    except json.JSONDecodeError as e:
        raise argparse.ArgumentTypeError(
            f"Invalid JSON in {filepath}: {e.msg} (line {e.lineno})"
        )

    return str(path.absolute())


# Main Entry Point
def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = create_parser()
    return parser.parse_args(args)
