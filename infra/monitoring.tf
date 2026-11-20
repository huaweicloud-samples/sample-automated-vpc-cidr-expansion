# =============================================================================
# 通知链路与观测面
#
# 三条主题各司其职：
#   expansion_commands   巡检 → 扩容的指令通道，被扩容函数的消息触发器消费
#   expansion_notifications 扩容结果通知，邮件订阅
#   dead_letter          异步调用失败兜底，保证失败事件不会进黑洞
#
# 指标由巡检函数在运行期上报到自定义命名空间（var.ces_metric_namespace），
# 本文件只声明「读哪些指标、如何展示」。命名空间、指标名与维度名必须与
# 函数代码中的取值完全一致，否则大盘无数据。
# =============================================================================

resource "huaweicloud_smn_topic" "expansion_commands" {
  name         = "${var.name_prefix}-commands"
  display_name = "地址扩容指令"
  introduction = "巡检函数投递、扩容函数消费的指令通道"

  # 取值是字符串形式的布尔：允许用户（函数以委托临时凭证发布）与云服务发布
  users_publish_allowed    = "true"
  services_publish_allowed = "true"

  enterprise_project_id = var.enterprise_project_id
  tags                  = var.tags
}

resource "huaweicloud_smn_topic" "expansion_notifications" {
  name         = "${var.name_prefix}-notifications"
  display_name = "地址扩容结果"
  introduction = "扩容成功与失败的邮件通知"

  users_publish_allowed    = "true"
  services_publish_allowed = "false"

  enterprise_project_id = var.enterprise_project_id
  tags                  = var.tags
}

resource "huaweicloud_smn_topic" "dead_letter" {
  name         = "${var.name_prefix}-dead-letter"
  display_name = "地址扩容失败事件"
  introduction = "函数异步调用重试耗尽或事件过期后的兜底投递"

  users_publish_allowed    = "false"
  services_publish_allowed = "true"

  enterprise_project_id = var.enterprise_project_id
  tags                  = var.tags
}

# 订阅创建后需在收到的邮件中点击确认，否则通知不投递
resource "huaweicloud_smn_subscription" "notification_email" {
  topic_urn = huaweicloud_smn_topic.expansion_notifications.topic_urn
  protocol  = "email"
  endpoint  = var.notification_email
  remark    = "地址扩容结果接收人"
}

# 死信同样落到邮箱：兜底链路必须有人看，否则失败会静默
resource "huaweicloud_smn_subscription" "dead_letter_email" {
  topic_urn = huaweicloud_smn_topic.dead_letter.topic_urn
  protocol  = "email"
  endpoint  = var.notification_email
  remark    = "失败事件接收人"
}

# -----------------------------------------------------------------------------
# 关于指标告警规则
#
# 本栈不创建 `huaweicloud_ces_alarmrule`：该资源在 1.96.1 版本上对非 ECS
# 命名空间存在指标白名单校验缺陷（自定义命名空间的指标会被映射到其它服务的
# 默认指标，创建时报 ces.0014 check metric whitelist failed）。
# 阈值判定因此留在巡检函数内完成（var.utilization_threshold /
# var.warning_threshold），需要控制台告警时另行创建，通知链路不依赖它。
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# 地址利用率大盘
# -----------------------------------------------------------------------------

resource "huaweicloud_ces_dashboard" "capacity" {
  count          = var.enable_dashboard ? 1 : 0
  name           = "${var.name_prefix}-capacity"
  row_widget_num = 3

  enterprise_project_id = var.enterprise_project_id

  extend_info {
    enable_legend = true
    period        = "300"
    refresh_time  = 60
  }
}

resource "huaweicloud_ces_dashboard_widget" "capacity" {
  for_each = var.enable_dashboard ? { for w in local.dashboard_widgets : w.key => w } : {}

  dashboard_id        = huaweicloud_ces_dashboard.capacity[0].id
  title               = each.value.title
  view                = var.dashboard_widget_view
  metric_display_mode = var.dashboard_metric_display_mode
  unit                = each.value.unit

  location {
    left   = each.value.left
    top    = each.value.top
    width  = 4
    height = 3
  }

  metrics {
    namespace   = var.ces_metric_namespace
    metric_name = each.value.metric
    alias       = [each.value.title]

    dimensions {
      name        = var.metric_dimension_name
      filter_type = var.dashboard_dimension_filter_type
      # 指定维度值筛选时给出取值；全部资源维度下不传值
      values = local.dashboard_dimension_values
    }
  }

  properties {
    top_n = var.dashboard_top_n
    order = "desc"
  }
}
