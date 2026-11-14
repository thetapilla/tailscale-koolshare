# 架构与接口约定

本文写给维护后台、前端、构建工具，或做第三方集成的开发者。用户操作见[使用指南](docs/USER-GUIDE.md)，发布流程见[构建与发布](docs/BUILD.md)。

| 信息 | 以哪里为准 |
| --- | --- |
| 插件版本 | [VERSION](VERSION) |
| 预装核心的版本 | [tools/bundled_core.json](tools/bundled_core.json) |
| 核心的编译选项 | [tools/core_recipe.json](tools/core_recipe.json) |
| 核心的精确源码和工具链 | 对应核心发布的依赖锁 |

## 组件与运行环境

| 组件 | 职责 |
| --- | --- |
| `tailscale_lib.sh` | 后台库，以路由器的 BusyBox ash 为目标，负责生命周期、操作锁、配置、任务、防火墙和自动恢复。 |
| `tailscale_core_lib.sh` | 管理核心版本和持久化的更新事务。 |
| `tsks-helper` | Go 辅助程序，负责有时间或大小上限的本地操作、签名校验和安全解包。 |
| 插件页面 | 原生的软件中心页面，经 httpdb 调用脚本，用结构化的任务结果显示操作进度。 |

安装器与运行时后台共用生命周期锁，防止应用设置、切换核心和安装同时修改状态。

## 目录与文件

默认根目录为 `/koolshare`。测试可以设置 `TSKS_ROOT`、`TSKS_RUN`、`TSKS_WEB`、`TSKS_PROC` 和 `TSKS_SYSFS`，指向隔离的目录。

| 名称 | 默认路径与约定 |
| --- | --- |
| 插件数据 | `/koolshare/tailscale`，下文记为 `DATA` |
| 核心版本 | `DATA/cores/<version>-<build>-<arch>/` |
| 核心指针 | `DATA/current`、`DATA/previous`，是指向 `cores/...` 的相对符号链接 |
| 设备身份 | `/koolshare/configs/tailscale/tailscaled.state`，生命周期操作保留这个文件 |
| 恢复次数记录 | `/koolshare/configs/tailscale/watchdog-ledger` |
| 运行目录 | `/tmp/tailscale3`，下文记为 `RUN` |
| LocalAPI socket | `RUN/tailscaled.sock` |
| 生命周期锁 | `RUN/operation.lock`，使用非阻塞的 `flock`；固件发起的生命周期请求每秒重试一次 |
| 辅助程序 | `/koolshare/bin/tsks-helper` |
| 发布公钥 | `DATA/release.pub` |
| 前端 | `/koolshare/webs/Module_tailscaleks.asp` 和 `/koolshare/res/tailscale3.js` |
| 软件中心模块名 | `tailscaleks`；登记键为 `softcenter_module_tailscaleks_<字段>` |
| 公开任务结果 | `/tmp/upload/tailscale3_<job-id>.json` 和 `.log`，浏览器经 `/_temp/` 读取 |

每个核心目录包含 `tailscale.combined`、`descriptor.json`，以及名为 `tailscale` 和 `tailscaled` 的相对入口链接。公开任务目录只存放状态和脱敏后的日志；身份文件、核心事务快照和密钥放在私有目录。

## 请求与任务协议

- httpdb 把请求 ID 放在脚本的 `$1`，方法参数从 `$2` 开始。生命周期和核心方法也支持在命令行直接调用，这时动作放在 `$1`。
- 前端的请求 ID 限定在 `1` 到 `99999999`，到上限后从 `1` 重新开始，以免软件中心拒绝九位的 ID。
- 后端校验任务 ID 时接受最多 15 位十进制数字，用于查询已有任务；恢复任务时仍生成新的、不超过八位的传输 ID。

| 方法 | 参数 | 用途 |
| --- | --- | --- |
| `tailscale_config` | `web_submit`、`start`、`stop`、`restart`、`start_nat` | 配置及服务生命周期 |
| `tailscale_fettle` | 无 | 结构化状态 |
| `tailscale_status` | 无 | 连接详情任务 |
| `tailscale_tsnets` | `1` | 接口地址与流量 |
| `tailscale_ncheck` | 无 | 网络检查任务 |
| `tailscale_core` | `check`、`update [0\|1]`、`rollback`、`cleanup` | 核心管理任务 |
| `tailscale_job` | 任务 ID | 查询已有任务 |
| `tailscale_diagnostics` | 无 | 诊断摘要任务 |

