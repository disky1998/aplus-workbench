# -*- coding: utf-8 -*-
"""一键发布：生成 version.json → 提交仓库 → 建 GitHub Release 并上传 exe

前置：gh auth login（本机已登录），version.py 里改好版本号，build_exe.bat 已产出 dist 里的 exe。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import version as V

HERE = Path(__file__).resolve().parent
TAG = f"v{V.__version__}"
EXE = HERE / "dist" / V.ASSET_NAME
NOTES = HERE / "RELEASE_NOTES.md"


def run(args, allow_fail=False, cwd=HERE):
    args = [str(a) for a in args]
    # 本机的 HTTP_PROXY/HTTPS_PROXY 会拦截 git 的 HTTPS 推送（静默 exit 128），显式绕开
    if args and args[0] == "git":
        args = ["git", "-c", "http.proxy=", "-c", "https.proxy="] + args[1:]
    print(f"\n$ {' '.join(args)}")
    p = subprocess.run(args, cwd=str(cwd), text=True,
                       encoding="utf-8", errors="replace",
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if p.stdout:
        print(p.stdout.rstrip())
    if p.returncode and not allow_fail:
        raise SystemExit(f"[失败] 退出码 {p.returncode}")
    return p


def _git(args, cwd=HERE):
    """不打印的 git 调用（同样绕开代理）"""
    return subprocess.run(["git", "-c", "http.proxy=", "-c", "https.proxy="] + list(args),
                          cwd=str(cwd), text=True, encoding="utf-8", errors="replace",
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


def main():
    if not EXE.exists():
        raise SystemExit(f"找不到 {EXE}\n先运行 build_exe.bat")

    print(f"=== 发布 {V.APP_NAME} {TAG} ===")

    # 1. version.json
    run([sys.executable, "make_release.py"])

    # 2. 提交源码
    run(["git", "init"], allow_fail=True)
    run(["git", "add", "-A"])
    run(["git", "commit", "-m", f"release {TAG}"], allow_fail=True)

    # 仓库不存在则创建（私有）
    rv = subprocess.run(["gh", "repo", "view", V.GITHUB_REPO], cwd=str(HERE),
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if rv.returncode != 0:
        print(f"\n[建仓] {V.GITHUB_REPO}（私有）")
        run(["gh", "repo", "create", V.GITHUB_REPO.split("/")[-1],
             "--private", "--source", ".", "--remote", "origin",
             "--description", f"{V.APP_NAME} · Amazon A+ 内容抓取"])

    remotes = _git(["remote"]).stdout
    if "origin" not in (remotes or "").split():
        run(["git", "remote", "add", "origin",
             f"https://github.com/{V.GITHUB_REPO}.git"])
    run(["git", "branch", "-M", V.GITHUB_BRANCH])
    run(["git", "push", "-u", "origin", V.GITHUB_BRANCH])

    # 3. Release：已存在就删掉重建
    exists = subprocess.run(["gh", "release", "view", TAG], cwd=str(HERE),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if exists.returncode == 0:
        run(["gh", "release", "delete", TAG, "--yes", "--cleanup-tag"], allow_fail=True)

    args = ["gh", "release", "create", TAG, str(EXE), str(HERE / "version.json"),
            "--title", f"{V.APP_NAME} {TAG}", "--target", V.GITHUB_BRANCH,
            "--latest"]
    if NOTES.exists():
        args += ["--notes-file", str(NOTES)]
    else:
        args += ["--notes", f"{V.APP_NAME} {TAG}"]
    run(args)

    # 4. 把只读令牌落到 exe 同目录（私有仓库更新用，不提交、不打进 exe）
    try:
        tok = subprocess.run(["gh", "auth", "token"], text=True,
                             stdout=subprocess.PIPE).stdout.strip()
        if tok:
            (HERE / "dist" / "gh_token.txt").write_text(tok, encoding="utf-8")
            (HERE / "gh_token.txt").write_text(tok, encoding="utf-8")
            print(f"\n[令牌] 已写入 gh_token.txt（exe 同目录，方便直接检查更新）")
    except Exception as e:
        print(f"[令牌] 未写入：{e}")

    print(f"\n[完成] {V.RELEASES_PAGE}")
    print(f"        下载直链 (exe) {V.RELEASES_PAGE.replace('/releases/latest', '')}"
          f"/releases/download/{TAG}/{V.ASSET_NAME}")


if __name__ == "__main__":
    main()
