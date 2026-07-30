"""Standalone Unicode-path model-save integration probe for COMSOL 6.4."""

from pathlib import Path
import sys
from tempfile import mkdtemp

import mph

ROOT = Path(__file__).parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from development_kit.scripts.acceptance_cleanup import CleanupRecorder


def _cleanup_probe(client, output_file, output_dir, result) -> int:
    cleanup = CleanupRecorder(result)
    if client is not None:
        cleanup.run("client_clear", client.clear, expose_result=False)
        cleanup.run("client_disconnect", client.disconnect, expose_result=False)
    cleanup.run(
        "output_unlink",
        lambda: output_file.unlink(missing_ok=True),
        expose_result=False,
    )
    cleanup.run("output_dir_rmdir", output_dir.rmdir, expose_result=False)
    return cleanup.finalize()


def main() -> None:
    """Save one model through clientapi to a Chinese path and clean it up."""
    client = None
    root = Path(__file__).resolve().parent
    output_dir = Path(mkdtemp(prefix="comsol_unicode_smoke_", dir=root))
    output_file = output_dir / "模型.mph"
    result = {"success": False}
    primary_error = None
    try:
        client = mph.Client(version="6.4")
        model = client.create("UnicodeSaveSmoke")
        model.java.save(str(output_file.resolve()))
        if not output_file.is_file() or output_file.stat().st_size == 0:
            raise AssertionError(f"COMSOL did not create a non-empty file: {output_file}")
        print("unicode save OK:", output_file, output_file.stat().st_size)
        result["success"] = True
    except BaseException as exc:
        primary_error = exc
    finally:
        exit_code = _cleanup_probe(client, output_file, output_dir, result)
    if primary_error is not None:
        raise primary_error
    if exit_code != 0:
        raise RuntimeError("Unicode save probe cleanup did not complete")


if __name__ == "__main__":
    main()
