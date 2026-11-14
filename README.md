<div align="center">

<img src="plugin/res/icon-tailscaleks.png" width="96" alt="Tailscale Koolshare 图标">

# Tailscale Koolshare

Koolshare 软件中心的 Tailscale 插件<br>
把路由器接入 Tailnet，共享家里的局域网，或者让它成为出口节点

[![最新版本](https://img.shields.io/github/v/release/thetapilla/tailscale-koolshare?label=%E6%9C%80%E6%96%B0%E7%89%88%E6%9C%AC)](https://github.com/thetapilla/tailscale-koolshare/releases/latest)
[![Plugin checks](https://github.com/thetapilla/tailscale-koolshare/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/thetapilla/tailscale-koolshare/actions/workflows/test.yml)

**[下载安装包](https://github.com/thetapilla/tailscale-koolshare/releases/latest)** · [使用指南](docs/USER-GUIDE.md) · [更新记录](CHANGELOG.md)

</div>

## 功能

- **接入 Tailnet**：在插件页面勾选启用，点链接登录授权；页面显示运行状态、健康提示和接口流量。
- **共享网段与出口**：把路由器的局域网或自定义网段宣告给 Tailnet，接受其他设备共享的子网，或者把路由器设为出口节点。
- **应用连接器**：让 Tailnet 中的设备按域名经这台路由器访问指定网站。
- **单独更新核心**：插件和 Tailscale 核心分开更新。核心由本项目用官方源码构建并签名，更新前校验签名和哈希，失败时自动恢复，也可以回退到上一版；GitHub 下载失败时自动改用镜像。
- **自动维护**：服务异常时在限额内自动重启；固件重建防火墙后，自动恢复所需的规则。
- **升级保留设置**：升级保留设置、设备身份和已安装的核心，也可以从插件市场的原版直接迁移。

## 快速开始

1. 按[下表](#选择安装包)选择安装包，从 [Releases](https://github.com/thetapilla/tailscale-koolshare/releases/latest) 下载。上传前不要改文件名。
2. 在软件中心的「离线安装」页面上传安装包。
3. 打开 Tailscale 插件页面，勾选「启用 Tailscale」，点「应用设置」。
4. 点页面上的「登录并授权」登录 Tailscale 账号；Tailnet 开启了设备审批的，再到管理控制台批准这台路由器。

共享局域网或提供出口时，还要在 Tailscale 管理控制台批准对应的路由。详细说明见[使用指南](docs/USER-GUIDE.md)。

> [!NOTE]
> 固件需要 Koolshare 软件中心和 Linux 4.1 或更新的内核。安装前请确认 JFFS 剩余空间，以 3.4.0 为例：全新安装约需 17 MB（通用包约 19 MB），从插件市场原版迁移约需 22 MB。详见[安装前确认](docs/USER-GUIDE.md#安装前确认)。

## 选择安装包

按机型选择；拿不准就用通用包，它包含两种架构，安装时自动选择。

| 安装包后缀 | 适用机型 | 核心架构 |
| --- | --- | --- |
| `_hnd` | 下面没有列出的机型，例如 RT-AX86U Pro、RT-BE86U | ARM32 |
| `_mtk` | 天选游戏路由（`TX-AX6000`）、TUF GAMING 小旋风 Pro（`TUF-AX4200Q`）、`RT-AX57_Go`、ROG 魔盒（`GS7`）、ROG 魔盒 Air（`GS7_Air`）、ROG 魔盒 X（`GS-BE7200X`）、灵耀魔方 Pro WiFi7 BE9400（`ZenWiFi_BT8P`） | ARM64 |
| `_ipq64` | TUF GAMING 小旋风 Pro WiFi7（`TUF_6500`） | ARM64 |
| `_ipq32` | 灵耀魔方 WiFi7 BE3600（`ZenWiFi_BD4`） | ARM32 |
| `_qca` | `RT-AX89X` | ARM32 |
| `_universal` | 以上全部机型 | ARM32 和 ARM64 |

括号里是固件中的型号标识。HND 机型即使处理器是 64 位，也使用 ARM32 核心，请按上表选择，不要按处理器选。平台包的大小约为通用包的一半，装好后的内容与通用包相同。每个 Release 都附有 `SHA256SUMS`，可以用来核对下载是否完整。

## 与插件市场原版的关系

> [!IMPORTANT]
> 本插件的模块名是 `tailscaleks`，和插件市场里的原版 Tailscale（模块名 `tailscale`）是两个插件。两者共用设置和文件位置，**不能同时安装**。

- 已经装了原版的路由器，直接安装本插件即可：设备身份和原有设置会保留，并先继续使用原版的核心。步骤见[从插件市场原版迁移](docs/USER-GUIDE.md#从插件市场原版迁移)。
- 装好本插件后，插件市场里的原版会显示「未安装」。不要再安装它：原版的文件与本插件同名，会把本插件覆盖。

## 核心更新

插件版本和 Tailscale 核心版本分开管理，核心不会自动更新。在插件页面点「检查更新」，再点「更新核心」，即可安装本项目构建并签名的稳定版核心。

- 更新前校验签名、文件哈希、版本和架构；更新失败时自动恢复原核心，保留了上一核心时可以手动回退。
- 先从 GitHub 下载，失败时自动改从镜像下载同一文件，校验规则不变。
- 核心用 Tailscale 官方源码精简构建，不包含 Tailscale SSH、Taildrop 等功能。构建选项见[核心构建配方](tools/core_recipe.json)，更新和回退步骤见[使用指南](docs/USER-GUIDE.md#核心管理)。

## 文档

| 文档 | 内容 | 读者 |
| --- | --- | --- |
| [使用指南](docs/USER-GUIDE.md) | 安装、迁移、各项设置、核心管理、常见问题 | 用户 |
| [更新记录](CHANGELOG.md) | 各版本的改动和升级说明 | 用户 |
| [架构与接口约定](CONTRACT.md) | 运行目录、任务协议、签名格式 | 开发者 |
| [构建与发布](docs/BUILD.md) | 构建环境、签名和发布流程 | 维护者 |
| [测试指南](docs/TESTING.md) | 自动化检查、固件兼容性和设备验证 | 维护者 |

## 来源与许可

本插件由 [ThetaPilla](https://github.com/thetapilla) 维护，源自 sadog / Koolshare 的原版 Tailscale 插件（[koolshare/rogsoft](https://github.com/koolshare/rogsoft/tree/master/tailscale)）。插件的页面布局、图标和软件中心接口沿用原版，保留原作者的署名和权利，详见 [NOTICE](NOTICE)。

Tailscale 核心使用官方源码构建，遵循 [BSD-3-Clause 许可](LICENSES/Tailscale-BSD-3-Clause.txt)。本项目不是 Tailscale 官方项目。
