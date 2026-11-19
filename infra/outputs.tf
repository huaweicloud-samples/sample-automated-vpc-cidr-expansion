# =============================================================================
# 输出
#
# 只输出运行期真正要用的定位信息：网络标识、函数引用、主题引用、状态存储连接参数。
# 口令一律不输出，状态存储口令在部署时由使用者提供。
# =============================================================================

# -----------------------------------------------------------------------------
# 网络
# -----------------------------------------------------------------------------

output "vpc_id" {
  description = "被保护的 VPC 标识，扩容函数按它定位附加网段与子网"
  value       = huaweicloud_vpc.main.id
}

output "vpc_main_cidr" {
  description = "VPC 主网段"
  value       = huaweicloud_vpc.main.cidr
}

output "default_route_table_id" {
  description = "默认路由表标识，扩容函数只读校验它是否已覆盖新网段，不向它写路由"
  value       = data.huaweicloud_vpc_route_table.default.id
}

output "expansion_address_group_id" {
  description = "扩展网段地址组标识，扩容函数把新网段追加进它"
  value       = huaweicloud_vpc_address_group.expansion.id
}

output "tier_security_group_ids" {
  description = "各层级安全组标识，随层级规划传给扩容函数并记入留痕；安全组规则以地址组承载目的地，扩容函数不改写安全组"
  value       = { for t, sg in huaweicloud_networking_secgroup.tier : t => sg.id }
}

output "baseline_subnet_ids" {
  description = "基线子网标识，供部署后核对与人工接管；层级名、掩码与可用区在 locals.tf 由 baseline_subnets 变量算成 TIER_PLAN 传给函数"
  value       = { for name, subnet in huaweicloud_vpc_subnet.baseline : name => subnet.id }
}

output "internet_gateway_id" {
  description = "互联网网关标识；未启用公网出口时为空"
  value       = var.public_egress_enabled ? huaweicloud_vpc_internet_gateway.main[0].id : null
}

output "expansion_tier_plan" {
  description = "参与地址扩展的层级规划，与传给扩容函数的 TIER_PLAN 内容一致"
  value       = local.tier_plan
}

# -----------------------------------------------------------------------------
# 函数与触发
# -----------------------------------------------------------------------------

output "monitor_function_urn" {
  description = "巡检函数引用，用于手工触发一轮巡检"
  value       = huaweicloud_fgs_function.monitor.urn
}

output "expansion_function_urn" {
  description = "扩容函数引用，用于手工投递一次扩容"
  value       = huaweicloud_fgs_function.expansion.urn
}

output "commands_topic_urn" {
  description = "扩容指令主题引用；手工触发扩容时向它发布含 vpc_id 的消息体"
  value       = huaweicloud_smn_topic.expansion_commands.topic_urn
}

output "notifications_topic_urn" {
  description = "扩容结果通知主题引用"
  value       = huaweicloud_smn_topic.expansion_notifications.topic_urn
}

output "dead_letter_topic_urn" {
  description = "失败事件兜底主题引用"
  value       = huaweicloud_smn_topic.dead_letter.topic_urn
}

output "monitor_cron" {
  description = "巡检定时表达式"
  value       = var.monitor_cron
}

output "function_subnet_id" {
  description = "函数接入 VPC 时使用的子网标识"
  value       = local.function_subnet_id
}

output "log_group_id" {
  description = "函数运行日志组标识"
  value       = huaweicloud_lts_group.runtime.id
}

# -----------------------------------------------------------------------------
# 存储与状态
# -----------------------------------------------------------------------------

output "assets_bucket" {
  description = "函数代码包与扩容留痕对象所在桶"
  value       = huaweicloud_obs_bucket.assets.bucket
}

output "history_object_prefix" {
  description = "扩容留痕对象在桶内的前缀"
  value       = local.history_object_prefix
}

output "state_store_endpoint" {
  description = "状态存储的 VPC 内访问地址，函数只在该 VPC 内可达"
  value       = huaweicloud_geminidb_instance.state_store.lb_ip_address
}

output "state_store_port" {
  description = "状态存储 DynamoDB 兼容接口端口"
  value       = huaweicloud_geminidb_instance.state_store.lb_port
}

output "state_store_tables" {
  description = "状态存储的三张表名，由函数首次运行时按接口创建"
  value = {
    locks   = local.lock_table_name
    config  = local.config_table_name
    history = local.history_table_name
  }
}

output "kms_key_id" {
  description = "加密代码包与留痕对象的主密钥标识"
  value       = huaweicloud_kms_key.assets.id
}

output "agency_name" {
  description = "函数运行期使用的委托名"
  value       = huaweicloud_identity_agency.functions.name
}

# -----------------------------------------------------------------------------
# 运行期参数：src 阶段的函数实现与验证脚本按这份键名读取
# -----------------------------------------------------------------------------

output "monitor_env_keys" {
  description = "巡检函数读取的环境变量键名清单"
  value       = sort(keys(local.monitor_user_data))
}

output "expansion_env_keys" {
  description = "扩容函数读取的环境变量键名清单"
  value       = sort(keys(local.expansion_user_data))
}

output "dashboard_name" {
  description = "地址利用率大盘名称；未启用大盘时为空"
  value       = var.enable_dashboard ? huaweicloud_ces_dashboard.capacity[0].name : null
}
