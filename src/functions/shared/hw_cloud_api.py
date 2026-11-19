"""华为云侧的全部读写收口在这一个类：网络（VPC / 子网 / 路由表 / 地址组）与消息通知。

每个方法都固定三件事——超时、重试上限、失败时抛出的异常类型，
并按「读可重试、写不盲重试」的口径区分（接口与用途均按官方 SDK v3/v2 的路径与
请求体字段实现）：

====================================  ==========================================
接口                                 用途
====================================  ==========================================
``GET  /v3/{p}/vpc/vpcs``             按标签发现被纳管的 VPC（分页）
``GET  /v3/{p}/vpc/vpcs/{vpc_id}``    读主网段与已生效的附加网段（``extend_cidrs``）
``PUT  /v3/{p}/vpc/vpcs/{vpc_id}/add-extend-cidr``  消费一个规划网段
``GET  /v1/{p}/subnets?vpc_id=``      列子网（含 ``available_ip_address_count``）
``POST /v1/{p}/subnets``              创建扩容子网
``GET  /v1/{p}/routetables/{id}``     只读校验默认路由表是否已覆盖新网段
``GET  /v3/{p}/vpc/address-groups/{id}`` / ``PUT``  把新网段确保进地址组
``POST /v2/{p}/notifications/topics/{urn}/publish``  发布扩容结果通知
====================================  ==========================================

写操作的重试策略（重要）：

* ``add_extend_cidr`` / ``create_subnet`` / ``update_address_group`` 超时后**结果未知**，
  本类一律不自动重发；由调用方用轮询或回读确认事实状态后再决定是否补做，
  避免出现「同一网段附加两次」「同名子网重复创建」的隐性副作用。
* 路由表在本实践里**只读**：华为云的 VPC 网段路由由平台在附加网段生效时自动维护，
  函数不写路由，只用它做生效校验与留痕。
"""

import json
from urllib.parse import quote

import hw_signer
from handler_errors import ConfigError, ServiceError
from hw_transport import HttpTransport

TIMEOUT_READ = 15
TIMEOUT_WRITE = 30
TIMEOUT_PUBLISH = 10

RETRY_READ = 2
RETRY_PUBLISH = 2
RETRY_WRITE = 0

_VPC_SERVICE = "vpc"
_SMN_SERVICE = "smn"
_CES_SERVICE = "ces"

#: 子网创建时可直接带上的标签数量上限（服务端约束）
_MAX_SUBNET_TAGS = 10


