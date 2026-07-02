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

import sys
import json
import argparse
import platform as _platform
from pathlib import Path
from rich_argparse import RichHelpFormatter

from platform_atlas.core import ui
from platform_atlas.core._version import __build__, __version__

theme = ui.theme

# =================================================
# Version Action
# =================================================

class _VersionAction(argparse.Action):
    """Custom --version action that includes system info."""

    def __init__(self, option_strings, dest=argparse.SUPPRESS, default=argparse.SUPPRESS, help=None):
        super().__init__(option_strings=option_strings, dest=dest, default=default, nargs=0, help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        py_version = sys.version.split()[0]
        py_path = sys.executable
        os_name = _platform.system()
        os_release = _platform.release()
        machine = _platform.machine()
        print(f"version: {__version__}")
        print(f"build:   {__build__}")
        print(f"python:  {py_version} ({py_path})")
        print(f"os:      {os_name} {os_release} ({machine})")
        parser.exit()


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
        action=_VersionAction,
        help='Show version and system information'
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
        '--tier',
        dest='tier_override',
        choices=['standard', 'extended', 'saas'],
        help='Override the active tier for this command (standard | extended | saas)'
    )

    parser.add_argument(
        '--whats-new',
        dest='whats_new',
        action='store_true',
        help='Show the "What\'s New" screen for the current version'
    )

    parser.add_argument(
        '--plain',
        action='store_true',
        dest='plain',
        help='Enable plain/compatibility mode (ASCII output, no colors or Unicode). '
             'Saved to config on first use — never needed again after that.'
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
    _add_tier_commands(subparsers)
    _add_preflight_command(subparsers)
    _add_guide_commands(subparsers)
    _add_continuous_commands(subparsers)
    _add_fleet_commands(subparsers)
    _add_support_bundle_command(subparsers)

    return parser


# =================================================
# CONTINUOUS-AUDIT Command Group
# =================================================

# Allowed run cadences for continuous audit. Same set as the WebUI dropdown.
# Sub-hour intervals would hammer the platform and aren't useful for drift monitoring.
_CONTINUOUS_INTERVAL_CHOICES: dict[str, int] = {
    '1h':   3600,
    '2h':   7200,
    '6h':   21600,
    '12h':  43200,
    '24h':  86400,
    '1d':   86400,
    '7d':   604800,
    '1w':   604800,
    '30d':  2592000,
}


def _continuous_interval(raw: str) -> int:
    """argparse type for --interval: accepts '1h'/'2h'/.../'1w' or the equivalent seconds."""
    s = raw.strip().lower()
    if s in _CONTINUOUS_INTERVAL_CHOICES:
        return _CONTINUOUS_INTERVAL_CHOICES[s]
    # Also accept numeric seconds, but only if they match one of the allowed values
    try:
        n = int(s)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid interval {raw!r} — choose one of: "
            + ", ".join(_CONTINUOUS_INTERVAL_CHOICES.keys())
        ) from None
    if n in _CONTINUOUS_INTERVAL_CHOICES.values():
        return n
    raise argparse.ArgumentTypeError(
        f"interval {n}s not allowed — choose one of: "
        + ", ".join(f"{lbl} ({sec}s)" for lbl, sec in _CONTINUOUS_INTERVAL_CHOICES.items())
    )


