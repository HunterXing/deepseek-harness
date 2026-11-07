#!/usr/bin/env python3
"""把 primary-runtime 需要下载的归档预取到构建缓存里（用 curl，不走 Node 的 fetch）。

为什么需要这一步：
  打包流程里的 prepare-runtime 用 Node 的 `fetch` 下载 node / python / wheels，
  在自建 runner 的容器里会 `TypeError: fetch failed`（同样的 URL 用 curl 或换台机器都正常，
  属于容器网络 + undici 的偶发不可靠）。这些文件是**按 sha256 命名的缓存**，
  所以只要构建前用 curl 把它们放进缓存目录，prepare-runtime 就不会再发起任何 fetch。

命名规则（同 scripts/primary-runtime/prepare.ts 的 downloadPrimaryRuntimeAsset）：
  缓存文件名 = 该文件的 sha256（小写十六进制）。

用法：
  prefetch_runtime_assets.py --repo . --target linux-x64 [--cache <dir>]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def mirrors(url: str) -> list[str]:
    """同一份字节的备用来源；sha256 校验兜底，故多源不会引入偏差。"""
    fallbacks: list[str] = []
    if url.startswith("https://nodejs.org/dist/"):
        # nodejs.org 走代理偶发 rc=35 连不上，npmmirror 同步同一份 dist
        fallbacks.append("https://npmmirror.com/mirrors/node/" + url[len("https://nodejs.org/dist/"):])
    return fallbacks


def assets_for(lock: dict, target: str) -> list[tuple[str, str]]:
    """Return (url, sha256) pairs the target needs, matching prepare.ts's download order."""
    entry = lock["targets"][target]
    # 真实文件名是 node-v<版本>-<后缀>，entry['nodeArchive'] 只是后缀（linux-x64.tar.gz）
    node_url = (f"https://nodejs.org/dist/v{lock['nodeVersion']}/"
                f"node-v{lock['nodeVersion']}-{entry['nodeArchive']}")
    python_name = (f"cpython-{lock['pythonVersion']}+{lock['pythonRelease']}-"
                   f"{entry['pythonTarget']}-install_only_stripped.tar.gz")
    python_url = ("https://github.com/astral-sh/python-build-standalone/releases/download/"
                  f"{lock['pythonRelease']}/{python_name}")
    items = [(node_url, entry["nodeSha256"]), (python_url, entry["pythonSha256"])]
    for wheel in [*entry.get("wheels", []), *lock.get("wheels", [])]:
        items.append((wheel["url"], wheel["sha256"]))
    return items


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, help="仓库根目录（含 scripts/primary-runtime/lock.json）")
    parser.add_argument("--target", required=True, help="例如 linux-x64")
    parser.add_argument("--cache", help="缓存目录，默认 <repo>/apps/desktop/.desktop-build/downloads")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    lock = json.loads((repo / "scripts/primary-runtime/lock.json").read_text())
    cache = Path(args.cache) if args.cache else repo / "apps/desktop/.desktop-build/downloads"
    cache.mkdir(parents=True, exist_ok=True)

    items = assets_for(lock, args.target)
    fetched = skipped = failed = 0
    for url, expected in items:
        destination = cache / expected
        if destination.is_file() and sha256_of(destination) == expected:
            skipped += 1
            continue
        ok = False
        for candidate in [url, *mirrors(url)]:
            print(f">>> 下载 {candidate}", flush=True)
            # --http1.1：这台机器上走代理时 HTTP/2 会吞掉重定向的 location 头，
            # 表现为 curl rc=35（SSL connect error）——改用 HTTP/1.1 即稳。
            # --retry-all-errors：rc=35/28 这类也要重试，不能只重试 HTTP 码。
            # --speed-limit/--speed-time：卡住的连接（<1KB/s 持续 30s）主动断开换源。
            result = subprocess.run(
                ["curl", "-fL", "--http1.1", "--retry", "5", "--retry-all-errors", "--retry-delay", "3",
                 "--speed-limit", "1024", "--speed-time", "30",
                 "-C", "-", "--connect-timeout", "20", "--max-time", "900",
                 "-o", str(destination), candidate],
                capture_output=True, text=True)
            if result.returncode != 0:
                print(f"    失败 rc={result.returncode}: {result.stderr.strip()[:160]}", file=sys.stderr)
                destination.unlink(missing_ok=True)
                continue
            actual = sha256_of(destination)
            if actual != expected:
                print(f"    校验失败 期望 {expected[:12]} 实得 {actual[:12]}", file=sys.stderr)
                destination.unlink(missing_ok=True)
                continue
            ok = True
            break
        if ok:
            fetched += 1
        else:
            failed += 1
        continue

    print(f"缓存就绪：新下载 {fetched}，已存在 {skipped}，失败 {failed}（共 {len(items)} 项）")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
