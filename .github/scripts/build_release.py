#!/usr/bin/env python3
"""CI 用：从仓库根目录构建 AstrBot 插件 zip（放进 dist/，不进 zip 自身）。

zip 内保留一层 ``astrbot_plugin_douyin_companion/`` 目录，
解压后可直接放进 ``AstrBot/data/plugins/``。
"""
from __future__ import annotations

import argparse
import hashlib
import zipfile
from pathlib import Path

PLUGIN_NAME = "astrbot_plugin_douyin_companion"
ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "dist"

EXCLUDE_DIRS = {
    ".git", ".github", "__pycache__", ".pytest_cache", ".ruff_cache",
    "dist", "build", "data", "logs", "media", "cookies",
}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".zip", ".tar.gz", ".sqlite3", ".log"}
EXCLUDE_FILES = {".DS_Store", "Thumbs.db", ".env"}

# 白名单：Release zip 只打 AstrBot 插件运行时需要的文件，
# 排除 CI 工作流、上游哨兵脚本、docs/ 报告与开发产物。
ALLOWED = {
    ".gitattributes", ".gitignore", "_conf_schema.json", "CHANGELOG.md",
    "LICENSE", "LICENSE-UPSTREAM", "NOTICE.md", "README.md", "UPSTREAM_SYNC.md",
    "metadata.yaml", "main.py", "requirements.txt",
    "douyin_core/__init__.py", "douyin_core/backend.py", "douyin_core/errors.py",
    "douyin_core/gate.py", "douyin_core/maintenance.py", "douyin_core/media.py",
    "douyin_core/normalize.py", "douyin_core/redact.py", "douyin_core/store.py",
    "douyin_core/understanding.py",
    "tests/test_offline.py", "tools/dy_query_adapter.py",
}


def build(version: str) -> tuple[Path, Path]:
    OUT.mkdir(parents=True, exist_ok=True)
    name = f"{PLUGIN_NAME}-v{version}"
    zip_path = OUT / f"{name}.zip"
    files: list[tuple[Path, Path]] = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        if path.name in EXCLUDE_FILES or any(str(rel).endswith(s) for s in EXCLUDE_SUFFIXES):
            continue
        if rel.as_posix() not in ALLOWED:
            continue
        files.append((path, rel))
    if not any(rel.name == "metadata.yaml" for _, rel in files):
        raise SystemExit("缺少 metadata.yaml，拒绝打包")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path, rel in files:
            zf.write(path, f"{PLUGIN_NAME}/{rel.as_posix()}")
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    sum_path = OUT / f"{zip_path.name}.sha256"
    sum_path.write_text(f"{digest}  {zip_path.name}\n", encoding="utf-8")
    return zip_path, sum_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default="0.1.0")
    args = parser.parse_args()
    zip_path, sum_path = build(args.version)
    print(f"ZIP : {zip_path} ({zip_path.stat().st_size} bytes)")
    print(f"SHA : {sum_path}")
    with zipfile.ZipFile(zip_path) as zf:
        print(f"FILES: {len(zf.namelist())}")
        for entry in zf.namelist()[:6]:
            print("  ", entry)


if __name__ == "__main__":
    main()
