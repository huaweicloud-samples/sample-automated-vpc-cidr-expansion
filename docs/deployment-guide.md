# 部署指导

本文给出从零开始到可验证部署的完整命令序列，每一步都写明确切预期输出；
并附一次端到端动手练习、全部 63 项部署参数的类型与默认值、故障排查表与资源清理范围。
架构与设计取舍见 [docs/architecture.md](architecture.md)，功能概览见 [README.md](../README.md)。

命令约定：标注「仓库根目录」的命令在克隆下来的仓库根执行；进入 `infra` 之后（`cd infra`）
的命令都在 `infra/` 目录内执行，路径全部是仓库内相对路径。

## 目录

- [部署前准备](#部署前准备)
- [步骤 1：构建两个函数代码包](#步骤-1构建两个函数代码包)
- [步骤 2：准备参数文件](#步骤-2准备参数文件)
- [步骤 3：初始化与静态校验](#步骤-3初始化与静态校验)
- [步骤 4：预览执行计划](#步骤-4预览执行计划)
- [步骤 5：执行部署](#步骤-5执行部署)
- [步骤 6：确认状态存储三张表就绪](#步骤-6确认状态存储三张表就绪)
- [步骤 7：验证观测链路](#步骤-7验证观测链路)
- [步骤 8：端到端动手练习](#步骤-8端到端动手练习)
- [参数表（全部 63 项）](#参数表全部-63-项)
- [常见故障与排查](#常见故障与排查)
- [清理资源](#清理资源)
- [交付验收清单](#交付验收清单)

## 部署前准备

### 1. 工具与凭据

| 项目 | 要求 | 说明 |
|-|-|-|
| Terraform | `>= 1.5` | 版本约束见 `infra/versions.tf` |
| huaweicloud Provider | `~> 1.96.1` | `terraform init` 自动拉取，无需预装 |
| Python | 3.9 及以上 | 用于跑离线单元测试、构建代码包与生成测试事件；构建脚本只依赖标准库 |
| 区域 | `cn-north-4`（默认） | 本实践在该区域验证；换区域需同步改四个参数，见下方第 3 节 |
| 访问密钥 | 账号的密钥 ID 与秘密密钥 | 在控制台「服务与支持 → 我的凭证 → 我的访问密钥」获取 |

凭据只放在执行 Terraform 的环境里，不要写进 `terraform.tfvars`、代码或仓库任何文件：

```bash
export HW_ACCESS_KEY="<HW_ACCESS_KEY>"
export HW_SECRET_KEY="<HW_SECRET_KEY>"
# 使用临时安全令牌时另加：
# export HW_SECURITY_TOKEN="<临时安全令牌>"
```

Windows PowerShell 用 `$env:HW_ACCESS_KEY = "<HW_ACCESS_KEY>"` 形式设置。
函数运行期不需要任何凭据：它使用本栈创建的委托签发的临时凭据，
`DDB_PASSWORD` 是状态存储实例的数据库账号口令（即 `geminidb_password`），由部署参数下发。

区域与项目由 Provider 读取上述环境变量完成鉴权，本栈不额外声明凭据资源。

### 2. 两条必须先确认的硬前提

**(1) 状态存储实例的规格码与接口版本必须按目标区域实查。**

`geminidb_spec_code` 与 `geminidb_engine_version` 没有可用默认值——示例参数文件里写的是
`<GEMINIDB_SPEC_CODE>` 与 `<GEMINIDB_DDB_INTERFACE_VERSION>`，沿用占位值会在创建实例这一步失败。
实查方式（首次部署前推荐前者）：

- **控制台购买页**：进入 GeminiDB 的实例购买页，选择「DynamoDB 兼容接口」，按区域与可用区读出
  「接口版本 + 规格码 + 存储类型」的真实可组合同，把规格码原样填进 `geminidb_spec_code`。
- **数据源核对接口版本**：`huaweicloud_geminidb_configuration_datastores` 是只读数据源，
  返回区域内可用的「接口名称 + 实例形态 + 版本」清单，可用来核对
  `geminidb_datastore_type` / `geminidb_engine_version` / `geminidb_instance_mode` 三者是否成组可用。
  在临时目录里声明它并 `terraform plan` 即可打印结果，不创建任何资源。

  ```hcl
  data "huaweicloud_geminidb_configuration_datastores" "probe" {
    region = "cn-north-4"
  }
  ```

  请注意 `huaweicloud_geminidb_available_flavors` 需要传入一个已存在实例的 `instance_id`，
  它回答的是「这台实例还能改成哪些规格」，因此不能用于首次部署前的规格实查。

本栈固定接口类型为 `DynamoDB`、存储引擎为 `rocksDB`；实例形态默认 `Cluster`、节点数默认 3、
单节点存储默认 100 GB、存储类型默认 `ultra`。实查得到的规格码必须与这四项组合相容，
且实例可用区（`geminidb_availability_zones`）要落在基线子网所在可用区之内。

**(2) `notification_email` 必须是真实可收信的邮箱。**

SMN 邮件订阅在服务侧校验端点格式，占位邮箱会让订阅创建失败；订阅创建成功后还要在收到的
确认邮件里点击确认，否则结果通知与失败事件都不会投递。验证完成后可以按需退订。

### 3. 需要提前准备的外部信息

| 信息 | 用途 | 对应参数 |
|-|-|-|
| VPC 主网段 | 地址基线；决定首次扩容走哪条路径 | `vpc_cidr` |
| 预留的附加网段候选清单 | 按优先级排序的地址规划，函数只从这份清单里取 | `planned_secondary_cidrs` |
| 子网分层规划 | 每层名称、样例子网名与网段、所在可用区、是否参与地址扩展 | `availability_zones`、`baseline_subnets` |
| 状态存储口令 | 实例初始账号口令，同时是函数侧 `DDB_PASSWORD` | `geminidb_password` |
| 收件邮箱 | 结果通知与失败事件的收件地址 | `notification_email` |
| 企业项目 ID | 资源归属，默认 `0`（默认企业项目） | `enterprise_project_id` |
| 触发线与二次校验下限 | 巡检何时投指令、扩容何时才动手 | `utilization_threshold`、`warning_threshold` |

`baseline_subnets` 里至少要有 1 条子网（参数校验会拦住空清单），且每条 `cidr` 必须是合法 IPv4 网段
——这两条是 `infra/variables.tf` 里显式声明的校验，`terraform validate` 就能拦住。
层级名（`tier`）同时决定该层安全组名 `<name_prefix>-sg-<tier>`；`expandable = false` 的层级
不参与地址扩展，函数不会在那一层追加子网。

## 步骤 1：构建两个函数代码包

`terraform plan` / `apply` 会读取这两个 zip 文件（`huaweicloud_obs_bucket_object` 的 `source` 指向它们），
所以必须在部署前构建。仓库根目录执行：

```bash
python src/functions/build_packages.py
```

预期输出（两个包各一段，字节数与校验值随源码变更）：

```text
monitor -> src/functions/build/cidr-monitor.zip (47240 bytes)
  sha256: 221df7380e6da87e2eca9e3fd066b3462ce21ac3691c2c73eb95e45b7d0af234
  - ddb_signer.py
  - handler_errors.py
  - history_writer.py
  - hw_cloud_api.py
  - hw_signer.py
  - hw_transport.py
  - invocation.py
  - net_plan.py
  - state_store.py
  - dispatcher.py
  - index.py
  - inspector.py
  - metrics.py
  - monitor_conf.py
  - snapshot.py
expansion -> src/functions/build/cidr-expansion.zip (...)
  sha256: ...
```

核对两点：

1. 打印的两个包路径与 `var.monitor_package` / `var.expansion_package` 的默认值逐字一致
   （`../src/functions/build/` 是相对 `infra/` 的路径）；
2. 包内条目是共用模块与各自源码目录的 `.py` 文件拍平到 zip 根目录，不含 `tests/`、`build/` 与构建脚本；
   入口契约 `index.main_handler` 由构建脚本在打包前静态校验，`index.py` 必须在根目录。

只想构建一个包：`python src/functions/build_packages.py monitor` 或 `expansion`。

同时跑一次离线测试，确认链路判定分支在你机器上全部通过：

```bash
cd src/functions
python -m pytest tests -q
```

预期输出末行统计为 395 条用例全部通过（耗时随机器略有差异）：

```text
........................................................................ [ 91%]
...................................                                      [100%]
395 passed in 0.41s
```

这一步不联网：云端交互全部由仓库内自带的替身接管，因此可以在任何环境执行，
也是「改动函数代码后不必先部署即可验证」的入口。回到仓库根目录继续下一步（`cd ../..`）。

## 步骤 2：准备参数文件

```bash
cd infra
copy terraform.tfvars.example terraform.tfvars    # Windows
# cp terraform.tfvars.example terraform.tfvars    # Linux/macOS
```

编辑 `terraform.tfvars`，把全部尖括号占位替换为真实取值：

| 占位符 | 替换为 | 备注 |
|-|-|-|
| `<GEMINIDB_SPEC_CODE>` | 区域内实查到的规格码 | 见「硬前提 (1)」 |
| `<GEMINIDB_DDB_INTERFACE_VERSION>` | 区域内已上架的 DynamoDB 兼容接口版本 | 与本栈的 `geminidb_datastore_type = "DynamoDB"` 组合可用 |
| `<GEMINIDB_PASSWORD>` | 实例初始账号口令 | 由你自备强口令；该值同时下发为函数的 `DDB_PASSWORD` |
| `ops@example.com` | 真实收件邮箱 | 示例文件给的是一个可通过格式校验的占位地址，必须换成能收信的邮箱 |

三条建议按自己的环境改写而不是沿用默认的参数：

- `baseline_subnets`：决定链路的层级结构与子网名序号起点；
- `planned_secondary_cidrs`：决定"扩到哪"，必须与主网段和现有子网都不重叠；
- `utilization_threshold` 与 `warning_threshold`：前者触发投递、后者是动手下限，
  代码要求 `utilization_threshold >= warning_threshold`，反过来会在函数启动时报配置错误。

口令与密钥类取值不要提交到仓库；`geminidb_password` 声明为敏感参数，
不会出现在 `terraform plan` / `apply` 的输出与状态回显里。

## 步骤 3：初始化与静态校验

```bash
terraform init
terraform validate
terraform fmt -check
```

| 命令 | 预期输出 |
|-|-|
| `terraform init` | 末尾出现 `Terraform has been successfully initialized!`，列出已安装的 `huaweicloud` Provider |
| `terraform validate` | `Success! The configuration is valid.`（若首次运行需要下载模块或插件，会有相应提示） |
| `terraform fmt -check` | 无输出、退出码 0（表示配置已是格式化后的写法） |

`validate` 只检查语法与参数校验规则，不联网也不读云端。此时如果 `baseline_subnets` 的
`cidr` 写法非法、清单为空，或 `history_retention_days` 不大于 0，都会在这里被拦住。

代码包文件是否存在要到 `plan` 才检查得到（`source` 属性需要读文件内容），所以下一步若报
读文件失败，请回到步骤 1 重新构建。

## 步骤 4：预览执行计划

```bash
terraform plan
```

末尾一行必须是：

```text
Plan: 35 to add, 0 to change, 0 to destroy.
```

这个数字与本栈声明的资源块数量对应：`infra/` 下共 35 个资源块与 2 个数据源
（数据源只在计划期内求值，不进入 add/change/destroy 统计）。资源清单与用途见
[README.md 的涉及云服务](../README.md#涉及云服务)。

计划里值得先看一眼的几处：

- `huaweicloud_vpc_address_group.expansion`：`addresses` 会是 `planned_secondary_cidrs` 的全部条目，
  `max_capacity` 为 `expansion_address_group_capacity`；
- `huaweicloud_networking_secgroup_rule.expansion_cidr`：入站规则的目的地是地址组标识而不是某个网段，
  这是"改完即通"的落点；
- `huaweicloud_geminidb_instance.state_store`：`datastore.type` 为 `DynamoDB`、
  `storage_engine` 为 `rocksDB`，子网为 `geminidb_data_subnet_name` 对应的基线子网；
- 两个 `huaweicloud_fgs_function`：`user_data` 里能看到运行配置键名清单，取值为占位符或真实值；
- **三张状态表不会出现在计划里**：它们由函数首次运行时按需创建，不属于部署栈声明的资源。

## 步骤 5：执行部署

```bash
terraform apply
```

Review 阶段确认后开始创建，预期末尾输出：

```text
Apply complete! Resources: 35 added, 0 changed, 0 destroyed.
```

随后打印 26 个输出项。按用途分三组核对：

**网络定位**

```bash
terraform output vpc_id
terraform output vpc_main_cidr
terraform output default_route_table_id
terraform output expansion_address_group_id
terraform output tier_security_group_ids
terraform output baseline_subnet_ids
terraform output internet_gateway_id
terraform output function_subnet_id
terraform output expansion_tier_plan
```

`expansion_tier_plan` 应打印 `public`、`private`、`ops` 三个层级（默认参数下
`data` 与 `svc` 两层 `expandable = false`，不参与扩展），每个层级带样例子网名、可用区集合与掩码 24。
`tier_security_group_ids` 应包含全部层级（包括不参与扩展的层级）的安全组标识。

**函数、消息与日志**

```bash
terraform output monitor_function_urn
terraform output expansion_function_urn
terraform output commands_topic_urn
terraform output notifications_topic_urn
terraform output dead_letter_topic_urn
terraform output monitor_cron
terraform output log_group_id
terraform output dashboard_name
```

`monitor_cron` 回显定时表达式（默认 `0 */5 * * * ?`）；`dashboard_name` 为
`<name_prefix>-capacity`（`enable_dashboard = false` 时该项为空）。

**存储与运行配置**

```bash
terraform output state_store_endpoint
terraform output state_store_port
terraform output state_store_tables
terraform output assets_bucket
terraform output history_object_prefix
terraform output kms_key_id
terraform output agency_name
terraform output monitor_env_keys
terraform output expansion_env_keys
```

`state_store_endpoint` 是 VPC 内私有地址（因此函数必须挂进本 VPC 才可达），
`state_store_tables` 打印三张表名 `<前缀>-locks` / `<前缀>-config` / `<前缀>-history`，
最后两项是两个函数读取的环境变量键名清单，可直接与 `infra/locals.tf` 对照。

## 步骤 6：确认状态存储三张表就绪

三张表不在 Terraform 里声明，由函数首启创建，所以 `apply` 完成时它们还不存在。归属如下：

| 表 | 由谁创建 | 时机 |
|-|-|-|
| `<前缀>-config` | 巡检函数 | 每轮巡检的第一步：先确保配置表存在，再写回观测值 |
| `<前缀>-locks` | 扩容函数 | 取锁之前；即使第一条指令先于首次巡检到达也不会失败 |
| `<前缀>-history` | 扩容函数 | 与锁表同批确保存在；建不出来只记警告，不阻断扩容 |

因此确认路径是"先跑一轮巡检，再看日志"：

1. 等一个定时周期，或按步骤 7 手工触发一次；
2. 在 LTS 的 `<前缀>-monitor` 日志流里查找 `config_table_ensured`（首次还有 `table_created=<表名>`）；
3. 首条扩容指令被消费时，在 `<前缀>-expansion` 日志流里查找 `state_table_created`。

表建不出来的常见原因：函数子网与实例所在子网不通（检查 `function_subnet_name` 与
`geminidb_data_subnet_name` 都在本 VPC 内）、`DDB_PASSWORD` 与实例账号口令不一致、
或实例仍在创建中（`lb_ip_address` 尚未可用）。

## 步骤 7：验证观测链路

1. 取输出项：`terraform output monitor_function_urn`、`terraform output vpc_id`、
   `terraform output state_store_tables`。
2. 在函数控制台选中巡检函数，用「同步测试」传入这份事件（函数不依赖事件内容，只为日志标记来源）：

   ```json
   { "trigger_type": "TIMER" }
   ```

3. 预期返回形如（字段名与 `src/functions/monitor/index.py` 一致）：

   ```json
   {
     "execution_id": "1f2e3d4c...",
     "trigger": "TIMER",
     "monitored_count": 1,
     "observed": 1,
     "observe_failed": 0,
     "metrics_reported": { "samples": 4, "batches": 1, "failed_batches": 0 },
     "decision_counts": { "below_threshold": 1 },
     "decisions": [ { "vpc_id": "<vpc_id>", "utilization": 3.16, "next_cidr": "10.1.0.0/16",
                      "dispatched": false, "reason": "below_threshold" } ],
     "threshold": 85.0,
     "dispatches": [],
     "snapshot": { "written": true, "key": "cidr-monitor-snapshot/<日期>/<执行编号>.json" }
   }
   ```

   刚部署完的地址几乎没有被占用，`reason` 为 `below_threshold` 属预期结论；
   `monitored_count` 至少为 1 是因为本栈给 VPC 打了 `MonitorCIDR=true` 标签。
4. 到云监控控制台自定义命名空间 `vpc_cidr_expansion.custom`、维度 `VpcId` 下确认四条序列
   `UtilizationPercentage`、`AvailableIPs`、`TotalCapacity`、`SubnetCount` 都有数据；
   大盘 `<name_prefix>-capacity` 的四个图件随之出现曲线。

若 `utilization` 为 `null` 且 `metrics_reported.samples` 为 3：说明子网余量字段读不到，
函数按口径不猜测数值，该 VPC 不投指令、利用率序列也不上报；先查函数的网络接入与委托权限。

## 步骤 8：端到端动手练习

这条路径把「指令投递 → 取锁 → 附加网段 → 建子网 → 网段联动 → 台账 → 通知」全部走一遍，
不需要先把地址用满。第 1、3 步在 `infra/` 或仓库根目录执行，其余在控制台完成。

1. **确认栈下发的规划与定位**（`infra/` 目录）：

   ```bash
   terraform output expansion_tier_plan
   terraform output vpc_id
   terraform output commands_topic_urn
   terraform output notifications_topic_urn
   terraform output state_store_tables
   ```

2. **确认订阅已确认**：在 `notification_email` 邮箱里点击 SMN 的确认邮件；未确认时通知不会投递。

3. **构造一条扩容指令**（仓库根目录执行，只依赖标准库）：

   ```bash
   python -c "import json;print(json.dumps({'vpc_id':'<把 output vpc_id 的值贴在这里>','reason':'manual-drill','utilization':92.0},ensure_ascii=False))"
   ```

   输出形如 `{"vpc_id": "xxx", "reason": "manual-drill", "utilization": 92.0}`。

4. **发布到指令主题**：在 SMN 控制台选中 `<前缀>-commands` 主题，「发布消息」时把上一步的 JSON 作为
   消息正文。扩容函数由消息触发器唤起并异步执行。

   `utilization` 传 92.0 的作用：扩容侧只把「巡检算好的利用率」作为二次校验输入，
   传入高于 `warning_threshold`（默认 80）的值即可让链路真正动手，而不必先耗尽地址。

   默认 `vpc_cidr = 10.0.0.0/16` 时，空闲地址（63744）大于一次全层级铺开所需的 1280 个地址，
   因此本次走 `existing_space`：不新增网段，只在现有空闲块上铺子网。
   要观察「消费规划网段」这一步，把主网段收紧——例如 `vpc_cidr = 10.0.0.0/21`
   并把 7 条基线子网全部放进该网段——重新 `terraform apply` 后再投递一次指令，
   此时结果里 `mode` 为 `secondary_cidr`、`new_cidr` 为 `10.1.0.0/16`。

5. **查看这一次执行的返回结果**（函数控制台「执行结果」），字段与 `expansion_flow.run()` 一致：

   ```json
   {
     "status": "SUCCESS",
     "vpc_id": "<vpc_id>",
     "vpc_name": "vpc-cidr-expansion-vpc",
     "execution_id": "1f2e3d4c...",
     "mode": "secondary_cidr",
     "new_cidr": "10.1.0.0/16",
     "utilization": 92.0,
     "required_addresses": 1280,
     "cidr_count_after": 2,
     "subnet_retry_budget_seconds": 90,
     "subnet_retry_used_seconds": 0,
     "subnets_created": [ { "tier": "public", "name": "vpc-cidr-expansion-public-3",
                             "cidr": "10.0.7.0/24", "availability_zone": "cn-north-4a",
                             "gateway_ip": "10.0.7.1", "id": "<子网标识>" } ],
     "subnets_failed": [],
     "address_group": { "updated": false, "reason": "已是成员" },
     "route_check": { "checked": true, "covered": true, "matched": "10.1.0.0/16" },
     "history_table": "vpc-cidr-expansion-history",
     "history_written": true,
     "lock": { "released": true, "reason": "released" }
   }
   ```

   `address_group.updated` 为 `false`、`reason` 为「已是成员」是默认参数下的正常结论：
   地址组在部署期就把全部规划网段纳入，运行期只是确认成员关系。把 `infra/network.tf` 的
   `addresses` 改为只放已启用网段后，这里会变成 `{"updated": true, ...}`。

6. **查收通知邮件**：标题为 `[SUCCESS] VPC CIDR Expansion - <账号> (cn-north-4)`（标题按纯 ASCII 生成），
   正文由 `src/functions/expansion/notify_text.py` 拼装、按中文定宽字段输出，逐个列出每个新建子网的
   名称、网段、可用区与标识：

   ```text
   VPC 网段自动扩容 - 成功

   账号               <账号ID>
   区域               cn-north-4
   VPC              <vpc_id> (vpc-cidr-expansion-vpc)
   触发原因             manual-drill
   触发利用率            92.0%
   判定阈值             80.0%
   扩展方式             消费规划网段后铺开子网
   新增附加网段           是 (10.1.0.0/16)
   子网创建数            5
       - vpc-cidr-expansion-public-3  10.0.7.0/24  cn-north-4a (<子网标识>)
       - vpc-cidr-expansion-public-4  10.1.0.0/24  cn-north-4b (<子网标识>)
       - vpc-cidr-expansion-app-private-3  10.1.1.0/24  cn-north-4a (<子网标识>)
       - vpc-cidr-expansion-app-private-4  10.1.2.0/24  cn-north-4b (<子网标识>)
       - vpc-cidr-expansion-ops-private-2  10.1.3.0/24  cn-north-4a (<子网标识>)
   地址组联动            已是成员
   路由覆盖             已覆盖 (10.1.0.0/16)
   网段数量             1 -> 2
   扩容后可用地址          65280

   执行编号: 1f2e3d4c...
   耗时: 41s
   留痕: 表 vpc-cidr-expansion-history：已写入；对象 expansion-history/<vpc_id>/<时间>-<执行编号>.json
   ```

   「扩展方式」在走 `existing_space` 时打印「复用现有空闲地址」，「新增附加网段」打印
   「否（现有地址空间足够）」。「扩容后可用地址」取扩容后再读一次的现状，
   若某些子网的余量字段读不到，该行打印 `unknown`。「留痕」行也可能是「对象未落：<原因>」——
   快照对象是旁路，不影响本次结论。

7. **确认控制台现状与结果一致**：VPC 详情页出现附加网段 `10.1.0.0/16`；子网列表出现新建子网，
   标签为 `ManagedBy=sample-automated-vpc-cidr-expansion`、`Tier=<层级>`、
   `ExpansionExecution=<执行编号>`、`ExpansionCidr=<网段>`；默认路由表出现覆盖新网段的网段路由
   （由平台自动维护，函数只读校验）。

8. **验证串行与幂等护栏**：
   - 连续投递两条指令（执行编号不同），第二条返回 `SKIPPED_LOCK_BUSY`，不动任何资源；
   - 把消息里的 `vpc_id` 换成另一个 VPC 标识，返回 `SKIPPED_TARGET_MISMATCH`，同样不动任何资源；
   - 把 `utilization` 改成 50（低于 `warning_threshold`），返回 `SKIPPED_BELOW_THRESHOLD`；
     这三种跳过状态都不发通知邮件，避免每个周期都寄一封；
   - 再投递一次同样的指令，函数不会重复附加同一个网段（挑网段时跳过已附加条目），
     新建子网按命名序号继续往后排。

9. **验证巡检侧自动触发**（可选）：把 `utilization_threshold` 临时降到当前利用率以下（例如 `5`）、
   同时把 `warning_threshold` 降到它以下（例如 `2`）后 `terraform apply`，等下一个巡检周期看
   指令主题出现一条消息、扩容函数被唤起。投递静默窗口是 1800 秒，同一 VPC 半小时内只会投一条。
   验证完把两个阈值改回来。

10. **确认日志与留痕**：LTS 里 `<前缀>-monitor` 与 `<前缀>-expansion` 分别记录两个函数的执行过程，
    `<前缀>-command-delivery` 记录指令投递；桶内 `expansion-history/<vpc_id>/` 下是历史对象、
    `cidr-monitor-snapshot/<日期>/` 下是每轮巡检快照；历史表 `<前缀>-history` 按
    `vpc_id` + `timestamp` 能查到本次执行的台账行。

## 参数表（全部 63 项）

`必填` 列：**是** = 没有可用默认值，必须替换；**实查** = 必须按目标区域查出真实取值；
**复核** = 有默认值但强烈建议按自己的环境改写；其余为按需调整。

| 变量 | 类型 | 默认值 | 必填 | 说明 |
|-|-|-|-|-|
| `region` | string | `cn-north-4` | 否 | 部署区域 |
| `name_prefix` | string | `vpc-cidr-expansion` | 否 | 全部资源名、表名与主题名的统一前缀 |
| `enterprise_project_id` | string | `"0"` | 否 | 企业项目 ID，`0` 为默认企业项目 |
| `tags` | map(string) | `{ ManagedBy = "vpc-cidr-automation" }` | 否 | 统一附加到可标签化资源的标签 |
| `vpc_cidr` | string | `10.0.0.0/16` | 复核 | VPC 主网段；决定首次扩容走哪条路径 |
| `availability_zones` | list(string) | `["cn-north-4a","cn-north-4b","cn-north-4c"]` | 否 | 本实践使用的可用区 |
| `baseline_subnets` | list(object({name,cidr,az,tier,expandable})) | 7 条基线子网 | 复核 | 层级结构载体；`expandable = false` 的层级不参与扩展；`cidr` 非法或清单为空会被参数校验拦住 |
| `function_subnet_name` | string | `"svc-private-1"` | 否 | 函数接入 VPC 使用的基线子网名（取 `baseline_subnets` 的 `name`） |
| `planned_secondary_cidrs` | list(string) | `["10.1.0.0/16","10.2.0.0/16"]` | 复核 | 外部地址规划，按优先级逐个消费，必须与主网段和现有子网不重叠 |
| `secondary_cidr_max_count` | number | `8` | 否 | 单 VPC 网段总数上限（主网段 + 附加网段）；函数侧可配区间 1..20 |
| `expansion_subnet_netmask` | number | `24` | 否 | 扩容子网默认前缀长度；函数优先沿用同层已有子网掩码；可配区间 8..28 |
| `expansion_address_group_capacity` | number | `20` | 否 | 地址组容量上限，运行期新增成员越界会明确报错 |
| `public_egress_enabled` | bool | `true` | 否 | 是否声明互联网网关与默认出口路由；为 `false` 时 `internet_gateway_id` 输出为空 |
| `monitor_tag_key` | string | `"MonitorCIDR"` | 否 | 纳管标签键 |
| `monitor_tag_value` | string | `"true"` | 否 | 纳管标签值，按字符串比较、大小写敏感 |
| `utilization_threshold` | number | `85` | 复核 | 触发扩容的利用率（百分比），取值 0-100，不得低于 `warning_threshold` |
| `warning_threshold` | number | `80` | 复核 | 扩容侧二次校验下限，取值 0-100，低于它则不动任何资源 |
| `monitor_cron` | string | `"0 */5 * * * ?"` | 否 | 巡检定时表达式，6 段 Cron（秒 分 时 日 月 周）；「周」与「日」不能同时为 `*` |
| `trigger_enabled` | bool | `true` | 否 | 首次创建时定时器是否启用；创建后的周期与启停以运行期为准 |
| `function_runtime` | string | `"Python3.9"` | 否 | 两个函数的运行时 |
| `monitor_handler` | string | `"index.main_handler"` | 否 | 巡检函数入口，格式 `<模块名>.<函数名>` |
| `expansion_handler` | string | `"index.main_handler"` | 否 | 扩容函数入口 |
| `monitor_package` | string | `"../src/functions/build/cidr-monitor.zip"` | 否 | 巡检代码包路径，须与构建脚本产物路径逐字一致 |
| `expansion_package` | string | `"../src/functions/build/cidr-expansion.zip"` | 否 | 扩容代码包路径 |
| `monitor_memory_size` | number | `256` | 否 | 巡检函数内存（MB） |
| `monitor_timeout` | number | `300` | 复核 | 巡检函数超时（秒），需覆盖一轮全部纳管 VPC 的扫描 |
| `monitor_max_instances` | string | `"5"` | 否 | 巡检函数并发实例上限（字符串形态下发） |
| `expansion_memory_size` | number | `256` | 否 | 扩容函数内存（MB） |
| `expansion_timeout` | number | `900` | 复核 | 扩容函数超时（秒），须大于「轮询上限 + 子网重试预算 + 收尾余量」 |
| `expansion_max_instances` | string | `"1"` | 否 | 扩容函数并发上限，压到 1 是同一 VPC 串行的第一道闸 |
| `lock_ttl_seconds` | number | `900` | 否 | 锁过期时间（秒），下限 60；进程异常退出后由它自愈 |
| `association_poll_interval_seconds` | number | `10` | 否 | 附加网段生效的轮询间隔（秒），下限 1 |
| `association_poll_deadline_seconds` | number | `600` | 否 | 轮询总时长上限（秒），下限 10，且不得小于轮询间隔 |
| `subnet_retry_attempts` | number | `3` | 否 | 子网创建重试次数，下限 1 |
| `subnet_retry_interval_seconds` | number | `30` | 否 | 子网创建重试间隔（秒），下限 1；与次数相乘是整段共享的重试预算 |
| `async_retry_attempts` | number | `2` | 否 | 异步调用失败后的重试次数 |
| `async_event_max_age_seconds` | number | `3600` | 否 | 异步事件最长存活时间（秒） |
| `notification_email` | string | 无默认 | **是** | 结果通知与失败事件收件邮箱；订阅需在邮件里确认 |
| `log_retention_days` | number | `30` | 否 | 函数运行日志保留天数 |
| `history_retention_days` | number | `90` | 否 | 留痕对象保留天数，必须大于 0（参数校验） |
| `ces_metric_namespace` | string | `"vpc_cidr_expansion.custom"` | 复核 | 必须是 `service.item` 格式：仅字母、数字与下划线，总长 3-32，段首为字母，禁连字符 |
| `metric_dimension_name` | string | `"VpcId"` | 否 | 指标维度名，函数按它以 VPC 标识上报，须与大盘取数一致 |
| `enable_dashboard` | bool | `true` | 否 | 是否创建地址容量大盘（含 4 个图件） |
| `dashboard_widget_view` | string | `"line"` | 否 | 图件视图类型：`bar` / `line` / `bar_chart` / `table` / `circular_bar` / `area_chart` |
| `dashboard_metric_display_mode` | string | `"single"` | 否 | 指标展示方式：`single` / `multiple` |
| `dashboard_dimension_filter_type` | string | `"all_instances"` | 否 | 维度过滤方式：`all_instances` / `specific_instances`；被纳管 VPC 在运行期才发现，默认取全部资源 |
| `dashboard_dimension_value` | string | `""` | 否 | 选 `specific_instances` 时填真实 VPC 标识 |
| `dashboard_top_n` | number | `20` | 否 | 图件展示的序列条数上限 |
| `geminidb_spec_code` | string | 无默认 | **实查** | 状态存储实例规格码 |
| `geminidb_datastore_type` | string | `"DynamoDB"` | 否 | 接口类型，本实践依赖它提供的兼容接口 |
| `geminidb_storage_engine` | string | `"rocksDB"` | 否 | 存储引擎 |
| `geminidb_engine_version` | string | `"3.0"` | **实查** | 接口版本，须与区域内已上架版本一致 |
| `geminidb_instance_mode` | string | `"Cluster"` | 否 | 实例形态 |
| `geminidb_node_count` | number | `3` | 否 | 节点数 |
| `geminidb_volume_size` | number | `100` | 否 | 单节点存储容量（GB） |
| `geminidb_volume_type` | string | `"ultra"` | 否 | 存储类型，须与实查到的规格组合相容 |
| `geminidb_availability_zones` | list(string) | `["cn-north-4a","cn-north-4b","cn-north-4c"]` | 否 | 实例可用区，须为基线子网所在可用区的子集 |
| `geminidb_data_subnet_name` | string | `"data-private-1"` | 否 | 实例所在基线子网名（取 `baseline_subnets` 的 `name`） |
| `geminidb_password` | string（敏感） | 无默认 | **是** | 实例初始账号口令，同时作为函数侧 `DDB_PASSWORD`；不出现在计划与输出回显中 |
| `geminidb_charging_mode` | string | `"postPaid"` | 否 | 实例资源类型：`postPaid` 或 `prePaid` |
| `agency_delegated_service_name` | string | `"op_svc_cff"` | 否 | 被委托方云服务标识（函数图服务） |
| `agency_domain_roles` | list(string) | `["OBS Administrator","KMS CMK Decrypter"]` | 复核 | 全局服务级角色清单，须与 IAM 权限列表名称一致 |
| `agency_project_roles` | list(string) | `["VPC FullAccess","FunctionGraph FullAccess","SMN FullAccess","CES FullAccess","GeminiDB FullAccess","LTS FullAccess"]` | 复核 | 项目级角色清单；两类角色放错作用域会出现「委托创建成功但调用被拒」 |

## 常见故障与排查

| 现象 | 出现在哪一步 | 原因与处理 |
|-|-|-|
| `plan` 报读不到 `cidr-monitor.zip` / `cidr-expansion.zip` | 步骤 4 | 代码包未构建或路径不一致；回到步骤 1，并核对 `monitor_package` / `expansion_package` |
| `validate` 报 `baseline_subnets` 相关校验错误 | 步骤 3 | `cidr` 不是合法 IPv4 网段，或清单为空；至少需要 1 条基线子网 |
| `validate` 报 `history_retention_days 必须大于 0` | 步骤 3 | 该参数被改为 0 或负值 |
| apply 在状态存储实例上报规格不可用 | 步骤 5 | `geminidb_spec_code` / `geminidb_engine_version` 仍是占位取值或与区域内实际组合不符；按「硬前提 (1)」实查后替换 |
| apply 在邮件订阅上报端点非法 | 步骤 5 | `notification_email` 未填真实邮箱；SMN 订阅端点由服务侧校验 |
| apply 在自定义指标命名空间上报错 | 步骤 5 | `ces_metric_namespace` 不是 `service.item` 格式（含连字符、超长或段首非字母） |
| apply 在委托上报权限错误，函数运行期调用被拒 | 步骤 5 / 步骤 7 | 两类角色放错作用域：`OBS Administrator`、`KMS CMK Decrypter` 属全局服务级，其余在本栈放项目级 |
| 计划里看不到三张状态表 | 步骤 4 | 正常：表不在部署栈声明，由函数首启创建，见步骤 6 |
| `apply` 输出的 `state_store_endpoint` 还没可用 | 步骤 5 | 实例仍在创建中；函数首启读写会失败，等实例进入可用状态后重试一轮巡检 |
| 巡检返回 `monitored_count` 为 0 | 步骤 7 | 目标 VPC 没有 `MonitorCIDR=true` 标签，或标签值与 `monitor_tag_value` 不完全相等（按字符串比较、大小写敏感） |
| 巡检返回某 VPC 的 `utilization` 为 `null`、上报样本为 3 | 步骤 7 | 子网余量字段读不到，函数不猜测数值：该 VPC 不投指令、利用率序列不上报；先查函数的网络接入与权限 |
| 大盘没有数据 | 步骤 7 | 命名空间、指标名、维度名三处必须与函数上报完全一致；`enable_dashboard` 是否为 `true`；选 `specific_instances` 时 `dashboard_dimension_value` 必须填真实 VPC 标识 |
| 扩容返回 `SKIPPED_BELOW_THRESHOLD` | 步骤 8 | 事件里的 `utilization`（或自行计算的利用率）低于 `warning_threshold`，属二次校验护栏；确认阈值或调整投递内容 |
| 扩容返回 `SKIPPED_TARGET_MISMATCH` | 步骤 8 | 指令里的 `vpc_id` 与函数绑定的 `TARGET_VPC_ID` 不一致；该函数的层级规划只对着本栈的 VPC 设计 |
| 扩容返回 `SKIPPED_LOCK_BUSY` | 步骤 8 | 同一 VPC 已有一次扩容在跑；`lock_ttl_seconds`（默认 900 秒）到期后锁自动可被再次取得 |
| 期望看到「消费规划网段」但结果 `mode` 是 `existing_space` | 步骤 8 | 默认主网段的空闲地址（63744）大于一次全层级铺开所需的 1280，按设计不需要新扩网段；按步骤 8 第 4 步收紧主网段再投一次 |
| 巡检一直不投指令 | 步骤 8 | 四条判定任一不满足：观测不可用、未达触发线、地址规划已用尽、距上次投递不足 1800 秒静默窗口 |
| 报「地址规划已用尽」 | 步骤 8 | `planned_secondary_cidrs` 里剩余候选都与主网段、已附加网段或现有子网重叠，或格式非法；补一个真正不重叠的网段并同步地址组容量 |
| 报网段数量触顶 | 步骤 8 | 已附加网段数达到 `secondary_cidr_max_count`（可配上限 20）；提高该参数需重新评估地址组容量与安全组暴露面 |
| 报地址组容量不足 | 步骤 8 | 结果里出现 `AddressGroupCapacityFull`：调高 `expansion_address_group_capacity` 后重新交付 |
| 附加网段轮询超时 | 步骤 8 | 结果里出现 `AssociationTimeout`，`waited_seconds` 到 `association_poll_deadline_seconds`（默认 600 秒）；先确认函数在 VPC 内可访问网络服务，再考虑放宽窗口 |
| 结果里 `subnets_failed` 非空、状态为 `PARTIAL_SUCCESS` | 步骤 8 | 整段共享的重试预算耗尽或空闲地址不足；对照 `subnet_retry_used_seconds` 与 `subnet_retry_budget_seconds` 判断是提高预算还是先释放地址，再投一次指令补齐 |
| 子网创建超时后不确定是否建成 | 步骤 8 | 函数按名称回读，命中即视为创建成功、绝不重发；确认控制台子网列表与台账里的 `subnets_created` 一致即可 |
| 通知邮件收不到 | 步骤 8 | 邮件订阅未在确认邮件里点确认；结果主题 `users_publish_allowed` 必须为 `"true"`；函数日志里 `notification_failed` 会给出服务端返回 |
| 台账写失败但资源已改动 | 步骤 8 | 通知与结果里的「留痕」行显示「写入失败」；函数不会因此回滚已附加的网段与已创建的子网，请按日志里的 `vpc_id` 与执行编号人工补录，并检查状态存储连通性与口令 |
| 函数日志出现 `expansion_target_mismatch` / `config_row_unavailable` | 步骤 8 | 前者是指令对象不符；后者不是故障——配置表读不到时回落到部署栈下发的规划，结果里 `plan_source` 为 `environment` |
| 后续 apply 把运行期改动改回去了 | 部署后 | 检查是否改动了本栈持有的字段。附加网段、地址组成员、默认路由下一跳、基线子网标签、巡检周期与启停这五处以运行期为准；函数名、规格、环境变量、并发上限、桶与主题配置、实例规格由本栈持有，必须显式改参数后 apply |
| 函数仍运行旧代码 | 部署后 | 重新构建包并 `terraform apply`；代码包对象变化会触发函数重建，云端才会取到新包 |

## 清理资源

在 `infra/` 目录执行：

```bash
cd infra
terraform destroy
```

确认后执行，预期输出末行为 `Destroy complete! Resources: N destroyed.`

销毁范围覆盖本栈声明的全部 35 个资源：

| 分组 | 资源 |
|-|-|
| 网络 | `huaweicloud_vpc.main`、7 条 `huaweicloud_vpc_subnet.baseline`、`huaweicloud_vpc_internet_gateway.main`、`huaweicloud_vpc_route.public_egress` |
| 联动面 | `huaweicloud_vpc_address_group.expansion`、各层级 `huaweicloud_networking_secgroup.tier`、两类 `huaweicloud_networking_secgroup_rule.*` |
| 状态存储 | `huaweicloud_geminidb_instance.state_store` |
| 存储 | `huaweicloud_obs_bucket.assets` 及 `..._bpa`、两条 `..._bucket_policy`、两个 `..._bucket_object` |
| 无服务器 | 两个 `huaweicloud_fgs_function`、两个 `huaweicloud_fgs_trigger`、两条 `huaweicloud_fgs_async_invoke_configuration` |
| 消息 | 三个 `huaweicloud_smn_topic`、两条 `huaweicloud_smn_subscription`、`huaweicloud_smn_logtank.commands` |
| 日志 | `huaweicloud_lts_group.runtime` 与三条 `huaweicloud_lts_stream` |
| 监控 | `huaweicloud_ces_dashboard.capacity` 与 `huaweicloud_ces_dashboard_widget.capacity` |
| 安全与身份 | `huaweicloud_kms_key.assets`、`huaweicloud_identity_agency.functions` |

运行期产物不在 Terraform 状态里，请一并手工清理：

- **运行期新建的子网**：请在 VPC 删除前逐个删除，否则 VPC 删除会被依赖拦住；
  附加网段随 VPC 一并消失，无需单独处理。
- **三张状态表**：随实例销毁。
- **桶内留痕对象**：桶设置了 `force_destroy = true`，随桶清空；生命周期规则也会在
  `history_retention_days` 到期后清理留痕对象。
- **本地构建产物**：`src/functions/build/*.zip` 由构建脚本生成，可随时删除，重新构建即可复现。
- 若个别资源因你在控制台手工关联了其它资源而删除失败，请清理该依赖后重跑 `terraform destroy`。
- 及时释放资源，避免残留；同时清理你在邮箱侧为本次验证添加的订阅确认与过滤规则。

## 交付验收清单

部署完成后请按下表逐项在真实环境里核对，全部通过后本实践才算在你的环境中交付完成：

| 编号 | 验收项 | 判定依据 |
|-|-|-|
| 1 | 部署计划与设计一致 | `terraform plan` 末尾为 `Plan: 35 to add, 0 to change, 0 to destroy.` |
| 2 | 输出项可取 | `terraform output` 打印 26 项，`expansion_tier_plan` 含三个参与扩展层级 |
| 3 | 巡检能发现纳管对象 | 手工测试返回 `monitored_count >= 1` 且 `observe_failed` 为 0 |
| 4 | 指标与大盘有数据 | 自定义命名空间下四条序列齐全，大盘四个图件出现曲线 |
| 5 | 三张表就绪 | 日志出现 `config_table_ensured`，消费指令时出现 `state_table_created` |
| 6 | 端到端链路跑通 | 投递指令后返回 `status` 为 `SUCCESS` 或 `PARTIAL_SUCCESS`，`subnets_created` 与控制台一致 |
| 7 | 通知确实到达 | 邮箱收到 `[SUCCESS] VPC CIDR Expansion - ...` 标题的邮件，正文逐个列出新建子网 |
| 8 | 台账与快照落盘 | 历史表能查到本次执行行，桶内 `expansion-history/<vpc_id>/` 有对应对象 |
| 9 | 护栏按预期生效 | 三种跳过状态分别为 `SKIPPED_LOCK_BUSY` / `SKIPPED_TARGET_MISMATCH` / `SKIPPED_BELOW_THRESHOLD`，且都不发通知 |
| 10 | 失败可见 | 人为制造一次失败（如把口令改错）后，兜底主题与失败邮件都能看到该事件 |
| 11 | 清理彻底 | `terraform destroy` 成功，控制台无残留资源 |
