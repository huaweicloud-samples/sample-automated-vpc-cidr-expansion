# =============================================================================
# 全局定位信息、层级发现与运行期配置
#
# 两个函数的运行期参数集中在本文件生成：键名即函数代码读取的环境变量名，
# 改这里等于改接口契约，需与 src/functions 下的实现保持一致。
# =============================================================================

data "huaweicloud_account" "current" {}

locals {
  domain_id = data.huaweicloud_account.current.id

  agency_name = "${var.name_prefix}-cff-agency"

  # 函数代码包与运行期产物统一放在一个私有桶内
  assets_bucket         = "${var.name_prefix}-assets-${local.domain_id}"
  obs_internal_endpoint = "obs.${var.region}.myhuaweicloud.com"

  monitor_zip_key   = "functions/cidr-monitor.zip"
  expansion_zip_key = "functions/cidr-expansion.zip"

  monitor_code_url   = "https://${local.assets_bucket}.${local.obs_internal_endpoint}/${local.monitor_zip_key}"
  expansion_code_url = "https://${local.assets_bucket}.${local.obs_internal_endpoint}/${local.expansion_zip_key}"

  # 扩容留痕对象在桶内的前缀
  history_object_prefix = "expansion-history"

  # 状态存储的三张表：锁、每 VPC 配置、扩容历史
  lock_table_name    = "${var.name_prefix}-locks"
  config_table_name  = "${var.name_prefix}-config"
  history_table_name = "${var.name_prefix}-history"

  internal_service_endpoint = "https://${local.obs_internal_endpoint}"
}

# -----------------------------------------------------------------------------
# 层级（tier）发现
#
# 一个层级 = 一组同前缀、同掩码、同安全组的子网。扩容时按层级在各可用区
# 各建一个子网，并沿用该层级的名称序号与安全组。
# -----------------------------------------------------------------------------

locals {
  # 全部层级：每层一个安全组，基线子网按层归属
  tiers = distinct([for s in var.baseline_subnets : s.tier])

  # 参与地址扩展的层级：只有这些层级会被扩容函数追加子网
  expansion_tiers = distinct([for s in var.baseline_subnets : s.tier if s.expandable])

  subnet_plan_by_name = {
    for s in var.baseline_subnets : s.name => merge(s, {
      gateway_ip = cidrhost(s.cidr, 1)
      netmask    = tonumber(split("/", s.cidr)[1])
    })
  }

  tier_subnet_names = {
    for t in local.tiers : t => sort([for s in var.baseline_subnets : s.name if s.tier == t])
  }

  tier_availability_zones = {
    for t in local.tiers : t => distinct([for s in var.baseline_subnets : s.az if s.tier == t])
  }

  # 层级掩码取该层已有子网中最常出现的前缀长度，扩容子网与之保持一致
  tier_netmasks = {
    for t in local.tiers : t => max([
      for s in var.baseline_subnets : tonumber(split("/", s.cidr)[1]) if s.tier == t
    ])
  }

  tier_security_group_ids = {
    for t in local.tiers : t => huaweicloud_networking_secgroup.tier[t].id
  }

  # 基线子网中承载状态存储实例的那一个
  data_subnet_tier       = local.subnet_plan_by_name[var.geminidb_data_subnet_name].tier
  data_subnet_id         = huaweicloud_vpc_subnet.baseline[var.geminidb_data_subnet_name].id
  data_security_group_id = huaweicloud_networking_secgroup.tier[local.data_subnet_tier].id

  # 函数接入 VPC 使用的子网：状态存储只暴露 VPC 内私有地址
  function_subnet_tier = local.subnet_plan_by_name[var.function_subnet_name].tier
  function_subnet_id   = huaweicloud_vpc_subnet.baseline[var.function_subnet_name].id
}

# -----------------------------------------------------------------------------
# 传递给函数的层级规划：扩容所需的全部网络语义都在这份 JSON 里，
# 函数不再依赖运行期的隐式推断。
# -----------------------------------------------------------------------------