class CloudApi(object):
    """一次扩容调用内使用的服务端点集合。"""

    def __init__(self, credentials, region, project_id=None, logger=None, transport=None):
        if not region:
            raise ConfigError("缺少区域配置", key="HW_REGION")
        self.credentials = credentials
        self.region = region
        self.project_id = project_id or getattr(credentials, "project_id", None)
        if not self.project_id:
            raise ConfigError("缺少项目 ID", key="HW_PROJECT_ID")
        self.logger = logger
        self.transport = transport or HttpTransport(logger=logger)

    # ------------------------------------------------------------------
    # VPC：网段
    # ------------------------------------------------------------------
    def list_vpcs(self):
        """列出本项目内全部 VPC（含标签），供巡检侧按标签筛选被纳管对象。

        返回元素字段：``id``、``name``、``cidr``、``status``、``tags``（已归一成字典）。
        分页游标同时兼容 ``metadata.marker`` 与 ``page_info.next_marker`` 两种响应形态。
        """
        marker = None
        collected = []
        while True:
            query = [("limit", "2000")]
            if marker:
                query.append(("marker", marker))
            payload = self._call(
                "GET", _VPC_SERVICE, "/v3/%s/vpc/vpcs" % self._project(),
                query=query, timeout=TIMEOUT_READ, retries=RETRY_READ)
            page = payload.get("vpcs") if isinstance(payload, dict) else None
            page = page if isinstance(page, list) else []
            for item in page:
                if isinstance(item, dict):
                    collected.append({
                        "id": item.get("id"),
                        "name": item.get("name"),
                        "cidr": item.get("cidr"),
                        "status": item.get("status"),
                        "tags": _as_tag_mapping(item.get("tags")),
                    })
            if len(page) == 0:
                break
            marker = _page_next_marker(payload, page, marker)
            if not marker:
                break
        return collected

    def show_vpc(self, vpc_id):
        """读取 VPC 现状，返回 ``{"id","cidr","extend_cidrs","status"}``。"""
        payload = self._call(
            "GET", _VPC_SERVICE,
            "/v3/%s/vpc/vpcs/%s" % (self._project(), _encode(vpc_id)),
            timeout=TIMEOUT_READ, retries=RETRY_READ)
        vpc = payload.get("vpc") if isinstance(payload, dict) else None
        if not isinstance(vpc, dict):
            raise ServiceError("VPC 详情响应缺少 vpc 字段", vpc_id=vpc_id, body=_truncate(payload))
        return {
            "id": vpc.get("id") or vpc_id,
            "name": vpc.get("name"),
            "cidr": vpc.get("cidr"),
            "extend_cidrs": _as_list(vpc.get("extend_cidrs")),
            "status": vpc.get("status"),
        }

    def add_extend_cidr(self, vpc_id, cidr):
        """把一个规划网段附加到 VPC。

        **只发一次**：超时后由 :meth:`wait_for_extend_cidr` 回读判定是否已生效，
        服务端对重复附加会返回冲突，盲目重发反而会把链路卡住。
        """
        body = {"vpc": {"extend_cidrs": [cidr]}}
        return self._call(
            "PUT", _VPC_SERVICE,
            "/v3/%s/vpc/vpcs/%s/add-extend-cidr" % (self._project(), _encode(vpc_id)),
            body=body, timeout=TIMEOUT_WRITE, retries=RETRY_WRITE)

    def wait_for_extend_cidr(self, vpc_id, cidr, interval_seconds, deadline_seconds,
                             sleeper=None):
        """轮询附加网段是否生效——写超时后的事实状态确认。

        :returns: ``{"state": "associated"|"timeout", "waited_seconds": int, "vpc": dict}``
        """
        import time

        sleep = sleeper or time.sleep
        waited = 0
        deadline = max(int(deadline_seconds), int(interval_seconds))
        while True:
            vpc = self.show_vpc(vpc_id)
            if cidr in vpc["extend_cidrs"]:
                return {"state": "associated", "waited_seconds": waited, "vpc": vpc}
            if waited >= deadline:
                return {"state": "timeout", "waited_seconds": waited, "vpc": vpc}
            sleep(min(int(interval_seconds), max(deadline - waited, 1)))
            waited += min(int(interval_seconds), max(deadline - waited, 1))

    # ------------------------------------------------------------------
    # VPC：子网
    # ------------------------------------------------------------------
    def list_subnets(self, vpc_id):
        """列出 VPC 内全部子网（分页游标由 ``marker`` 承载）。"""
        marker = None
        collected = []
        while True:
            query = [("vpc_id", vpc_id), ("limit", "2000")]
            if marker:
                query.append(("marker", marker))
            payload = self._call(
                "GET", _VPC_SERVICE, "/v1/%s/subnets" % self._project(),
                query=query, timeout=TIMEOUT_READ, retries=RETRY_READ)
            page = payload.get("subnets") or []
            collected.extend(page)
            next_marker = _next_marker(payload, page, marker)
            if not next_marker or next_marker == marker:
                break
            marker = next_marker
        return collected

    def create_subnet(self, vpc_id, spec):
        """创建一个扩容子网。

        ``spec`` 关键字段：``name``、``cidr``、``gateway_ip``、``availability_zone``、
        ``description``、``dhcp_enable``、``primary_dns``、``secondary_dns``、
        ``dns_list``、``tags``（``key*value`` 字符串列表）。
        """
        subnet_body = {
            "name": spec["name"],
            "description": spec.get("description") or "自动扩容子网",
            "vpc_id": vpc_id,
            "cidr": spec["cidr"],
            "gateway_ip": spec["gateway_ip"],
            "availability_zone": spec["availability_zone"],
            "dhcp_enable": bool(spec.get("dhcp_enable", True)),
        }
        for key, field in (("primary_dns", "primary_dns"), ("secondary_dns", "secondary_dns"),
                           ("dns_list", "dnsList")):
            if spec.get(key):
                subnet_body[field] = spec[key]
        tags = _subnet_tags(spec.get("tags") or {})
        if tags:
            subnet_body["tags"] = tags

        payload = self._call(
            "POST", _VPC_SERVICE, "/v1/%s/subnets" % self._project(),
            body={"subnet": subnet_body}, timeout=TIMEOUT_WRITE, retries=RETRY_WRITE)
        created = payload.get("subnet") if isinstance(payload, dict) else None
        if not isinstance(created, dict) or not created.get("id"):
            raise ServiceError("子网创建响应缺少 subnet.id", body=_truncate(payload))
        return created

    def subnet_exists(self, vpc_id, name):
        """按名称确认子网是否已存在——写超时后的事实状态回读。"""
        for subnet in self.list_subnets(vpc_id):
            if subnet.get("name") == name:
                return subnet
        return None

    # ------------------------------------------------------------------
    # VPC：路由表（只读校验）
    # ------------------------------------------------------------------
    def show_route_table(self, route_table_id):
        payload = self._call(
            "GET", _VPC_SERVICE, "/v1/%s/routetables/%s" % (self._project(), _encode(route_table_id)),
            timeout=TIMEOUT_READ, retries=RETRY_READ)
        table = payload.get("routetable") if isinstance(payload, dict) else None
        if not isinstance(table, dict):
            raise ServiceError("路由表响应缺少 routetable 字段", body=_truncate(payload))
        return table

    # ------------------------------------------------------------------
    # VPC：地址组（安全组网段联动的唯一可变集合）
    # ------------------------------------------------------------------
    def show_address_group(self, address_group_id):
        payload = self._call(
            "GET", _VPC_SERVICE,
            "/v3/%s/vpc/address-groups/%s" % (self._project(), _encode(address_group_id)),
            timeout=TIMEOUT_READ, retries=RETRY_READ)
        group = payload.get("address_group") if isinstance(payload, dict) else None
        if not isinstance(group, dict):
            raise ServiceError("地址组响应缺少 address_group 字段", body=_truncate(payload))
        return {
            "id": group.get("id") or address_group_id,
            "name": group.get("name"),
            "ip_set": _as_list(group.get("ip_set")),
            "max_capacity": group.get("max_capacity"),
        }

    def set_address_group_members(self, address_group_id, ip_set):
        """整体写回地址组成员。

        华为云地址组的成员集合以 PUT 覆盖，因此调用方必须先读后写，
        本方法只负责写入并返回是否成功；容量越界由服务端拒绝并原样上抛。
        """
        return self._call(
            "PUT", _VPC_SERVICE,
            "/v3/%s/vpc/address-groups/%s" % (self._project(), _encode(address_group_id)),
            body={"address_group": {"ip_set": list(ip_set)}},
            timeout=TIMEOUT_WRITE, retries=RETRY_WRITE)

    # ------------------------------------------------------------------
    # 监控指标
    # ------------------------------------------------------------------
    def report_metric_batch(self, samples):
        """把一批自定义指标写入监控服务的自定义命名空间。

        ``samples`` 的每个元素形如
        ``{"namespace","metric_name","dimensions","value","unit","timestamp"}``，
        其中 ``timestamp`` 是毫秒时间戳。命名空间、指标名与维度名由部署栈下发，
        必须与大盘取数用的取值一致，否则大盘无数据。

        这条写允许重试：同一条序列的标识是「命名空间 + 指标名 + 维度 + 时间戳」，
        带着相同时间戳重发只会覆盖同一个点，不会产生第二条数据，
        与「写不盲重试」针对的「附加网段/建子网」那类会改变资源事实的操作不同。
        """
        if not samples:
            return {"skipped": "no samples"}
        body = [{
            "namespace": item["namespace"],
            "metricName": item["metric_name"],
            "dimensions": item.get("dimensions") or [],
            "value": float(item.get("value") or 0),
            "unit": item.get("unit") or "Count",
            "timestamp": int(item.get("timestamp")),
        } for item in samples]
        return self._call(
            "POST", _CES_SERVICE, "/v1/%s/metric-data" % self._project(),
            body=body, timeout=TIMEOUT_PUBLISH, retries=RETRY_PUBLISH)

    # ------------------------------------------------------------------
    # 消息通知
    # ------------------------------------------------------------------
    def publish_message(self, topic_urn, subject, message):
        """向结果通知主题发布一条消息（邮件订阅需已确认，否则不会投递）。

        主题正文只承载可打印 ASCII 标题与中文正文：标题会进入邮件头，
        含中文时部分收件端会拒收或乱码。
        """
        if not topic_urn:
            return {"skipped": "notification topic not configured"}
        path = "/v2/%s/notifications/topics/%s/publish" % (self._project(), _encode(topic_urn))
        return self._call(
            "POST", _SMN_SERVICE, path,
            body={"subject": str(subject)[:100], "message": message},
            timeout=TIMEOUT_PUBLISH, retries=RETRY_PUBLISH)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _call(self, method, service, path, body=None, query=None, headers=None,
              timeout=TIMEOUT_READ, retries=0):
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        request_headers = {"Accept": "application/json"}
        if encoded is not None:
            request_headers["Content-Type"] = "application/json"
        for key, value in (headers or {}).items():
            request_headers[key] = value

        signed_headers, url, payload_bytes = hw_signer.build_request(
            base_url=self._base_url(service),
            method=method,
            path=path,
            credentials=self.credentials,
            query_params=query or [],
            headers=request_headers,
            body=encoded,
        )
        response = self.transport.request(
            method=method, url=url, headers=signed_headers, body=payload_bytes,
            timeout=timeout, retries=retries)
        data = response.json()
        self._log(method, path, response, data)
        return data

    def _base_url(self, service):
        return "https://%s.%s.myhuaweicloud.com" % (service, self.region)

    def _project(self):
        return _encode(self.project_id)

    def _log(self, method, path, response, payload):
        if self.logger is None:
            return
        self.logger.info(
            "cloud_api %s",
            json.dumps({
                "method": method,
                "path": path,
                "status": response.status_code,
                "request_id": response.request_id(),
                "body": _truncate(payload),
            }, default=str, ensure_ascii=False))


