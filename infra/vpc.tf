# =============================================================================
# 被保护的网络与运行期状态存储
#
# 本文件声明「等待被扩展的 VPC」本身：主网段、按层级划分的基线子网、公网出口，
# 以及与网络同处一个 VPC 的状态存储实例（锁 / 配置 / 历史三张表）。
# 附加网段与扩容子网由巡检与扩容函数在运行期写入，不在本栈的期望状态里。
# =============================================================================

resource "huaweicloud_vpc" "main" {
  name                  = "${var.name_prefix}-vpc"
  cidr                  = var.vpc_cidr
  description           = "地址自动化扩展实践所保护的 VPC"
  enterprise_project_id = var.enterprise_project_id

  # 纳管标记：巡检函数按标签发现需要监控的 VPC，
  # 附加网段规划与层级掩码同样以标签形式留一份，便于控制台核对。
  tags = merge(var.tags, {
    (var.monitor_tag_key) = var.monitor_tag_value
    CidrExpansionPlan     = join(",", var.planned_secondary_cidrs)
    CidrExpansionMask     = tostring(var.expansion_subnet_netmask)
    CidrExpansionWarning  = tostring(var.warning_threshold)
    CidrExpansionTrigger  = tostring(var.utilization_threshold)
  })

  lifecycle {
    # 附加网段是本实践的运行期产物：扩容函数每次消费一个规划网段并写回 VPC。
    # 本栈只在创建时确定主网段，不声明期望的附加网段集合。
    # 若不让出该字段的所有权，下一次 apply 会把已完成的网段扩展静默改回空集合，
    # 等同于回滚一次线上扩容——扩容子网会瞬间失去所属网段。
    ignore_changes = [secondary_cidrs]
  }
}

# -----------------------------------------------------------------------------
# 基线子网：每个层级至少一个，供扩容函数发现层级名、掩码、可用区与安全组
# -----------------------------------------------------------------------------

resource "huaweicloud_vpc_subnet" "baseline" {
  for_each = local.subnet_plan_by_name

  name              = "${var.name_prefix}-${each.value.name}"
  description       = "基线子网，层级 ${each.value.tier}"
  vpc_id            = huaweicloud_vpc.main.id
  cidr              = each.value.cidr
  gateway_ip        = each.value.gateway_ip
  availability_zone = each.value.az

  tags = merge(var.tags, {
    Name         = each.value.name
    Tier         = each.value.tier
    Baseline     = "true"
    AutoExpanded = "false"
  })

  lifecycle {
    # 判断依据：基线子网的网段与可用区在生命周期内不变，运行期新建的子网
    # 不在本栈状态内，故此处不需要让出任何字段的所有权。
    # 但标签会被运维在控制台补录（如责任人归属），让出 tags 避免被抹掉。
    ignore_changes = [tags]
  }
}

# -----------------------------------------------------------------------------
# 公网出口：互联网网关 + 默认路由
#
# add_route 关闭，默认路由由本栈以 huaweicloud_vpc_route 显式持有，
# 避免网关自动写入的路由与本栈声明的同一条目冲突。
# -----------------------------------------------------------------------------

resource "huaweicloud_vpc_internet_gateway" "main" {
  count = var.public_egress_enabled ? 1 : 0

  name      = "${var.name_prefix}-igw"
  vpc_id    = huaweicloud_vpc.main.id
  add_route = false

  # 网关创建时校验 VPC 内已有子网，与子网并行创建会报「子网不存在」
  depends_on = [huaweicloud_vpc_subnet.baseline]
}

resource "huaweicloud_vpc_route" "public_egress" {
  count = var.public_egress_enabled ? 1 : 0

  vpc_id         = huaweicloud_vpc.main.id
  route_table_id = data.huaweicloud_vpc_route_table.default.id
  destination    = "0.0.0.0/0"
  type           = "igw"
  nexthop        = huaweicloud_vpc_internet_gateway.main[0].id
  description    = "public 层级的默认出口路由"

  lifecycle {
    # 默认路由的下一跳是运行期会被改写的字段：接入公网 NAT 网关或企业路由器后，
    # 运维会把 0.0.0.0/0 的下一跳从互联网网关改为对应设备。
    # 让出该字段，否则下一次 apply 会把出口路由静默切回互联网网关，公网链路中断。
    ignore_changes = [nexthop]
  }
}

# -----------------------------------------------------------------------------
# 状态存储：锁 / 配置 / 历史三张表，对外提供 DynamoDB 兼容接口
#
# 表本身由巡检函数在首次运行时按接口创建，本栈只交付实例、网络与安全组。
# 规格码与接口参数不代选：由 var.geminidb_spec_code 显式提供，
# 版本与引擎参数须与目标区域实际上架取值一致。
# -----------------------------------------------------------------------------

resource "huaweicloud_geminidb_instance" "state_store" {
  name              = "${var.name_prefix}-state"
  mode              = var.geminidb_instance_mode
  availability_zone = join(",", var.geminidb_availability_zones)
  charging_mode     = var.geminidb_charging_mode

  vpc_id            = huaweicloud_vpc.main.id
  subnet_id         = local.data_subnet_id
  security_group_id = local.data_security_group_id

  password = var.geminidb_password

  datastore {
    type           = var.geminidb_datastore_type
    storage_engine = var.geminidb_storage_engine
    version        = var.geminidb_engine_version
  }

  flavor {
    num       = var.geminidb_node_count
    size      = var.geminidb_volume_size
    spec_code = var.geminidb_spec_code
    storage   = var.geminidb_volume_type
  }

  enterprise_project_id = var.enterprise_project_id

  tags = merge(var.tags, {
    Purpose = "cidr-expansion-state"
  })

  # 所有权判断：运行期只读写实例内的表数据，不改写实例规格、网络归属或凭据，
  # 因此本资源不需要让出任何字段；容量确实需要扩时由使用者改 var.geminidb_* 后 apply。
}