def _add_continuous_commands(subparsers):
    """Add continuous-audit (drift monitoring) commands."""

    cont_parser = subparsers.add_parser(
        'continuous-audit',
        help='Schedule narrow Platform-only drift checks (per environment)',
        formatter_class=AtlasHelpFormatter,
        description=(
            'Continuous audit re-runs a narrow Platform-OAuth-only capture '
            'against the active ruleset on a schedule, diffing observed values '
            'against the previous run to surface drift as alerts.'
        ),
    )

    cont_subparsers = cont_parser.add_subparsers(
        dest='continuous_action',
        title='Continuous Actions',
        help='Action to perform',
        metavar='<action>',
        required=True,
    )

    cont_subparsers.add_parser(
        'run-once',
        help='Execute one continuous-audit cycle and write the JSON report',
        formatter_class=AtlasHelpFormatter,
        description='Run a single Platform-only capture + validate, then write the JSON report.',
    )

    cont_subparsers.add_parser(
        'status',
        help='Show enable state, last run, and alert counts',
        formatter_class=AtlasHelpFormatter,
        description='Show the continuous-audit state for the active environment.',
    )

    alerts_parser = cont_subparsers.add_parser(
        'alerts',
        help='List drift alerts for the active environment',
        formatter_class=AtlasHelpFormatter,
        description='List drift alerts (rules whose observed value changed since the previous run).',
    )
    alerts_parser.add_argument(
        '--severity',
        choices=['critical', 'high', 'warning', 'info'],
        help='Filter to a single severity level',
    )
    alerts_parser.add_argument(
        '--unacked',
        action='store_true',
        help='Show only unacknowledged alerts',
    )

    ack_parser = cont_subparsers.add_parser(
        'ack',
        help='Acknowledge a drift alert by ID, or all of them',
        formatter_class=AtlasHelpFormatter,
        description='Acknowledge an alert. Acked alerts re-open if drift recurs on the same rule.',
    )
    ack_parser.add_argument(
        'alert_id',
        nargs='?',
        help='Alert ID to acknowledge (omit when using --all)',
    )
    ack_parser.add_argument(
        '--all',
        dest='all_alerts',
        action='store_true',
        help='Acknowledge every unacked alert in this environment',
    )

    enable_parser = cont_subparsers.add_parser(
        'enable',
        help='Enable continuous audit for the active environment',
        formatter_class=AtlasHelpFormatter,
        description='Enable continuous audit on the active environment overlay.',
    )
    enable_parser.add_argument(
        '--interval',
        type=_continuous_interval,
        metavar='INTERVAL',
        help='Run cadence: 1h, 2h, 6h, 12h, 24h, 1w (default: 1h)',
    )
    enable_parser.add_argument(
        '--retain',
        type=int,
        metavar='RUNS',
        help='Number of run reports to keep on disk (default: 168)',
    )
    enable_parser.add_argument(
        '--env',
        dest='target_env',
        metavar='ENV',
        help='Environment name to enable continuous audit for (default: active environment)',
    )
    enable_parser.add_argument(
        '--ruleset',
        dest='ruleset_id',
        metavar='RULESET_ID',
        help='Ruleset ID to use for continuous audit (default: active ruleset)',
    )
    enable_parser.add_argument(
        '--profile',
        dest='profile_id',
        metavar='PROFILE_ID',
        help='Profile ID to apply (default: active profile)',
    )

    cont_subparsers.add_parser(
        'disable',
        help='Disable continuous audit for the active environment',
        formatter_class=AtlasHelpFormatter,
        description='Disable continuous audit on the active environment. History is preserved.',
    )

    # ── policy ─────────────────────────────────────────────────────────
    policy_parser = cont_subparsers.add_parser(
        'policy',
        help='Show or set the alert policy (any | regression)',
        formatter_class=AtlasHelpFormatter,
        description=(
            'Choose which drift events generate alerts and notifications. '
            '"any" surfaces every change (default). "regression" surfaces '
            'only PASS → FAIL transitions — the classic "something that was '
            'working just broke" alert. Drift history (events.ndjson) is '
            'always recorded in full regardless of this setting.'
        ),
    )
    policy_parser.add_argument(
        'policy_value',
        nargs='?',
        choices=['any', 'regression'],
        help='New policy. Omit to display the current setting.',
    )

    # ── watch subgroup ─────────────────────────────────────────────────
    watch_parser = cont_subparsers.add_parser(
        'watch',
        help='Manage the rule-number watchlist (alert only on listed rules)',
        formatter_class=AtlasHelpFormatter,
        description=(
            'When the watchlist is non-empty, only drift events for rules in '
            'the list generate alerts and notifications. Useful when only a '
            'handful of rules matter (e.g. "just watch PLAT-001 and PLAT-042").'
        ),
    )
    watch_subparsers = watch_parser.add_subparsers(
        dest='watch_action',
        title='Watch Actions',
        help='Action to perform',
        metavar='<action>',
        required=True,
    )

    watch_subparsers.add_parser(
        'list',
        help='Show the current watchlist',
        formatter_class=AtlasHelpFormatter,
    )

    watch_add = watch_subparsers.add_parser(
        'add',
        help='Add one or more rule numbers to the watchlist',
        formatter_class=AtlasHelpFormatter,
        description=(
            'Add rule numbers (e.g. PLAT-001 PLAT-042) to the watchlist. '
            'Rule numbers are case-insensitive; duplicates are ignored.'
        ),
    )
    watch_add.add_argument(
        'rules',
        nargs='+',
        metavar='RULE',
        help='Rule number(s) to add. Comma-separated values are also accepted.',
    )

    watch_remove = watch_subparsers.add_parser(
        'remove',
        help='Remove one or more rule numbers from the watchlist',
        formatter_class=AtlasHelpFormatter,
    )
    watch_remove.add_argument(
        'rules',
        nargs='+',
        metavar='RULE',
        help='Rule number(s) to remove.',
    )

    watch_subparsers.add_parser(
        'clear',
        help='Clear the watchlist (alert on all rules again)',
        formatter_class=AtlasHelpFormatter,
        description='Empty the watchlist. After this, every rule is eligible for alerts.',
    )

    # TODO: Log file watching — deferred to a later version of Atlas.
    # Uncomment this block (and matching sections in handlers/continuous.py,
    # engine.py, routes/continuous.py, and landing.html) to re-enable.
    #
    # lw_parser = cont_subparsers.add_parser(
    #     'log-watch',
    #     help='Manage log-pattern watches for error detection (Extended only)',
    #     formatter_class=AtlasHelpFormatter,
    #     description=(
    #         'When log watching is enabled, each continuous-audit run also tails '
    #         'Platform, webserver, and/or MongoDB logs and generates alerts when '
    #         'a configured pattern matches. Extended mode only (requires SSH).'
    #     ),
    # )
    # lw_subparsers = lw_parser.add_subparsers(
    #     dest='log_watch_action',
    #     title='Log-Watch Actions',
    #     help='Action to perform',
    #     metavar='<action>',
    #     required=True,
    # )
    # lw_subparsers.add_parser('list', help='List all configured log-watch entries',
    #                          formatter_class=AtlasHelpFormatter)
    # lw_subparsers.add_parser('enable', help='Enable log watching for the active environment',
    #                          formatter_class=AtlasHelpFormatter)
    # lw_subparsers.add_parser('disable', help='Disable log watching (watches are preserved)',
    #                          formatter_class=AtlasHelpFormatter)
    # lw_add = lw_subparsers.add_parser('add', help='Add a log-watch pattern entry',
    #                                   formatter_class=AtlasHelpFormatter,
    #                                   description='Add a pattern checked against log files on every audit run.')
    # lw_add.add_argument('pattern', help='Regex or literal keyword to match in log lines')
    # lw_add.add_argument('--name', dest='lw_name', metavar='NAME')
    # lw_add.add_argument('--id', dest='lw_id', metavar='ID')
    # lw_add.add_argument('--source', dest='lw_source',
    #                     choices=['platform', 'webserver', 'mongodb', 'any'], default='any')
    # lw_add.add_argument('--severity', choices=['critical', 'warning', 'info'], default='warning')
    # lw_add.add_argument('--threshold', dest='lw_threshold',
    #                     choices=['any', 'count', 'window'], default='any')
    # lw_add.add_argument('--count', dest='lw_count', type=int, default=1, metavar='N')
    # lw_add.add_argument('--window', dest='lw_window', type=int, default=60, metavar='MINUTES')
    # lw_remove = lw_subparsers.add_parser('remove', help='Remove a log-watch entry by ID',
    #                                      formatter_class=AtlasHelpFormatter)
    # lw_remove.add_argument('lw_id', metavar='ID', help='Watch ID to remove')

    # ── notify subgroup ────────────────────────────────────────────────
    notify_parser = cont_subparsers.add_parser(
        'notify',
        help='Manage outbound drift notification channels (Slack, webhook)',
        formatter_class=AtlasHelpFormatter,
        description=(
            'Manage Slack and generic-webhook channels that receive drift events '
            'when continuous audit detects an alert-state transition (new alert '
            'or re-opened acked alert).'
        ),
    )
    notify_subparsers = notify_parser.add_subparsers(
        dest='notify_action',
        title='Notify Actions',
        help='Action to perform',
        metavar='<action>',
        required=True,
    )

    notify_add = notify_subparsers.add_parser(
        'add',
        help='Add a Slack or webhook channel',
        formatter_class=AtlasHelpFormatter,
        description='Register a new outbound notification channel for the active (or chosen) environment.',
    )
    notify_add.add_argument(
        'channel_type',
        choices=['slack', 'webhook'],
        metavar='<type>',
        help='Channel type: slack | webhook',
    )
    notify_add.add_argument('url', metavar='URL', help='Channel URL (Slack incoming webhook or HTTPS endpoint)')
    notify_add.add_argument('--name', dest='channel_name', metavar='NAME', help='Human-friendly channel label')
    notify_add.add_argument('--id', dest='channel_id', metavar='ID', help='Stable channel ID (default: auto-generated)')
    notify_add.add_argument(
        '--header',
        dest='channel_headers',
        action='append',
        default=[],
        metavar='KEY=VALUE',
        help='Custom HTTP header for webhook channels (repeatable)',
    )
    notify_add.add_argument(
        '--secret',
        dest='channel_secret',
        metavar='SECRET',
        help='HMAC-SHA256 signing secret (webhook only — added as X-Atlas-Signature header)',
    )
    notify_add.add_argument(
        '--env', dest='target_env', metavar='ENV',
        help='Environment to manage channels for (default: active environment)',
    )

    notify_list = notify_subparsers.add_parser(
        'list',
        help='List configured notification channels',
        formatter_class=AtlasHelpFormatter,
    )
    notify_list.add_argument(
        '--env', dest='target_env', metavar='ENV',
        help='Environment to list channels for (default: active environment)',
    )

    notify_remove = notify_subparsers.add_parser(
        'remove',
        help='Remove a channel by ID',
        formatter_class=AtlasHelpFormatter,
    )
    notify_remove.add_argument('channel_id', metavar='ID', help='Channel ID to remove')
    notify_remove.add_argument(
        '--env', dest='target_env', metavar='ENV',
        help='Environment the channel belongs to (default: active environment)',
    )

    notify_test = notify_subparsers.add_parser(
        'test',
        help='Send a synthetic test payload to a channel',
        formatter_class=AtlasHelpFormatter,
    )
    notify_test.add_argument('channel_id', metavar='ID', help='Channel ID to test')
    notify_test.add_argument(
        '--env', dest='target_env', metavar='ENV',
        help='Environment the channel belongs to (default: active environment)',
    )


