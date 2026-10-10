# -*- coding: utf-8 -*-
"""商品视频下载 + 合并成 MP4

亚马逊商品视频（VSE）给的是 HLS 清单（.m3u8），不是 mp4 直链：

    default.jobtemplate.hls.m3u8              # master：4 档码率 360/480/720/1080
      └── default.jobtemplate.hls1080.m3u8    # media：15 个 .ts 分片
            └── default.jobtemplate.hls1080_00001.ts ...

所以流程是：选最高档 -> 并行拉分片 -> 拼成一个 .ts -> 用 ffmpeg 无损封装成 .mp4。
一个商品有多个视频时，最后再用 ffmpeg concat 合成单个 <SKU>.mp4。

ffmpeg 缺失时降级保存 .ts（可播放，但不是 mp4），并在日志里明确提示。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin

try:
    import requests
except ImportError:                                   # pragma: no cover
    requests = None

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

# 亚马逊 CDN 对单条连接限速很死（实测 25 KB/s），必须靠并发堆带宽。
# 实测同一段 1080p 的 15 个分片：8 路 31s / 16 路 18s / 24 路 58s -> 取 16。
SEG_WORKERS = 16
SEG_RETRY = 3
HTTP_TIMEOUT = 60
PREFER_HEIGHT = 1080     # 首选清晰度

_HDR = {"User-Agent": UA, "Accept": "*/*"}
_TLS = threading.local()          # 每线程一个 Session，复用连接，省掉重复 TLS 握手


def _session():
    if requests is None:
        return None
    s = getattr(_TLS, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update(_HDR)
        _TLS.s = s
    return s


# ---------------------------------------------------------------- ffmpeg 探测

_FFMPEG_CACHE = {"path": None, "probed": False}


def ffmpeg_path() -> str | None:
    """找 ffmpeg：应用目录 -> PATH -> winget 链接 -> 常见安装位置"""
    if _FFMPEG_CACHE["probed"]:
        return _FFMPEG_CACHE["path"]

    cands = []
    # 1) 与应用同级 / bin 子目录（用户可自行丢一个进来）
    here = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    cands += [here / "ffmpeg.exe", here / "bin" / "ffmpeg.exe",
              Path(__file__).parent / "ffmpeg.exe"]
    # 2) PATH
    w = shutil.which("ffmpeg")
    if w:
        cands.insert(0, Path(w))
    # 3) winget 的 Links 目录（装完不加 PATH 也能命中）
    local = os.environ.get("LOCALAPPDATA") or ""
    if local:
        cands.append(Path(local) / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe")
    # 4) 常见手装位置
    cands += [Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
              Path(r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"),
              Path(r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe")]

    found = None
    for c in cands:
        try:
            if c and Path(c).is_file():
                found = str(c)
                break
        except Exception:
            continue
    _FFMPEG_CACHE.update(path=found, probed=True)
    return found


def has_ffmpeg() -> bool:
    return ffmpeg_path() is not None


def _run(cmd, log=None, timeout=1800) -> tuple:
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout)
        out = (p.stdout or b"").decode("utf-8", "replace")
        return p.returncode, out
    except Exception as e:
        return -1, f"{e}"


# ---------------------------------------------------------------- HTTP

def _get(url: str, timeout: int = HTTP_TIMEOUT, retry: int = SEG_RETRY) -> bytes:
    last = ""
    for i in range(retry):
        try:
            s = _session()
            if s is not None:
                r = s.get(url, timeout=timeout)
                if r.ok and r.content:
                    return r.content
                last = f"HTTP {r.status_code}"
            else:
                import urllib.request
                req = urllib.request.Request(url, headers=_HDR)
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    body = resp.read()
                if body:
                    return body
                last = "空响应"
        except Exception as e:
            last = str(e)[:120]
        if i < retry - 1:
            time.sleep(1.2 * (i + 1))
    raise RuntimeError(f"下载失败 {url[-70:]} -> {last}")


def _text(url: str) -> str:
    return _get(url).decode("utf-8", "replace")


# ---------------------------------------------------------------- m3u8 解析

def _is_master(text: str) -> bool:
    return "#EXT-X-STREAM-INF" in text


def parse_master(text: str, base: str) -> list:
    """master 清单 -> [(height, bandwidth, 绝对URL)]，按清晰度升序"""
    out = []
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if not ln.startswith("#EXT-X-STREAM-INF"):
            continue
        m_res = re.search(r"RESOLUTION=(\d+)x(\d+)", ln)
        m_bw = re.search(r"(?:AVERAGE-)?BANDWIDTH=(\d+)", ln)
        height = int(m_res.group(2)) if m_res else 0
        bw = int(m_bw.group(1)) if m_bw else 0
        url = ""
        for j in range(i + 1, len(lines)):
            s = lines[j].strip()
            if s and not s.startswith("#"):
                url = s
                break
        if url:
            out.append((height, bw, urljoin(base, url)))
    out.sort(key=lambda x: (x[0], x[1]))
    return out


def parse_media(text: str, base: str) -> tuple:
    """media 清单 -> (分片URL列表, 是否加密)"""
    segs, encrypted = [], False
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.startswith("#EXT-X-KEY") and "METHOD=NONE" not in s.upper():
            encrypted = True
            continue
        if s.startswith("#"):
            continue
        segs.append(urljoin(base, s))
    return segs, encrypted


def pick_variant(url: str, prefer: int = PREFER_HEIGHT, log=None) -> tuple:
    """把 master/媒体清单统一解析成 (分片URL列表, 备注)"""
    text = _text(url)
    note = ""
    if _is_master(text):
        vs = parse_master(text, url)
        if not vs:
            raise RuntimeError("master 清单里没有可用清晰度")
        # 首选 <= prefer 的最高档；没有就取最高档
        cand = [v for v in vs if v[0] and v[0] <= prefer] or vs
        h, _bw, vurl = cand[-1]
        note = f"{h}p" if h else f"{vs[-1][1] // 1000}kbps"
        if log:
            log(f"      清晰度可选：{'/'.join(str(v[0]) or '?' for v in vs)} -> 取 {note}")
        text = _text(vurl)
        base = vurl
    else:
        base = url
    segs, enc = parse_media(text, base)
    if not segs:
        raise RuntimeError("清单里没有分片")
    if enc:
        note += " (加密清单)"
    return segs, note


# ---------------------------------------------------------------- 下载 + 封装

def download_hls(url: str, ts_path: Path, log=None, prefer: int = PREFER_HEIGHT) -> dict:
    """下载 HLS 并落成一个 .ts 文件"""
    segs, note = pick_variant(url, prefer, log)
    if log:
        log(f"      分片 {len(segs)} 个，并行 {SEG_WORKERS} 路下载 ...")

    got = [None] * len(segs)
    fails = []
    done = {"n": 0}

    def one(i):
        try:
            got[i] = _get(segs[i])
        except Exception as e:
            fails.append((i, str(e)[:80]))
        done["n"] += 1
        if log and (done["n"] % 10 == 0 or done["n"] == len(segs)):
            log(f"      进度 {done['n']}/{len(segs)}")

    with ThreadPoolExecutor(max_workers=min(SEG_WORKERS, len(segs))) as ex:
        list(ex.map(one, range(len(segs))))

    if fails:
        # 缺片就直接失败，避免产出静默损坏的视频
        raise RuntimeError(f"{len(fails)}/{len(segs)} 个分片下载失败："
                           f"{fails[0][1]}")

    ts_path.parent.mkdir(parents=True, exist_ok=True)
    with open(ts_path, "wb") as f:
        for b in got:
            f.write(b)
    size = ts_path.stat().st_size
    if log:
        log(f"      合并分片 -> {ts_path.name} ({size // 1024} KB) {note}")
    return {"path": ts_path, "size": size, "segments": len(segs), "quality": note}


def ts_to_mp4(ts_path: Path, mp4_path: Path, log=None) -> tuple:
    """ffmpeg 无损封装 .ts -> .mp4（失败则退回重编码）"""
    ff = ffmpeg_path()
    if not ff:
        return False, "未找到 ffmpeg"
    mp4_path.parent.mkdir(parents=True, exist_ok=True)

    base = [ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(ts_path)]
    tries = [
        (base + ["-c", "copy", "-movflags", "+faststart", str(mp4_path)], "无损封装"),
        (base + ["-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart",
                 str(mp4_path)], "无损封装+aac修正"),
        (base + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                 "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
                 str(mp4_path)], "重编码"),
    ]
    last = ""
    for cmd, how in tries:
        rc, out = _run(cmd, log)
        if rc == 0 and mp4_path.exists() and mp4_path.stat().st_size > 1024:
            if log:
                log(f"      ffmpeg {how} -> {mp4_path.name} "
                    f"({mp4_path.stat().st_size // 1024} KB)")
            return True, ""
        last = f"{how}: {out.strip()[-160:]}"
        if log:
            log(f"      ffmpeg {how} 失败，换下一种")
    return False, last


def merge_mp4s(paths: list, out_path: Path, log=None) -> tuple:
    """多个 mp4 顺序拼成一个（先无损，失败重编码）"""
    if not paths:
        return False, "没有可合并的视频"
    if len(paths) == 1:
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            if Path(paths[0]).resolve() != out_path.resolve():
                shutil.copyfile(paths[0], out_path)
            return True, ""
        except Exception as e:
            return False, str(e)
    ff = ffmpeg_path()
    if not ff:
        return False, "未找到 ffmpeg"

    lst = out_path.parent / "_concat.txt"
    lst.parent.mkdir(parents=True, exist_ok=True)
    with open(lst, "w", encoding="utf-8") as f:
        for p in paths:
            f.write("file '" + str(Path(p).resolve()).replace("'", "'\\''") + "'\n")

    base = [ff, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(lst)]
    tries = [
        (base + ["-c", "copy", "-movflags", "+faststart", str(out_path)], "无损拼接"),
        (base + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                 "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
                 str(out_path)], "重编码拼接"),
    ]
    last = ""
    for cmd, how in tries:
        rc, out = _run(cmd, log)
        if rc == 0 and out_path.exists() and out_path.stat().st_size > 1024:
            if log:
                log(f"      ffmpeg {how} -> {out_path.name} "
                    f"({out_path.stat().st_size // 1024} KB)")
            try:
                lst.unlink()
            except Exception:
                pass
            return True, ""
        last = f"{how}: {out.strip()[-160:]}"
    try:
        lst.unlink()
    except Exception:
        pass
    return False, last


def download_videos(videos: list, dest_dir: Path, log=None,
                    prefer: int = PREFER_HEIGHT) -> dict:
    """下载一个 SKU 的全部商品视频并合并成 <SKU>.mp4

    videos: product.py 抓到的 [{'url':..., 'title':..., 'duration':..., ...}]
    返回 {'files': [完成后的 mp4 路径], 'parts': [中间 mp4 路径], 'errors': [...]}
    """
    dest_dir = Path(dest_dir)
    res = {"files": [], "parts": [], "errors": [], "count": 0,
           "bytes": 0, "quality": "", "need_ffmpeg": False}
    vids = [v for v in (videos or []) if (v or {}).get("url")]
    if not vids:
        return res

    ff = ffmpeg_path()
    log = log or (lambda *_a, **_k: None)
    tmp = Path(tempfile.mkdtemp(prefix="amzv_"))
    parts = []
    try:
        for i, v in enumerate(vids, 1):
            url = v["url"]
            title = (v.get("title") or "").strip()
            dur = v.get("duration") or 0
            log(f"    [视频 {i}/{len(vids)}] {title or '(无标题)'} "
                f"{('约 ' + str(dur) + 's') if dur else ''}")
            stem = f"{i:02d}"
            try:
                if re.search(r"\.mp4(\?|$)", url, re.I):
                    # 直链 mp4
                    p = tmp / f"{stem}.mp4"
                    p.write_bytes(_get(url))
                    parts.append(p)
                    log(f"      直链 mp4 -> {p.stat().st_size // 1024} KB")
                else:
                    ts = tmp / f"{stem}.ts"
                    meta = download_hls(url, ts, log, prefer)
                    res["quality"] = meta.get("quality") or res["quality"]
                    if ff:
                        mp4 = tmp / f"{stem}.mp4"
                        ok, err = ts_to_mp4(ts, mp4, log)
                        if ok:
                            parts.append(mp4)
                            try:
                                ts.unlink()
                            except Exception:
                                pass
                        else:
                            res["errors"].append(f"视频{i} 转 mp4 失败: {err}")
                            parts.append(ts)
                    else:
                        res["need_ffmpeg"] = True
                        parts.append(ts)
                        log("      ⚠ 未检测到 ffmpeg，已保存为 .ts（装上 ffmpeg 可自动转 mp4）")
            except Exception as e:
                res["errors"].append(f"视频{i}: {str(e)[:160]}")
                log(f"      ✗ 失败 -> {str(e)[:160]}")

        if not parts:
            return res

        ext = ".ts" if all(p.suffix == ".ts" for p in parts) else ".mp4"
        out = dest_dir / f"{dest_dir.parent.name}{ext}"
        ok, err = merge_mp4s(parts, out, log)
        if ok:
            res["files"].append(out)
            res["bytes"] = out.stat().st_size
            res["parts"] = list(parts)
            res["count"] = len(vids)
        else:
            res["errors"].append(f"合并失败: {err}")
            # 合并失败也要把已下好的分片留下，别白干
            dest_dir.mkdir(parents=True, exist_ok=True)
            for i, p in enumerate(parts, 1):
                try:
                    q = dest_dir / f"{dest_dir.parent.name}_{i:02d}{p.suffix}"
                    shutil.copyfile(p, q)
                    res["files"].append(q)
                except Exception:
                    pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return res


def package_videos(out_root: Path, items=None, zip_path: Path | None = None) -> dict:
    """把各 SKU 的 video/ 目录打成一个 zip（ZIP_STORED，mp4 本身已压缩）"""
    import zipfile

    out_root = Path(out_root)
    zip_path = Path(zip_path) if zip_path else None
    if zip_path:
        zip_path.parent.mkdir(parents=True, exist_ok=True)
    skus, count, total = [], 0, 0

    zf = zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) if zip_path else None
    try:
        order = [s for s, *_ in items] if items else None
        dirs = ([out_root / s for s in order] if order else
                sorted(p for p in out_root.iterdir() if p.is_dir()) if out_root.is_dir() else [])
        for sd in dirs:
            vd = sd / "video"
            if not vd.is_dir():
                continue
            files = sorted(f for f in vd.iterdir() if f.is_file())
            if not files:
                continue
            skus.append(sd.name)
            for f in files:
                count += 1
                total += f.stat().st_size
                if zf:
                    zf.write(f, f"{sd.name}/{f.name}")
    finally:
        if zf:
            zf.close()
    return {"zip": str(zip_path) if zip_path else "", "skus": skus,
            "count": count, "bytes": total, "empty": count == 0}


def package_product_images(out_root: Path, items=None,
                           zip_path: Path | None = None) -> dict:
    """把各 SKU 的 images/（商品主图）打成一个 zip，并附 manifest.csv"""
    import csv
    import io
    import zipfile

    out_root = Path(out_root)
    zip_path = Path(zip_path) if zip_path else None
    if zip_path:
        zip_path.parent.mkdir(parents=True, exist_ok=True)
    rows, count, total = [], 0, 0

    zf = zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) if zip_path else None
    try:
        order = [s for s, *_ in items] if items else None
        dirs = ([out_root / s for s in order] if order else
                sorted(p for p in out_root.iterdir() if p.is_dir()) if out_root.is_dir() else [])
        for sd in dirs:
            idir = sd / "images"
            if not idir.is_dir():
                continue
            files = sorted(f for f in idir.iterdir() if f.is_file())
            if not files:
                continue
            for f in files:
                count += 1
                total += f.stat().st_size
                rows.append({"SKU": sd.name, "文件": f"{sd.name}/{f.name}",
                             "大小KB": f.stat().st_size // 1024})
                if zf:
                    zf.write(f, f"{sd.name}/{f.name}")
        if zf:
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=["SKU", "文件", "大小KB"],
                               lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
            zf.writestr("manifest.csv", "\ufeff" + buf.getvalue())
    finally:
        if zf:
            zf.close()
    return {"zip": str(zip_path) if zip_path else "", "count": count,
            "bytes": total, "skus": len({r["SKU"] for r in rows}), "empty": count == 0}
