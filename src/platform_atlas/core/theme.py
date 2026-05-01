"""Atlas Theme System and Theme Registry"""

from dataclasses import dataclass

@dataclass(frozen=True)
class Theme:
    """Themes for Platform Atlas"""

    # === CORE COLORS ===
    primary: str = "#00D7FF"
    primary_dim: str = "#0094B3"
    primary_glow: str = "#4DFFFF"

    secondary: str = "#A78BFA"
    secondary_dim: str = "#7C3AED"

    accent: str = "#FF6B9D"
    accent_soft: str = "#FFB4D1"

    # === STATUS COLORS ===
    success: str = "#10B981"
    success_glow: str = "#34D399"
    success_dim: str = "#059669"

    error: str = "#EF4444"
    error_glow: str = "#F87171"
    error_dim: str = "#DC2626"

    warning: str = "#F59E0B"
    warning_glow: str = "#FBBF24"
    warning_dim: str = "#D97706"

    info: str = "#3B82F6"
    info_glow: str = "#60A5FA"
    info_dim: str = "#2563EB"

    # === TEXT HIERARCHY ===
    text_primary: str = "#F8F9FA"
    text_secondary: str = "#E5E7EB"
    text_dim: str = "#9CA3AF"
    text_muted: str = "#6B7280"
    text_ghost: str = "#4B5563"

    # === BACKGROUNDS & SURFACES ===
    bg_primary: str = "#0F0F14"
    bg_secondary: str = "#1A1A24"
    bg_elevated: str = "#252532"
    bg_input: str = "#2D2D3A"

    # === BORDERS & DIVIDERS ===
    border_primary: str = "#00D7FF"
    border_secondary: str = "#A78BFA"
    border_dim: str = "#374151"
    border_ghost: str = "#1F2937"

    # === PROGRESS & INDICATORS ===
    progress_complete: str = "#00D7FF"
    progress_remaining: str = "#374151"
    progress_success: str = "#10B981"

    # === SEVERITY INDICATORS ===
    severity_critical: str = "#DC2626"
    severity_warning: str = "#F59E0B"
    severity_info: str = "#3B82F6"

    # === SPECIAL EFFECTS ===
    glow_cyan: str = "#4DFFFF"
    glow_purple: str = "#C4B5FD"
    shadow: str = "#000000"

    # === PANEL TINTS (subtle tinted backgrounds for dashboard panels) ===
    tint_primary: str = "#0A1E2A"      # Cyan-tinted
    tint_secondary: str = "#15102A"    # Purple-tinted
    tint_accent: str = "#1F0A1A"       # Pink-tinted
    tint_success: str = "#0A1F16"      # Green-tinted
    tint_warning: str = "#1F1A0A"      # Amber-tinted
    tint_error: str = "#1F0A0A"        # Red-tinted
    tint_info: str = "#0A1230"         # Blue-tinted
    tint_neutral: str = "#111822"      # Neutral dark

    # === HEADER / BANNER ===
    banner_bg: str = "#0D2137"         # Deep blue banner background
    banner_fg: str = "#00D7FF"         # Banner foreground
    banner_rule: str = "#1B4B6D"       # Horizontal rule under banner

    # === SEMANTIC COLORS ===
    badge_new: str = "#8B5CF6"
    badge_deprecated: str = "#DC2626"
    badge_beta: str = "#F59E0B"

    spinner_color: str = "#00D7FF"
    link_color: str = "#60A5FA"
    link_hover: str = "#93C5FD"


# === THEME PRESETS ===
ATLAS_HORIZON_DARK = Theme()

ATLAS_HORIZON_CORE = Theme(
    # === CORE COLORS ===
    primary="#F08787",
    primary_dim="#C46A6A",
    primary_glow="#FFACA0",

    secondary="#FEE2AD",
    secondary_dim="#D4B878",

    accent="#A8BF45",
    accent_soft="#C8D98A",

    # === STATUS COLORS ===
    success="#A8BF45",
    success_glow="#C2D66B",
    success_dim="#849830",

    error="#FF6B6B",
    error_glow="#FF9E9E",
    error_dim="#D94444",

    warning="#FFB86A",
    warning_glow="#FECE94",
    warning_dim="#D4903A",

    info="#7ABAC8",
    info_glow="#A2D4DE",
    info_dim="#5899A8",

    # === TEXT HIERARCHY ===
    text_primary="#FEF0DC",
    text_secondary="#FFDDBA",
    text_dim="#C49E7A",
    text_muted="#8A7058",
    text_ghost="#5C4535",

    # === BACKGROUNDS & SURFACES ===
    bg_primary="#0F0304",
    bg_secondary="#1A0808",
    bg_elevated="#261010",
    bg_input="#321A1A",

    # === BORDERS & DIVIDERS ===
    border_primary="#F08787",
    border_secondary="#FEE2AD",
    border_dim="#3D2018",
    border_ghost="#2A1210",

    # === PROGRESS & INDICATORS ===
    progress_complete="#F08787",
    progress_remaining="#3D2018",
    link_color="#FEE2AD",

    # === SPECIAL EFFECTS ===
    glow_cyan="#7ABAC8",
    glow_purple="#FEE2AD",
    shadow="#000000",

    # === PANEL TINTS ===
    tint_primary="#1F0C0C",
    tint_secondary="#1F1808",
    tint_accent="#141A08",
    tint_success="#141A08",
    tint_warning="#1F1508",
    tint_error="#1F0808",
    tint_info="#0C1618",
    tint_neutral="#170A08",

    # === HEADER / BANNER ===
    banner_bg="#1F0C0C",
    banner_fg="#F08787",
    banner_rule="#3D1A1A",
)

