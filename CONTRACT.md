# 架构与接口约定

本文面向维护后台、前端、构建工具或第三方集成的开发者。用户操作见 [使用指南](docs/USER-GUIDE.md)，发布流程见 [构建与发布](docs/BUILD.md)。插件版本以 [VERSION](VERSION) 为准，预装核心版本以 [预装核心配置](tools/bundled_core.json) 为准，核心编译选项见 [构建配方](tools/core_recipe.json)，精确源码和工具链见对应核心发布的依赖锁。

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

httpdb 将请求 ID 放在脚本参数 `$1`，方法参数从 `$2` 开始。支持直接命令行调用的生命周期和核心方法将动作放在 `$1`。前端请求 ID 限定在 `1` 至 `99999999`，到达上限后从 `1` 继续，避免软件中心拒绝九位 ID。后端任务 ID 校验接受最多 15 位十进制数字，供查询已有任务使用；恢复任务时仍生成新的八位以内传输 ID。

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

锁冲突返回 `{"accepted":false,"error":"busy"}`。响应丢失时，前端先按原任务 ID 查询结果，避免重复提交变更。

脚本回复统一经过 `ts_reply`。软件中心的 httpdb 会把回调正文直接放入外层 JSON 的字符串字段，因此回传前必须进行 JSON 字符串转义，并去掉转义结果最外层的引号。回调使用 `POST /_resp/<请求 ID>`，由辅助程序完成转义，curl 以二进制正文发送，保留换行、反斜杠和 Unicode 的编码。

浏览器先解析 HTTP 响应，再将 `response.result` 字符串解析为插件结果；前端同时接受对象形式的结果。`GET /_api/tailscale_` 直接返回配置对象数组，不经过脚本回调。直接命令行调用没有请求 ID 时，`ts_reply` 向标准输出写入原始插件 JSON。

任务 JSON 原子写入，字段如下：

| 字段 | 含义 |
| --- | --- |
| `schema` | 协议版本，当前为 `1` |
| `id` | 十进制任务 ID 字符串 |
| `state` | `running`、`success`、`failed` 或 `rolled_back` |
| `phase` | 当前执行阶段 |
| `message` | 可显示的结果或进度信息 |
| `updated_at` | Unix 时间戳 |

查不到任务时，查询接口返回 `state: "unknown"`。后台尝试取得操作锁，并在锁内重新查询记录；确认任务仍不存在且没有正在执行的操作后，才返回 `operation_busy: false`。客户端核对任务 ID 后结束失效记录的跟踪，重新读取设置和状态；不把任务缺失判定为成功，也不自动重发变更。`operation_busy` 为 `true` 或缺失时继续跟踪。日志作为独立文本显示，不作为成功或失败的判据。

状态接口返回 `schema`、`enabled`、`plugin_version`、`core_version`、`core_version_long`、`backend_state`、`online`、`health_codes`、`health_messages`、`auth_url`、`monitoring_available`，以及 `watchdog` 和 `core` 对象。`online` 可以是布尔值或 `null`。`watchdog` 包含 `enabled`、`last_recovery`、`count_24h`；恢复记录表示尝试次数。`core` 包含 `installed`、`available`、`can_rollback`。状态读取异常时可附带 `error`。

`core_version` 是用于展示和发布版本比较的版本号；LocalAPI 不可用时回退到已安装描述符中的版本。`core_version_long` 保留 daemon 上报的完整版本字符串，用于诊断；尚未取得 LocalAPI 版本时为空字符串。版本规范化规则见下文辅助程序接口。

`core` 还包含 `installed_build`、`available_build`、`update_available`、`previous` 和 `previous_build`。构建号来自对应描述符，可以是 `rN`、`legacy` 或空字符串。`update_available` 仅在存在有效更新结果且目标 `cores/<version>-<build>-<arch>` 与当前指针不同时为 `true`，与更新事务的目标判断一致。页面只在运行版本与已安装版本相符时附上已安装构建号。

接口流量响应为 `{"interfaces":[{"if":"tailscale0","ip":"...","rx":0,"tx":0}]}`。`rx`、`tx` 为字节计数。连接详情任务通过有时间上限的 CLI 调用生成状态文本，并经日志脱敏后显示。

状态还包含 `routes: {"advertised": [], "primary": []}`。辅助程序从偏好中的 `AdvertiseRoutes` 及状态 `Self.PrimaryRoutes` 读取，过滤非法前缀、主机位和默认路由，规范化并去重。`primary` 表示控制面选定由本机承担的网段，缺失不等于未批准。

## 配置提交

