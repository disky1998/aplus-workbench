# -*- coding: utf-8 -*-
"""生成 version.json（仓库根 + Release 资产），供更新检查回退读取"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import version as V

HERE = Path(__file__).resolve().parent
EXE = HERE / "dist" / V.ASSET_NAME
NOTES = HERE / "RELEASE_NOTES.md"
OUT = HERE / "version.json"


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    if not EXE.exists():
        raise SystemExit(f"找不到 {EXE}，先跑 build_exe.bat")
    tag = f"v{V.__version__}"
    base = f"https://github.com/{V.GITHUB_REPO}/releases/download/{tag}"
    notes = NOTES.read_text(encoding="utf-8") if NOTES.exists() else ""
    size = EXE.stat().st_size

    data = {
        "app": V.APP_NAME,
        "version": V.__version__,
        "tag": tag,
        "published": time.strftime("%Y-%m-%d"),
        "notes": notes,
        "size": size,
        "sha256": sha256(EXE),
        "download_url": f"{base}/{V.ASSET_NAME}",
        "html_url": f"https://github.com/{V.GITHUB_REPO}/releases/tag/{tag}",
        "assets": [{
            "name": V.ASSET_NAME,
            "size": size,
            "url": f"{base}/{V.ASSET_NAME}",
            "is_exe": True,
        }],
    }
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[ok] version.json -> v{V.__version__}  {size / 1048576:.1f} MB")
    print(f"      sha256 {data['sha256'][:16]}…")


if __name__ == "__main__":
    main()