ATLAS_HORIZON_PRISM = Theme(
    # === CORE COLORS ===
    primary="#4EC9B0",
    primary_dim="#3A9987",
    primary_glow="#7EDDC8",

    secondary="#C586C0",
    secondary_dim="#9B59A0",

    accent="#E8A468",
    accent_soft="#F0C59A",

    # === STATUS COLORS ===
    success="#98C379",
    success_glow="#B5D89E",
    success_dim="#7AA85E",

    error="#E06C75",
    error_glow="#EF9AA0",
    error_dim="#BE3E4A",

    warning="#E5C07B",
    warning_glow="#F0D9A0",
    warning_dim="#C9A24E",

    info="#61AFEF",
    info_glow="#8CC8F5",
    info_dim="#3A8FD4",

    # === TEXT HIERARCHY ===
    text_primary="#DCE0E8",
    text_secondary="#BAC2D0",
    text_dim="#8891A5",
    text_muted="#5C6478",
    text_ghost="#3E4455",

    # === BACKGROUNDS & SURFACES ===
    bg_primary="#1A1B2E",
    bg_secondary="#21223A",
    bg_elevated="#2A2C46",
    bg_input="#313450",

    # === BORDERS & DIVIDERS ===
    border_primary="#4EC9B0",
    border_secondary="#C586C0",
    border_dim="#3A3D56",
    border_ghost="#282A40",

    # === PROGRESS & INDICATORS ===
    progress_complete="#4EC9B0",
    progress_remaining="#3A3D56",
    progress_success="#98C379",

    # === SEVERITY INDICATORS ===
    severity_critical="#E06C75",
    severity_warning="#E5C07B",
    severity_info="#61AFEF",

    # === SPECIAL EFFECTS ===
    glow_cyan="#7EDDC8",
    glow_purple="#D9A8D6",
    shadow="#0D0E1A",

    # === SEMANTIC COLORS ===
    badge_new="#C586C0",
    badge_deprecated="#E06C75",
    badge_beta="#E5C07B",

    spinner_color="#4EC9B0",
    link_color="#61AFEF",
    link_hover="#8CC8F5",

    # === PANEL TINTS ===
    tint_primary="#0E2220",
    tint_secondary="#1E1228",
    tint_accent="#221A10",
    tint_success="#122010",
    tint_warning="#221E10",
    tint_error="#221012",
    tint_info="#0E1628",
    tint_neutral="#1E2038",

    # === HEADER / BANNER ===
    banner_bg="#0E2824",
    banner_fg="#4EC9B0",
    banner_rule="#2A4A44",
)

ATLAS_HORIZON_LIGHT = Theme(
    # === CORE COLORS ===
    primary="#0891B2",
    primary_dim="#06748E",
    primary_glow="#22D3EE",

    secondary="#7C3AED",
    secondary_dim="#6D28D9",

    accent="#DB2777",
    accent_soft="#F472B6",

    # === STATUS COLORS ===
    success="#059669",
    success_glow="#34D399",
    success_dim="#047857",

    error="#DC2626",
    error_glow="#F87171",
    error_dim="#B91C1C",

    warning="#D97706",
    warning_glow="#FBBF24",
    warning_dim="#B45309",

    info="#2563EB",
    info_glow="#60A5FA",
    info_dim="#1D4ED8",

    # === TEXT HIERARCHY ===
    text_primary="#1F2937",
    text_secondary="#374151",
    text_dim="#4B5563",
    text_muted="#6B7280",
    text_ghost="#9CA3AF",

    # === BACKGROUNDS & SURFACES ===
    bg_primary="#FFFFFF",
    bg_secondary="#F9FAFB",
    bg_elevated="#F3F4F6",
    bg_input="#E5E7EB",

    # === BORDERS & DIVIDERS ===
    border_primary="#0891B2",
    border_secondary="#7C3AED",
    border_dim="#D1D5DB",
    border_ghost="#E5E7EB",

    # === PROGRESS & INDICATORS ===
    progress_complete="#0891B2",
    progress_remaining="#D1D5DB",
    progress_success="#059669",

    # === SEVERITY INDICATORS ===
    severity_critical="#DC2626",
    severity_warning="#D97706",
    severity_info="#2563EB",

    # === SPECIAL EFFECTS ===
    glow_cyan="#22D3EE",
    glow_purple="#A78BFA",
    shadow="#9CA3AF",

    # === SEMANTIC COLORS ===
    badge_new="#7C3AED",
    badge_deprecated="#DC2626",
    badge_beta="#D97706",

    spinner_color="#0891B2",
    link_color="#2563EB",
    link_hover="#3B82F6",

    # === PANEL TINTS ===
    tint_primary="#E8F8FC",
    tint_secondary="#F0EAFF",
    tint_accent="#FFF0F6",
    tint_success="#E8FFF4",
    tint_warning="#FFF8E8",
    tint_error="#FFF0F0",
    tint_info="#EEF4FF",
    tint_neutral="#F3F4F6",

    # === HEADER / BANNER ===
    banner_bg="#E0F7FA",
    banner_fg="#0891B2",
    banner_rule="#B2EBF2",
)

