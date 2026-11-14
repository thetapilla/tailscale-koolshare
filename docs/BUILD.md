# 构建与发布

本文写给插件和核心的发布维护者。运行时协议见[架构与接口约定](../CONTRACT.md)，测试范围见[测试指南](TESTING.md)。

## 环境与版本

构建需要 Python 3.12 或更新版本、Docker，以及运行前端测试用的 Node.js。构建主机可以是 amd64 或 arm64；要运行另一种目标架构的冒烟测试，Docker 需要对应的 QEMU/binfmt 支持。

| 文件 | 作用 |
| --- | --- |
| [VERSION](../VERSION) | 插件版本。 |
| [tools/bundled_core.json](../tools/bundled_core.json) | 安装包预装的核心版本和构建号，与插件版本分开指定。只能填已发布并签名的不可变核心 Release，按插件发布的需要更新。 |
| [tools/core_recipe.json](../tools/core_recipe.json) | 核心构建配方：基线源码校验值、UPX 版本及校验值、容器镜像摘要、功能标签、压缩参数和核心大小上限。`initial_version` 是源码构建的默认基线，不决定插件预装哪个版本。 |
| [tools/helper_recipe.json](../tools/helper_recipe.json) | 辅助程序的工具链配方。辅助程序按自身的 `go.mod` 和这份配方选择工具链，使用独立的缓存目录和依赖锁。 |

## 构建插件安装包

在仓库根目录依次构建辅助程序、准备预装核心、打包：

```sh
python3 tools/build_helper.py
python3 tools/prepare_bundle.py
python3 tools/package.py
```

- `build_helper.py` 用辅助程序自己的工具链构建两种 ARM 产物和主机上用的验证程序，分别写入 `build/helpers/<arch>/` 和 `build/tsks-helper`。
- `prepare_bundle.py` 按 `bundled_core.json` 从对应的 `core-v<VERSION>-<BUILD>` Release 下载原签名清单和两份核心归档，用 `plugin/release.pub` 验证签名，再校验版本、构建号、文件哈希、大小、归档结构和 ELF 架构；两种架构的源码提交和配方也必须一致。全部通过后才替换打包输入：核心解包到 `build/cores/<arch>/`，原签名清单和归档保存在 `build/core-release/`。
  - 已经下载好的核心资源，可以用 `prepare_bundle.py --release-dir /path/to/core-release` 导入。目录里要有 `manifest.json` 和清单列出的两份归档，校验与下载时相同。
  - 打包直接复用这些已签名的资源，不需要发布私钥，也不改动原核心 Release。
- `package.py` 核对预装版本、签名描述符和 ELF 架构，在 `build/packages/` 中分别暂存六个包，写入版本、前端缓存标识和文件校验清单，并规范化归档元数据。通用包和对应平台包里同架构的 payload、脚本和资源来自同一组输入。安装脚本的完整文本也要通过软件中心的保护规则检查。

安装包写到仓库旁边的 `../dist/`：

```text
tailscaleks_<VERSION>_universal.tar.gz
tailscaleks_<VERSION>_hnd.tar.gz
tailscaleks_<VERSION>_qca.tar.gz
tailscaleks_<VERSION>_ipq32.tar.gz
tailscaleks_<VERSION>_ipq64.tar.gz
tailscaleks_<VERSION>_mtk.tar.gz
SHA256SUMS
```

## 检查

打包后执行以下检查。完整的覆盖范围和固件组件测试见[测试指南](TESTING.md)。

```sh
python3 tools/test_helper.py
python3 -m unittest discover -s tests -p 'test_*.py' -v
node tests/test_ui.js
python3 tools/test_busybox.py
python3 tools/smoke_install.py --lifecycle
```

