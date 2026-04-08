"""Platform Atlas - Configuration auditing for Itential Automation Platform"""

def validate(*args, **kwargs):
    """Run Validation against captured data"""
    from platform_atlas.validation.validation_engine import validate as _validate
    return _validate(*args, **kwargs)

def validate_from_files(*args, **kwargs):
    """Run validation from capture and ruleset files"""
    from platform_atlas.validation.validation_engine import validate_from_files as _validate_from_files
    return _validate_from_files(*args, **kwargs)

__all__ = ["validate", "validate_from_files"]
