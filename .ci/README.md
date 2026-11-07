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

## Release 资产只保留最新版本

`ci-mac-x64` / `ci-mac-arm64` 两个 Release 的资产**只增不减**：
`softprops/action-gh-release` 的 `overwrite_files: true` 只覆盖**同名**文件，
而每次构建的产物文件名都带新版本号，历史 zip 就全留着了。
实测一度累积到 31 / 30 个资产、约 **11GB** 历史包。

OTA 的更新源是固定 tag（`ci-mac-x64`），根本不需要历史 zip。
所以 `build-desktop.yml` 的 `publish-release` job 在发布之后加了一步
「清理旧版本资产」，每次发布自动删掉历史包。

### 保留什么、删什么

| 资产 | 处理 | 理由 |
|---|---|---|
| `nightly-mac.yml` / `latest-mac.yml` / `nightly.yml` | **保留** | electron-updater 的更新入口 |
| 当前版本的 `.zip` + `.zip.blockmap` | **保留** | 本次发布的产物 |
| **上一版本的 `.zip.blockmap`** | **保留** | 差分下载要用（见下） |
| 历史版本的 `.zip` | 删除 | 大头（每个 370–390MB） |
| 历史版本的 `.zip.blockmap`（非上一版） | 删除 | 只有上一版会被差分引用 |
| 无版本号的遗留包（如 `dsh-mac-x64-<sha>.zip`） | 删除 | 既非当前版本，又让差分定位失效 |

### 为什么不能把历史 blockmap 全删光

electron-updater 的差分下载靠「把 URL 里的新版本号替换成旧版本号」
去定位**客户端已安装版本**的 blockmap：

```js
// electron-updater 6.8.9  package/out/providers/Provider.js:24
getBlockMapFiles(baseUrl, oldVersion, newVersion, oldBlockMapFileBaseUrl = null) {
    const oldBlockMapUrl = newUrlFromBase(
      `${baseUrl.pathname.replace(new RegExp(escapeRegExp(newVersion), "g"), oldVersion)}.blockmap`, ...);
    return [oldBlockMapUrl, newBlockMapUrl];
}
```
调用处 `package/out/AppUpdater.js:649` 传的 `oldVersion` 是 `this.app.version`
—— 客户端**当前装着的**版本。

旧 blockmap 删光 → 差分**静默退化成整包下载**（几百 MB），用户完全无感但体验崩。
上一版的 blockmap 只有几百 KB，保留它不影响瘦身效果。

### 改这个清理逻辑时注意

- 清理步骤**放在发布之后**且**不阻断**（`set -uo pipefail`，不用 `-e`）：
  发布成功是主目标，清理失败只发 `::warning::`，不该让整条流水线变红。
- 「上一版本」是**除当前版本外版本号最大**的那个。版本号形如
  `0.2.2-alpha.3.20261103.42`，日期段 + run 号在后，字典序 == 时间序。
- 改 workflow 后**本地验三道**再推（推上去才知道对不对，本地验更便宜）：
  `yaml.safe_load` 能解析 → 抽出的 shell 过 `bash -n` → **actionlint**。
  判别逻辑还要用真实资产数据真跑一遍（劫持 `gh` 命令做 dry-run）。

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

## `linux.patch`

Linux（`linux-x64` → AppImage）的接线补丁（**15 个文件，+59/−32**）。
`unsigned-macos.patch` 打完再打这一份；两份都只在构建期应用，用完即弃。

上游的桌面打包链只认 `mac-arm64` / `mac-x64` / `win-x64` 三个目标，Linux 是从零接上去的：

| 文件 | 改什么 |
|---|---|
| `desktop-build-paths.mjs`（+`.d.mts`） | 支持目标集合与「目标→平台」映射加 `linux-x64` |
| `package-target.ts` | 目标表加 `linux-x64`（`--linux` / `--x64`）；Linux 目标要求 Linux 构建机；免签名 Linux 复用 macOS 那套「本地默认环境」（不读 `.env.macos`、不带更新策略）；非 Windows 的打包并发取 4 |
| `desktop-package-environment.mjs`（+`.d.mts`） | 校验阶段跳过 macOS / Windows 的签名与策略解析 |
| `electron-builder-config.mjs` | Linux 段补 `icon` / `executableName`；**policy 与 update 都强制 undefined** —— 上游的 policy 解析只认 win32/darwin，带上它 Linux 上启动即抛 `desktop policy: unsupported platform`；而 update 一旦有值，electron-builder 会写出指向不存在 feed 的 `app-update.yml`，更新器反复失败 |
| `prepare-runtime.ts` / `prepare-dsh.ts` | Electron 可执行文件路径：Linux 解包后是顶层 `electron`，不是 `Electron.app/Contents/MacOS/Electron` |
| `prepare-cli.ts` | 非 Windows 都补 0755 —— 没有可执行位的 `dsh` 启动器跑不起来 |
| `desktop-auto-update-environment.{mjs,d.mts}` / `desktop-release-store.ts` / `desktop-publish-targets.ts` / `desktop-upload-plan.ts` / `desktop-toolchain-preflight.ts` | 目标联合类型与 zod 枚举加 `linux-x64`；不改的话 `tsc -p tsconfig.host.json` 的 host aggregate check 直接失败（最容易漏的一层：运行时能跑，类型检查先炸） |

**刻意没做的两件事**：

1. **不做 Linux 自动更新**。AppImage 接 electron-updater 要另写 `latest-linux.yml` 与可用的 feed，
   当前需求是「产物在自己机器上、手动下载快」。Linux 目标因此把 update 置空，
   产物里没有 `app-update.yml`，更新器整体停用 —— 与「手动下载」的预期一致，不会对着空地址报错。
2. **Linux 不进 COS / EdgeOne 上传链**。产物只发 Gitea Release，`desktop-upload-plan.ts` 里的
   `linux-x64` 条目只是为了满足类型。

**已知差异**：LibreOffice 引擎在 Linux 上没有原生包（npm 上只有 darwin / win32），
`selectOfficeEngine` 会自动退到 `libreoffice-kit-wasm`，文档转换走 WASM，比 mac / win 慢。

## Gitea 线：`.gitea/workflows/build-linux.yml`

Linux 包的独立发布线，跑在自有 Gitea（`gitea.xingheng.work`）的 runner 上，产物发到本仓库 Release
（tag `ci-linux`、文件名固定 `dsh-linux-x64.AppImage`），下载地址因此永久稳定。

与 GitHub 线的关系：**代码同源**（同一分支、同一套补丁），**产物分工** —— GitHub 出 macOS，Gitea 出 Linux。

三条约束写在 workflow 头部注释里，动之前先读：不能出现 `uses:`（runner 拉不到 github.com 上的 action）、
代理必须显式导出（含 `NODE_USE_ENV_PROXY=1`，否则 Python 轮子那批 fetch 直接超时）、二进制与 npm 走国内镜像。
