# 架构与接口约定

本文面向维护后台、前端、构建工具或第三方集成的开发者。用户操作见 [使用指南](docs/USER-GUIDE.md)，发布流程见 [构建与发布](docs/BUILD.md)。插件版本以 [VERSION](VERSION) 为准，核心和工具链的固定输入以 [构建配方](tools/core_recipe.json) 为准。

## 组件与运行环境

后台脚本以路由器 BusyBox ash 为目标，`tailscale_lib.sh` 提供生命周期、操作锁、配置、任务、防火墙和自动恢复功能。`tailscale_core_lib.sh` 管理核心版本及持久化更新事务。Go 辅助程序 `tsks-helper` 负责有时间或大小上限的本地操作、签名校验和安全解包。

原生软件中心页面通过 httpdb 调用脚本，使用结构化任务结果显示操作进度。安装器与运行时后台共用生命周期锁，防止配置应用、核心切换和安装交叉修改状态。

## 目录与文件

默认根目录为 `/koolshare`。测试可以显式设置 `TSKS_ROOT`、`TSKS_RUN`、`TSKS_WEB`、`TSKS_PROC` 和 `TSKS_SYSFS` 指向隔离目录。

| 名称 | 默认路径与约定 |
| --- | --- |
| 插件数据 | `/koolshare/tailscale`，下文记为 `DATA` |
| 核心版本 | `DATA/cores/<version>-<build>-<arch>/` |
| 核心指针 | `DATA/current`、`DATA/previous`，相对符号链接指向 `cores/...` |
| 设备身份 | `/koolshare/configs/tailscale/tailscaled.state`，生命周期操作保留此文件 |
| 恢复次数记录 | `/koolshare/configs/tailscale/watchdog-ledger` |
| 运行目录 | `/tmp/tailscale3`，下文记为 `RUN` |
| LocalAPI socket | `RUN/tailscaled.sock` |
| 生命周期锁 | `RUN/operation.lock`，使用非阻塞 `flock` |
| 辅助程序 | `/koolshare/bin/tsks-helper` |
| 发布公钥 | `DATA/release.pub` |
| 前端 | `/koolshare/webs/Module_tailscale.asp` 与 `/koolshare/res/tailscale3.js` |
| 公开任务结果 | `/tmp/upload/tailscale3_<job-id>.json` 和 `.log`，浏览器通过 `/_temp/` 读取 |

每个核心目录包含 `tailscale.combined`、`descriptor.json`，以及名为 `tailscale` 和 `tailscaled` 的相对入口链接。公开任务目录只存放状态和经过脱敏的日志；身份文件、核心事务快照和密钥保存在私有目录。

## 请求与任务协议

httpdb 将请求 ID 放在脚本参数 `$1`，方法参数从 `$2` 开始。支持直接命令行调用的生命周期和核心方法将动作放在 `$1`。前端生成不大于 `1000000000` 的正整数请求 ID，以兼容 32 位传输；后端任务 ID 校验接受最多 15 位十进制数字。

| 方法 | 参数 | 用途 |
| --- | --- | --- |
| `tailscale_config` | `web_submit`、`start`、`stop`、`restart`、`start_nat` | 配置及服务生命周期 |
| `tailscale_fettle` | 无 | 结构化状态 |
| `tailscale_status` | 无 | 连接详情任务 |
| `tailscale_tsnets` | `1` | 接口地址与流量 |
| `tailscale_ncheck` | 无 | 网络检查任务 |
| `tailscale_core` | `check`、`update`、`rollback` | 核心管理任务 |
| `tailscale_job` | 任务 ID | 查询已有任务 |
| `tailscale_diagnostics` | 无 | 诊断摘要任务 |

异步操作取得锁并创建任务后，返回：

```json
{"accepted":true,"job_id":"12345"}
```

锁冲突返回 `{"accepted":false,"error":"busy"}`。软件中心传输层的 `response.result` 可能是 JSON 对象，也可能是 JSON 字符串，前端应兼容两种表示。响应丢失时，前端先按原任务 ID 查询结果，避免重复提交变更。

任务 JSON 原子写入，字段如下：

| 字段 | 含义 |
| --- | --- |
| `schema` | 协议版本，当前为 `1` |
| `id` | 十进制任务 ID 字符串 |
| `state` | `running`、`success`、`failed` 或 `rolled_back` |
| `phase` | 当前执行阶段 |
| `message` | 可显示的结果或进度信息 |
| `updated_at` | Unix 时间戳 |

查不到任务时，查询接口返回 `state: "unknown"`，客户端应重新读取状态确认结果。日志作为独立文本显示，不作为成功或失败的判据。

状态接口返回 `schema`、`enabled`、`plugin_version`、`core_version`、`backend_state`、`online`、`health_codes`、`health_messages`、`auth_url`、`monitoring_available`，以及 `watchdog` 和 `core` 对象。`online` 可以是布尔值或 `null`。`watchdog` 包含 `enabled`、`last_recovery`、`count_24h`；恢复记录表示尝试次数。`core` 包含 `installed`、`available`、`can_rollback`。状态读取异常时可附带 `error`。

接口流量响应为 `{"interfaces":[{"if":"tailscale0","ip":"...","rx":0,"tx":0}]}`。`rx`、`tx` 为字节计数。连接详情任务通过有时间上限的 CLI 调用生成状态文本，并经日志脱敏后显示。

## 配置提交

前端通过 `GET /_api/tailscale_` 读取配置。保存时调用 `tailscale_config`，参数为 `["web_submit", "<七位配置快照>"]`，请求的 `fields` 保持为空对象。快照必须匹配 `^[01]{7}$`，位顺序如下：

