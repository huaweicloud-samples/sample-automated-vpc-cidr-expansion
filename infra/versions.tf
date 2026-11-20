# =============================================================================
# 版本与提供方约束
# =============================================================================

terraform {
  required_version = ">= 1.5"

  required_providers {
    huaweicloud = {
      source  = "huaweicloud/huaweicloud"
      version = "~> 1.96.1"
    }
  }
}
