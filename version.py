# -*- coding: utf-8 -*-
"""版本与更新源配置 —— 发版时只改这里"""

__version__ = "1.1.0"
APP_NAME = "A+ 抓取工作台"

# GitHub 仓库（owner/repo）。更新检查走它的 Release。
GITHUB_REPO = "disky1998/aplus-workbench"
GITHUB_BRANCH = "main"

# 更新资产文件名（Release 里上传的 exe 名）
ASSET_NAME = "APlusWorkbench.exe"

RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RAW_VERSION_URL = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}/version.json"
RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases/latest"


def parse_version(v: str):
    """1.2.3 / v1.2.3 → (1,2,3)，无法解析返回 ()"""
    import re
    m = re.search(r"(\d+(?:\.\d+)*)", str(v or ""))
    if not m:
        return ()
    return tuple(int(x) for x in m.group(1).split("."))


def is_newer(latest: str, current: str = __version__) -> bool:
    a, b = parse_version(latest), parse_version(current)
    if not a:
        return False
    if not b:
        return True
    n = max(len(a), len(b))
    a = a + (0,) * (n - len(a))
    b = b + (0,) * (n - len(b))
    return a > b
