"""结果通知的文案。

收件体验保持稳定（``src/workflow`` 的历史约定）：
邮件标题是 ``[SUCCESS] / [FAILED] VPC CIDR Expansion - <账号> (<区域>)``，正文是定宽字段。

两处刻意的差异：

* **标题强制纯 ASCII**。SMN 的 ``subject`` 会进入邮件头，含中文时部分收件端拒收或乱码，
  正文则按 UTF-8 承载中文，可读性优先。
* **正文用中文**。除扩容结果本身，另外补上华为云形态特有的两项：
  地址组联动（新网段是否已进入安全组引用的地址组）与路由覆盖校验结果——
  这两项承载的是本实践里的「新网段是否已对内网互通生效」，
  是判断「扩容是否真的可用」的关键信息，必须出现在通知里而不是只留在日志。

本模块只做纯字符串拼装，不发消息也不读配置之外的任何数据，
因此不会把凭据带进通知：字段清单是显式列举的，不存在「整个配置字典直接打印」的路径。
"""

SUBJECT_PREFIX = "VPC CIDR Expansion"

#: 通知标题的状态标签（ASCII）
TAG_SUCCESS = "SUCCESS"
TAG_PARTIAL = "PARTIAL"
TAG_FAILED = "FAILED"


def subject(status, account_id, region):
    """构造纯 ASCII 标题；服务端限制标题最长 100 字符，由发布侧截断。"""
    return "[%s] %s - %s (%s)" % (
        _tag(status), _ascii(SUBJECT_PREFIX), _ascii(account_id or "unknown"),
        _ascii(region or "unknown"))


def success_message(result, config=None):
    """成功/部分成功的正文。"""
    status = result.get("status")
    lines = [
        "VPC 网段自动扩容 - %s" % ("部分成功" if status == "PARTIAL_SUCCESS" else "成功"),
        "",
        _field("账号", _attr(config, "account_id")),
        _field("区域", _attr(config, "region")),
        _field("VPC", "%s (%s)" % (result.get("vpc_id"), result.get("vpc_name") or "unknown")),
        _field("触发原因", result.get("reason") or "unknown"),
        _field("触发利用率", _percent(result.get("utilization"))),
        _field("判定阈值", _percent(result.get("warning_threshold"))),
        _field("扩展方式", _mode(result.get("mode"))),
        _field("新增附加网段", _cidr_line(result)),
        _field("子网创建数", len(result.get("subnets_created") or [])),
    ]
    for item in result.get("subnets_created") or []:
        # 带上子网 ID：名字是规划里重复使用的，运维要按 ID 才能在控制台定位这一次建出来的子网
        lines.append("    - %s  %s  %s (%s)" % (item.get("name"), item.get("cidr"),
                                                item.get("availability_zone"),
                                                item.get("id") or "unknown"))

    failed = result.get("subnets_failed") or []
    if failed:
        lines.append(_field("子网失败数", len(failed)))
        for item in failed:
            lines.append("    ! %s (%s / %s): %s" % (item.get("name"), item.get("tier"),
                                                     item.get("availability_zone"),
                                                     item.get("reason")))

    group = result.get("address_group") or {}
    lines.append(_field("地址组联动", _group_line(group)))
    lines.append(_field("路由覆盖", _route_line(result.get("route_check"))))
    lines.append(_field("网段数量", "%s -> %s" % ((result.get("state_before") or {}).get("cidr_count"),
                                               result.get("cidr_count_after"))))
    lines.append(_field("扩容后可用地址", (result.get("state_after") or {}).get("available_ips")))
    lines.append("")
    lines.append("执行编号: %s" % result.get("execution_id"))
    lines.append("耗时: %ss" % result.get("elapsed_seconds"))
    lines.append("留痕: %s" % _history_line(result))
    return "\n".join(lines) + "\n"


def failure_message(error, request=None, config=None, result=None):
    """失败正文：把「哪一步失败、现场是什么」一次讲清。"""
    fields = {}
    as_fields = getattr(error, "as_fields", None)
    if callable(as_fields):
        fields = as_fields()

    lines = [
        "VPC 网段自动扩容 - 失败",
        "",
        _field("账号", _attr(config, "account_id")),
        _field("区域", _attr(config, "region")),
        _field("VPC", (request.vpc_id if request is not None else (result or {}).get("vpc_id"))),
        _field("触发原因", getattr(request, "reason", None) or (result or {}).get("reason")),
        _field("错误类型", fields.get("error") or type(error).__name__),
        _field("错误信息", fields.get("reason") or str(error)),
    ]
    for key in ("status_code", "request_id", "error_code", "cidr", "waited_seconds",
                "cidr_count", "max_count", "lock_holder", "keys", "table"):
        value = fields.get(key)
        if value not in (None, "", []):
            lines.append(_field(key, value))

    created = (result or {}).get("subnets_created") or []
    if created:
        lines.append(_field("已创建子网", len(created)))
        for item in created:
            lines.append("    - %s  %s" % (item.get("name"), item.get("cidr")))
        lines.append("已附加网段: %s" % (result or {}).get("new_cidr"))

    skipped = (result or {}).get("skipped_plan") or []
    if skipped:
        lines.append(_field("规划跳过", "; ".join(
            "%s(%s)" % (item.get("cidr"), item.get("reason")) for item in skipped)))

    lines.append("")
    lines.append("执行编号: %s" % (getattr(request, "execution_id", None)
                                 or (result or {}).get("execution_id") or "unknown"))
    lines.append("锁状态: 已释放（未释放时由锁 TTL 自愈）")
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------
def _field(label, value):
    return "%-16s %s" % (label, "unknown" if value in (None, "") else value)


def _attr(obj, name):
    return getattr(obj, name, None) if obj is not None else None


def _percent(value):
    return "unknown" if value is None else "%s%%" % value


def _mode(value):
    return {"secondary_cidr": "消费规划网段后铺开子网",
            "existing_space": "复用现有空闲地址"}.get(value, value or "unknown")


def _cidr_line(result):
    if not result.get("secondary_cidr_added"):
        return "否（现有地址空间足够）"
    return "是 (%s)" % result.get("new_cidr")


def _group_line(group):
    if not group:
        return "未执行"
    if group.get("updated"):
        return "已追加新网段"
    return "%s" % (group.get("reason") or "未变更")


def _route_line(check):
    check = check or {}
    if not check.get("checked"):
        return "未校验（%s）" % (check.get("reason") or "unknown")
    if check.get("covered"):
        return "已覆盖 (%s)" % check.get("matched")
    return "未见覆盖（平台通常自动维护网段路由，仅作观测）"


def _history_line(result):
    table = result.get("history_table")
    written = result.get("history_written")
    object_result = result.get("history_object") or {}
    parts = []
    parts.append("表 %s：%s" % (table or "unknown", "已写入" if written else "写入失败"))
    if object_result.get("written"):
        parts.append("对象 %s" % object_result.get("key"))
    elif object_result.get("detail"):
        parts.append("对象未落：%s" % object_result.get("detail"))
    return "；".join(parts)


def _ascii(value):
    """标题只保留可打印 ASCII，避免中文进入邮件头。"""
    text = str(value or "")
    return "".join(char for char in text if 32 <= ord(char) < 127).strip() or "unknown"


def _tag(status):
    if status == "PARTIAL_SUCCESS":
        return TAG_PARTIAL
    return TAG_FAILED if status in (None, "FAILED") else TAG_SUCCESS