ATLAS_DRACULA = Theme(
    # === CORE COLORS ===
    # Pink is Dracula's defining color — keywords, primary UI actions
    primary="#FF79C6",
    primary_dim="#E04FA0",
    primary_glow="#FF9DD9",

    # Purple — secondary/supporting elements, reserved words
    secondary="#BD93F9",
    secondary_dim="#9B6FE0",

    # Cyan — accent/highlight, types and support structures
    accent="#8BE9FD",
    accent_soft="#B8F0FF",

    # === STATUS COLORS ===
    success="#50FA7B",          # Dracula Green — functions/methods
    success_glow="#7AFCA0",
    success_dim="#2DD45C",

    error="#FF5555",            # Dracula Red — errors, deletion
    error_glow="#FF8080",
    error_dim="#E03030",

    warning="#FFB86C",          # Dracula Orange — constants/numbers
    warning_glow="#FFCC94",
    warning_dim="#E09040",

    info="#8BE9FD",             # Dracula Cyan — informational
    info_glow="#B8F0FF",
    info_dim="#5EC8E0",

    # === TEXT HIERARCHY ===
    text_primary="#F8F8F2",     # Dracula Foreground
    text_secondary="#E0DEDB",   # Slightly dimmed foreground
    text_dim="#6272A4",         # Dracula Comment — subdued text
    text_muted="#565A6E",       # Between comment and selection
    text_ghost="#44475A",       # Dracula Selection — very muted

    # === BACKGROUNDS & SURFACES ===
    bg_primary="#282A36",       # Dracula Background
    bg_secondary="#21222C",     # Dark surface variant (sidebar, backdrop)
    bg_elevated="#343746",      # Light surface variant (cards, panels)
    bg_input="#44475A",         # Dracula Selection — interactive/input areas

    # === BORDERS & DIVIDERS ===
    border_primary="#FF79C6",   # Pink — primary borders
    border_secondary="#BD93F9", # Purple — secondary borders
    border_dim="#44475A",       # Selection — dim borders
    border_ghost="#343746",     # Light surface — ghost borders

    # === PROGRESS & INDICATORS ===
    progress_complete="#FF79C6",
    progress_remaining="#44475A",
    progress_success="#50FA7B",

    # === SEVERITY INDICATORS ===
    severity_critical="#FF5555",
    severity_warning="#FFB86C",
    severity_info="#8BE9FD",

    # === SPECIAL EFFECTS ===
    glow_cyan="#8BE9FD",        # Dracula Cyan
    glow_purple="#BD93F9",      # Dracula Purple
    shadow="#191A21",           # Darkest Dracula surface variant

    # === PANEL TINTS (dark Dracula-tinted panel backgrounds) ===
    tint_primary="#2D2132",     # Pink-tinted
    tint_secondary="#261F3A",   # Purple-tinted
    tint_accent="#182A30",      # Cyan-tinted
    tint_success="#162A1E",     # Green-tinted
    tint_warning="#2A2318",     # Orange-tinted
    tint_error="#2A1E1E",       # Red-tinted
    tint_info="#1A2233",        # Cyan/blue-tinted
    tint_neutral="#2A2B38",     # Neutral dark

    # === HEADER / BANNER ===
    banner_bg="#21222C",        # Darker Dracula surface
    banner_fg="#FF79C6",        # Pink
    banner_rule="#44475A",      # Selection — rule separator

    # === SEMANTIC COLORS ===
    badge_new="#BD93F9",
    badge_deprecated="#FF5555",
    badge_beta="#FFB86C",

    spinner_color="#FF79C6",
    link_color="#8BE9FD",       # Cyan — links
    link_hover="#B8F0FF",       # Lighter cyan — hover
)

THEME_REGISTRY: dict[str, Theme] = {
    "horizon-dark": ATLAS_HORIZON_DARK,
    "horizon-core": ATLAS_HORIZON_CORE,
    "horizon-prism": ATLAS_HORIZON_PRISM,
    "horizon-light": ATLAS_HORIZON_LIGHT,
    "dracula": ATLAS_DRACULA,
}

DEFAULT_THEME_ID = "horizon-prism"

def get_theme_by_id(theme_id: str) -> Theme:
    """Look up a theme by its config ID. Falls back to default"""
    return THEME_REGISTRY.get(theme_id, ATLAS_HORIZON_DARK)

def list_theme_ids() -> list[str]:
    """Return all available theme IDs"""
    return list(THEME_REGISTRY.keys())