def _encode(value):
    """路径参数整体转义（主题 URN 含 ``:`` 与 ``/``）。"""
    return quote(str(value), safe="")


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item]
    return []


def _as_tag_mapping(value):
    """把标签归一成字典：兼容 ``{"k":"v"}`` 与 ``[{"key":..,"value":..}]`` 两种响应形态。"""
    if isinstance(value, dict):
        return {str(k): ("" if v is None else str(v)) for k, v in value.items()}
    mapping = {}
    for item in value or []:
        if isinstance(item, dict) and item.get("key"):
            mapping[str(item["key"])] = "" if item.get("value") is None else str(item["value"])
    return mapping


def _page_next_marker(payload, page, current_marker):
    """分页游标：优先响应里的游标字段，退化到本页最后一个对象 ID。"""
    body = payload if isinstance(payload, dict) else {}
    for container in ("metadata", "page_info"):
        block = body.get(container)
        if isinstance(block, dict):
            for field in ("marker", "next_marker", "next"):
                if block.get(field):
                    return str(block[field])
    if page and page[-1].get("id") and page[-1]["id"] != current_marker:
        return str(page[-1]["id"])
    return None


def _subnet_tags(mapping):
    """把标签字典转成创建接口要求的 ``key*value`` 字符串列表，并截到上限。"""
    rendered = []
    for key, value in mapping.items():
        if value is None:
            continue
        rendered.append("%s*%s" % (key, value))
    return rendered[:_MAX_SUBNET_TAGS]


def _next_marker(payload, page, current):
    """分页游标：优先用响应里的 ``metadata.marker``，退化到本页最后一个子网 ID。"""
    marker = None
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    if isinstance(metadata, dict):
        marker = metadata.get("marker") or metadata.get("next")
    if not marker and page:
        marker = page[-1].get("id")
    return marker or None


def _truncate(payload, limit=500):
    text = json.dumps(payload, default=str, ensure_ascii=False) if payload is not None else ""
    return text if len(text) <= limit else text[:limit] + "..."
