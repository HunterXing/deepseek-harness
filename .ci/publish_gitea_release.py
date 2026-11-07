#!/usr/bin/env python3
"""把 Linux 产物发布到本仓库的 Gitea Release。

为什么不用对象存储：这条线整条不出 GitHub，产物就落在自己的 Gitea 上——
内网直连下载，且只需要仓库 token（不用再配 COS/MinIO 凭据）。

设计取舍：
  * **固定 tag + 固定文件名**：`ci-linux` / `dsh-linux-x64.AppImage`，于是下载地址永久不变，
    更新时不用去翻 Release 页面找带版本号的新文件名；版本号写在 Release 标题和 VERSION.txt 里。
  * **只保留本次产出的资产**：旧包一律删掉，避免资产越攒越多（同 GitHub 线的清理逻辑）。
  * **脚本而不是内联 curl**：Gitea 的 workflow 里塞长 heredoc 很容易踩到 YAML 解析，
    且这套逻辑要能在本地对着真 API 跑通再上 CI。

用法：
  REPO_TOKEN=xxx publish_gitea_release.py \\
    --api https://gitea.xingheng.work/api/v1 \\
    --repo hunterxing/deepseek-harness --tag ci-linux \\
    --title "linux-x64 0.2.2-rc.1.20261107.3" --version 0.2.2-rc.1.20261107.3 \\
    --files out/dsh-linux-x64.AppImage out/VERSION.txt out/SHA256SUMS.txt
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

TIMEOUT = 600

# 直连，绝不走代理：容器里 https_proxy 指向宿主 Clash，Python 的 ssl 走它会
# `ssl.SSLEOFError: EOF occurred in violation of protocol`（实测把发布步骤打挂）。
# Gitea 在自家内网/公网都直连可达，不需要代理。
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(base: str, path: str, token: str, method: str = "GET", body=None,
         raw: bytes | None = None, ctype: str | None = None):
    """发一个 API 请求，返回 (status, payload)。4xx/5xx 不抛，交给调用方判。"""
    headers = {"Authorization": f"token {token}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    elif raw is not None:
        data = raw
        headers["Content-Type"] = ctype or "application/octet-stream"
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with OPENER.open(req, timeout=TIMEOUT) as response:
            payload = response.read()
            if payload[:1] in (b"{", b"["):
                return response.status, json.loads(payload)
            return response.status, payload
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def multipart(field: str, filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    return head + content + tail, f"multipart/form-data; boundary={boundary}"


def fail(message: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"::error::{message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", required=True, help="例如 https://gitea.xingheng.work/api/v1")
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--files", nargs="+", required=True)
    args = parser.parse_args()

    token = os.environ.get("REPO_TOKEN", "").strip()
    if not token:
        fail("环境变量 REPO_TOKEN 为空：本 job 需要仓库读写 token")

    api = args.api.rstrip("/")
    repo = args.repo
    files = [Path(name) for name in args.files]
    for path in files:
        if not path.is_file() or path.stat().st_size == 0:
            fail(f"产物缺失或为空：{path}")

    # 1) 取或建 Release
    status, release = call(api, f"/repos/{repo}/releases/tags/{args.tag}", token)
    if status == 404:
        status, release = call(api, f"/repos/{repo}/releases", token, method="POST", body={
            "tag_name": args.tag,
            "target_commitish": "master",
            "name": args.title,
            "body": f"Linux AppImage，版本 `{args.version}`。由 Gitea Actions 自动构建并覆盖发布。",
            "draft": False,
            "prerelease": True,
        })
        if status not in (201, 200):
            fail(f"创建 Release 失败：HTTP {status} {release!r}")
        print(f">>> 已创建 Release {args.tag} (id={release['id']})")
    elif status != 200:
        fail(f"读取 Release 失败：HTTP {status} {release!r}")
    else:
        release_id = release["id"]
        status, updated = call(api, f"/repos/{repo}/releases/{release_id}", token, method="PATCH", body={
            "tag_name": args.tag,
            "name": args.title,
            "body": f"Linux AppImage，版本 `{args.version}`。由 Gitea Actions 自动构建并覆盖发布。",
            "prerelease": True,
        })
        if status != 200:
            print(f"::warning::更新 Release 标题失败：HTTP {status}")
        print(f">>> 复用 Release {args.tag} (id={release_id})")

    release_id = release["id"]
    wanted = {path.name for path in files}

    # 2) 删掉本次没产出的旧资产
    status, assets = call(api, f"/repos/{repo}/releases/{release_id}/assets", token)
    if status != 200:
        fail(f"读取资产列表失败：HTTP {status} {assets!r}")
    for asset in assets:
        if asset["name"] in wanted:
            print(f"  替换同名资产 {asset['name']}")
            call(api, f"/repos/{repo}/releases/assets/{asset['id']}", token, method="DELETE")
        else:
            print(f"  删除旧资产 {asset['name']} ({asset.get('size', 0)}B)")
            status, _ = call(api, f"/repos/{repo}/releases/assets/{asset['id']}", token, method="DELETE")
            if status not in (204, 200):
                print(f"::warning::删除失败 {asset['name']} HTTP {status}")

    # 3) 上传
    for path in files:
        content = path.read_bytes()
        body, ctype = multipart("attachment", path.name, content)
        status, payload = call(api, f"/repos/{repo}/releases/{release_id}/assets?name={urllib.parse.quote(path.name)}",
                               token, method="POST", raw=body, ctype=ctype)
        if status not in (201, 200):
            fail(f"上传失败 {path.name}：HTTP {status} {payload!r}"[:2000])
        print(f"  已上传 {path.name} ({len(content)}B)")

    # 4) 回读确认：资产数、大小、下载地址
    status, assets = call(api, f"/repos/{repo}/releases/{release_id}/assets", token)
    if status != 200:
        fail(f"回读资产失败：HTTP {status}")
    names = {asset["name"]: asset for asset in assets}
    missing = [path.name for path in files if path.name not in names]
    if missing:
        fail(f"回读缺资产：{missing}")
    print(">>> 发布完成")
    for path in files:
        local = path.stat().st_size
        remote = names[path.name]["size"]
        flag = "OK" if local == remote else "SIZE-MISMATCH"
        download = names[path.name].get("browser_download_url", "")
        print(f"  [{flag}] {path.name}  本地 {local}B / 远端 {remote}B  {download}")


if __name__ == "__main__":
    main()