异步操作取得锁并创建任务后返回：

```json
{"accepted":true,"job_id":"12345"}
```

锁冲突返回 `{"accepted":false,"error":"busy"}`。响应丢失时，前端先按原任务 ID 查询结果，避免重复提交变更。

### 固件发起的请求

固件的 WAN 和 NAT 钩子各自在后台运行，可能同时进入插件，失败后也没有调用方重试。因此：

- 不带请求 ID 的 `start`、`stop`、`restart` 在锁被占用时每秒重试取锁，最长 120 秒，取得锁后记录等了多久；超时返回 `busy` 并写入固件系统日志。带请求 ID 的页面请求仍立即返回 `busy`。
- `start_nat` 不等待，而是留下 `RUN/nat-pending`，由下一次每分钟维护补做。
- 固件发起的生命周期请求最终失败时，包括因未完成的核心事务没能恢复而没有执行的情况，以 `tailscale-plugin` 标签写入固件系统日志，路由器重启后仍可排查。

### 回复编码

脚本的回复统一经过 `ts_reply`，回调为 `POST /_resp/<请求 ID>`。软件中心的 httpdb 会把回调正文原样放进外层 JSON 的一个字符串字段，所以正文回传前必须先做 JSON 字符串转义，再去掉结果最外层的引号。转义由辅助程序完成，curl 以二进制正文发送，换行、反斜杠和 Unicode 的编码都保持不变。

浏览器先解析 HTTP 响应，再把 `response.result` 字符串解析为插件结果；前端也接受对象形式的结果。`GET /_api/tailscale_` 直接返回配置对象数组，不经过脚本回调。在命令行直接调用、没有请求 ID 时，`ts_reply` 把原始的插件 JSON 写到标准输出。

### 任务记录

任务 JSON 以原子方式写入，字段如下：

| 字段 | 含义 |
| --- | --- |
| `schema` | 协议版本，当前为 `1` |
| `id` | 十进制任务 ID 字符串 |
| `state` | `running`、`success`、`failed` 或 `rolled_back` |
| `phase` | 当前执行阶段 |
| `message` | 可以显示的结果或进度信息 |
| `updated_at` | Unix 时间戳 |

查不到任务时，查询接口返回 `state: "unknown"`。后台会尝试取得操作锁，在锁内再查一次；确认任务仍不存在、也没有正在执行的操作，才返回 `operation_busy: false`。客户端核对任务 ID 后停止跟踪这条失效记录，重新读取设置和状态；它不把任务缺失当作成功，也不自动重发变更。`operation_busy` 为 `true` 或缺失时继续跟踪。日志只作为独立文本显示，不用来判断成功或失败。

### 状态与接口

`tailscale_fettle` 返回的状态：

