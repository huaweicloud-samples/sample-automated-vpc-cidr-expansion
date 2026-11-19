# =============================================================================
# 函数运行日志
#
# 日志组承载两个函数的运行日志，保留期由 var.log_retention_days 控制；
# 每个函数一条独立日志流，便于按链路分别排查。
# =============================================================================

resource "huaweicloud_lts_group" "runtime" {
  group_name  = "${var.name_prefix}-runtime"
  ttl_in_days = var.log_retention_days

  enterprise_project_id = var.enterprise_project_id

  tags = merge(var.tags, {
    Purpose = "cidr-expansion-runtime-logs"
  })
}

resource "huaweicloud_lts_stream" "monitor" {
  group_id    = huaweicloud_lts_group.runtime.id
  stream_name = "${var.name_prefix}-monitor"
  ttl_in_days = var.log_retention_days
}

resource "huaweicloud_lts_stream" "expansion" {
  group_id    = huaweicloud_lts_group.runtime.id
  stream_name = "${var.name_prefix}-expansion"
  ttl_in_days = var.log_retention_days
}

# -----------------------------------------------------------------------------
# 指令消息的日志留痕
#
# 扩容指令主题把投递记录写入同一日志组的一条独立流，
# 便于事后核对「哪条指令在什么时间被哪个函数消费」；
# 这是消息通知服务的原生投递日志能力，不涉及指标采集。
# -----------------------------------------------------------------------------

resource "huaweicloud_lts_stream" "commands" {
  group_id    = huaweicloud_lts_group.runtime.id
  stream_name = "${var.name_prefix}-command-delivery"
  ttl_in_days = var.log_retention_days
}

resource "huaweicloud_smn_logtank" "commands" {
  topic_urn     = huaweicloud_smn_topic.expansion_commands.topic_urn
  log_group_id  = huaweicloud_lts_group.runtime.id
  log_stream_id = huaweicloud_lts_stream.commands.id
}
