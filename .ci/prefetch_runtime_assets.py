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


def assets_for(lock: dict, target: str) -> list[tuple[str, str]]:
    """Return (url, sha256) pairs the target needs, matching prepare.ts's download order."""
    entry = lock["targets"][target]
    node_url = f"https://nodejs.org/dist/v{lock['nodeVersion']}/{entry['nodeArchive']}"
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
        print(f">>> 下载 {url}", flush=True)
        # curl 用 --fail：HTTP 错误直接非零退出，不留半个文件；-C - 断点续传；重试 3 次
        result = subprocess.run(
            ["curl", "-fL", "--retry", "3", "--retry-delay", "2", "-C", "-",
             "--connect-timeout", "20", "--max-time", "900",
             "-o", str(destination), url],
            capture_output=True, text=True)
        if result.returncode != 0:
            print(f"    失败 rc={result.returncode}: {result.stderr.strip()[:200]}", file=sys.stderr)
            destination.unlink(missing_ok=True)
            failed += 1
            continue
        actual = sha256_of(destination)
        if actual != expected:
            print(f"    校验失败 期望 {expected[:12]} 实得 {actual[:12]}", file=sys.stderr)
            destination.unlink(missing_ok=True)
            failed += 1
            continue
        fetched += 1

    print(f"缓存就绪：新下载 {fetched}，已存在 {skipped}，失败 {failed}（共 {len(items)} 项）")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
