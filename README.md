# Tailscale Koolshare

适用于 Koolshare 软件中心的 Tailscale 路由器插件。通过路由器管理页面接入 Tailnet，管理子网路由和出口节点，并独立更新 Tailscale 核心。

- 查看服务状态、连接健康信息和接口流量。
- 保留已有配置与设备身份，支持从原版插件迁移。
- 手动检查、验证并更新核心，支持回退上一核心。
- 提供诊断摘要和有频率限制的自动恢复。

## 下载与安装

从 [最新插件 Release](https://github.com/thetapilla/tailscale-koolshare/releases/latest) 下载安装包，在软件中心的「离线安装」页面上传。插件版本以 [VERSION](VERSION) 为准；每批发布文件的校验值见同一 Release 中的 `SHA256SUMS`。

| 软件中心平台 | 安装包后缀 | 核心架构 |
| --- | --- | --- |
| 自动识别 | `_universal.tar.gz` | 包含 ARM32 和 ARM64，安装时选择所需架构 |
| HND | `_hnd.tar.gz` | ARM32（`arm`） |
| QCA | `_qca.tar.gz` | ARM32（`arm`） |
| IPQ32 | `_ipq32.tar.gz` | ARM32（`arm`） |
| IPQ64 | `_ipq64.tar.gz` | ARM64（`arm64`） |
| MTK | `_mtk.tar.gz` | ARM64（`arm64`） |

按固件的软件中心平台选择安装包；不确定时使用通用包。平台包体积较小，通用包只会安装设备需要的架构。固件需提供可用的 Koolshare 软件中心及 Linux 4.1 或更新内核。详细要求见 [使用指南](docs/USER-GUIDE.md)。

安装完成后打开插件页面，启用 Tailscale，按页面的「登录并授权」链接完成首次接入。升级安装会保留已有核心、配置和设备身份；安装及应用设置期间，Tailscale 连接可能短暂中断。

## 核心更新

插件版本与 Tailscale 核心版本分别管理。在页面点击「检查更新」，再点击「更新核心」安装本项目构建并签名的稳定核心。更新前会校验签名、文件哈希、版本和架构；更新失败时尝试恢复原核心，保留上一核心时可手动回退。更新需要手动发起。

随安装包提供的核心来自官方 Tailscale 源码，采用精简构建；Tailscale SSH、Taildrop 等功能未包含在该构建中。完整构建选项见 [核心构建配方](tools/core_recipe.json)，更新和回退步骤见 [使用指南](docs/USER-GUIDE.md#核心更新与回退)。

## 文档

- [使用指南](docs/USER-GUIDE.md)：安装、设置、核心管理与故障排查。
- [架构与接口约定](CONTRACT.md)：运行目录、任务协议和签名格式。
- [构建与发布](docs/BUILD.md)：构建环境、签名和发布流程。
- [测试指南](docs/TESTING.md)：自动化检查、固件兼容性和设备验证。
- [更新记录](CHANGELOG.md)。

## 来源与许可

插件的页面布局、图标和软件中心接口源自 [Koolshare rogsoft](https://github.com/koolshare/rogsoft/tree/master/tailscale)，保留原作者 sadog / Koolshare 的署名和权利。来源说明见 [NOTICE](NOTICE)。Tailscale 使用官方源码及其 [BSD-3-Clause 许可](LICENSES/Tailscale-BSD-3-Clause.txt)。
