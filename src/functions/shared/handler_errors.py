"""扩容链路的异常类型。

所有异常都携带可结构化的上下文字段，函数入口统一把它们转成
「一行日志 + 一条失败通知 + 一个可重试的失败结果」，
因此每个异常都必须能被 :func:`main_handler` 无差别处理。

分类口径（决定要不要重试，务必按语义选类）：

- :class:`ConfigError`：运行期参数缺失或自相矛盾。**重试无意义**，
  但本实践仍让平台按异步配置重试一次——配置由部署栈下发，
  重试期间人工修复配置就能自愈。
- :class:`ServiceError`：云服务返回非 2xx，带状态码与请求编号，可直接开工单。
- :class:`TransportError`：请求没有拿到响应（超时、连接失败）。
  **结果未知**，写操作一律不得盲目重试，改由后续轮询步骤确认事实状态。
- :class:`StateStoreError`：状态存储（DynamoDB 兼容接口）侧的错误。
- :class:`LockUnavailable`：同一 VPC 已有扩容在执行。
- :class:`PlanExhausted`：地址规划已用尽或触达网段数量上限。
- :class:`AssociationTimeout`：附加网段在轮询窗口内没有生效。
"""


class ExpansionError(Exception):
    """扩容链路基类。"""

    #: 该错误是否值得让平台重投一次事件
    retryable = True

    def __init__(self, message, **fields):
        super(ExpansionError, self).__init__(message)
        self.message = message
        self.fields = dict(fields)

    def as_fields(self):
        merged = {"error": type(self).__name__, "reason": self.message}
        merged.update(self.fields)
        return merged

    def __str__(self):
        parts = ["%s: %s" % (type(self).__name__, self.message)]
        for key in sorted(self.fields):
            parts.append("%s=%s" % (key, self.fields[key]))
        return " ".join(parts)


class ConfigError(ExpansionError):
    """环境变量缺失或取值非法（键名由部署栈下发，见 infra/locals.tf）。"""

    retryable = False


class TransportError(ExpansionError):
    """请求未得到响应：结果未知，写操作不得盲目重试。"""

    def __init__(self, message, url=None, detail=None):
        super(TransportError, self).__init__(message, url=url, detail=detail)


class ServiceError(ExpansionError):
    """云服务返回非 2xx。"""

    def __init__(self, message, status_code=None, request_id=None, body=None,
                 method=None, url=None, error_code=None, **fields):
        super(ServiceError, self).__init__(
            message,
            status_code=status_code,
            request_id=request_id,
            body=body,
            method=method,
            url=url,
            error_code=error_code,
            **fields
        )
        self.status_code = status_code
        self.request_id = request_id
        self.error_code = error_code
        # 下游按属性取响应体判定语义（条件写未命中、冲突类错误），
        # 只放进上下文字段会让这些判定落空
        self.body = body

    def retryable_by_status(self):
        if self.status_code is None:
            return True
        return self.status_code in (429, 500, 502, 503, 504)


class StateStoreError(ExpansionError):
    """状态存储读写失败，附带兼容接口侧的错误码。"""

    def __init__(self, message, error_code=None, status_code=None, body=None, **fields):
        super(StateStoreError, self).__init__(
            message, error_code=error_code, status_code=status_code, body=body, **fields)
        self.error_code = error_code
        self.status_code = status_code
        self.body = body


class TableNotFound(StateStoreError):
    """目标表还不存在——由调用方决定是建表还是报错。"""


class LockUnavailable(ExpansionError):
    """同一 VPC 的扩容锁被占用。"""

    retryable = False

    def __init__(self, message, vpc_id=None, lock_holder=None):
        super(LockUnavailable, self).__init__(message, vpc_id=vpc_id, lock_holder=lock_holder)


class PlanExhausted(ExpansionError):
    """地址规划耗尽或触达 VPC 网段数量上限。"""

    retryable = False

    def __init__(self, message, vpc_id=None, plan=None, cidr_count=None, max_count=None):
        super(PlanExhausted, self).__init__(
            message, vpc_id=vpc_id, plan=plan, cidr_count=cidr_count, max_count=max_count)


class AssociationTimeout(ExpansionError):
    """附加网段未在轮询窗口内生效。"""

    def __init__(self, message, vpc_id=None, cidr=None, waited_seconds=None):
        super(AssociationTimeout, self).__init__(
            message, vpc_id=vpc_id, cidr=cidr, waited_seconds=waited_seconds)


class PartialExpansion(ExpansionError):
    """部分层级子网创建失败：链路产出可用但不完整。"""

    retryable = False

    def __init__(self, message, failed=None, created=None):
        super(PartialExpansion, self).__init__(message, failed=failed, created=created)
        self.failed = failed or []
        self.created = created or []