前端通过 `GET /_api/tailscale_` 读取配置。所有选项和网段列表先修改页面草稿，点击「应用设置」才提交。保存时调用 `tailscale_config`，参数为 `["web_submit", "<九位配置快照>", "<编码网段列表>"]`，请求的 `fields` 保持为空对象。快照匹配 `^[01]{9}$`，位顺序如下：

1. `tailscale_enable`
2. `tailscale_ipv4_enable`
3. `tailscale_ipv6_enable`
4. `tailscale_advertise_routes`
5. `tailscale_accept_routes`
6. `tailscale_exit_node`
7. `tailscale_watchdog_enable`
8. `tailscale_accept_dns`（默认 `0`）
9. `tailscale_custom_routes_enable`（默认 `0`）

`tailscale_custom_routes` 保存规范 CIDR 的逗号分隔明文，默认空；关闭自定义开关会保留列表。列表最多 32 项、2048 UTF-8 字节。线上第三参数为 `b64.` 加无填充 base64url 编码的 UTF-8 列表；空列表为 `b64.`。真实固件 httpdb 会丢弃空参数，因此不能直接传空字符串。最长合法 32 条 IPv6 输入已通过实际传输验证。部分固件会将过长的编码请求拒绝为 `result: -6`，前端将其作为明确拒绝显示，保留草稿并解锁，不查询不存在的任务。

九位快照必须带编码列表；七位快照必须只有快照参数，仅更新原有七个开关，三个新键保持原值。无快照的旧式 `web_submit` 仍保留兼容。格式、参数个数和列表先同步校验，失败返回 `{"accepted":false,"error":"invalid_custom_routes","detail":"序号 条目：原因"}`，不创建任务、不写 DBus。快照格式错误使用 `invalid_config_snapshot`。

后台取得生命周期锁后才应用配置。备份和恢复包括路由字符串，空值删除相应键。字符串保存在独立的私有文件，按实际 DBus 协议仅去掉一个记录终止换行。正常恢复会重新校验；若旧值原本已损坏，允许合法新提交修复，失败时仅原样恢复这份不可信数据并明确告警，启动仍拒绝使用它。路由值从不作为 Shell 源码执行。查询和核心更新请求不携带 DBus 写入字段。

前端按任务 ID 保存方法、操作意图、目标启用状态、配置快照和明文网段草稿，刷新后继续查询同一任务。脏检查比较九位快照与网段列表；旧七位草稿只恢复原开关，其余字段来自当前配置。配置提交与执行期间显示进度，失败后保留草稿；网段错误详情以文本显示并定位相应行。配置生命周期和已进入 `switching` 阶段的核心任务显示过渡状态，暂缓显示预期的 `local_api_unavailable`；HTTP 请求错误、配置错误、任务失败或结果长期无法确认仍显示提示。诊断、更新检查和下载阶段不采用此过渡状态。

IPv4/IPv6 开关控制外层 UDP 41641 的防火墙规则。宣告列表由启用的本机 LAN 网段和启用的自定义列表合并、保序去重；两者均关闭时传空列表。LAN 加 32 个独立自定义网段可合并为 33 条，出口节点默认路由由独立选项控制。Tailscale 自身的 netfilter 保持启用；自定义网段复用现有转发和 NAT 路径。插件 NAT 链放在既有 Fullcone 等 POSTROUTING 钩子之后。

## 核心事务与自动恢复

更新依次执行签名校验、资源预检、下载解包、原子指针切换和本地健康检查。`DATA/update.txn` 记录事务阶段，`DATA/update.state` 保存切换前的身份快照。成功切换记录上一核心；失败时尝试恢复原指针和状态。显式生命周期或核心操作先恢复未完成事务；自动恢复任务遇到未完成事务时跳过，安装与卸载则拒绝继续。

核心切换后的自检比较规范化后的发布版本，带源码提交后缀时另核对签名描述符中的源码提交前缀。自检对本地接口和设备信息的短暂不可用进行有限重试；已授权设备重新要求登录、已知身份改变、版本或源码不符仍判定失败。控制面尚未同步设备 ID，但原节点密钥仍存在且本地生命周期为 `Starting` 或 `Running` 时，可以完成切换并等待后续同步。

自动恢复在服务启用期间按分钟检查。启动宽限期为 180 秒；本地服务连续三次检查失败，或控制连接异常持续至少 600 秒且 WAN 与经验证的 HTTPS 可达时，才进入恢复判断。恢复前再次确认本地状态。等待登录、设备审批、明确停用和关闭同步等状态豁免恢复。

