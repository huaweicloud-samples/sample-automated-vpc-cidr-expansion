# =============================================================================
# 输入变量
#
# 与运行形态和资源规格相关的入参全部显式给出：地址段规划、函数内存与超时、
# 轮询与重试窗口、存储规格、日志保留期、监控命名空间。
# 不提供「由数据源下标代选规格」的写法。
# =============================================================================

variable "region" {
  description = "华为云区域，本实践在 cn-north-4 验证"
  type        = string
  default     = "cn-north-4"
}

variable "name_prefix" {
  description = "所有资源名称的统一前缀"
  type        = string
  default     = "vpc-cidr-expansion"
}

variable "enterprise_project_id" {
  description = "企业项目 ID，默认企业项目为 0"
  type        = string
  default     = "0"
}

variable "tags" {
  description = "统一附加到可标签化资源的标签"
  type        = map(string)
  default = {
    ManagedBy = "vpc-cidr-automation"
  }
}

# -----------------------------------------------------------------------------
# 被保护的 VPC 与地址规划
# -----------------------------------------------------------------------------

variable "vpc_cidr" {
  description = "VPC 主网段"
  type        = string
  default     = "10.0.0.0/16"
}

variable "availability_zones" {
  description = "本实践使用的可用区，按显式顺序引用"
  type        = list(string)
  default     = ["cn-north-4a", "cn-north-4b", "cn-north-4c"]
}

variable "baseline_subnets" {
  description = <<-EOT
    VPC 的基线子网规划：名称、网段、可用区、所属层级（tier）与是否参与地址扩展。
    层级名同时决定该层子网的安全组名（<name_prefix>-sg-<tier>），
    扩容函数只在 expandable = true 的层级里追加子网，并沿用该层的名称序号与安全组。
  EOT
  type = list(object({
    name       = string
    cidr       = string
    az         = string
    tier       = string
    expandable = bool
  }))
  default = [
    { name = "public-1", cidr = "10.0.1.0/24", az = "cn-north-4a", tier = "public", expandable = true },
    { name = "public-2", cidr = "10.0.2.0/24", az = "cn-north-4b", tier = "public", expandable = true },
    { name = "app-private-1", cidr = "10.0.10.0/24", az = "cn-north-4a", tier = "private", expandable = true },
    { name = "app-private-2", cidr = "10.0.11.0/24", az = "cn-north-4b", tier = "private", expandable = true },
    { name = "ops-private-1", cidr = "10.0.20.0/24", az = "cn-north-4a", tier = "ops", expandable = true },
    { name = "data-private-1", cidr = "10.0.30.0/24", az = "cn-north-4b", tier = "data", expandable = false },
    { name = "svc-private-1", cidr = "10.0.40.0/24", az = "cn-north-4b", tier = "svc", expandable = false },
  ]

  validation {
    condition     = alltrue([for s in var.baseline_subnets : can(cidrnetmask(s.cidr))])
    error_message = "baseline_subnets 中的 cidr 必须是合法 IPv4 网段。"
  }

  validation {
    condition     = length(var.baseline_subnets) > 0
    error_message = "至少需要一个基线子网，否则无法发现扩容层级。"
  }
}

variable "function_subnet_name" {
  description = <<-EOT
    函数访问 VPC 使用的基线子网名（baseline_subnets 中的 name 字段）。
    状态存储以 VPC 内私有地址提供服务，函数必须挂到该子网才能读写锁、配置与历史。
  EOT
  type        = string
  default     = "svc-private-1"
}

variable "planned_secondary_cidrs" {
  description = <<-EOT
    外部地址规划：预留给 VPC 附加网段的候选网段，按优先级排序。
    函数在使用前会逐个校验与主网段、已用附加网段、现有子网的重叠。
  EOT
  type        = list(string)
  default     = ["10.1.0.0/16", "10.2.0.0/16"]
}

variable "secondary_cidr_max_count" {
  description = "单个 VPC 允许承载的网段总数上限（主网段 + 附加网段）"
  type        = number
  default     = 8
}

variable "expansion_subnet_netmask" {
  description = "扩容子网默认前缀长度；函数优先沿用同层已有子网的掩码"
  type        = number
  default     = 24
}

variable "expansion_address_group_capacity" {
  description = "扩展网段地址组的容量上限，决定还能追加多少条网段"
  type        = number
  default     = 20
}

variable "public_egress_enabled" {
  description = "是否为 public 层级所在 VPC 声明公网出口路由（互联网网关 + 默认路由）"
  type        = bool
  default     = true
}

# -----------------------------------------------------------------------------
# 自动化开关与阈值
# -----------------------------------------------------------------------------

variable "monitor_tag_key" {
  description = "被纳管 VPC 的标签键"
  type        = string
  default     = "MonitorCIDR"
}

variable "monitor_tag_value" {
  description = "被纳管 VPC 的标签值"
  type        = string
  default     = "true"
}

variable "utilization_threshold" {
  description = "触发扩容的 VPC 地址利用率（百分比）"
  type        = number
  default     = 85
}

