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

    # === TIER BADGES (Itential brand colors — never themed) ===
    # Standard mode is presented in Itential Blue (#1B93D2);
    # Extended mode in Itential Orange (#FF6633); SaaS mode in
    # Itential Pink (#C5258F). Holding these constant across every
    # theme preset keeps the Mode pill readable as a brand cue,
    # not a UI accent.
    tier_standard: str = "#1B93D2"
    tier_extended: str = "#FF6633"
    tier_saas: str = "#C5258F"

    spinner_color: str = "#00D7FF"
    link_color: str = "#60A5FA"
    link_hover: str = "#93C5FD"


# === THEME PRESETS ===
ATLAS_HORIZON_DARK = Theme()

ATLAS_HORIZON_CORE = Theme(
    # === CORE COLORS === (matches report.html's paper-and-ink accent: brass)
    primary="#C98847",
    primary_dim="#A3672E",
    primary_glow="#E8B87F",

    secondary="#1B93D2",
    secondary_dim="#136C9C",

    accent="#F3EFE3",
    accent_soft="#FBF8F0",

    # === STATUS COLORS === (report.html --status-* muted semantic family)
    success="#4C7A3D",
    success_glow="#7FA968",
    success_dim="#375C2C",

    error="#B23B2E",
    error_glow="#D6685C",
    error_dim="#8A2E24",

    warning="#96811F",
    warning_glow="#C2AC4A",
    warning_dim="#6E5E16",

    info="#1B93D2",
    info_glow="#5AB4E0",
    info_dim="#136C9C",

    # === TEXT HIERARCHY === (report.html .darkzone text scale)
    text_primary="#F3EFE3",
    text_secondary="#B9BFCF",
    text_dim="#7D869C",
    text_muted="#565E72",
    text_ghost="#3D4356",

    # === BACKGROUNDS & SURFACES === (report.html .darkzone navy scale)
    bg_primary="#0D1424",
    bg_secondary="#1C2942",
    bg_elevated="#232F4A",
    bg_input="#2A3554",

    # === BORDERS & DIVIDERS ===
    border_primary="#C98847",
    border_secondary="#1B93D2",
    border_dim="#2E3A56",
    border_ghost="#1C2942",

    # === PROGRESS & INDICATORS ===
    progress_complete="#C98847",
    progress_remaining="#2E3A56",
    link_color="#C98847",

    # === SPECIAL EFFECTS ===
    glow_cyan="#1B93D2",
    glow_purple="#7A3B63",
    shadow="#000000",

    # === PANEL TINTS ===
    tint_primary="#241C10",
    tint_secondary="#0E1C28",
    tint_accent="#1E1B14",
    tint_success="#111C10",
    tint_warning="#1E1908",
    tint_error="#20100E",
    tint_info="#0C1830",
    tint_neutral="#121826",

    # === HEADER / BANNER ===
    banner_bg="#0D1424",
    banner_fg="#C98847",
    banner_rule="#2E3A56",
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
    # Light-terminal theme: pure white background, near-black text, rich ink
    # colors throughout. No pastels. "glow" variants are the EMPHASIS shade —
    # one stop darker than the base so labels/tips punch on white, not wash out.

    # === CORE COLORS ===
    # Deep navy primary — authoritative ink against white
    primary="#0C4A6E",
    primary_dim="#082F49",
    primary_glow="#0369A1",

    secondary="#4C1D95",
    secondary_dim="#3B0764",

    accent="#9D174D",
    accent_soft="#831843",

    # === STATUS COLORS ===
    success="#14532D",
    success_glow="#15803D",
    success_dim="#052E16",

    error="#7F1D1D",
    error_glow="#991B1B",
    error_dim="#450A0A",

    warning="#78350F",
    warning_glow="#92400E",
    warning_dim="#451A03",

    info="#1E3A8A",
    info_glow="#1D4ED8",
    info_dim="#172554",

    # === TEXT HIERARCHY ===
    # Near-black primary down to mid-gray for ghost — all readable on white
    text_primary="#0F172A",
    text_secondary="#1E293B",
    text_dim="#334155",
    text_muted="#475569",
    text_ghost="#64748B",

    # === BACKGROUNDS & SURFACES ===
    bg_primary="#FFFFFF",
    bg_secondary="#F8FAFC",
    bg_elevated="#F1F5F9",
    bg_input="#E2E8F0",

    # === BORDERS & DIVIDERS ===
    border_primary="#0C4A6E",
    border_secondary="#4C1D95",
    border_dim="#CBD5E1",
    border_ghost="#E2E8F0",

    # === PROGRESS & INDICATORS ===
    progress_complete="#0C4A6E",
    progress_remaining="#CBD5E1",
    progress_success="#14532D",

    # === SEVERITY INDICATORS ===
    severity_critical="#7F1D1D",
    severity_warning="#78350F",
    severity_info="#1E3A8A",

    # === SPECIAL EFFECTS ===
    glow_cyan="#0C4A6E",
    glow_purple="#4C1D95",
    shadow="#64748B",

    # === SEMANTIC COLORS ===
    badge_new="#4C1D95",
    badge_deprecated="#7F1D1D",
    badge_beta="#78350F",

    spinner_color="#0C4A6E",
    link_color="#1E3A8A",
    link_hover="#1D4ED8",

    # === PANEL TINTS ===
    # Barely-off-white tints — just enough hue to differentiate, no pastels
    tint_primary="#F0F9FF",
    tint_secondary="#F5F3FF",
    tint_accent="#FFF1F2",
    tint_success="#F0FDF4",
    tint_warning="#FFFBEB",
    tint_error="#FEF2F2",
    tint_info="#EFF6FF",
    tint_neutral="#F8FAFC",

    # === HEADER / BANNER ===
    # Sky-blue tinted banner — clearly distinct from the white page but light
    # enough that all the dark navy/accent colors remain readable inside it.
    banner_bg="#DBEAFE",
    banner_fg="#0C4A6E",
    banner_rule="#93C5FD",
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

ATLAS_HORIZON_ATLAS = Theme(
    # === CORE COLORS ===
    # Bioluminescent blue-green primary — the deep-ocean "living light" signature
    primary="#00FFB3",
    primary_dim="#00B87A",
    primary_glow="#4DFFD4",

    # Ocean blue secondary
    secondary="#0099DD",
    secondary_dim="#006699",

    # Electric teal accent
    accent="#00E5FF",
    accent_soft="#80F4FF",

    # === STATUS COLORS ===
    success="#00DD88",
    success_glow="#33FFAA",
    success_dim="#00AA66",

    error="#FF4466",
    error_glow="#FF7799",
    error_dim="#CC2244",

    warning="#FFCC00",
    warning_glow="#FFE066",
    warning_dim="#CC9900",

    info="#44AAFF",
    info_glow="#77CCFF",
    info_dim="#2277DD",

    # === TEXT HIERARCHY ===
    text_primary="#C8E8FF",
    text_secondary="#90C4E8",
    text_dim="#4A7A9A",
    text_muted="#2A4E6A",
    text_ghost="#162A3A",

    # === BACKGROUNDS & SURFACES ===
    bg_primary="#050D1A",
    bg_secondary="#08111F",
    bg_elevated="#0F2040",
    bg_input="#142840",

    # === BORDERS & DIVIDERS ===
    border_primary="#00FFB3",
    border_secondary="#0099DD",
    border_dim="#0A2A3A",
    border_ghost="#061520",

    # === PROGRESS & INDICATORS ===
    progress_complete="#00FFB3",
    progress_remaining="#0A2A3A",
    progress_success="#00DD88",

    # === SEVERITY INDICATORS ===
    severity_critical="#FF4466",
    severity_warning="#FFCC00",
    severity_info="#44AAFF",

    # === SPECIAL EFFECTS ===
    glow_cyan="#00FFB3",
    glow_purple="#0099DD",
    shadow="#020810",

    # === PANEL TINTS ===
    tint_primary="#041820",     # Blue-green tinted
    tint_secondary="#041020",   # Ocean blue tinted
    tint_accent="#042030",      # Teal tinted
    tint_success="#041C14",     # Green tinted
    tint_warning="#1A1800",     # Yellow tinted
    tint_error="#1A0A12",       # Red tinted
    tint_info="#041428",        # Info blue tinted
    tint_neutral="#081018",     # Neutral dark

    # === HEADER / BANNER ===
    banner_bg="#030A12",
    banner_fg="#00FFB3",
    banner_rule="#0A2440",

    # === SEMANTIC COLORS ===
    badge_new="#0099DD",
    badge_deprecated="#FF4466",
    badge_beta="#FFCC00",

    spinner_color="#00FFB3",
    link_color="#44AAFF",
    link_hover="#77CCFF",
)

THEME_REGISTRY: dict[str, Theme] = {
    "horizon-atlas": ATLAS_HORIZON_ATLAS,
    "horizon-dark": ATLAS_HORIZON_DARK,
    "horizon-core": ATLAS_HORIZON_CORE,
    "horizon-prism": ATLAS_HORIZON_PRISM,
    "horizon-light": ATLAS_HORIZON_LIGHT,
    "dracula": ATLAS_DRACULA,
}

DEFAULT_THEME_ID = "horizon-atlas"

def get_theme_by_id(theme_id: str) -> Theme:
    """Look up a theme by its config ID. Falls back to default"""
    return THEME_REGISTRY.get(theme_id, ATLAS_HORIZON_ATLAS)

def list_theme_ids() -> list[str]:
    """Return all available theme IDs"""
    return list(THEME_REGISTRY.keys())
