"""Standalone Unicode-path model-save integration probe for COMSOL 6.4."""

from pathlib import Path
from tempfile import mkdtemp

import mph


def main() -> None:
    """Save one model through clientapi to a Chinese path and clean it up."""
    client = None
    root = Path(__file__).resolve().parent
    output_dir = Path(mkdtemp(prefix="comsol_unicode_smoke_", dir=root))
    output_file = output_dir / "模型.mph"
    try:
        client = mph.Client(version="6.4")
        model = client.create("UnicodeSaveSmoke")
        model.java.save(str(output_file.resolve()))
        if not output_file.is_file() or output_file.stat().st_size == 0:
            raise AssertionError(f"COMSOL did not create a non-empty file: {output_file}")
        print("unicode save OK:", output_file, output_file.stat().st_size)
    finally:
        if client is not None:
            try:
                client.clear()
            except Exception:
                pass
            try:
                client.disconnect()
            except Exception:
                pass
        output_file.unlink(missing_ok=True)
        try:
            output_dir.rmdir()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    main()
