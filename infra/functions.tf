# =============================================================================
# 函数与函数代码包存储
#
# 编排落地形态：定时巡检函数 + 指令主题 + 扩容函数（消息触发），
# 地址扩展的每个步骤收敛进扩容函数的一次调用内，分支与等待在代码里显式表达。
#
# 两个函数都挂进被保护的 VPC：状态存储只暴露 VPC 内私有地址。
# =============================================================================

# -----------------------------------------------------------------------------
# 代码包与运行期留痕的存放桶
# -----------------------------------------------------------------------------

resource "huaweicloud_obs_bucket" "assets" {
  bucket             = local.assets_bucket
  storage_class      = "STANDARD"
  acl                = "private"
  versioning         = true
  force_destroy      = true
  encryption         = true
  sse_algorithm      = "kms"
  kms_key_id         = huaweicloud_kms_key.assets.id
  bucket_key_enabled = "true"
  policy_format      = "obs"

  enterprise_project_id = var.enterprise_project_id

  tags = merge(var.tags, {
    Purpose = "cidr-expansion-artifacts"
  })

  # 只清理扩容留痕对象；函数代码包随栈保留
  lifecycle_rule {
    name    = "expire-expansion-history"
    prefix  = "${local.history_object_prefix}/"
    enabled = true

    expiration {
      days = var.history_retention_days
    }
  }

  lifecycle_rule {
    name    = "expire-history-noncurrent-versions"
    prefix  = "${local.history_object_prefix}/"
    enabled = true

    noncurrent_version_expiration {
      days = var.history_retention_days
    }
  }

  # 所有权判断：桶内对象与生命周期规则均由本栈持有，运行期只追加历史对象，
  # 不改写桶级配置，故无需让出任何字段。
}

