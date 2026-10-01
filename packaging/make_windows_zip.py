from __future__ import annotations

import argparse
import zipfile
from pathlib import Path


def build_zip(source: Path, output: Path) -> None:
    source = source.resolve()
    output = output.resolve()
    if not (source / "NexusCore.exe").is_file():
        raise SystemExit("NexusCore.exe is missing from the package directory.")
    if not (source / "Source" / ".git" / "HEAD").is_file():
        raise SystemExit("Bundled Source/.git metadata is missing.")
    if not (source / "backend" / "ChatNexus.Backend.exe").is_file():
        raise SystemExit("ChatNexus.Backend.exe is missing from the package.")

    forbidden = ("pythonnet", "python.runtime.dll", "pywebview")
    for path in source.rglob("*"):
        relative_lower = path.relative_to(source).as_posix().lower()
        if any(token in relative_lower for token in forbidden):
            raise SystemExit(f"Forbidden legacy desktop dependency found in package: {relative_lower}")

    output.unlink(missing_ok=True)
    with zipfile.ZipFile(
        output,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
        allowZip64=True,
    ) as archive:
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source).as_posix()
            name = f"ChatNexus/{relative}"
            if path.is_dir():
                info = zipfile.ZipInfo(name.rstrip("/") + "/")
                info.create_system = 0
                info.external_attr = 0x10
                archive.writestr(info, b"")
            elif path.is_file():
                info = zipfile.ZipInfo(name)
                info.create_system = 0
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0
                archive.writestr(
                    info,
                    path.read_bytes(),
                    compress_type=zipfile.ZIP_DEFLATED,
                    compresslevel=6,
                )

    with zipfile.ZipFile(output) as archive:
        bad = archive.testzip()
        if bad:
            raise SystemExit(f"ZIP CRC validation failed at {bad}")
        required = {
            "ChatNexus/NexusCore.exe",
            "ChatNexus/runtime/llama/llama-server.exe",
            "ChatNexus/backend/ChatNexus.Backend.exe",
            "ChatNexus/Source/.git/HEAD",
        }
        missing = required.difference(archive.namelist())
        if missing:
            raise SystemExit(f"ZIP is missing required entries: {sorted(missing)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("output")
    args = parser.parse_args()
    build_zip(Path(args.source), Path(args.output))
