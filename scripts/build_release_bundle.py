"""Build a curated, non-PyPI SEO Writer source bundle."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import tarfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = ["README.md", "LICENSE", "NOTICE", "pyproject.toml", "uv.lock"]
TREE_FILES = {
    "src": {".py"},
    "skills": {".md", ".yaml", ".yml"},
    "examples": {".md", ".yaml", ".yml", ".json"},
    "tests": {".py", ".yaml", ".yml", ".json"},
}
EXTRA_FILES = [
    "bin/seo-writer",
    "scripts/build_release_bundle.py",
    "scripts/release_smoke.py",
    "docs/ARCHITECTURE.md",
    "docs/AUDIT.md",
    "docs/MIGRATION.md",
    "docs/RELEASE.md",
    "docs/WHY.md",
]
FORBIDDEN_SUFFIXES = {".csv", ".db", ".sqlite", ".sqlite3", ".pt", ".onnx", ".safetensors", ".bin"}


def project_version() -> str:
    source = (PROJECT_ROOT / "src" / "seo_writer" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([^"]+)"$', source, flags=re.MULTILINE)
    if not match:
        raise RuntimeError("cannot resolve SEO Writer version")
    return match.group(1)


def release_files() -> list[Path]:
    files = [PROJECT_ROOT / name for name in ROOT_FILES + EXTRA_FILES]
    for directory, suffixes in TREE_FILES.items():
        files.extend(
            path
            for path in (PROJECT_ROOT / directory).rglob("*")
            if path.is_file() and path.suffix.lower() in suffixes
        )
    missing = [str(path.relative_to(PROJECT_ROOT)) for path in files if not path.is_file()]
    if missing:
        raise RuntimeError(f"release files missing: {', '.join(missing)}")
    unique = sorted(set(files), key=lambda path: path.relative_to(PROJECT_ROOT).as_posix())
    for path in unique:
        relative = path.relative_to(PROJECT_ROOT)
        if path.suffix.lower() in FORBIDDEN_SUFFIXES or path.name.startswith(".env"):
            raise RuntimeError(f"forbidden release file: {relative}")
    return unique


def add_bytes(archive: tarfile.TarFile, name: str, payload: bytes, mode: int = 0o644) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mode = mode
    info.mtime = 0
    archive.addfile(info, io.BytesIO(payload))


def build(output_dir: Path) -> dict[str, object]:
    version = project_version()
    bundle_root = f"seo-writer-{version}"
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / f"{bundle_root}-source.tar.gz"
    files = release_files()
    manifest = {
        "product": "SEO Writer Skill + local CLI",
        "version": version,
        "distribution": "source-bundle-not-pypi",
        "files": [path.relative_to(PROJECT_ROOT).as_posix() for path in files],
        "customer_data_included": False,
        "credentials_included": False,
        "model_weights_included": False,
    }

    with tarfile.open(archive_path, "w:gz") as archive:
        for path in files:
            relative = path.relative_to(PROJECT_ROOT).as_posix()
            info = archive.gettarinfo(str(path), arcname=f"{bundle_root}/{relative}")
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            with path.open("rb") as source:
                archive.addfile(info, source)
        add_bytes(
            archive,
            f"{bundle_root}/RELEASE-MANIFEST.json",
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
        )

    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    checksum_path = archive_path.with_suffix(archive_path.suffix + ".sha256")
    checksum_path.write_text(f"{digest}  {archive_path.name}\n", encoding="utf-8")
    return {
        "archive": str(archive_path),
        "sha256": digest,
        "checksum_file": str(checksum_path),
        "file_count": len(files) + 1,
        **{key: manifest[key] for key in ("product", "version", "distribution")},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "dist")
    args = parser.parse_args()
    print(json.dumps(build(args.output_dir.resolve()), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
