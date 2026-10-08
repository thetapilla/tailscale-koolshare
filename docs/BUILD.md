# 构建与发布

本文面向插件和核心发布维护者。运行时协议见 [架构与接口约定](../CONTRACT.md)，测试范围见 [测试指南](TESTING.md)。

## 构建输入

需要 Python 3.12 或更新版本、Docker，以及用于前端测试的 Node.js。构建主机支持 amd64 或 arm64；运行另一种目标架构的冒烟测试时，Docker 需提供对应的 QEMU/binfmt 支持。

[VERSION](../VERSION) 是插件版本的来源。[tools/core_recipe.json](../tools/core_recipe.json) 固定初始上游核心及源码校验值、UPX 版本及校验值、容器镜像摘要、功能标签、压缩参数和核心大小上限。

构建从官方 Tailscale 仓库解析稳定版本标签，锁定具体提交和源码归档 SHA-256。初始版本还必须匹配配方中的固定校验值。核心 Go 版本根据该提交的 `go.mod`、可选 `toolchain` 指令及 `go.toolchain.version` 自动选择，取其中明确要求的最高稳定版本，并从 Go 官方发布元数据获取两种主机架构的下载地址、大小和 SHA-256。构建沿用官方 Go 工具链；上游定制工具链的提交号记录在锁中，便于追溯。声明无法解析或对应正式工具链尚不可用时，流程会停止并保留现有更新源。

Go 模块依赖跟随该源码的 `go.mod`/`go.sum`，以 `-mod=readonly` 和校验数据库验证。UPX 由配方单独锁定。辅助程序依据自身 `go.mod` 和 [helper_recipe.json](../tools/helper_recipe.json) 选择工具链，使用独立缓存目录和依赖锁。

## 依赖锁与重建

`build/core-lock.json` 记录核心版本、构建号、源码提交与归档哈希、Go 声明、精确工具链及其归档校验值，以及静态配方哈希。静态哈希覆盖配方、源码解析器、工具链解析器和构建脚本；完整依赖锁的哈希写入签名清单的 `recipe_sha256`。

Actions 在检查阶段生成或恢复锁，通过 artifact 传给构建阶段，并按版本、构建号和配方键缓存，便于失败后重跑。已发布的锁从 `build-metadata.json` 读取，并与两个架构签名描述符中的配方哈希核对。构建只使用锁定的源码和工具链，启动编译前还会验证实际 `GOVERSION`。工具链目录按用途、版本、主机架构和锁哈希隔离，归档与已解压目录均有完整性检查。

用 `build_core.py --lock /path/to/core-lock.json` 重建时，需使用与锁匹配的配方源码。历史发布中尚无依赖锁的版本，应使用其原发布提交重建。同一核心版本有意更换编译器或配方时，增加构建号，生成新的不可变发布。

核心采用 combined 构建，通过两个入口提供 CLI 和 daemon。编译使用 `CGO_ENABLED=0`、`GOOS=linux`、`GOARM=7` 或 `GOARM64=v8.0`，并启用 `-trimpath`、`-buildvcs=false`、`-mod=readonly`、符号裁剪和空 build ID。具体裁剪标签及 UPX 参数以配方为准。压缩后的程序必须通过 UPX 完整性检查及大小限制。

## 构建与验证

在仓库根目录运行：

```sh
python3 tools/build_core.py
python3 tools/build_helper.py
python3 tools/smoke_core.py
python3 tools/test_helper.py
python3 -m unittest discover -s tests -p 'test_*.py' -v
node tests/test_ui.js
python3 tools/test_busybox.py
```

`build_core.py` 默认构建配方中的初始版本，也可通过 `--version <稳定版本>` 指定官方稳定版本。本地首次构建会解析依赖，并在 `.cache/locks/` 保存该配方的锁；指定 `--lock` 则复用已有输入。核心输出到 `build/cores/<arch>/`，辅助程序输出到 `build/helpers/<arch>/`，主机构建的辅助程序位于 `build/tsks-helper`。辅助程序构建可独立运行，无需先构建核心。

核心源码及工具缓存位于 `.cache/`。Docker 将构建脚本和辅助程序源码挂载为只读，核心源码所在的缓存目录及构建产物目录可写。核心和辅助程序编译使用调用用户的 UID/GID。签名密钥目录不挂入构建容器。