variable "warning_threshold" {
  description = "低于该利用率时拒绝扩容请求（二次校验，防止误触发）"
  type        = number
  default     = 80
}

variable "monitor_cron" {
  description = "巡检定时表达式，6 段 Cron（秒 分 时 日 月 周）"
  type        = string
  default     = "0 */5 * * * ?"
}

variable "trigger_enabled" {
  description = "首次创建时巡检定时触发器是否启用；创建后的启停由运行期决定，不再由本栈回写"
  type        = bool
  default     = true
}

# -----------------------------------------------------------------------------
# 函数规格
# -----------------------------------------------------------------------------

variable "function_runtime" {
  description = "函数运行时版本"
  type        = string
  default     = "Python3.9"
}

variable "monitor_handler" {
  description = <<-EOT
    巡检函数入口，格式 <模块名>.<函数名>：代码包根目录下的 <模块名>.py 中的 <函数名>。
    该值即 src 阶段必须交付的接口契约之一。
  EOT
  type        = string
  default     = "index.main_handler"
}

variable "expansion_handler" {
  description = "扩容函数入口，格式同上"
  type        = string
  default     = "index.main_handler"
}

variable "monitor_memory_size" {
  description = "巡检函数内存（MB）"
  type        = number
  default     = 256
}

variable "monitor_timeout" {
  description = "巡检函数超时（秒），需覆盖一轮全部 VPC 的扫描"
  type        = number
  default     = 300
}

variable "monitor_max_instances" {
  description = "巡检函数并发实例上限"
  type        = string
  default     = "5"
}

variable "expansion_memory_size" {
  description = "扩容函数内存（MB）"
  type        = number
  default     = 256
}

variable "expansion_timeout" {
  description = <<-EOT
    扩容函数超时（秒）。单次调用要跑完「取锁 → 附加网段 → 轮询生效 → 建子网 →
    路由与安全组联动 → 写历史 → 通知 → 放锁」，必须大于
    association_poll_deadline_seconds + subnet_create_retry_budget + 收尾余量。
  EOT
  type        = number
  default     = 900
}

variable "expansion_max_instances" {
  description = "扩容函数并发实例上限；同一 VPC 由分布式锁串行化，这里只设最小并发"
  type        = string
  default     = "1"
}

variable "lock_ttl_seconds" {
  description = "扩容锁的过期时间（秒），进程异常退出后由该时间自动释放"
  type        = number
  default     = 900
}

variable "association_poll_interval_seconds" {
  description = "附加网段生效轮询间隔（秒）"
  type        = number
  default     = 10
}

variable "association_poll_deadline_seconds" {
  description = "附加网段生效轮询总时长上限（秒）"
  type        = number
  default     = 600
}

variable "subnet_retry_attempts" {
  description = "单个子网创建失败时的重试次数"
  type        = number
  default     = 3
}

variable "subnet_retry_interval_seconds" {
  description = "子网创建重试间隔（秒）"
  type        = number
  default     = 30
}

variable "async_retry_attempts" {
  description = "异步调用失败重试次数（消息触发链路的兜底）"
  type        = number
  default     = 2
}

variable "async_event_max_age_seconds" {
  description = "异步事件最长存活时间（秒）"
  type        = number
  default     = 3600
}

# -----------------------------------------------------------------------------
# 通知与观测
# -----------------------------------------------------------------------------

variable "notification_email" {
  description = "接收扩容结果通知的邮箱地址；订阅创建后需在邮件中确认才会投递"
  type        = string
}

variable "log_retention_days" {
  description = "函数运行日志保留天数"
  type        = number
  default     = 30
}

variable "history_retention_days" {
  description = "扩容留痕对象在存储桶内的保留天数"
  type        = number
  default     = 90

  validation {
    condition     = var.history_retention_days > 0
    error_message = "history_retention_days 必须大于 0。"
  }
}

variable "ces_metric_namespace" {
  description = <<-EOT
    自定义指标命名空间，须为 service.item 格式：仅含字母、数字与下划线，
    总长 3-32，段首为字母且不以 SYS/AGT/SRE 开头；函数上报与大盘取数共用该值。
  EOT
  type        = string
  default     = "vpc_cidr_expansion.custom"
}

variable "metric_dimension_name" {
  description = "指标维度名，函数按该维度以 VPC ID 上报"
  type        = string
  default     = "VpcId"
}

variable "enable_dashboard" {
  description = "是否创建地址利用率大盘"
  type        = bool
  default     = true
}

variable "dashboard_widget_view" {
  description = "大盘图件视图类型：line | bar | table | area_chart"
  type        = string
  default     = "line"
}

variable "dashboard_metric_display_mode" {
  description = "大盘图件的指标展示方式：single | multiple；本实践每个图件取一个指标"
  type        = string
  default     = "single"
}

variable "dashboard_dimension_filter_type" {
  description = <<-EOT
    大盘维度过滤方式，合法值：all_instances（该维度下全部资源）、
    specific_instances（仅指定维度值）。被纳管 VPC 在运行期由标签发现，
    部署期无法枚举其 ID，因此默认 all_instances。
  EOT
  type        = string
  default     = "all_instances"
}