# =================================================
# FLEET Command Group
# =================================================

def _add_fleet_commands(subparsers):
    """Add fleet (multi-environment compliance overview) commands."""
    fleet_parser = subparsers.add_parser(
        'fleet',
        help='Multi-environment compliance overview from local cache',
        formatter_class=AtlasHelpFormatter,
        description=(
            'Aggregate the most recent locally-cached state for every configured '
            'environment — last capture, pass-rate, continuous-audit state, '
            'unacked alerts. Read-only; does not trigger captures.'
        ),
    )
    fleet_subparsers = fleet_parser.add_subparsers(
        dest='fleet_action',
        title='Fleet Actions',
        help='Action to perform',
        metavar='<action>',
        required=True,
    )

    status = fleet_subparsers.add_parser(
        'status',
        help='Show compliance overview across all environments',
        formatter_class=AtlasHelpFormatter,
        description='Render a table of every environment with its tier, last-capture age, pass rate, and drift alert counts.',
    )
    status.add_argument(
        '--json', dest='as_json', action='store_true',
        help='Emit machine-readable JSON instead of a Rich table',
    )


# =================================================
# TIER Command Group
# =================================================

def _add_tier_commands(subparsers):
    """Add tier management commands."""

    tier_parser = subparsers.add_parser(
        'tier',
        help='Show, set, upgrade, or downgrade the active tier',
        formatter_class=AtlasHelpFormatter,
        description=(
            'Manage Platform Atlas\'s mode tier:\n'
            '  • Standard — Platform OAuth + optional IAG4 (5-minute setup)\n'
            '  • Extended — Full audit including SSH, MongoDB, Redis, Kubernetes\n'
            '  • SaaS     — Single-gateway audit (GW4 or GW5); chosen per-environment at create time'
        ),
    )

    tier_subparsers = tier_parser.add_subparsers(
        dest='tier_action',
        title='Tier Actions',
        help='Action to perform',
        metavar='<action>',
        required=True,
    )

    tier_subparsers.add_parser(
        'show',
        help='Display the active tier and what is enabled',
        formatter_class=AtlasHelpFormatter,
        description='Show the current tier with a summary of enabled capability.',
    )

    set_parser = tier_subparsers.add_parser(
        'set',
        help='Set the global default tier',
        formatter_class=AtlasHelpFormatter,
        description='Persist a new global tier to ~/.atlas/config.json.',
    )
    set_parser.add_argument(
        'tier_value',
        nargs='?',
        choices=['standard', 'extended', 'saas'],
        help='Target tier (interactive picker if omitted; saas is per-environment only)',
    )

    tier_subparsers.add_parser(
        'upgrade',
        help='Upgrade Standard to Extended (interactive)',
        formatter_class=AtlasHelpFormatter,
        description='Interactive flow to switch from Standard to Extended Mode.',
    )

    tier_subparsers.add_parser(
        'downgrade',
        help='Downgrade Extended to Standard (interactive)',
        formatter_class=AtlasHelpFormatter,
        description='Interactive flow to switch from Extended to Standard Mode.',
    )

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
    create.add_argument(
        '--tier',
        choices=['standard', 'extended', 'saas'],
        dest='tier',
        help='Bind the session to a tier (defaults to the active config tier)'
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
    run.add_argument(
        '--skip-adapter-check',
        action='store_true',
        help='Skip the GitLab adapter version check during validation'
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
        '--log-since',
        metavar='DATE',
        default=None,
        help='Only include log entries on or after this date (YYYY-MM-DD). '
             'Enables date-range mode: uses grep instead of tail for all log types.'
    )
    run.add_argument(
        '--log-until',
        metavar='DATE',
        default=None,
        help='Only include log entries on or before this date (YYYY-MM-DD). '
             'Can be combined with --log-since for a specific window.'
    )
    run.add_argument(
        '--log-days',
        dest='log_days',
        type=int,
        default=None,
        metavar='N',
        help='Analyze logs from the last N days (1-30). '
             'Shorthand for --log-since; cannot be combined with --log-since.'
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
        '--unified',
        action='store_true',
        help='Also generate a single combined report: one standalone '
             'unified_report.html with Compliance, Operational, and Architecture '
             'as tabbed pages in the top bar (HTML only). Written alongside the '
             'classic report files without overwriting them.'
    )
    run.add_argument(
        '--debug-raw-capture',
        action='store_true',
        help='Also write 01_raw_capture.json — the full reshaped capture before '
             'ruleset filtering. Useful for tracing dot-notation paths when authoring '
             'new rules. Overrides the env\'s debug_export_raw_capture flag for this run.'
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
        description='Package a session (reports + report.json + metadata) into an '
                    'organization-named archive to attach to an Itential ER ticket'
    )
    export.add_argument(
        'session_name',
        nargs='?',
        help='Session name (prompts for the active or a recent session if omitted)'
    )
    export.add_argument(
        '--output',
        help='Output file path (default: ATLAS-<org>-<session>-<date> in current directory)'
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
        help='Also bundle troubleshooting files (session.log, 01_capture.json, debug.log)'
    )
    export.add_argument(
        '--no-redact',
        dest='redact',
        action='store_false',
        default=True,
        help='Alias for --include-debug (kept for backward compatibility)'
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

    # session prune
    prune = session_subparsers.add_parser(
        'prune',
        help='Prune old sessions (default: --older-than 30d --dry-run)',
        formatter_class=AtlasHelpFormatter,
        description=(
            'Delete sessions matching the specified filters. '
            'Dry-run is the default — pass --no-dry-run (and confirm) to delete for real. '
            'All filter flags AND together.'
        ),
    )
    prune.add_argument(
        '--older-than',
        dest='older_than',
        type=_continuous_interval,
        default=_CONTINUOUS_INTERVAL_CHOICES['30d'],
        metavar='DURATION',
        help='Prune sessions older than DURATION (e.g. 30d, 7d, 1w, 24h). Default: 30d',
    )
    prune.add_argument(
        '--keep-last',
        dest='keep_last',
        type=int,
        metavar='N',
        default=None,
        help='Keep the N most-recent sessions (by updated_at); prune the rest',
    )
    prune.add_argument(
        '--status',
        dest='prune_status',
        choices=['ok', 'warn', 'fail', 'aborted', 'empty'],
        default=None,
        metavar='STATUS',
        help='Only prune sessions with this final status (ok|warn|fail|aborted|empty)',
    )
    prune.add_argument(
        '--env',
        dest='prune_env',
        default=None,
        metavar='NAME',
        help='Only prune sessions bound to this environment',
    )
    prune.add_argument(
        '--dry-run',
        dest='dry_run',
        action='store_true',
        default=True,
        help='Show what would be deleted without removing anything (default)',
    )
    prune.add_argument(
        '--no-dry-run',
        dest='dry_run',
        action='store_false',
        help='Run for real (requires confirmation unless --yes is also passed)',
    )
    prune.add_argument(
        '--yes', '-y',
        dest='yes',
        action='store_true',
        help='Skip confirmation prompt (still respects --dry-run)',
    )

    # session trend
    trend = session_subparsers.add_parser(
        'trend',
        help='Show compliance trends across sessions',
        formatter_class=AtlasHelpFormatter,
        description='Display a category heat matrix of pass rates across sessions over time'
    )
    trend.add_argument(
        '--env',
        metavar='ENV',
        help='Environment to filter by (defaults to active environment)',
    )
    trend.add_argument(
        '--all-envs',
        dest='all_envs',
        action='store_true',
        help='Show sessions from all environments',
    )
    trend.add_argument(
        '--limit',
        type=int,
        default=20,
        metavar='N',
        help='Maximum number of sessions to include (default: 20, oldest→newest)',
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

    # ruleset profile rule-disable
    rule_disable = profile_subparsers.add_parser(
        'rule-disable',
        help='Disable a rule in the active profile',
        formatter_class=AtlasHelpFormatter,
        description='Add an enabled=false override for a rule in the active profile'
    )
    rule_disable.add_argument('rule_number', help='Rule number to disable (e.g. PLAT-001)')

    # ruleset profile rule-enable
    rule_enable = profile_subparsers.add_parser(
        'rule-enable',
        help='Re-enable a rule in the active profile',
        formatter_class=AtlasHelpFormatter,
        description='Remove the enabled=false override for a rule in the active profile'
    )
    rule_enable.add_argument('rule_number', help='Rule number to re-enable (e.g. PLAT-001)')

    # ruleset skip-rule
    skip_rule = ruleset_subparsers.add_parser(
        'skip-rule',
        help='Suppress a rule for the active environment',
        formatter_class=AtlasHelpFormatter,
        description=(
            "Add a rule number to the active environment's skip_rules list. "
            'Suppressed rules still run but appear as "Suppressed" in reports '
            'so it is clear the rule was intentionally silenced.'
        ),
    )
    skip_rule.add_argument('rule_number', help='Rule number to suppress (e.g. PLAT-001)')
    skip_rule.add_argument(
        '--reason',
        default=None,
        help='Justification for suppressing this rule (min 10 characters). Prompted interactively if omitted.',
    )

    # ruleset unskip-rule
    unskip_rule = ruleset_subparsers.add_parser(
        'unskip-rule',
        help='Remove a rule suppression for the active environment',
        formatter_class=AtlasHelpFormatter,
        description=(
            "Remove a rule number from the active environment's skip_rules list, "
            're-enabling it for validation and reports.'
        ),
    )
    unskip_rule.add_argument('rule_number', help='Rule number to restore (e.g. PLAT-001)')

    # ruleset update
    ruleset_subparsers.add_parser(
        'update',
        help='Check for and download ruleset updates',
        formatter_class=AtlasHelpFormatter,
        description=(
            'Fetch the ruleset manifest from GitHub, compare available versions '
            'against what is installed, and optionally download updates. '
            'Only downloads rulesets compatible with your current Atlas version.'
        ),
    )

    # ruleset sync
    sync = ruleset_subparsers.add_parser(
        'sync',
        help='Sync bundled rulesets and profiles into ~/.atlas (use --force to reset)',
        formatter_class=AtlasHelpFormatter,
        description=(
            'Copy bundled rulesets and profiles from the installed Atlas package '
            'into ~/.atlas. By default this runs the same version-aware sync as '
            'startup: new and strictly-newer files are copied and locally-newer '
            'rulesets are preserved. With --force, every local ruleset and profile '
            'is deleted first — including custom files and rulesets downloaded via '
            '"ruleset update" — and the bundled set is copied fresh. --force is '
            'destructive, takes no backup, and prompts for confirmation unless '
            '--yes is given.'
        ),
    )
    sync.add_argument(
        '--force',
        action='store_true',
        help='Full wipe: delete all local rulesets/profiles, then re-copy from source (destructive, no backup)',
    )
    sync.add_argument(
        '-y', '--yes',
        action='store_true',
        help='Skip the confirmation prompt (non-interactive use)',
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
    cred_store_group = credentials.add_mutually_exclusive_group()
    cred_store_group.add_argument(
        '--use-file-store',
        dest='cred_use_file_store',
        action='store_true',
        help='Switch this environment to the encrypted local file credential '
             'backend, then exit'
    )
    cred_store_group.add_argument(
        '--use-keyring',
        dest='cred_use_keyring',
        action='store_true',
        help='Switch this environment to the OS keyring credential backend, then exit'
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

    # config plain
    config_subparsers.add_parser(
        'plain',
        help='Enable or disable plain/compatibility mode',
        formatter_class=AtlasHelpFormatter,
        description='Toggle plain (compatibility) mode — disables colors, Unicode borders, '
                    'and ANSI codes for terminals that do not support Rich formatting.'
    )

    # config edit
    config_subparsers.add_parser(
        'edit',
        help='Edit individual configuration settings (behavior and connection timeouts)',
        formatter_class=AtlasHelpFormatter,
        description='Interactively change individual Atlas settings — manual input mode, log '
                    'retention, validation depth, and connection/request timeouts (SSH, '
                    'Platform API, Redis, MongoDB). Presents a grouped menu of safe options.'
    )

    # config doctor
    doctor = config_subparsers.add_parser(
        'doctor',
        help='Run a health check on the current Atlas configuration',
        formatter_class=AtlasHelpFormatter,
        description=(
            'Verify global config, active environment, credential store, '
            'platform/gateway URLs, ruleset, and SSH key path in one pass. '
            'Useful after setup or when a capture failed for an unclear reason.'
        ),
    )
    doctor.add_argument(
        '--json',
        dest='json',
        action='store_true',
        help='Emit results as JSON to stdout (machine-readable; no Rich output)',
    )
    doctor.add_argument(
        '--no-url-probes',
        dest='no_url_probes',
        action='store_true',
        help='Skip the slow TCP reachability checks for Platform and Gateway4 URLs',
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
    create.add_argument(
        '--from-file',
        dest='from_file',
        metavar='PATH',
        help='Create environment from a JSON file (skips the interactive wizard)'
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

    # env architecture
    arch = env_subparsers.add_parser(
        'architecture',
        help="Collect or edit an environment's architecture information",
        formatter_class=AtlasHelpFormatter,
        description='Open the architecture form (HTML or CLI prompts) to record deployment '
                    'architecture details for an environment. Answers are saved per-environment '
                    'and included in the audit report. Run this anytime, outside of a capture.'
    )
    arch.add_argument(
        'env_name',
        nargs='?',
        help='Environment name (uses the active environment if not specified)'
    )
    arch.add_argument(
        '--force',
        action='store_true',
        help='Re-walk every section, even ones already answered'
    )

    # env sockets
    sockets = env_subparsers.add_parser(
        'sockets',
        help='Check and manage ControlMaster SSH socket health',
        formatter_class=AtlasHelpFormatter,
        description=(
            'Show the status of every ControlMaster socket for an environment. '
            'Detects missing, stale (socket file exists but master not responding), '
            'and unconfigured nodes. Use --clean to remove stale socket files so '
            'fresh master connections can be opened.'
        ),
    )
    sockets.add_argument(
        'env_name',
        nargs='?',
        help='Environment name (uses the active environment if not specified)',
    )
    sockets.add_argument(
        '--clean',
        action='store_true',
        help='Remove stale socket files (socket exists but master is not responding)',
    )
    sockets.add_argument(
        '--open',
        action='store_true',
        dest='open_sockets',
        help='Automatically open master SSH connections (you will be prompted for credentials)',
    )
    sockets.add_argument(
        '--all',
        action='store_true',
        dest='show_all_nodes',
        help='Show all nodes including those with no SSH destination configured',
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
# SUPPORT-BUNDLE Command
# =================================================

def _add_support_bundle_command(subparsers):
    """Add the support-bundle diagnostic collection command."""
    sb = subparsers.add_parser(
        'support-bundle',
        help='Collect a diagnostic support bundle ZIP for triage',
        formatter_class=AtlasHelpFormatter,
        description=(
            'Collect Platform health endpoints, logs (Extended mode), and '
            'system info into a single ZIP file for support triage. '
            'Standard mode: API health endpoints + redacted config only. '
            'Extended mode: above + SSH-based logs and system facts.'
        ),
    )
    sb.add_argument(
        '--log-days',
        dest='log_days',
        type=int,
        default=None,
        metavar='N',
        help='Days of logs to collect, 1-30 (Extended only). '
             'Omit to be prompted; defaults to 7 under --yes.',
    )
    sb.add_argument(
        '--output',
        metavar='PATH',
        help='Output path for the ZIP file (default: ./atlas-support-bundle-<timestamp>.zip)',
    )
    sb.add_argument(
        '--yes', '-y',
        dest='yes',
        action='store_true',
        help='Skip confirmation prompt',
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

        # Tier subcommand
        elif args.command == 'tier' and hasattr(args, 'tier_action'):
            path.append(args.tier_action)

        # Fleet subcommand
        elif args.command == 'fleet' and hasattr(args, 'fleet_action'):
            path.append(args.fleet_action)

        # Continuous-audit subcommand
        elif args.command == 'continuous-audit' and hasattr(args, 'continuous_action'):
            path.append(args.continuous_action)
            # Three-level nested actions (notify add/list/remove/test,
            # watch add/list/remove/clear, log-watch add/remove/list/enable/disable).
            # Their argparse dests follow the ``<group>_action`` convention so we
            # can resolve them generically. log-watch uses log_watch_action.
            action_key = args.continuous_action.replace("-", "_") + "_action"
            nested_value = getattr(args, action_key, None)
            if nested_value:
                path.append(nested_value)

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