两种架构的冒烟测试在无网络、只读容器中执行，使用临时运行目录。测试范围包括版本探测、userspace daemon 启动、辅助程序读取 LocalAPI、路由及出口偏好和 CLI 检查。固件 TUN、防火墙及真实 Tailnet 流量的验证方法见 [设备与固件验证](TESTING.md#设备与固件验证)。

## 签名与安装包

发布密钥为 Ed25519 私钥，以 128 个十六进制字符保存；提交的 `plugin/release.pub` 是对应公钥。首次建立发布信任根时才生成密钥，重建现有版本需使用与现有公钥匹配的密钥。

验证完成后，在构建主机执行：

```sh
python3 tools/release_core.py --key .secrets/release.key
python3 tools/package.py
```

`release_core.py` 检查两种架构的成功构建记录。新格式元数据必须与依赖锁中的版本、源码、实际 Go 版本和配方一致，再绑定大小和程序哈希生成签名清单。它使用主机辅助程序立即验证两个架构的描述符。`--created-at` 可指定原清单的时间戳，用于重建相同签名封装。该命令生成本地产物，不发布到 GitHub。

核心归档及元数据写入 `build/core-release/`。安装包写入仓库相邻的 `../dist/`：

```text
tailscale_<VERSION>_universal.tar.gz
tailscale_<VERSION>_hnd.tar.gz
tailscale_<VERSION>_qca.tar.gz
tailscale_<VERSION>_ipq32.tar.gz
tailscale_<VERSION>_ipq64.tar.gz
tailscale_<VERSION>_mtk.tar.gz
SHA256SUMS
```

打包先验证签名描述符和 ELF 架构，再在 `build/packages/` 中分别暂存六个包，写入版本与文件校验清单，并规范化归档元数据。通用包与对应平台包的同架构 payload、脚本和资源来自同一组输入。安装脚本的完整文本也必须通过软件中心保护规则检查。

## 插件发布

插件 Release 对应 [VERSION](../VERSION) 中的版本，上传上述六个标准安装包及同批 `SHA256SUMS`。发布前按 [发布产物检查](TESTING.md#发布产物检查) 核对归档内容，并确认 README 的最新发布入口指向插件安装包。

同版本的文档或打包修正替换该插件 Release 中对应的标准文件及校验清单，保留统一的版本入口，并将插件标签更新到对应提交，使源码归档与安装包一致。只有需要发布新插件版本时才调整插件版本及更新记录。插件包内附带的核心仍遵守下述核心发布规则。

## 核心发布与稳定源

[core-release.yml](../.github/workflows/core-release.yml) 定期读取官方稳定源，并支持手动指定稳定版本。检查任务验证当前签名稳定源；构建及测试任务具有只读仓库权限且不接触签名密钥。只有发布任务具有写入权限，并从 `core-release` 环境的 `TSKS_SIGNING_KEY` secret 读取密钥。

发布流程先验证本地签名、两个核心归档的大小、哈希和解包结构，再创建带版本的草稿 Release 并上传资源。完整资源验证通过后发布；远端核心归档重新下载并与签名描述符核对，之后才更新 `core-stable/manifest.json`。

带版本的核心资源不可覆盖。已存在的公开核心 Release 只有在远端签名封装与本地描述符一致、两个远端归档校验通过时才能继续恢复稳定源；恢复复用原远端封装。完整草稿验证后可继续发布，不完整草稿会失败并要求维护者处理。稳定源已匹配时保持不变，版本或构建号倒退会被拒绝。

修改同一上游版本的核心构建配方，需要增加配方中的 `build` 编号，生成新的核心 Release。新上游版本要求更高 Go 时，由依赖解析阶段自动选择并锁定；双架构运行、UPX 完整性和 12 MiB 大小检查仍须全部通过后才发布。编译器升级涉及最低内核或运行环境变化时，还需核对目标固件兼容性。

维护者手动执行核心发布使用：

```sh
GITHUB_SHA="$(git rev-parse HEAD)" python3 tools/publish_core.py
```

该命令使用当前提交作为新建 Release 的目标，需要可用的 GitHub CLI 认证和发布权限，会写入远端 Release。它遵循与 CI 相同的验证及不可变资源规则。详细格式见 [签名清单与核心归档](../CONTRACT.md#签名清单与核心归档)。