# 公开访问阻断：四项全开，禁止任何匿名读取
resource "huaweicloud_obs_bucket_bpa" "assets" {
  bucket                  = huaweicloud_obs_bucket.assets.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# 仅允许 HTTPS 取包：代码包与留痕对象一律走加密传输
resource "huaweicloud_obs_bucket_policy" "assets_tls" {
  bucket        = huaweicloud_obs_bucket.assets.id
  policy_format = "obs"

  policy = jsonencode({
    Version = "2008-10-17"
    Statement = [
      {
        Sid       = "EnforceTLSOnly"
        Effect    = "Deny"
        Principal = { ID = ["*"] }
        Action    = ["*"]
        Resource = [
          huaweicloud_obs_bucket.assets.bucket,
          "${huaweicloud_obs_bucket.assets.bucket}/*",
        ]
        Condition = {
          Bool = {
            SecureTransport = ["false"]
          }
        }
      }
    ]
  })
}

# 函数以委托临时凭证读写留痕：对象存储数据面只认桶策略，不认委托角色
resource "huaweicloud_obs_bucket_policy" "assets_agency" {
  bucket        = huaweicloud_obs_bucket.assets.id
  policy_format = "obs"
  depends_on    = [huaweicloud_identity_agency.functions]

  policy = jsonencode({
    Version = "2008-10-17"
    Statement = [
      {
        Sid    = "AllowFunctionAgency"
        Effect = "Allow"
        Principal = {
          ID = ["domain/${local.domain_id}:agency/${local.agency_name}"]
        }
        Action = [
          "GetObject",
          "PutObject",
          "ListBucket",
        ]
        Resource = [
          huaweicloud_obs_bucket.assets.bucket,
          "${huaweicloud_obs_bucket.assets.bucket}/*",
        ]
      }
    ]
  })
}

resource "huaweicloud_obs_bucket_object" "monitor_code" {
  bucket       = huaweicloud_obs_bucket.assets.id
  key          = local.monitor_zip_key
  source       = var.monitor_package
  content_type = "application/zip"
  encryption   = true
  kms_key_id   = huaweicloud_kms_key.assets.id
}

resource "huaweicloud_obs_bucket_object" "expansion_code" {
  bucket       = huaweicloud_obs_bucket.assets.id
  key          = local.expansion_zip_key
  source       = var.expansion_package
  content_type = "application/zip"
  encryption   = true
  kms_key_id   = huaweicloud_kms_key.assets.id
}

# -----------------------------------------------------------------------------
# 巡检函数：发现被纳管 VPC、算地址利用率、上报指标、超阈值投递扩容指令
# -----------------------------------------------------------------------------

resource "huaweicloud_fgs_function" "monitor" {
  name             = "${var.name_prefix}-monitor"
  app              = var.name_prefix
  description      = "按标签发现被纳管 VPC，统计地址利用率并在超阈值时投递扩容指令"
  runtime          = var.function_runtime
  handler          = var.monitor_handler
  memory_size      = var.monitor_memory_size
  timeout          = var.monitor_timeout
  max_instance_num = var.monitor_max_instances
  code_type        = "obs"
  code_url         = local.monitor_code_url
  agency           = huaweicloud_identity_agency.functions.name
  user_data        = jsonencode(local.monitor_user_data)

  vpc_id     = huaweicloud_vpc.main.id
  network_id = local.function_subnet_id

  enable_lts_log  = true
  log_group_id    = huaweicloud_lts_group.runtime.id
  log_group_name  = huaweicloud_lts_group.runtime.group_name
  log_stream_id   = huaweicloud_lts_stream.monitor.id
  log_stream_name = huaweicloud_lts_stream.monitor.stream_name

  enterprise_project_id = var.enterprise_project_id

  tags = merge(var.tags, {
    FunctionRole = "monitor"
  })

  lifecycle {
    # 代码包更新后函数需重建，否则继续运行旧包
    replace_triggered_by = [huaweicloud_obs_bucket_object.monitor_code]
  }
}

# -----------------------------------------------------------------------------
# 扩容函数：一次调用跑完「取锁 → 附加网段 → 轮询生效 → 建子网 →
# 网段联动 → 写历史 → 通知 → 放锁」
# -----------------------------------------------------------------------------

resource "huaweicloud_fgs_function" "expansion" {
  name             = "${var.name_prefix}-expansion"
  app              = var.name_prefix
  description      = "为指定 VPC 消费一个规划网段并按层级扩容子网，全程持锁"
  runtime          = var.function_runtime
  handler          = var.expansion_handler
  memory_size      = var.expansion_memory_size
  timeout          = var.expansion_timeout
  max_instance_num = var.expansion_max_instances
  code_type        = "obs"
  code_url         = local.expansion_code_url
  agency           = huaweicloud_identity_agency.functions.name
  user_data        = jsonencode(local.expansion_user_data)

  vpc_id     = huaweicloud_vpc.main.id
  network_id = local.function_subnet_id

  enable_lts_log  = true
  log_group_id    = huaweicloud_lts_group.runtime.id
  log_group_name  = huaweicloud_lts_group.runtime.group_name
  log_stream_id   = huaweicloud_lts_stream.expansion.id
  log_stream_name = huaweicloud_lts_stream.expansion.stream_name

  enterprise_project_id = var.enterprise_project_id

  tags = merge(var.tags, {
    FunctionRole = "expansion"
  })

  lifecycle {
    replace_triggered_by = [huaweicloud_obs_bucket_object.expansion_code]

    # 所有权判断：函数名、规格、环境变量由本栈持有；运行期被改写的是
    # 并发实例数与函数版本别名（灰度发布），不在本栈期望状态内。
    # 这里不让出任何字段：并发上限改小会直接导致扩容链路停摆，
    # 必须由本栈显式变更。
  }
}

# -----------------------------------------------------------------------------
# 触发链路
#
# 巡检：定时触发器（6 段 Cron）。
# 扩容：指令主题的消息触发器，巡检函数投递、扩容函数消费。
# -----------------------------------------------------------------------------

resource "huaweicloud_fgs_trigger" "monitor_schedule" {
  function_urn = huaweicloud_fgs_function.monitor.urn
  type         = "TIMER"
  # 触发器状态只有 ACTIVE / DISABLED 两个取值
  status = var.trigger_enabled ? "ACTIVE" : "DISABLED"

  timer {
    name     = "${var.name_prefix}-inspect"
    schedule = var.monitor_cron
    # 定时规则类型：Cron（6 段）或 Period（周期秒数）
    schedule_type          = "Cron"
    additional_information = jsonencode({ source = "cidr-capacity-inspection" })
  }

  lifecycle {
    # 巡检周期是运行期运维会就地调整的字段（变更窗口临时改频率），
    # 且定时触发器的这些字段在云端不支持原地改写，只能销毁重建；
    # 不让出就会让下一次 apply 试图把周期改回本栈声明值。
    # 启停同理：人工在控制台暂停自动化后，本栈不得把它重新打开。
    ignore_changes = [timer, status]
  }
}

resource "huaweicloud_fgs_trigger" "expansion_command" {
  function_urn = huaweicloud_fgs_function.expansion.urn
  type         = "SMN"
  status       = "ACTIVE"

  smn {
    topic_urn = huaweicloud_smn_topic.expansion_commands.topic_urn
  }
}

# -----------------------------------------------------------------------------
# 异步调用兜底：失败事件投递到死信主题
#
# 目标类型取值为大写的 OBS / SMN / DIS / FunctionGraph，
# param 是这些服务所需参数的 JSON 字符串（SMN 为 topic_urn）。
# -----------------------------------------------------------------------------

resource "huaweicloud_fgs_async_invoke_configuration" "expansion" {
  function_urn                   = huaweicloud_fgs_function.expansion.urn
  max_async_retry_attempts       = var.async_retry_attempts
  max_async_event_age_in_seconds = var.async_event_max_age_seconds
  enable_async_status_log        = true

  on_failure {
    destination = "SMN"
    param       = jsonencode({ topic_urn = huaweicloud_smn_topic.dead_letter.topic_urn })
  }
}

resource "huaweicloud_fgs_async_invoke_configuration" "monitor" {
  function_urn                   = huaweicloud_fgs_function.monitor.urn
  max_async_retry_attempts       = var.async_retry_attempts
  max_async_event_age_in_seconds = var.async_event_max_age_seconds
  enable_async_status_log        = true

  on_failure {
    destination = "SMN"
    param       = jsonencode({ topic_urn = huaweicloud_smn_topic.dead_letter.topic_urn })
  }
}