两种架构的安装冒烟测试使用正式安装包，在临时容器中检查安装和 userspace 服务的生命周期。已签名核心之间的更新与回退由 `smoke_core_update.py` 验证，输入准备见[真实核心更新事务](TESTING.md#真实核心更新事务)。这些测试都不需要加入 Tailnet；固件 TUN、防火墙和真实流量的验证方法见[设备与固件验证](TESTING.md#设备与固件验证)。

## 发布插件

插件 Release 对应 [VERSION](../VERSION) 中的版本：

- 标签为 `v<VERSION>`，标题只写版本号，例如 `3.4.0`。
- 附件为六个标准安装包和同批的 `SHA256SUMS`。
- 正文用中文，取 [CHANGELOG](../CHANGELOG.md) 中该版本的小节，末尾附一段安装包说明。正文里的相对链接改成指向该版本标签的绝对链接。

发布前按[发布产物检查](TESTING.md#发布产物检查)核对归档内容。先以非 latest 状态发布，等 Plugin checks 等工作流通过后再设为 latest，让 README 的最新发布入口指向插件安装包。发布后下载全部附件，用 `SHA256SUMS` 复核。

同一版本的文档或打包修正：替换 Release 中对应的标准文件和校验清单，保留同一个版本入口，并把插件标签移到修正后的提交，让源码归档与安装包一致。只有发布新的插件版本时，才修改插件版本号和更新记录。插件包附带的核心仍遵守下文的核心发布规则。

核心 Release 的标题为 `core-<VERSION>-<BUILD>`，标签为 `core-v<VERSION>-<BUILD>`；稳定源 Release 的标题为 `Core feed`。

## 构建和签名新核心

### 源码和工具链

构建从官方 Tailscale 仓库解析稳定版本标签，锁定具体提交和源码归档的 SHA-256；初始版本还必须与配方中的固定校验值一致。

核心的 Go 版本由该提交的 `go.mod`、可选的 `toolchain` 指令和 `go.toolchain.version` 自动决定：取其中明确要求的最高稳定版本，再从 Go 官方发布元数据获取两种主机架构的下载地址、大小和 SHA-256。构建使用官方 Go 工具链；上游定制工具链的提交号记录在锁中，便于追溯。声明无法解析，或对应的正式工具链还没有发布时，流程停止，现有更新源保持不变。

Go 模块依赖跟随该源码的 `go.mod`/`go.sum`，用 `-mod=readonly` 和校验数据库验证。UPX 由配方单独锁定。

核心采用 combined 构建，用两个入口分别提供 CLI 和 daemon。编译使用 `CGO_ENABLED=0`、`GOOS=linux`、`GOARM=7` 或 `GOARM64=v8.0`，启用 `-trimpath`、`-buildvcs=false`、`-mod=readonly`、符号裁剪和空 build ID。具体的裁剪标签和 UPX 参数以配方为准。压缩后的程序必须通过 UPX 完整性检查和大小上限。

### 构建与签名

先把 `CORE_VERSION` 设为目标官方稳定版本，再执行：

```sh
python3 tools/build_core.py --version "$CORE_VERSION"
python3 tools/build_helper.py
python3 tools/smoke_core.py
python3 tools/release_core.py --key .secrets/release.key
```

- `build_core.py` 不指定版本时使用配方中的 `initial_version`。本地第一次构建会解析依赖，并在 `.cache/locks/` 保存该配方的锁；指定 `--lock` 则复用已有的输入。核心和成功构建记录输出到 `build/cores/<arch>/`，总体元数据写入 `build/core-build.json`。
- `smoke_core.py` 读取构建元数据，验证两种架构的真实压缩程序、版本探测、userspace daemon、LocalAPI、路由和出口偏好，以及 CLI 检查。
- `release_core.py` 检查两种架构的成功构建记录。新格式的元数据必须与依赖锁中的版本、源码、实际 Go 版本和配方一致；之后绑定大小和程序哈希生成签名清单，并用主机辅助程序验证两种架构的描述符。`--created-at` 可以指定原清单的时间戳，用来重建相同的签名封装。核心归档、签名清单和元数据写入 `build/core-release/`；这个命令只生成本地产物，不发布到 GitHub。

源码和工具缓存放在 `.cache/`。Docker 把构建脚本和辅助程序源码挂载为只读，核心源码缓存和产物目录可写；核心和辅助程序以调用者的 UID/GID 编译。签名密钥目录不挂进构建容器。

发布密钥是 Ed25519 私钥，以 128 个十六进制字符保存；仓库里的 `plugin/release.pub` 是对应的公钥。只在第一次建立发布信任根时生成密钥，之后的核心发布必须使用与这把公钥匹配的密钥。

核心发布（见下节）完成后，把需要预装的版本和构建号写入 `bundled_core.json`，再重新构建插件安装包。

## 核心发布与稳定源

[core-release.yml](../.github/workflows/core-release.yml) 每天读取官方稳定源，也可以手动指定稳定版本运行：

- 检查任务验证当前的签名稳定源。
- 构建和测试任务只有仓库读权限，接触不到签名密钥。
- 只有发布任务有写权限，从 `core-release` 环境的 `TSKS_SIGNING_KEY` secret 读取密钥。

发布任务先验证本地签名，以及两个核心归档的大小、哈希和解包结构，再创建带版本的草稿 Release 并上传资源。完整资源验证通过后发布；重新下载远端的核心归档，与签名描述符核对一致后，才更新 `core-stable/manifest.json`。

稳定源更新后，`mirror` 任务把签名清单和它列出的两个归档复制到下载镜像（Cloudflare R2 存储桶 `tailscale-core`）：

- 凭据从 `core-release` 环境的 `R2_ACCOUNT_ID`、`R2_ACCESS_KEY_ID` 和 `R2_SECRET_ACCESS_KEY` 读取，应当是只允许读写该存储桶对象的 R2 令牌。三项没有配齐时，任务给出提示并跳过，不影响发布。
- 镜像上传失败不会回滚已经完成的发布。修好后手动运行这个工作流即可补齐，不需要发布新核心。

不可变规则：

- 带版本的核心资源不能覆盖。已公开的核心 Release，只有在远端签名封装与本地描述符一致、两个远端归档都校验通过时，才能继续用来恢复稳定源；恢复时复用原来的远端封装。
- 完整的草稿验证通过后可以继续发布；不完整的草稿会让流程失败，需要维护者处理。
- 稳定源已经匹配时保持不变；版本或构建号倒退会被拒绝。

修改同一上游版本的核心构建配方时，要增加配方中的 `build` 编号，生成新的核心 Release。新上游版本要求更高的 Go 时，由依赖解析阶段自动选择并锁定；双架构运行、UPX 完整性和 12 MiB 大小检查仍须全部通过才发布。编译器升级涉及最低内核或运行环境的变化时，还要核对目标固件的兼容性。

维护者手动发布核心：

```sh
GITHUB_SHA="$(git rev-parse HEAD)" python3 tools/publish_core.py
```

这个命令以当前提交作为新建 Release 的目标，需要可用的 GitHub CLI 认证和发布权限，会写入远端 Release。它遵循与 CI 相同的验证和不可变资源规则。清单和归档的格式见[签名清单与核心归档](../CONTRACT.md#签名清单与核心归档)。

核心需要新版插件来迁移偏好时，先用 `publish_core.py --release-only` 发布并验证不可变的核心资源，再发布预装该核心的插件；插件公开可用后，用不带这个参数的同一命令推进稳定源。第二次运行会复用远端的签名封装并核对归档。例如带 `ts_omit_clientupdate` 的核心，需要插件 3.1.0 或更新版本在同一次设置中关闭已有的自动更新偏好。

## 依赖锁与重建

`build/core-lock.json` 记录核心版本、构建号、源码提交和归档哈希、Go 声明、精确的工具链及其归档校验值，以及静态配方哈希。静态哈希覆盖配方、源码解析器、工具链解析器和构建脚本；完整依赖锁的哈希写入签名清单的 `recipe_sha256`。

Actions 在检查阶段生成或恢复锁，通过 artifact 传给构建阶段，并按版本、构建号和配方键缓存，失败后可以重跑。已发布的锁从 `build-metadata.json` 读取，并与两种架构签名描述符中的配方哈希核对。构建只使用锁定的源码和工具链，开始编译前还会验证实际的 `GOVERSION`。工具链目录按用途、版本、主机架构和锁哈希分开存放，归档和解压后的目录都有完整性检查。

用 `build_core.py --lock /path/to/core-lock.json` 重建时，要使用与锁匹配的配方源码。历史发布中还没有依赖锁的版本，应使用它原来的发布提交重建。同一核心版本有意更换编译器或配方时，增加构建号，生成新的不可变发布。