1. `tailscale_enable`
2. `tailscale_ipv4_enable`
3. `tailscale_ipv6_enable`
4. `tailscale_advertise_routes`
5. `tailscale_accept_routes`
6. `tailscale_exit_node`
7. `tailscale_watchdog_enable`

后台取得生命周期锁后才应用配置，避免 httpdb 在脚本获取锁之前写入 DBus。无快照的旧式 `web_submit` 仍保留兼容。查询和核心更新请求不携带 DBus 写入字段。

IPv4/IPv6 开关控制外层 UDP 41641 的防火墙规则；LAN 宣告来自固件当前 LAN 地址及掩码。Tailscale 自身的 netfilter 保持启用。插件 NAT 链放在既有 Fullcone 等 POSTROUTING 钩子之后，地址变化和 NAT 事件由维护任务补充刷新。

## 核心事务与自动恢复

更新依次执行签名校验、资源预检、下载解包、原子指针切换和本地健康检查。`DATA/update.txn` 记录事务阶段，`DATA/update.state` 保存切换前的身份快照。成功切换记录上一核心；失败时尝试恢复原指针和状态。显式生命周期或核心操作先恢复未完成事务；自动恢复任务遇到未完成事务时跳过，安装与卸载则拒绝继续。

自动恢复在服务启用期间按分钟检查。启动宽限期为 180 秒；本地服务连续三次检查失败，或控制连接异常持续至少 600 秒且 WAN 与经验证的 HTTPS 可达时，才进入恢复判断。恢复前再次确认本地状态。等待登录、设备审批、明确停用和关闭同步等状态豁免恢复。

恢复尝试先写入持久记录，再重启插件服务。尝试间隔至少 1800 秒，任意连续 24 小时最多两次。地址与防火墙维护独立于自动恢复开关。

## 辅助程序接口

| 命令 | 行为 |
| --- | --- |
| `timeout SECONDS COMMAND [ARGS...]` | 直接执行参数，不经过 shell；保留退出码，超时返回 `124` |
| `version BINARY` | 设置 `TS_BE_CLI=1`，在 5 秒内读取并校验核心版本 |
| `status SOCKET` | 通过受限 LocalAPI 请求返回状态、健康信息和身份存在标志；不返回原始 state |
| `fetch URL DEST MAX_BYTES` | 有大小和时间上限的 HTTPS 下载，仅接受允许的发布及 CDN 主机 |
| `verify ENVELOPE PUBKEY ARCH` | 校验签名与清单，输出所选架构的扁平描述符 |
| `extract ARCHIVE DESCRIPTOR DEST` | 校验归档与核心哈希、大小、结构及 ELF 架构，安全解包 |
| `keygen PRIVATE_FILE PUBLIC_FILE` | 生成十六进制 Ed25519 密钥文件，供构建主机使用 |
| `sign PAYLOAD PRIVATE_FILE ENVELOPE` | 签名核心清单，供构建主机或 CI 使用 |
| `json-get FILE FIELD` | 读取点分字段或数字数组索引 |
| `quote STRING` | 输出 JSON 字符串编码 |
| `atomic-link TARGET LINK` | 原子替换指向 `cores/...` 的相对链接，并同步父目录 |
| `log PATH MAX_BYTES` | 从标准输入读取日志、脱敏凭据，并按上限轮转 |

## 签名清单与核心归档

稳定源为 [core-stable/manifest.json](https://github.com/thetapilla/tailscale-koolshare/releases/download/core-stable/manifest.json)。外层 JSON 包含 `payload` 和 `signature` 两个 Base64 字符串；Ed25519 签名覆盖解码后的原始 payload 字节。

payload 包含：

- `schema: 1`、`channel: "stable"`。
- `version`、`build`、`source_commit`、`recipe_sha256`、RFC 3339 格式的 `created_at`。
- `artifacts.arm` 和 `artifacts.arm64`，各包含 `url`、`size`、`sha256`、`unpacked_size` 和 `binary_sha256`。

验证后描述符将所选 artifact 与 `version`、`build`、`arch`、`source_commit`、`recipe_sha256` 合并。核心文件 URL 使用以下格式：

```text
https://github.com/thetapilla/tailscale-koolshare/releases/download/core-v<VERSION>-<BUILD>/tailscale-core_<VERSION>_<BUILD>_<ARCH>.tar.gz
```

清单上限为 64 KiB，归档上限为 13 MiB，核心上限为 12 MiB。核心归档仅包含一个普通文件 `tailscale.combined`，权限为 `0755`。入口链接由安装器或更新器创建。

带版本的核心 Release 及其资源保持不可变。`core-stable` 只承载可更新的签名清单。稳定源检查拒绝版本或构建号倒退；显式本地回退独立处理。

## 插件包约定

安装包只有一个 `tailscale/` 根目录，包含插件文件、版本、公钥、签名核心清单、`manifest.sha256` 和 `payload/<arch>/`。每个架构 payload 包含核心、辅助程序和描述符。通用包包含两种架构及五个平台标记，平台包仅含相应架构，安装器只安装所选架构。

平台对应关系为 `hnd`、`qca`、`ipq32` → `arm`；`ipq64`、`mtk` → `arm64`。规范输出目录为仓库相邻的 `../dist/`，文件名为 `tailscale_<VERSION>_<PLATFORM>.tar.gz`，共六个安装包及 `SHA256SUMS`。包内版本在归档前写入；文件排序、权限、所有者及时间戳统一规范化。
