"""Version naming utilities for model version management."""

from datetime import datetime
from pathlib import Path

from .runtime_paths import default_runtime_dir


def default_models_root() -> Path:
    """Return the writable runtime directory for versioned model copies."""
    return default_runtime_dir() / "models"


def get_model_directory(
    model_name: str,
    base_path: str | Path | None = None,
) -> Path:
    """
    Get the directory path for a model.
    
    Args:
        model_name: Name of the model.
        base_path: Optional model-storage root. Defaults to the runtime
            directory's ``models`` subdirectory.
        
    Returns:
        Path to the model directory
    """
    # Clean model name (remove .mph extension if present)
    clean_name = Path(model_name).stem
    root = Path(base_path).expanduser() if base_path is not None else default_models_root()
    return root / clean_name


def generate_version_name(base_name: str) -> str:
    """
    Generate a versioned name with timestamp suffix.
    
    Args:
        base_name: Original model name (with or without .mph extension)
        
    Returns:
        Versioned name with timestamp, e.g., "model_20260215_143022.mph"
    """
    path = Path(base_name)
    stem = path.stem
    extension = path.suffix or ".mph"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{stem}_{timestamp}{extension}"


def generate_version_path(model_name: str, base_path: str | Path | None = None) -> str:
    """
    Generate a versioned file path with timestamp suffix.
    Uses structured path: <runtime>/models/{model_name}/{model_name}_{timestamp}.mph
    
    Args:
        model_name: Name of the model (used for directory)
        base_path: Optional directory in which to create the model subdirectory.
            When omitted, uses the runtime directory's ``models`` subdirectory.
        
    Returns:
        Versioned file path with timestamp
    """
    # Clean model name
    clean_name = Path(model_name).stem
    
    # Get model directory
    model_dir = get_model_directory(clean_name, base_path=base_path)
    model_dir.mkdir(parents=True, exist_ok=True)
    
    # Generate versioned filename
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    versioned_name = f"{clean_name}_{timestamp}.mph"
    
    return str(model_dir / versioned_name)


def generate_latest_path(
    model_name: str,
    base_path: str | Path | None = None,
) -> str:
    """
    Generate path for the 'latest' version of a model.
    Uses structured path: <runtime>/models/{model_name}/{model_name}_latest.mph
    
    Args:
        model_name: Name of the model.
        base_path: Optional model-storage root. Defaults to the runtime
            directory's ``models`` subdirectory.
        
    Returns:
        Path for the latest version
    """
    clean_name = Path(model_name).stem
    model_dir = get_model_directory(clean_name, base_path=base_path)
    model_dir.mkdir(parents=True, exist_ok=True)
    return str(model_dir / f"{clean_name}_latest.mph")


def list_model_versions(
    model_name: str,
    base_path: str | Path | None = None,
) -> list[str]:
    """
    List all versions of a model.
    
    Args:
        model_name: Name of the model.
        base_path: Optional model-storage root. Defaults to the runtime
            directory's ``models`` subdirectory.
        
    Returns:
        List of version file paths, sorted by modification time (newest first)
    """
    model_dir = get_model_directory(model_name, base_path=base_path)
    if not model_dir.exists():
        return []
    
    versions = []
    for f in model_dir.glob("*.mph"):
        if "_latest" not in f.name:
            versions.append(str(f))
    
    # Sort by modification time, newest first
    versions.sort(key=lambda x: Path(x).stat().st_mtime, reverse=True)
    return versions


def parse_version_info(name: str) -> dict | None:
    """
    Parse version information from a model name.
    
    Args:
        name: Model name to parse
        
    Returns:
        Dict with 'base_name', 'timestamp', 'datetime' or None if not versioned
    """
    path = Path(name)
    stem = path.stem
    
    import re
    match = re.match(r"^(.+)_(\d{8}_\d{6})$", stem)
    if match:
        base_name = match.group(1)
        timestamp_str = match.group(2)
        try:
            dt = datetime.strptime(timestamp_str, "%Y%m%d_%H%M%S")
            return {
                "base_name": base_name,
                "timestamp": timestamp_str,
                "datetime": dt,
            }
        except ValueError:
            pass
    return None