- 字段：`schema`、`enabled`、`plugin_version`、`core_version`、`core_version_long`、`backend_state`、`online`、`health_codes`、`health_messages`、`auth_url`、`monitoring_available`、`routes`，以及 `watchdog` 和 `core` 两个对象。`online` 可以是布尔值或 `null`。读取状态出错时可以附带 `error`。
- `core_version`：用于展示和与发布版本比较的版本号；LocalAPI 不可用时取已安装描述符中的版本。`core_version_long`：daemon 上报的完整版本字符串，用于诊断；还没取得 LocalAPI 版本时为空字符串。版本规范化规则见[辅助程序接口](#辅助程序接口)。
- `watchdog`：`enabled`、`last_recovery`、`count_24h`；恢复记录表示的是尝试次数。
- `core`：`installed`、`available`、`can_rollback`、`installed_build`、`available_build`、`update_available`、`previous`、`previous_build`、`can_cleanup`、`keep_previous`。
  - 构建号来自对应的描述符，可以是 `rN`、`legacy` 或空字符串。页面只在运行版本与已安装版本相符时，才附上已安装的构建号。
  - `update_available` 只在有有效的更新结果、且目标 `cores/<version>-<build>-<arch>` 与当前指针不同时为 `true`，与更新事务判断目标的方式一致。
  - `can_cleanup` 表示核心目录里有当前核心以外的条目。
  - `keep_previous` 是保存的旧核心保留选择，未设置或值无效时为 `true`。
- `routes`：`{"advertised": [], "primary": []}`。辅助程序从偏好的 `AdvertiseRoutes` 和状态的 `Self.PrimaryRoutes` 读取，过滤非法前缀、主机位和默认路由，规范化并去重。`primary` 表示控制面选定由本机承担的网段，某个网段不在其中不等于未批准。

接口流量的响应为 `{"interfaces":[{"if":"tailscale0","ip":"...","rx":0,"tx":0}]}`，`rx`、`tx` 是字节数。连接详情任务通过有时间上限的 CLI 调用生成状态文本，经日志脱敏后显示。

## 配置提交

前端用 `GET /_api/tailscale_` 读取配置。所有选项和网段列表都先改页面草稿，点「应用设置」才提交：调用 `tailscale_config`，参数为 `["web_submit", "<十位配置快照>", "<编码后的网段列表>"]`，请求的 `fields` 保持为空对象。

### 配置快照

快照匹配 `^[01]{10}$`，各位依次对应：

1. `tailscale_enable`
2. `tailscale_ipv4_enable`
3. `tailscale_ipv6_enable`
4. `tailscale_advertise_routes`
5. `tailscale_accept_routes`
6. `tailscale_exit_node`
7. `tailscale_watchdog_enable`
8. `tailscale_accept_dns`（默认 `0`）
9. `tailscale_custom_routes_enable`（默认 `0`）
10. `tailscale_app_connector`（未设置时沿用核心当前的值）

### 网段列表

- `tailscale_custom_routes` 保存规范 CIDR 的逗号分隔明文，默认为空；关闭自定义开关时保留列表。列表最多 32 项、2048 个 UTF-8 字节。
- 线上的第三个参数是 `b64.` 加上无填充 base64url 编码的 UTF-8 列表，空列表为 `b64.`。真实固件的 httpdb 会丢弃空参数，所以不能直接传空字符串。最长的合法输入（32 条 IPv6）已经过实际传输验证。
- 部分固件会把过长的编码请求拒绝为 `result: -6`。前端把它显示为明确的拒绝，保留草稿并解锁，不去查询并不存在的任务。

### 旧格式与校验

- 十位和九位快照必须带编码后的列表；九位快照只更新前九个开关和列表，保留连接器键。
- 七位快照只能带快照这一个参数，只更新原来的七个开关，其余键保持原值。
- 不带快照的旧式 `web_submit` 仍然兼容。
- 快照和列表在受理前同步校验，失败时不创建任务、不写 DBus：快照格式或参数个数不对返回 `{"accepted":false,"error":"invalid_config_snapshot"}`；列表校验失败返回 `{"accepted":false,"error":"invalid_custom_routes","detail":"序号 条目：原因"}`。

### 应用与失败恢复

- 后台取得生命周期锁后才应用配置。
- 备份和恢复包括网段列表字符串；恢复时空值表示删除对应的键。字符串保存在单独的私有文件里，按实际的 DBus 协议只去掉一个记录结尾的换行。
- 连接器新键原本不存在、而十位提交已经改动了核心时，失败恢复会把写入前读到的核心开关保存为迁移值。这样无论原服务处于停用状态，还是到了下次启动，都不会沿用失败操作的新值。
- 正常恢复会重新校验。旧值本来就已损坏时，允许合法的新提交修复它；新提交失败时，只把这份不可信的数据原样恢复并明确告警，启动时仍拒绝使用它。
- 网段列表的值从不作为 Shell 源码执行。
- 查询和核心更新请求不带 DBus 写入字段。

### 页面草稿

- 前端按任务 ID 保存方法、操作意图、目标启用状态、配置快照和明文网段草稿，刷新页面后继续查询同一个任务。
- 判断草稿有没有改动时，比较十位快照和网段列表。旧的七位草稿只恢复前七个开关，旧的九位草稿恢复前九个开关和列表，其余字段取当前配置。
- 连接器键还没迁移时，页面优先采用已读到的核心开关；核心也读不到时，这一项保持待迁移状态，提交九位快照来保留它。
- 提交和执行期间显示进度，失败后保留草稿；网段错误的详情以文本显示，并定位到对应的行。
- 应用设置引起的生命周期变化，以及已进入 `switching` 阶段的核心任务，显示过渡状态，暂不显示预期中的 `local_api_unavailable`；HTTP 请求错误、配置错误、任务失败或结果长期无法确认时照常提示。诊断、更新检查和下载阶段不使用过渡状态。

### 网段与防火墙

- IPv4/IPv6 开关控制外层 UDP 41641 的防火墙规则。
- 插件负责的非默认条目，由启用的本机 LAN 网段和启用的自定义列表按顺序合并、去重；两者都关闭时这部分为空。LAN 加 32 个不同的自定义网段最多合并为 33 条。出口节点的默认路由由单独的选项控制。
- Tailscale 自身的 netfilter 保持启用；自定义网段沿用现有的转发和 NAT 路径。插件的 NAT 链放在已有的 Fullcone 等 POSTROUTING 钩子之后。

## 防火墙规则维护

| 术语 | 含义 |
| --- | --- |
| 固件重建 | 固件用 `iptables-restore` 整表替换 filter 或 nat 表，表里原有的规则和自定义链全部消失 |
| NAT 事件 | 固件应用 nat 表后调用的 `start_nat`；在同一轮重建中，它可能在 filter 表被替换之前就运行 |
| 插件规则 | 插件创建的 `TSKS_*` 链，以及它们在内置链上的跳转 |
| daemon 规则 | tailscaled 创建的 `ts-input`、`ts-forward`、`ts-postrouting` 链及跳转；tailscaled 只在首次配置路由时安装，被清除后不会重装 |
| 规则列表 | `iptables -t filter -S` 与 `iptables -t nat -S` 的成功输出 |

插件以规则列表为准判断规则在不在，不依赖事件的先后顺序。列表命令失败或超时视为未知，这一轮不做判断。只检查 IPv4：固件在同一轮重建中也会处理 IPv6 的 filter 表，重新应用时两个地址族一起处理。

每分钟维护在取得状态采样后读取规则列表：

- daemon 规则的三个跳转（`INPUT`→`ts-input`、`FORWARD`→`ts-forward`、nat 表的 `POSTROUTING`→`ts-postrouting`）任一缺失，且后端状态为 `Running`、主 WAN 已连接时，以自动启动的方式重启服务，由 tailscaled 重新安装。重启沿用持久化的连接意图，不消耗自动恢复的限额，也不受自动恢复开关影响。
- 其他后端状态下 tailscaled 还没安装规则，不算丢失。WAN 未连接时重启会让 daemon 失去网络映射，所以推迟。
- `RUN/netfilter-repairs` 记录连续重启的次数：
  - 看到三个跳转都在时清零；
  - 取得操作锁的 `start`、`stop`、`restart`、`web_submit` 和 `start_nat` 也会清零；
  - 因锁被占用而推迟的 NAT 事件，补做时清零一次；
  - 达到 3 次后停止重启，并记录一次日志；
  - 每次重启前先写入并读回计数，无法记录时不重启。

插件规则在以下任一情况下重新应用：Tailscale 地址与上次成功应用时不同；存在 `RUN/nat-pending`；规则列表中缺少插件的跳转。后两种情况不要求已经取得 Tailscale 地址。

`start_nat` 只重新应用插件规则，从不重启服务。固件在一轮重建中可能多次触发 NAT 事件，事件还可能早于 filter 表被替换；如果每次都重启，会在规则仍被清除的期间就把重启次数用完。daemon 规则交给随后的每分钟维护恢复。新的一轮重建可以解释新的丢失，所以 `start_nat` 会把 `RUN/netfilter-repairs` 清零。

## 宣告列表归属

| 术语 | 含义 |
| --- | --- |
| 连接器 | tailscaled 内置的 App Connector 功能 |
| 域名清单 | 管理控制台分配给本机标签的应用域名 |
| 学习记录 | 连接器记录的域名与地址的对应关系；能否持久保存取决于控制面的 `AppCStoreRoutes` |
| 宣告列表 | `Prefs.AdvertiseRoutes`，包括出口默认条目、LAN、自定义网段和连接器加入的地址 |
| 连接器加入的地址 | 当前宣告列表与学习记录的交集；学习记录中有、但没有实际宣告的地址不算 |
| 批准 | 控制台允许使用宣告的网段；`Self.PrimaryRoutes` 只反映由本机承担主路径的那部分 |
| 转发条目 | 接受子网的设备为获准的前缀在本地安装的条目 |
| 路由参数 | `set` 的 `--advertise-routes` 和 `--advertise-exit-node`，两者都会设置 `AdvertiseRoutesSet` |
| 清空 | 核心收到路由参数后，清除连接器的学习记录和内存中的域名清单 |
| 清空后的学习失效 | 域名清单还没重新下发时，DNS 接口仍可能应答，但连接器学不到新地址 |
| 自查 | 以本机身份查询本机的 peerAPI DNS 接口，让连接器学习已分配的精确域名 |

设 P 为当前宣告列表，D 为插件负责的 LAN、自定义和出口条目，L 为学习地址转成的 `/32`、`/128` 加上 `appc-route-info.Control`：

- 连接器开启时，目标为 `D ∪ (P ∩ L)`；关闭时，目标为 D。学习记录中还没实际宣告的地址，不会仅因为启动而被加入。
- 所有前缀先按 `netip.Prefix.Masked()` 规范化，再按集合比较，忽略顺序和重复。
- 两条默认前缀只由出口开关负责，不从连接器的 Control 条目继承。

只有 P 与目标不同时，那唯一的一次 `tailscale set` 才带上两个路由参数。非默认条目先按插件的输入顺序排列，再接上排序后的连接器条目；出口的两条默认前缀由出口开关处理。这样核心不会因为重复写入 `AdvertiseRoutesSet` 而清空学习记录和内存中的域名清单。`--advertise-connector` 每次都写入；DNS 开关与连接器开关互不相关。

- 连接器键不存在时，启动从已有的就绪状态采样中读取核心开关，确认是布尔值后保存下来；读取失败时停止应用。键已存在时以配置为准。
- 连接器保持开启、目标不变时，保留学习记录；开启连接器或改变它的宣告列表后，尽力执行一次自查。
- 计划计算或配置写入失败，会让本次操作失败；自查失败只写警告。
- 标签、域名分配和访问规则由管理控制台管理。

## 核心事务与自动恢复

### 更新与清理

`tailscale_core update` 可以带一个参数 `0` 或 `1`，表示更新后是否保留旧核心。参数在取锁前校验，受理后写入 `tailscale_core_keep_previous`，写入失败就不执行更新。不带参数时使用已保存的值，未设置或值无效时按保留处理。这个键不属于配置快照，不经「应用设置」提交。

更新依次执行签名校验、资源预检、下载解包、原子切换指针和本地健康检查。`DATA/update.txn` 记录事务阶段，`DATA/update.state` 保存切换前的身份快照。切换成功时记录上一核心；失败时尝试恢复原来的指针和状态。事务提交并清除记录后，才按保留选择删除核心：保留时，删除 `current` 和 `previous` 都不指向的核心目录；不保留时，另外删除 `previous` 及其目标。删除失败只写警告，更新结果仍为成功。资源预检空间不足时，按同样的规则先删除没有指针指向的核心，仍不足再删除上一核心。

`cleanup` 删除 `DATA/cores` 下当前核心以外的全部条目，并移除 `previous`。删除回退目标之前，先校验当前核心的哈希和版本，校验不通过就不删除。有未完成的事务或没有持有锁时拒绝删除；只处理 `DATA/cores` 的直接子项，遇到符号链接只删除链接本身。

显式的生命周期或核心操作，会先恢复未完成的事务；自动恢复任务遇到未完成的事务时跳过；安装和卸载则拒绝继续。

### 切换后的自检

核心切换后的自检比较规范化后的发布版本；版本带源码提交后缀时，另外核对签名描述符中的源码提交前缀。自检对本地接口和设备信息的短暂不可用做有限次重试；已授权的设备重新要求登录、已知身份改变、版本或源码不符时，仍判定失败。控制面还没同步设备 ID，但原节点密钥仍在、且本地生命周期为 `Starting` 或 `Running` 时，可以完成切换，等待之后的同步。

### 自动恢复

自动恢复在服务启用期间每分钟检查一次。启动后有 180 秒宽限期；本地服务连续三次检查失败，或控制连接异常持续至少 600 秒、且 WAN 与经验证的 HTTPS 都可达时，才进入恢复判断。恢复前再确认一次本地状态。等待登录、等待设备审批、明确停用和关闭同步等状态不触发恢复。

恢复尝试先写入持久记录，再重启插件服务。自动恢复沿用 daemon 持久化的连接意图，保留主动断开、登出或等待登录的状态；手动启动则在应用托管的偏好后，通过辅助程序的 `connect` 请求连接。两次尝试至少间隔 1800 秒，任意连续 24 小时内最多两次。地址与防火墙的维护不受自动恢复开关影响。

### 启动时应用的偏好

启动时用同一次 `tailscale set` 应用路由等托管偏好、页面指定的 `--accept-dns=true|false`，以及 `--auto-update=false --update-check=false`。

- DNS 默认关闭，已有的身份也按开关执行；CLI 的临时改动会在下次应用或启动时被托管配置覆盖。其他未托管的偏好保持不变。
- 自动更新偏好在同一次编辑中归一，兼容切换和回退核心。
- `have_node_key` 继续用于身份和健康检查，不用状态文件的大小判断身份。
- 自动启动不调用 `connect`；手动连接失败时进入原有的配置恢复流程。

离线冷启动可能在拿到控制面信息之前一直处于 `NoState`。只有在 LocalAPI 正常、节点密钥存在、运行意图为真、没有登出、结构化健康信息可读，并且没有 `state-store-health` 警告时，启动和核心自检才把它视为等待控制面同步；其余的 `NoState` 不算启动成功。这个判断不改变看门狗对监测能力的要求。

## 辅助程序接口

| 命令 | 行为 |
| --- | --- |
| `sha256 FILE` | 流式计算常规文件的 SHA-256，输出十六进制摘要 |
| `check-tree ROOT` | 验证安装清单、文件哈希和目录完整性，拒绝越界路径、重复条目和符号链接 |
| `elf BINARY ARCH` | 检查常规文件的 ELF 标识、字节序和 ARM 架构，不执行被检查的程序 |
| `fifo PATH` | 创建权限为 `0600` 的日志管道，不覆盖已有路径 |
| `temp TEMPLATE` | 按以 `XXXXXX` 结尾的模板独占创建权限为 `0600` 的临时文件，输出路径 |
| `timeout SECONDS COMMAND [ARGS...]` | 直接执行参数，不经过 shell；保留退出码，超时返回 `124` |
| `version BINARY` | 设置 `TS_BE_CLI=1`，在 5 秒内读取 CLI 输出的第一个字段，要求是纯 `X.Y.Z` 发布版本 |
| `status SOCKET` | 通过受限的 LocalAPI 请求返回状态、健康信息和身份存在标志；不返回原始 state |
| `connect SOCKET` | 读取 prefs，必要时只 PATCH `WantRunning`，再读取状态；只在 `NeedsLogin` 时请求交互式登录。请求失败时以非零码退出 |
| `routes LIST` | 校验逗号分隔的 CIDR，规范化、去重并保序输出；最多 32 项、2048 字节，失败时输出单条 TSV 原因 |
| `routes-wire ENCODED` | 严格解码 `b64.` + 无填充 base64url 的 UTF-8 数据，再做同样的网段校验 |
| `routes-plan SOCKET LIST EXIT CONNECTOR` | 读取偏好，按需读取学习记录，按归属规则输出 `keep` 或 `set<TAB>非默认条目列表`；失败时退出码为 1，并输出固定的英文原因 |
| `appc-prime SOCKET` | 查询分配给本机的精确域名并等待学习，输出分配、查询、学习的计数和拒绝标志；本地读取出错时退出码为 1，部分查询失败时仍返回统计 |
| `fetch URL DEST MAX_BYTES` | 有大小和时间上限的 HTTPS 下载，只接受允许的发布、CDN 和镜像主机；发布地址失败后改用镜像，由镜像完成时输出 `mirror` |
| `verify ENVELOPE PUBKEY ARCH` | 校验签名和清单，输出所选架构的扁平描述符 |
| `extract ARCHIVE DESCRIPTOR DEST` | 校验归档和核心的哈希、大小、结构和 ELF 架构，安全解包 |
| `keygen PRIVATE_FILE PUBLIC_FILE` | 生成十六进制的 Ed25519 密钥文件，供构建主机使用 |
| `sign PAYLOAD PRIVATE_FILE ENVELOPE` | 签名核心清单，供构建主机或 CI 使用 |
| `json-get FILE FIELD` | 读取点分字段或数组的数字索引 |
| `quote STRING` | 输出 JSON 字符串编码 |
| `atomic-link TARGET LINK` | 原子替换指向 `cores/...` 的相对链接，并同步父目录 |
| `log PATH MAX_BYTES` | 从标准输入读取日志，脱敏凭据，并按上限轮转 |

### 版本规范化

`status SOCKET` 的 `version_long` 原样保留 LocalAPI 的 `Version`。`version` 只把正式发布格式 `X.Y.Z-t<9 位小写十六进制>`（可选再带 `-g<9 位小写十六进制>` 后缀）规范化为 `X.Y.Z`，例如 `1.104.1-t9a522a978` 对应 `1.104.1`。纯数字的发布版本保持不变；带开发版、dirty 或其他未知后缀的版本保持原值，不能借此通过与纯数字发布版本的相等检查。

辅助程序的 `health_available` 只表示结构化的健康快照读取成功，不依赖控制面提供的 `Self` 信息。`monitoring_available` 仍要求完整的连接状态和运行意图数据，供自动恢复判断使用。结构化健康信息读不到时，不能只凭密钥存在就把 `NoState` 认定为正常等待。

### `connect`

`connect` 只访问固定的端点：`GET /localapi/v0/prefs`、必要时的 `PATCH /localapi/v0/prefs`、`GET /localapi/v0/status?peers=false`，以及按状态决定是否调用的 `POST /localapi/v0/login-interactive`。PATCH 的请求体固定为 `{"WantRunning":true,"WantRunningSet":true}`，要求返回 HTTP 200；登录请求要求 2xx。每个请求限时 3 秒，不接受调用者提供的路径或请求体。输出包含 `ok`、`want_running_set`、`login_requested`、`backend_state`。

### `routes`

`routes` 拒绝空白、主机位、默认路由，以及与 Tailscale 地址空间（含 4via6）、环回、组播、链路本地、未指定和 IPv4 映射 IPv6 范围重叠的前缀。有主机位时给出建议的网络地址，但不自动更正。错误是单条 `序号<TAB>条目<TAB>原因` 记录，控制字符转义为可见形式，过长的错误条目会被截断；经 JSON 转义后由页面以文本显示。

### 应用连接器

`status.app_connector` 包含 `advertise`、`tags`、`assigned_domains`、`wildcards`、`learned` 和 `error`。

- `advertise` 来自本次读取的 prefs，未知时为 `null`；标签和应用配置来自读取到的 Self。
- 连接器开启时才额外请求一次 `appc-route-info`。
- 应用的 `connectors` 包含 `*` 或本机标签时，视为分配给本机，结果去重；学习地址来自 `Domains`，页面只作文本展示。
- 读取学习记录失败，不会把已知的开启值改成关闭。

`appc-prime` 只接受 LocalAPI 给出的 peerAPI 地址，主机必须是本机的 Tailscale IP，优先使用 IPv4，禁止跳转到其他主机。每个精确域名发送 A 和 AAAA 两个 RFC 8484 GET 查询；`*.` 通配域名只计数。查询和轮询都有总时限，Shell 另外设了 20 秒上限。输出为 `{"assigned":N,"primed":M,"learned":K,"wildcards":W,"denied":false}`；没能全部学到或收到 403 时，服务照常启动并记录警告。这个操作不修改设备标签或身份。

## 签名清单与核心归档

稳定源为 [core-stable/manifest.json](https://github.com/thetapilla/tailscale-koolshare/releases/download/core-stable/manifest.json)。外层 JSON 包含 `payload` 和 `signature` 两个 Base64 字符串；Ed25519 签名覆盖解码后的原始 payload 字节。

payload 包含：

- `schema: 1`、`channel: "stable"`。
- `version`、`build`、`source_commit`、`recipe_sha256`，以及 RFC 3339 格式的 `created_at`。
- `artifacts.arm` 和 `artifacts.arm64`，各自包含 `url`、`size`、`sha256`、`unpacked_size` 和 `binary_sha256`。

使用依赖锁的构建，把完整的源码与工具链锁的哈希写入 `recipe_sha256`，锁本身随 `build-metadata.json` 发布。锁包含源码归档和官方 Go 归档的 SHA-256，以及静态构建配方的哈希。签名前核对锁、总体元数据、每种架构的构建记录和实际文件；重用已发布的锁时，先验证签名描述符，防止未经认证的元数据改变重建的输入。

验证后的描述符把所选架构的 artifact 与 `version`、`build`、`arch`、`source_commit`、`recipe_sha256` 合在一起。核心文件的 URL 格式为：

```text
https://github.com/thetapilla/tailscale-koolshare/releases/download/core-v<VERSION>-<BUILD>/tailscale-core_<VERSION>_<BUILD>_<ARCH>.tar.gz
```

清单上限 64 KiB，归档上限 13 MiB，核心上限 12 MiB。核心归档只包含一个普通文件 `tailscale.combined`，权限为 `0755`；入口链接由安装器或更新器创建。

带版本的核心 Release 及其资源保持不可变，`core-stable` 只承载可以更新的签名清单。稳定源检查拒绝版本或构建号倒退；用户在本地显式回退另行处理。

核心配方包含 `ts_omit_clientupdate`，在编译期去掉内置的更新入口。版本发现和核心替换都由插件的签名通道负责。

## 下载镜像

| 术语 | 含义 |
| --- | --- |
| 发布地址 | `https://github.com/thetapilla/tailscale-koolshare/releases/download/` 下的文件 |
| 镜像 | 辅助程序 `mirrorHost` 常量指定的 HTTPS 主机，按发布地址之后的相对路径保存同一文件的副本 |
| 稳定清单 | `core-stable/manifest.json`，签名的更新清单 |

镜像只是第二条下载线路，不参与信任：清单的签名和归档的哈希仍由插件用公钥校验，镜像内容被替换只会导致更新失败。镜像只接受稳定清单和 `core-v<VERSION>-<BUILD>/tailscale-core_<VERSION>_<BUILD>_<ARCH>.tar.gz` 两类路径，不接受查询参数、端口和重定向；清单中签名的下载地址仍是发布地址。

`fetch` 先请求发布地址，允许重定向到 GitHub 的资源主机，总时限 180 秒。连接失败、状态码不是 200、超出大小上限、30 秒收不到数据或超过总时限时，改为请求镜像：总时限 480 秒，同样在 30 秒收不到数据时放弃。两处都失败时返回两处的原因，不写入目标文件。请求的 `User-Agent` 为 `tsks-helper`。

核心发布工作流的 `mirror` 任务在稳定清单更新后运行 `tools/mirror_core.py`：验证清单签名，按清单核对每个归档的大小和 SHA-256，先上传归档、最后上传清单，所以镜像上的清单不会指向还没上传的归档。归档以不可变的缓存策略保存，清单要求每次重新验证。已存在且大小相符的归档不重复上传。

## 插件包约定

`tools/bundled_core.json` 单独指定安装包预装的核心版本和构建号。`prepare_bundle.py` 从对应的不可变核心 Release 读取原签名清单和两种架构的归档，验证签名、哈希、结构、架构和共同来源后，导入打包输入。插件打包复用原来的签名封装，不重新签名核心；安装器升级时仍优先保留已有的有效核心。

安装包只有一个 `tailscaleks/` 根目录，包含插件文件、版本、公钥、签名的核心清单、`manifest.sha256` 和 `payload/<arch>/`。每种架构的 payload 包含核心、辅助程序和描述符。通用包包含两种架构和五个平台标记，平台包只含对应的架构；安装器只安装所选的架构。

软件中心按模块名把已安装的插件与在线目录对应起来，并按 `_` 拆分登记键，所以模块名不能含下划线和连字符，且必须与安装包根目录、`webs/Module_<模块名>.asp`、`res/icon-<模块名>.png` 和 `scripts/uninstall_<模块名>.sh` 一致。3.3.0 及更早的版本使用插件市场原版的模块名 `tailscale`；安装器把旧的登记键和旧文件纳入事务，成功后移除，失败时恢复。设置键（`tailscale_*`）、`DATA`、设备身份和脚本文件名不随模块名改变。登记的字段为 `version`、`install`、`name`、`title`、`description` 和 `author`。

安装器先检查清单路径和目录中的链接，再选择本机的辅助程序。执行辅助程序之前，用固件的 `sha256sum` 或 OpenSSL 核对它在清单中的摘要；两者都不可用时停止安装。通过这一步引导校验后，由辅助程序验证整个安装目录，再加载后台库。核心安装、更新和回滚的 SHA-256 校验直接使用辅助程序。

升级失败、恢复旧版本时，安装器还原文件、DBus 和身份快照，再直接用还原的 state 启动旧的 daemon。这一步不重新应用候选版本的托管偏好，也不请求连接或登录；就绪检查通过后，恢复定时任务和网络规则。状态存储出错或没能确认就绪时，保留安装备份并报告失败。

平台对应关系：`hnd`、`qca`、`ipq32` → `arm`；`ipq64`、`mtk` → `arm64`。规范的输出目录为仓库旁边的 `../dist/`，文件名为 `tailscaleks_<VERSION>_<PLATFORM>.tar.gz`，共六个安装包加 `SHA256SUMS`。包内版本在归档前写入；页面的脚本 URL 附带内容哈希，同版本替换脚本后，浏览器的缓存标识也会更新。文件的排序、权限、所有者和时间戳统一规范化。
