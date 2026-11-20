# =============================================================================
# 身份委托与加密主密钥
#
# 函数不持有长期密钥：运行期以委托临时凭证访问网络、消息、监控、日志与状态存储。
# 角色按作用域分两层放置——全局服务级（AX）进 domain_roles，
# 项目级（XA）进 project_role，放错层级时委托能建但调用会 403。
# =============================================================================

resource "huaweicloud_identity_agency" "functions" {
  name                   = local.agency_name
  delegated_service_name = var.agency_delegated_service_name
  description            = "地址扩展函数访问网络、存储、消息与监控服务的委托"
  duration               = "FOREVER"

  domain_roles = var.agency_domain_roles

  project_role {
    project = var.region
    roles   = var.agency_project_roles
  }
}

# -----------------------------------------------------------------------------
# 客户托管主密钥：加密代码包与运行期留痕对象
#
# key_alias 为必填项，密钥创建时服务侧会同步生成 alias/<key_alias> 指向本密钥，
# 因此不再单独声明别名资源——同一别名重复绑定会在创建时报别名已存在。
# 跨服务使用授权由上面的委托与桶策略承担，密钥自身不写策略资源。
# 不声明 pending_days：该参数在 provider 中表示「创建后立即进入待删除日程」，
# 填 "7" 会让密钥建出来就处于 state=4（待删除），provider 等待就绪超时，
# 且所有引用该密钥的资源（桶加密、函数代码包）连锁失败。
# 需要删除密钥时，请单独调用计划删除接口排期。
# -----------------------------------------------------------------------------

resource "huaweicloud_kms_key" "assets" {
  key_alias         = "${var.name_prefix}-assets"
  key_description   = "函数代码包与扩容留痕对象的加密主密钥"
  is_enabled        = true
  rotation_enabled  = true
  rotation_interval = 365

  enterprise_project_id = var.enterprise_project_id

  tags = var.tags
}