locals {
  tier_plan = [
    for t in local.expansion_tiers : {
      tier_name          = t
      sample_name        = "${var.name_prefix}-${element(local.tier_subnet_names[t], 0)}"
      availability_zones = local.tier_availability_zones[t]
      security_group_id  = local.tier_security_group_ids[t]
      netmask            = local.tier_netmasks[t]
    }
  ]

  # 状态存储（DynamoDB 兼容接口）的连接参数，两个函数共用
  state_store_access = {
    DDB_ENDPOINT  = huaweicloud_geminidb_instance.state_store.lb_ip_address
    DDB_PORT      = tostring(huaweicloud_geminidb_instance.state_store.lb_port)
    DDB_USER_NAME = huaweicloud_geminidb_instance.state_store.db_user_name
    DDB_PASSWORD  = var.geminidb_password
  }

  # 两个函数共用的定位信息。
  # 委托临时凭证不进环境变量：运行期只从 handler 的 context 取，
  # HW_PROJECT_ID 是 SDK 构造客户端时的项目作用域入参。
  common_runtime_env = {
    HW_REGION     = var.region
    HW_PROJECT_ID = data.huaweicloud_account.current.current_project_id
    HW_ACCOUNT_ID = local.domain_id
  }

  # 巡检函数：发现 VPC、算利用率、上报指标、超阈值时投递扩容指令
  monitor_user_data = merge(local.common_runtime_env, local.state_store_access, {
    MONITOR_TAG_KEY       = var.monitor_tag_key
    MONITOR_TAG_VALUE     = var.monitor_tag_value
    UTILIZATION_THRESHOLD = tostring(var.utilization_threshold)
    WARNING_THRESHOLD     = tostring(var.warning_threshold)
    CES_NAMESPACE         = var.ces_metric_namespace
    CES_DIMENSION_NAME    = var.metric_dimension_name
    COMMAND_TOPIC_URN     = huaweicloud_smn_topic.expansion_commands.topic_urn
    CONFIG_TABLE_NAME     = local.config_table_name
    OBS_BUCKET            = local.assets_bucket
    OBS_ENDPOINT          = local.internal_service_endpoint
    SECONDARY_CIDR_PLAN   = jsonencode(var.planned_secondary_cidrs)
  })

  # 扩容函数：一次调用跑完整条扩展链路
  expansion_user_data = merge(local.common_runtime_env, local.state_store_access, {
    LOCK_TABLE_NAME        = local.lock_table_name
    CONFIG_TABLE_NAME      = local.config_table_name
    HISTORY_TABLE_NAME     = local.history_table_name
    TARGET_VPC_ID          = huaweicloud_vpc.main.id
    TIER_PLAN              = jsonencode(local.tier_plan)
    SECONDARY_CIDR_PLAN    = jsonencode(var.planned_secondary_cidrs)
    CIDR_MAX_COUNT         = tostring(var.secondary_cidr_max_count)
    SUBNET_NETMASK         = tostring(var.expansion_subnet_netmask)
    ADDRESS_GROUP_ID       = huaweicloud_vpc_address_group.expansion.id
    DEFAULT_ROUTE_TABLE_ID = data.huaweicloud_vpc_route_table.default.id
    NOTIFICATION_TOPIC_URN = huaweicloud_smn_topic.expansion_notifications.topic_urn
    OBS_BUCKET             = local.assets_bucket
    OBS_ENDPOINT           = local.internal_service_endpoint
    HISTORY_OBJECT_PREFIX  = local.history_object_prefix
    LOCK_TTL_SECONDS       = tostring(var.lock_ttl_seconds)
    POLL_INTERVAL_SECONDS  = tostring(var.association_poll_interval_seconds)
    POLL_DEADLINE_SECONDS  = tostring(var.association_poll_deadline_seconds)
    SUBNET_RETRY_ATTEMPTS  = tostring(var.subnet_retry_attempts)
    SUBNET_RETRY_INTERVAL  = tostring(var.subnet_retry_interval_seconds)
    WARNING_THRESHOLD      = tostring(var.warning_threshold)
  })
}

# -----------------------------------------------------------------------------
# 大盘图件清单：与函数上报的指标名一一对应
# -----------------------------------------------------------------------------

locals {
  metric_names = {
    utilization = "UtilizationPercentage"
    available   = "AvailableIPs"
    capacity    = "TotalCapacity"
    subnets     = "SubnetCount"
  }

  # 维度过滤方式决定图件是否需要具体维度值：全部资源时不传值
  dashboard_dimension_values = (
    var.dashboard_dimension_filter_type == "specific_instances" ?
    [var.dashboard_dimension_value] : null
  )

  dashboard_widgets = [
    {
      key    = "utilization"
      title  = "地址利用率"
      metric = local.metric_names.utilization
      unit   = "Percent"
      left   = 0
      top    = 0
    },
    {
      key    = "available-ips"
      title  = "可用地址数"
      metric = local.metric_names.available
      unit   = "Count"
      left   = 4
      top    = 0
    },
    {
      key    = "capacity"
      title  = "地址总容量"
      metric = local.metric_names.capacity
      unit   = "Count"
      left   = 8
      top    = 0
    },
    {
      key    = "subnet-count"
      title  = "子网数量"
      metric = local.metric_names.subnets
      unit   = "Count"
      left   = 0
      top    = 3
    },
  ]
}
