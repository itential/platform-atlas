"""Capture engine and collector modules for Platform Atlas"""
from platform_atlas.capture.models import CaptureState, ModuleResult, SystemFacts

def run_capture(*args, **kwargs):
    """Run Capture Engine"""
    from platform_atlas.capture.capture_engine import run_capture as _rc
    return _rc(*args, **kwargs)

__all__ = ["run_capture", "CaptureState", "ModuleResult", "SystemFacts"]