恢复尝试先写入持久记录，再重启插件服务。自动恢复沿用 daemon 持久化的连接意图，保留主动断开、登出或等待登录状态；手动启动在应用托管偏好后通过辅助程序 `connect` 请求连接。尝试间隔至少 1800 秒，任意连续 24 小时最多两次。地址与防火墙维护独立于自动恢复开关。

启动时用同一次 `tailscale set` 应用路由等托管偏好、页面指定的 `--accept-dns=true|false` 及 `--auto-update=false --update-check=false`。DNS 默认关闭，既有身份也按开关执行；CLI 临时改动会在下次应用或启动时被托管配置覆盖。其他未托管偏好保留。自动更新偏好在同次编辑中归一，兼容核心切换和回退。`have_node_key` 继续用于身份与健康检查，状态文件大小不用于身份判定。自动启动不调用 `connect`；手动连接失败进入原有配置恢复流程。

离线冷启动可能在取得控制面信息前保持 `NoState`。启动与核心自检仅在 LocalAPI 正常、节点密钥存在、运行意图为真、未登出且结构化健康信息可读，并且没有 `state-store-health` 警告时，将其视为等待控制同步。其余 `NoState` 不作为启动成功。这个判断不改变看门狗对监测能力的要求。

## 辅助程序接口

| 命令 | 行为 |
| --- | --- |
| `sha256 FILE` | 流式计算常规文件的 SHA-256，输出十六进制摘要 |
| `check-tree ROOT` | 验证安装清单、文件哈希及目录完整性，拒绝越界路径、重复条目和符号链接 |
| `elf BINARY ARCH` | 检查常规文件的 ELF 标识、字节序和 ARM 架构，不执行待检程序 |
| `fifo PATH` | 创建权限为 `0600` 的日志管道，拒绝覆盖已有路径 |
| `temp TEMPLATE` | 按以 `XXXXXX` 结尾的模板独占创建权限为 `0600` 的临时文件，输出路径 |
| `timeout SECONDS COMMAND [ARGS...]` | 直接执行参数，不经过 shell；保留退出码，超时返回 `124` |
| `version BINARY` | 设置 `TS_BE_CLI=1`，在 5 秒内读取 CLI 输出的首个字段，要求为纯 `X.Y.Z` 发布版本 |
| `status SOCKET` | 通过受限 LocalAPI 请求返回状态、健康信息和身份存在标志；不返回原始 state |
| `connect SOCKET` | 读取 prefs，必要时只 PATCH `WantRunning`，再读取状态；仅 `NeedsLogin` 时请求交互式登录。请求失败则非零退出 |
| `routes LIST` | 校验逗号分隔 CIDR，规范化、去重并保序输出；最多 32 项和 2048 字节，失败输出单条 TSV 原因 |
| `routes-wire ENCODED` | 严格解码 `b64.` + 无填充 base64url UTF-8 数据，再执行相同网段校验 |
| `fetch URL DEST MAX_BYTES` | 有大小和时间上限的 HTTPS 下载，仅接受允许的发布及 CDN 主机 |
| `verify ENVELOPE PUBKEY ARCH` | 校验签名与清单，输出所选架构的扁平描述符 |
| `extract ARCHIVE DESCRIPTOR DEST` | 校验归档与核心哈希、大小、结构及 ELF 架构，安全解包 |
| `keygen PRIVATE_FILE PUBLIC_FILE` | 生成十六进制 Ed25519 密钥文件，供构建主机使用 |
| `sign PAYLOAD PRIVATE_FILE ENVELOPE` | 签名核心清单，供构建主机或 CI 使用 |
| `json-get FILE FIELD` | 读取点分字段或数字数组索引 |
| `quote STRING` | 输出 JSON 字符串编码 |
| `atomic-link TARGET LINK` | 原子替换指向 `cores/...` 的相对链接，并同步父目录 |
| `log PATH MAX_BYTES` | 从标准输入读取日志、脱敏凭据，并按上限轮转 |

`status SOCKET` 的 `version_long` 原样保留 LocalAPI 的 `Version`；`version` 只将正式发布格式 `X.Y.Z-t<9 位小写十六进制>` 及可选的 `-g<9 位小写十六进制>` 后缀规范化为 `X.Y.Z`。例如 `1.104.1-t9a522a978` 对应 `1.104.1`。纯数字发布版本保持不变；带开发版、dirty 或其他未知后缀的版本保持原值，不能据此通过与纯数字发布版本的相等检查。

辅助程序的 `health_available` 仅表示结构化健康快照读取成功；它不依赖控制面提供的 `Self` 信息。`monitoring_available` 继续要求完整的连接状态和运行意图数据，供自动恢复判定使用。结构化健康信息不可读时，不能仅凭密钥存在将 `NoState` 认定为正常等待。

