# =============================================================================
# 路由与安全组的网段联动面
#
# 两件事在本文件收口：
#   1. 默认路由表——华为云账号默认每个 VPC 只有一张路由表，因此本栈不新建自定义
#      路由表：新网段的网段路由由平台在附加网段生效时自动维护，扩容函数只读地
#      校验默认路由表是否已覆盖新网段，不向它写路由；本栈同样只读引用它，
#      并声明 public 层级需要的默认出口路由（见 vpc.tf）。
#   2. 地址组 + 安全组——附加网段生效后必须立刻进入内网互访的允许范围。
#      华为云安全组绑定到端口而非子网，规则里的「目的地」用地址组承载，
#      扩容函数只需把新网段追加进地址组，全部层级的安全组即刻覆盖新地址空间。
# =============================================================================

data "huaweicloud_vpc_route_table" "default" {
  vpc_id = huaweicloud_vpc.main.id
}

# -----------------------------------------------------------------------------
# 扩展网段地址组：安全组规则引用的唯一可变集合
# -----------------------------------------------------------------------------

resource "huaweicloud_vpc_address_group" "expansion" {
  name        = "${var.name_prefix}-expanded-cidrs"
  description = "扩展网段集合，供各层级安全组的内网互访规则引用"
  ip_version  = 4

  max_capacity = var.expansion_address_group_capacity

  # 部署期就把全部规划网段纳入允许范围，运行期由扩容函数负责把网段真正附加到 VPC。
  # 这样避免「网段已附加但还没进地址组」的访问缺口；代价是安全组在网段真正启用前
  # 就允许该地址空间的入站互访——要求严格按已启用网段授权时把这里改为空集合。
  addresses = var.planned_secondary_cidrs

  enterprise_project_id = var.enterprise_project_id
  force_destroy         = true

  lifecycle {
    # 成员集合是运行期产物：扩容函数按消费进度增删成员、并按容量上限收敛，
    # 地址组本身由本栈创建并持有容量与协议版本。
    # 不让出该字段，下一次 apply 会把运行期调整过的成员集合改回本栈声明值，
    # 已扩展出去的地址空间会立刻被安全组规则拒之门外。
    ignore_changes = [addresses]
  }
}

# -----------------------------------------------------------------------------
# 每个层级一个安全组：规则只引用主网段与扩展网段地址组
# -----------------------------------------------------------------------------

resource "huaweicloud_networking_secgroup" "tier" {
  for_each = toset(local.tiers)

  name        = "${var.name_prefix}-sg-${each.key}"
  description = "层级 ${each.key} 的网段联动安全组"

  enterprise_project_id = var.enterprise_project_id

  tags = merge(var.tags, {
    Tier = each.key
  })
}

locals {
  # 华为云安全组规则的 protocol 不接受 all，全协议放通需拆成三条
  intra_protocols = ["tcp", "udp", "icmp"]

  primary_cidr_rules = [
    for pair in setproduct(local.tiers, local.intra_protocols) : {
      key      = "${pair[0]}-primary-${pair[1]}"
      tier     = pair[0]
      protocol = pair[1]
    }
  ]

  expansion_cidr_rules = [
    for pair in setproduct(local.tiers, local.intra_protocols) : {
      key      = "${pair[0]}-expanded-${pair[1]}"
      tier     = pair[0]
      protocol = pair[1]
    }
  ]

  primary_cidr_rule_map   = { for r in local.primary_cidr_rules : r.key => r }
  expansion_cidr_rule_map = { for r in local.expansion_cidr_rules : r.key => r }
}

# 引用主网段的入站规则
resource "huaweicloud_networking_secgroup_rule" "primary_cidr" {
  for_each = local.primary_cidr_rule_map

  security_group_id = huaweicloud_networking_secgroup.tier[each.value.tier].id
  description       = "允许来自 VPC 主网段的 ${each.value.protocol} 互访"
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = each.value.protocol
  action            = "allow"
  remote_ip_prefix  = var.vpc_cidr
}

# 引用扩展网段地址组的入站规则：附加网段一旦追加进地址组即自动生效
resource "huaweicloud_networking_secgroup_rule" "expansion_cidr" {
  for_each = local.expansion_cidr_rule_map

  security_group_id       = huaweicloud_networking_secgroup.tier[each.value.tier].id
  description             = "允许来自已扩展网段的 ${each.value.protocol} 互访"
  direction               = "ingress"
  ethertype               = "IPv4"
  protocol                = each.value.protocol
  action                  = "allow"
  remote_address_group_id = huaweicloud_vpc_address_group.expansion.id

  # 所有权判断：规则本身（方向、协议、引用的地址组）由本栈持有，
  # 运行期改写的是地址组成员而不是这条规则，故此处无需让出任何字段。
}
