# .ci —— 构建期资源

这个目录里的东西**不参与上游代码**，只服务于本 fork 的自建产物流水线。

## `unsigned-macos.patch`

对 4 个桌面打包脚本的免签名补丁（10 个 hunk）：

| 文件 | 改什么 |
|---|---|
| `apps/desktop/scripts/electron-builder-config.mjs` | 允许 darwin 走 `--unsigned`；`forceCodeSigning` / `notarize` / `dmg.sign` 随 unsigned；跳过 afterSign 验签与 dmg 公证 |
| `apps/desktop/scripts/package-target.ts` | `--unsigned` 允许 mac 目标；跳过 keychain 包装与显式公证；给冒烟与 `prepare:dsh` 透传 `--unsigned` |
| `apps/desktop/scripts/prepare-dsh.ts` | 仅在 `!argv.includes('--unsigned')` 时签 Mach-O |
| `apps/desktop/scripts/smoke-packaged-runtime.ts` | 允许 macOS 免签名产物（路径为 `unsigned-artifacts/`） |

**为什么存成文件而不是直接改源码**：这 4 个文件属于上游流水线的一部分。
一旦把改动提交进 fork，每次同步上游就是 4 个冲突，fork 会烂掉。
存成补丁、由 workflow 在构建期 `git apply`，源码树就永远与上游逐字节一致，
`sync-upstream.yml` 的 `git merge --ff-only` 才永远能跑。

## 补丁失效了怎么办

上游一旦改动这 4 个脚本，`git apply` 会失败，**构建会响亮中止** —— 这是刻意的：
宁可构建失败，也不要产出一个悄悄跑偏的包。

修复流程：

```sh
# 1. 在本地同步到最新上游
cd ~/workspace/deepseek-harness
git fetch origin && git merge --ff-only origin/master

# 2. 找一个能成功构建的旧版本作为基线
git checkout <能构建的 tag>

# 3. 重新推导补丁：对照 .ci/unsigned-macos.patch 逐条核对
#    重点看这几处：
#      electron-builder-config.mjs  unsigned builds require Windows 的抛错
#                                    mac 段的 forceCodeSigning / notarize / dmg.sign
#                                    afterSign 与 artifactBuildCompleted 的前置 return
#      package-target.ts             --unsigned requires win-x64 的抛错
#                                    withMacOSSigningKeychain 包装
#                                    prepare:dsh / smoke 的 --unsigned 透传
#      prepare-dsh.ts                process.platform === 'darwin' 的签名调用
#      smoke-packaged-runtime.ts     unsigned artifacts require Windows 的抛错

# 4. 验证并更新本目录的补丁
git apply --check -p1 .ci/unsigned-macos.patch
```

若上游**新增**了签名相关文件或阶段（`git diff --stat <旧>..<新> -- apps/desktop/scripts/`），
补丁需要跟着扩展，不只是改行号。

## 切回官方签名

拿到 Apple Developer Program（$99/年）后：

1. 删掉 `build-desktop.yml` 里的「免签名补丁」步骤
2. 配成 repository secrets：`CSC_LINK`（p12 的 base64）、`CSC_KEY_PASSWORD`、
   `APPLE_API_KEY`、`APPLE_API_KEY_ID`、`APPLE_API_ISSUER`
3. 把打包命令的 `--unsigned --dir` 去掉

公证可以在 CI 侧完成，Apple 不要求构建机是 Mac。
