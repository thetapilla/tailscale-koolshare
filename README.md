# Tailscale Koolshare 3.0.0

适用于 Koolshare 软件中心的 Tailscale 路由器插件。提供可读后台、核心独立更新、状态诊断和有界自动恢复。

## 安装包

请下载 [3.0.0 安装兼容修订包 installfix1](https://github.com/thetapilla/tailscale-koolshare/releases/tag/v3.0.0-installfix1)。它修正了原始安装脚本注释触发软件中心保护扫描的兼容性问题，插件版本仍为3.0.0。原始发布文件保留供核对。

提供一个通用包和 HND、QCA、IPQ32、IPQ64、MTK 五个平台包。通用包自动识别平台，只安装该设备需要的架构；闪存空间较紧时选择对应平台包。RT-AX86U Pro、RT-BE86U 使用 HND 包。

从本仓库的插件 Release 下载 `tailscale_3.0.0_universal.tar.gz` 或对应平台包，在软件中心的离线安装页面上传。先保留当前版本和配置备份，安装时现有 Tailscale 连接会短暂中断。

全新安装携带 Tailscale 1.102.4。升级安装保留已安装核心和原有连接身份，包括从 2.x 迁移的核心；安装完成后可在插件页面独立更新核心。首次启用尚未授权的设备时，页面提供 Tailscale 登录链接。

## 核心更新

页面分别显示插件版本和核心版本。点击「检查更新」查询本仓库已构建、测试并签名的稳定核心，再点击「更新核心」安装。更新失败时自动恢复上一版；成功更新后可手动回滚。后台不会自行安装新版本。

核心由官方 Tailscale 源码构建为精简 combined 程序，以两个入口提供 CLI 和 daemon。裁剪集合沿用原版插件，包括 Tailscale SSH、Taildrop 等非原版插件功能；原有系统 SSH 服务不受此构建选项影响。构建参数和工具版本见 [构建说明](docs/BUILD.md)。

更新包按实际架构下载，验证 Ed25519 签名、SHA-256、版本、文件结构和 ELF 架构。更新需要至少 64 MiB 可用 RAM，并为持久存储保留 8 MiB 余量；不足时保留当前版本并报告原因。插件升级不会用附带核心覆盖已安装核心。

## 诊断与恢复

看门狗默认开启，可在页面关闭。每分钟检查本地服务与控制连接，对短时网络波动、等待授权或明确停用保持等待。符合恢复条件时重启插件服务，至少冷却30分钟，24小时最多两次；不重启路由器、不注销或删除设备身份。

地址变化和NAT重建的规则维护独立运行，繁忙期间收到的NAT刷新会排队重试。

「导出诊断」包含实际核心版本、后端状态、健康原因、资源概况及受限长度的脱敏运行日志。设备 state 文件、私钥与认证令牌不在诊断输出中。

## 开发与验证

源码、构建和测试可在本机 Docker 中运行。安装产物默认输出到工作目录的 `dist/`。参见 [构建说明](docs/BUILD.md)、[审计与验证](docs/AUDIT.md)、[实机验收](docs/ACCEPTANCE.md)。

Docker 测试覆盖脚本控制逻辑、更新事务、原版配置迁移、前端及两种架构的真实核心。各厂商固件的网络栈和长期稳定性仍需要实机验收。

## 来源与许可

插件基于 [Koolshare rogsoft](https://github.com/koolshare/rogsoft/tree/master/tailscale) 的界面布局、图标及软件中心接口，原作者 sadog/Koolshare 的署名和权利保留。项目中的新实现与原版的关系见 [NOTICE](NOTICE)。Tailscale 使用官方源码及其 [BSD-3-Clause 许可](LICENSES/Tailscale-BSD-3-Clause.txt)。