variable "dashboard_dimension_value" {
  description = "filter_type 为 specific_instances 时填写的维度值（VPC 标识）"
  type        = string
  default     = ""
}

variable "dashboard_top_n" {
  description = "大盘图件展示的序列条数上限"
  type        = number
  default     = 20
}

# -----------------------------------------------------------------------------
# 状态存储（DynamoDB 兼容接口）
#
# 锁、配置、历史三类状态保存在同一实例的三张表内；表由函数首次运行时创建。
# 规格码与引擎参数随区域上架情况变化，本栈不代选，必须由使用者显式提供。
# -----------------------------------------------------------------------------

variable "geminidb_spec_code" {
  description = <<-EOT
    状态存储实例的规格码，形如 <族名>.<规格>。
    取值请在控制台「GeminiDB → 购买 → 实例规格」按目标区域实查后填入，本栈不做自动选择。
    注意 huaweicloud_geminidb_available_flavors 数据源要求传入一个已存在实例的 instance_id，
    它回答的是「这台实例还能选什么」，不能在首次购买前用来取规格码。
  EOT
  type        = string
}

variable "geminidb_datastore_type" {
  description = "接口类型，本实践使用 DynamoDB 兼容接口"
  type        = string
  default     = "DynamoDB"
}

variable "geminidb_storage_engine" {
  description = "存储引擎"
  type        = string
  default     = "rocksDB"
}

variable "geminidb_engine_version" {
  description = "接口版本，需与目标区域已上架版本一致"
  type        = string
  default     = "3.0"
}

variable "geminidb_instance_mode" {
  description = "实例形态，集群模式提供多节点与副本"
  type        = string
  default     = "Cluster"
}

variable "geminidb_node_count" {
  description = "节点数量"
  type        = number
  default     = 3
}

variable "geminidb_volume_size" {
  description = "单节点存储容量（GB）"
  type        = number
  default     = 100
}

variable "geminidb_volume_type" {
  description = "存储类型"
  type        = string
  default     = "ultra"
}

variable "geminidb_availability_zones" {
  description = "状态存储实例使用的可用区，须为 baseline_subnets 所在可用区的子集"
  type        = list(string)
  default     = ["cn-north-4a", "cn-north-4b", "cn-north-4c"]
}

variable "geminidb_data_subnet_name" {
  description = "状态存储实例所在基线子网名（baseline_subnets 中的 name 字段）"
  type        = string
  default     = "data-private-1"
}

variable "geminidb_password" {
  description = "状态存储初始账号密码，部署时由使用者提供，仓库不落盘明文"
  type        = string
  sensitive   = true
}

variable "geminidb_charging_mode" {
  description = "状态存储实例的资源类型：postPaid 或 prePaid"
  type        = string
  default     = "postPaid"
}

# -----------------------------------------------------------------------------
# 函数代码包（由 src/functions/build_packages.py 生成）
# -----------------------------------------------------------------------------

variable "monitor_package" {
  description = "巡检函数代码包路径"
  type        = string
  default     = "../src/functions/build/cidr-monitor.zip"
}

variable "expansion_package" {
  description = "扩容函数代码包路径"
  type        = string
  default     = "../src/functions/build/cidr-expansion.zip"
}

# -----------------------------------------------------------------------------
# 委托角色
#
# 角色按作用域分开放置：全局服务级（AX）进 domain_roles，
# 项目级（XA）进 project_role.roles，放错层级会出现「委托创建成功但调用 403」。
# 角色名须与 IAM 权限列表完全一致。
# -----------------------------------------------------------------------------

variable "agency_delegated_service_name" {
  description = "被委托方云服务标识，函数图服务为 op_svc_cff"
  type        = string
  default     = "op_svc_cff"
}

variable "agency_domain_roles" {
  description = "全局服务级（AX）角色清单。IAM 角色分 AX / XA 两类，角色名或层级写错时委托创建不报错，但角色不进 token，运行期才报 403"
  type        = list(string)
  default = [
    # OBS 是全局级服务
    "OBS Administrator",
  ]
}

variable "agency_project_roles" {
  description = "项目级（XA）角色清单。角色名须与 IAM 权限列表逐字一致，凭印象写会在运行期才暴露"
  type        = list(string)
  default = [
    # IAM 项目级 VPC 管理角色为 VPC Administrator
    "VPC Administrator",
    "FunctionGraph FullAccess",
    # IAM 里没有 "SMN FullAccess"（只有 ReadOnlyAccess，不能发消息）；
    # 该角色策略恰为 smn:topic:publish，是最小发布权限
    "CTSAgencySmnPolicy",
    # IAM 里叫 "CES FullAccessPolicy"，少了 Policy 后缀即不存在
    "CES FullAccessPolicy",
    "GeminiDB FullAccess",
    "LTS FullAccess",
    # KMS 对象解密需要项目级权限；全局级 "KMS CMK Decrypter" 在 IAM 中并不存在
    "KMS CMKFullAccess",
  ]
}