`connect` 仅访问固定端点：`GET /localapi/v0/prefs`、必要的 `PATCH /localapi/v0/prefs`、`GET /localapi/v0/status?peers=false` 和按状态选择的 `POST /localapi/v0/login-interactive`。PATCH 请求体固定为 `{"WantRunning":true,"WantRunningSet":true}`，要求 HTTP 200；登录请求要求 2xx。每次请求限时 3 秒，不接受调用者提供路径或请求体。输出包含 `ok`、`want_running_set`、`login_requested`、`backend_state`。

`routes` 不接受空白、主机位、默认路由，以及与 Tailscale 地址空间（含 4via6）、环回、组播、链路本地、未指定和 IPv4 映射 IPv6 范围重叠的前缀。主机位错误给出建议网络地址，不自动更正。错误为 `序号<TAB>条目<TAB>原因` 单条记录，控制字符可见转义，超长错误条目截断；经 JSON 转义后由页面以文本显示。

## 签名清单与核心归档

稳定源为 [core-stable/manifest.json](https://github.com/thetapilla/tailscale-koolshare/releases/download/core-stable/manifest.json)。外层 JSON 包含 `payload` 和 `signature` 两个 Base64 字符串；Ed25519 签名覆盖解码后的原始 payload 字节。

payload 包含：

- `schema: 1`、`channel: "stable"`。
- `version`、`build`、`source_commit`、`recipe_sha256`、RFC 3339 格式的 `created_at`。
- `artifacts.arm` 和 `artifacts.arm64`，各包含 `url`、`size`、`sha256`、`unpacked_size` 和 `binary_sha256`。

采用依赖锁的构建将完整源码与工具链锁的哈希写入 `recipe_sha256`，锁本身随 `build-metadata.json` 发布。锁包含源码归档和官方 Go 归档的 SHA-256，以及静态构建配方哈希。签名前校对锁、总体元数据、每架构构建记录和实际文件；重用已发布锁时先验证签名描述符，防止未认证的元数据改变重建输入。

验证后描述符将所选 artifact 与 `version`、`build`、`arch`、`source_commit`、`recipe_sha256` 合并。核心文件 URL 使用以下格式：

```text
https://github.com/thetapilla/tailscale-koolshare/releases/download/core-v<VERSION>-<BUILD>/tailscale-core_<VERSION>_<BUILD>_<ARCH>.tar.gz
```

清单上限为 64 KiB，归档上限为 13 MiB，核心上限为 12 MiB。核心归档仅包含一个普通文件 `tailscale.combined`，权限为 `0755`。入口链接由安装器或更新器创建。

带版本的核心 Release 及其资源保持不可变。`core-stable` 只承载可更新的签名清单。稳定源检查拒绝版本或构建号倒退；显式本地回退独立处理。

核心配方包含 `ts_omit_clientupdate`，从编译期移除内置更新入口。版本发现和核心替换由插件的签名通道负责。

## 插件包约定

`tools/bundled_core.json` 独立指定安装包预装的核心版本和构建号。`prepare_bundle.py` 从对应不可变核心 Release 读取原签名清单和两个架构归档，验证签名、哈希、结构、架构及共同来源后导入打包输入。插件打包复用原签名封装，不重新签名核心；安装器升级时仍优先保留已有有效核心。

安装包只有一个 `tailscale/` 根目录，包含插件文件、版本、公钥、签名核心清单、`manifest.sha256` 和 `payload/<arch>/`。每个架构 payload 包含核心、辅助程序和描述符。通用包包含两种架构及五个平台标记，平台包仅含相应架构，安装器只安装所选架构。

安装器先检查清单路径和目录中的链接，再选择本机辅助程序。执行辅助程序前，使用固件的 `sha256sum` 或 OpenSSL 核对该程序的清单摘要；两者均不可用时停止安装。通过引导校验后，由辅助程序验证整个安装目录，再加载后台库。核心安装、更新及回滚的 SHA-256 校验直接使用辅助程序。

平台对应关系为 `hnd`、`qca`、`ipq32` → `arm`；`ipq64`、`mtk` → `arm64`。规范输出目录为仓库相邻的 `../dist/`，文件名为 `tailscale_<VERSION>_<PLATFORM>.tar.gz`，共六个安装包及 `SHA256SUMS`。包内版本在归档前写入；页面的脚本 URL 附带内容哈希，同版本替换脚本后也会更新浏览器缓存标识。文件排序、权限、所有者及时间戳统一规范化。
