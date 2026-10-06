# .ci —— 构建期资源

这个目录里的东西**不参与上游代码**，只服务于本 fork 的自建产物流水线。

## `unsigned-macos.patch`

免签名 + 自更新 + Windows 构建修复的合并补丁（**9 个文件 / 18 个 hunk**）。
名字里的 `macos` 只反映最初动机，现在三个平台都会应用。

| 文件 | 改什么 |
|---|---|
| `apps/desktop/scripts/electron-builder-config.mjs` | 允许 darwin 走 `--unsigned`；`forceCodeSigning` / `notarize` / `dmg.sign` 随 unsigned；跳过 afterSign 验签与 dmg 公证；未签名也写出 `app-update.yml`；`artifactName` 加 `-unsigned` 后缀 |
| `apps/desktop/scripts/package-target.ts` | `--unsigned` 允许 mac 目标；跳过 keychain 包装与显式公证；给冒烟与 `prepare:dsh` 透传 `--unsigned` |
| `apps/desktop/scripts/prepare-dsh.ts` | 仅在 `!argv.includes('--unsigned')` 时签 Mach-O |
| `apps/desktop/scripts/prepare-package-set.ts` | `tar -xOzf` 改用 `cwd` + basename 读取清单：GNU tar 把 Windows 绝对路径的冒号当远程主机分隔符，在 `D:\...` 上以 exit 2 失败 |
| `apps/desktop/scripts/smoke-packaged-runtime.ts` | 允许 macOS 免签名产物（路径为 `unsigned-artifacts/`） |
| `apps/desktop/scripts/desktop-auto-update-environment.mjs` | 未签名构建的 feed 地址默认值，按 `target` 推导为 `https://github.com/HunterXing/deepseek-harness/releases/download/ci-<target>/`，不依赖 workflow 注入 |
| `apps/desktop/src/main.ts` | 安装走自研替换，不再交给 Squirrel.Mac |
| `apps/desktop/src/update-coordinator.ts` | 接入自研替换；HTTP 传输禁用 HTTP/2 与 QUIC（Chromium 栈经代理传大文件会 `ERR_HTTP2_PROTOCOL_ERROR`） |
| `apps/desktop/src/update-replace.ts`（新增） | detached shell 替换 bundle：等旧进程退出 → `ditto` 解包 → 合并写回 → 重开，逐步追加日志 |

**为什么存成文件而不是直接改源码**：这 9 个文件属于上游流水线的一部分。
一旦把改动提交进 fork，每次同步上游就是 9 个冲突，fork 会烂掉。
存成补丁、由 workflow 在构建期 `git apply`，源码树就永远与上游逐字节一致，
`sync-upstream.yml` 的 `git merge --ff-only` 才永远能跑。

## `make-blockmap.mjs`

为 `--dir` + `ditto` 打出来的 macOS zip 补一个 `.blockmap`。
electron-builder 自己没产 zip，也就不会附带它，而 electron-updater 的 `MacUpdater`
没有 blockmap 就只会整包重下。脚本直接调用 `app-builder-lib` 内部同一个
`buildBlockMap`，不自实现分块算法，保证格式与 electron-builder 一致。

```sh
node .ci/make-blockmap.mjs <artifact.zip>           # 生成并校验
node .ci/make-blockmap.mjs --verify <artifact.zip>  # 只校验已有 blockmap
```

差分下载能生效的**前提**是产物文件名里含版本号：electron-updater 的
`Provider.getBlockMapFiles` 是把 URL 里的新版本号替换成旧版本号来定位上一版
blockmap 的，名字里没有版本号就找不到，差分必然退化成全量下载。

## 补丁失效了怎么办

上游一旦改动这 9 个文件，`git apply --check` 会失败，**构建会响亮中止** —— 这是刻意的：
宁可构建失败，也不要产出一个悄悄跑偏的包。workflow 里这一步带 `set -euo pipefail`，
并且断言改动的文件数是 9；少了它，`git apply` 失败后步骤仍会"成功"，
构建会带着未打补丁的源码跑完。

修复流程：

```sh
# 1. 在本地同步到最新上游
cd ~/workspace/deepseek-harness
git fetch origin && git merge --ff-only origin/master

# 2. 从干净树出发重新施加本地改动，然后整份重生成补丁
#    （本 fork 的本地工作树就是"已打补丁"的状态，可直接 diff）
git add -N apps/desktop/src/update-replace.ts
git diff HEAD -- apps/desktop/scripts apps/desktop/src > /tmp/newpatch.diff
git apply --check -R -p1 /tmp/newpatch.diff   # 反向能应用 == 补丁与工作树一致

# 3. 重点核对这几处：
#      electron-builder-config.mjs  unsigned builds require Windows 的抛错
#                                    mac 段的 forceCodeSigning / notarize / dmg.sign
#                                    afterSign 与 artifactBuildCompleted 的前置 return
#                                    app-update.yml 的写出条件
#      package-target.ts             --unsigned requires win-x64 的抛错
#                                    withMacOSSigningKeychain 包装
#                                    prepare:dsh / smoke 的 --unsigned 透传
#      prepare-dsh.ts                process.platform === 'darwin' 的签名调用
#      prepare-package-set.ts        tar -xOzf 的路径参数
#      smoke-packaged-runtime.ts     unsigned artifacts require Windows 的抛错
#      desktop-auto-update-environment.mjs  feed 地址默认值分支
#      update-coordinator.ts / main.ts / update-replace.ts  自研替换链路
```

若上游**新增**了签名相关文件或阶段（`git diff --stat <旧>..<新> -- apps/desktop/scripts/`），
补丁需要跟着扩展，不只是改行号。

## 切回官方签名

拿到 Apple Developer Program（$99/年）后：

1. 删掉 `build-desktop.yml` 里的「免签名补丁」步骤，并把补丁里
   `electron-builder-config.mjs` / `package-target.ts` 的 unsigned 分支收回上游形态
2. 配成 repository secrets：`CSC_LINK`（p12 的 base64）、`CSC_KEY_PASSWORD`、
   `APPLE_API_KEY`、`APPLE_API_KEY_ID`、`APPLE_ISSUER`
3. 把打包命令的 `--unsigned --dir` 去掉

公证可以在 CI 侧完成，Apple 不要求构建机是 Mac。