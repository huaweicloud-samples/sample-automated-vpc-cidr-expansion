"""一次扩容的顺序执行体：整条扩展链路在一次函数调用内跑完。

调用内的步骤与它们在本模块里的位置：

================================  =========================================
步骤                               本模块位置
================================  =========================================
读取 VPC 与子网现状               :meth:`ExpansionRunner._read_state`
利用率的二次校验                   :meth:`ExpansionRunner.run` 开头的阈值判定
从规划里消费一个网段               :meth:`_consume_plan_cidr`
附加网段并轮询生效                 :meth:`_associate`
新网段并入地址组                   :meth:`_ensure_address_group`
按层级批量创建子网                 :meth:`_create_subnets`
写扩容留痕                         :meth:`_record`
================================  =========================================

取锁 / 放锁 / 发通知在入口模块 :mod:`index` 完成：
放锁必须覆盖「本模块抛出任何异常」的情况，放在入口的收尾里比放在这里更可靠。

两处时序取舍写在这里而不是藏在调用方：

* **子网重试预算是「整段共享」而非「每子网各 3×30 秒」**。
  一次调用要铺开 ``层级数 × 可用区数``
  个子网，叠加 600 秒的附加网段轮询上限后必然吃穿 ``expansion_timeout = 900s``，
  进程被平台杀掉会留下「锁没放、通知没发、留痕没写」的三不管状态。
  因此整段预算由既有的两个键算出（``SUBNET_RETRY_ATTEMPTS × SUBNET_RETRY_INTERVAL``，
  结果作为 ``subnet_retry_budget_seconds`` 出现在结果与留痕里），预算耗尽即停止等待、
  把剩余子网记为失败，让链路至少产出可用的那部分与一份完整留痕。
* **写操作结果未知时先回读再决定**：附加网段与子网创建都不盲目重发，
  与 :mod:`hw_cloud_api` 的「读可重试、写不盲重试」口径一致。
"""

import time
from datetime import datetime, timezone

import net_plan
from handler_errors import (AssociationTimeout, ExpansionError, PartialExpansion,
                            PlanExhausted, ServiceError, TransportError)
from history_writer import HISTORY_KEY_SCHEMA, HistoryWriter, build_record
from runtime_conf import Tier
from state_store import key_schema

STATUS_SUCCESS = "SUCCESS"
STATUS_PARTIAL = "PARTIAL_SUCCESS"
STATUS_FAILED = "FAILED"
STATUS_SKIPPED_THRESHOLD = "SKIPPED_BELOW_THRESHOLD"
STATUS_SKIPPED_TARGET = "SKIPPED_TARGET_MISMATCH"
STATUS_SKIPPED_LOCK = "SKIPPED_LOCK_BUSY"

#: 需要向结果主题发通知的状态（跳过类不发，避免每个巡检周期都寄一封信）
NOTIFIED_STATUSES = (STATUS_SUCCESS, STATUS_PARTIAL, STATUS_FAILED)

#: 地址组 / 附加网段的冲突类错误特征：这类错误意味着「事实已经如此」，不是故障
_CONFLICT_MARKERS = ("409", "conflict", "already", "exist", "duplicate")

#: 华为云子网名称长度上限
_MAX_NAME_LENGTH = 64


def ensure_tables(store, config, logger=None):
    """把锁表准备就绪，历史表尽力而为。

    部署上把建表归给巡检函数，
    但指令消息完全可能先于首次巡检到达：``DescribeTable`` 是一次廉价读，
    缺失就补建，避免「栈刚部署完、第一条扩容指令必然失败」。
    配置表仍由巡检函数创建，本函数只读，读不到就回落到环境变量里的规划。
    """
    ensured = []
    try:
        ensured.append(store.ensure_table(config.lock_table, key_schema("vpc_id")))
    except ExpansionError as error:
        _log(logger, "error", "lock_table_unavailable detail=%s", error)
        raise

    try:
        ensured.append(store.ensure_table(config.history_table, HISTORY_KEY_SCHEMA))
    except ExpansionError as error:
        _log(logger, "warning", "history_table_unavailable detail=%s", error)
    return ensured


def required_addresses(tiers):
    """一次「全层级铺开」所需地址总数：每层每个可用区一个子网。"""
    return sum(tier.subnet_slots() * tier.addresses_per_subnet() for tier in tiers)


class ExpansionRunner(object):
    """一次调用的执行序列；所有依赖由入口注入，便于脱离云端驱动。"""

    def __init__(self, config, api, store, logger=None, sleeper=None, function=None):
        self.config = config
        self.api = api
        self.store = store
        self.logger = logger
        self.sleeper = sleeper or time.sleep
        self.function = function or "vpc-cidr-expansion"
        self.history = HistoryWriter(store, config.history_table, config, logger)
        self._budget_seconds = max(config.subnet_retry_attempts * config.subnet_retry_interval, 1)
        self._budget_used = 0

    # ------------------------------------------------------------------
    def run(self, request, credentials=None):
        """跑完一次扩容并返回结构化结果。

        :raises PlanExhausted: 网段数量触顶或规划网段已用尽
        :raises AssociationTimeout: 附加网段在轮询窗口内未生效
        :raises PartialExpansion: 一个子网都没建出来（此时留痕已写入，可据此排障）
        """
        started = time.time()
        started_at = _utc_now()

        plan = self._effective_plan(request.vpc_id)
        vpc, subnets, state = self._read_state(request.vpc_id)
        utilization = self._utilization(request, state)
        required = required_addresses(plan["tiers"])

        result = {
            "vpc_id": request.vpc_id,
            "vpc_name": vpc.get("name"),
            "execution_id": request.execution_id,
            "reason": request.reason,
            "utilization": utilization,
            "warning_threshold": plan["warning_threshold"],
            "required_addresses": required,
            "plan_source": plan["source"],
            "tiers": [tier.to_dict() for tier in plan["tiers"]],
            "state_before": state,
            "subnets_created": [],
            "subnets_failed": [],
            "skipped_plan": [],
            "secondary_cidr_added": False,
            "new_cidr": None,
            "function": self.function,
        }

        # 二次校验：巡检侧的数值优先，缺失时用云端现状自行计算
        if utilization is not None and utilization < plan["warning_threshold"]:
            result.update({
                "status": STATUS_SKIPPED_THRESHOLD,
                "mode": "not_required",
                "detail": "利用率 %s%% 低于扩容函数侧的阈值 %s%%，本次不动任何资源"
                          % (utilization, plan["warning_threshold"]),
                "elapsed_seconds": int(time.time() - started),
            })
            self._log("info", "expansion_skipped_threshold utilization=%s threshold=%s",
                      utilization, plan["warning_threshold"])
            return result

        # 需要性判断：现有空闲地址能否装下全层级
        mode = "secondary_cidr" if state["unallocated_addresses"] < required else "existing_space"
        cidrs = state["cidr_blocks"]
        if mode == "secondary_cidr":
            new_cidr, skipped, cidrs = self._consume_plan_cidr(
                request.vpc_id, plan, vpc, subnets, cidrs)
            result["secondary_cidr_added"] = True
            result["new_cidr"] = new_cidr
            result["skipped_plan"] = skipped
            result["route_check"] = self._route_check(new_cidr)
            result["address_group"] = self._ensure_address_group(new_cidr)
        else:
            result["route_check"] = {"checked": False, "reason": "本次未新增网段"}
            result["address_group"] = {"updated": False, "reason": "本次未新增网段"}
        result["mode"] = mode

        created, failed = self._create_subnets(
            request.vpc_id, plan["tiers"], cidrs, subnets, request.execution_id)
        result["subnets_created"] = created
        result["subnets_failed"] = failed
        result["netmask"] = min(tier.netmask for tier in plan["tiers"])
        result["cidr_count_after"] = len(cidrs)
        result["state_after"] = self._final_state(request.vpc_id)
        result["subnet_retry_budget_seconds"] = self._budget_seconds
        result["subnet_retry_used_seconds"] = self._budget_used

        if created and not failed:
            result["status"] = STATUS_SUCCESS
        elif created:
            result["status"] = STATUS_PARTIAL
        else:
            result["status"] = STATUS_FAILED

        result["elapsed_seconds"] = int(time.time() - started)
        result.update(self._record(result, request, plan, started_at, credentials))

        if result["status"] == STATUS_FAILED:
            error = PartialExpansion(
                "附加网段处理已完成但子网全部创建失败：层级未铺开",
                failed=failed, created=created)
            # 把现场结果挂在异常上，入口的失败通知才能带上「已改动了什么」
            error.result = result
            raise error

        self._log("info", "expansion_done status=%s created=%s failed=%s cidr=%s",
                  result["status"], len(created), len(failed), result.get("new_cidr"))
        return result

    # ------------------------------------------------------------------
    # 现状与规划
    # ------------------------------------------------------------------
    def _effective_plan(self, vpc_id):
        """配置表覆盖环境变量：按 VPC 定制的阈值 / 网段规划 / 层级优先。"""
        plan = {
            "warning_threshold": self.config.warning_threshold,
            "secondary_cidrs": [str(item) for item in self.config.secondary_cidr_plan],
            "tiers": list(self.config.tier_plan),
            "source": "environment",
        }
        if not self.config.config_table:
            return plan
        try:
            row = self.store.get_item(self.config.config_table, {"vpc_id": vpc_id})
        except ExpansionError as error:
            # 表不存在或读失败都不是故障：回落到部署栈下发的规划
            self._log("warning", "config_row_unavailable detail=%s", error)
            return plan
        if not isinstance(row, dict) or not row:
            return plan

        overridden = dict(plan)
        applied = []
        threshold = _as_float(row.get("warning_threshold"))
        if threshold is not None and 0 < threshold <= 100:
            overridden["warning_threshold"] = threshold
            applied.append("warning_threshold")
        cidrs = _as_string_list(row.get("secondary_cidrs"))
        if cidrs:
            overridden["secondary_cidrs"] = cidrs
            applied.append("secondary_cidrs")
        tiers = _as_dict_list(row.get("tiers"))
        if tiers:
            # 覆盖内容非法就直接失败：静默回落到全局规划会让「按 VPC 定制」这件事不可信
            overridden["tiers"] = [
                Tier.from_plan(item, self.config.subnet_netmask) for item in tiers
            ]
            applied.append("tiers")
        if applied:
            overridden["source"] = "config_table(%s)" % "+".join(applied)
            self._log("info", "plan_overridden_by_config_table fields=%s vpc_id=%s",
                      ",".join(applied), vpc_id)
        return overridden

    def _read_state(self, vpc_id):
        vpc = self.api.show_vpc(vpc_id)
        if vpc.get("status") and vpc["status"] != "AVAILABLE":
            self._log("warning", "vpc_status=%s", vpc["status"])
        subnets = self.api.list_subnets(vpc_id)
        return vpc, subnets, net_plan.summarize_state(_cidrs(vpc), subnets)

    def _final_state(self, vpc_id):
        """收尾再读一次：结果里的 ``state_after`` 是「扩容真的生效」的证据。"""
        try:
            return self._read_state(vpc_id)[2]
        except ExpansionError as error:
            self._log("warning", "final_state_unread detail=%s", error)
            return None

    def _utilization(self, request, state):
        """利用率口径：巡检侧算好的值优先，缺失时用同一常数自行计算。"""
        if request.utilization is not None:
            return float(request.utilization)
        return state.get("utilization")

    # ------------------------------------------------------------------
    # 网段：消费规划 → 附加 → 轮询生效
    # ------------------------------------------------------------------
    def _consume_plan_cidr(self, vpc_id, plan, vpc, subnets, cidrs):
        max_count = self.config.cidr_max_count
        if len(cidrs) >= max_count:
            raise PlanExhausted(
                "VPC 网段数量已达上限，无法再附加新网段",
                vpc_id=vpc_id, plan=plan["secondary_cidrs"],
                cidr_count=len(cidrs), max_count=max_count)

        picked, skipped = net_plan.pick_plan_cidr(
            plan["secondary_cidrs"], vpc.get("cidr"), vpc.get("extend_cidrs"),
            [item.get("cidr") for item in subnets])
        if not picked:
            raise PlanExhausted(
                "地址规划已用尽：没有可安全消费的规划网段",
                vpc_id=vpc_id, plan=plan["secondary_cidrs"],
                cidr_count=len(cidrs), max_count=max_count)

        self._associate(vpc_id, picked)
        return picked, skipped, cidrs + [picked]

    def _associate(self, vpc_id, cidr):
        """写一次附加，然后靠轮询回读确认生效——写超时不重发。"""
        try:
            self.api.add_extend_cidr(vpc_id, cidr)
        except ServiceError as error:
            if _is_conflict(error):
                self._log("warning", "extend_cidr_conflict 视为已附加，转回读 cidr=%s", cidr)
            elif _is_client_error(error):
                # 参数/权限类错误重试与等待都不会变好，立刻失败
                raise
            else:
                self._log("warning", "extend_cidr_unconfirmed detail=%s", error)
        except TransportError as error:
            self._log("warning", "extend_cidr_timeout 结果未知，转回读 detail=%s", error)

        outcome = self.api.wait_for_extend_cidr(
            vpc_id, cidr, self.config.poll_interval_seconds,
            self.config.poll_deadline_seconds, sleeper=self.sleeper)
        if outcome["state"] != "associated":
            raise AssociationTimeout(
                "附加网段未在轮询窗口内生效", vpc_id=vpc_id, cidr=cidr,
                waited_seconds=outcome["waited_seconds"])
        self._log("info", "extend_cidr_associated cidr=%s waited=%s",
                  cidr, outcome["waited_seconds"])
        return outcome

    # ------------------------------------------------------------------
    # 路由（只读校验）与地址组（安全组联动的真正落点）
    # ------------------------------------------------------------------
    def _route_check(self, cidr):
        """读默认路由表确认新网段已被覆盖；函数不写路由。

        华为云的 VPC 网段路由由平台在附加网段生效时自动维护，
        且账号侧每个 VPC 通常只允许 1 张路由表，
        因此这一步只做生效校验与留痕，不参与决策，更不试图改路由。
        """
        table_id = self.config.default_route_table_id
        if not table_id:
            return {"checked": False, "reason": "未配置 DEFAULT_ROUTE_TABLE_ID"}
        try:
            table = self.api.show_route_table(table_id)
        except ExpansionError as error:
            return {"checked": False, "reason": "路由表读取失败", "detail": _clip(str(error))}

        routes = table.get("routes") or []
        destinations = [item.get("destination") for item in routes if isinstance(item, dict)]
        matched = self._most_specific_cover(destinations, cidr)
        return {
            "checked": True,
            "covered": matched is not None,
            "matched": matched,
            "route_table_id": table.get("id") or table_id,
            "route_count": len(destinations),
            "note": "只读校验：网段路由由平台自动维护；默认路由不参与判定",
        }

    @staticmethod
    def _most_specific_cover(destinations, cidr):
        """在所有能覆盖 ``cidr`` 的路由里取最具体的一条。

        默认路由 ``0.0.0.0/0`` 必须排除：它覆盖一切，
        拿它判定「新网段已被 VPC 本地路由覆盖」等于永远判定通过，
        这条软校验就失去意义了（本实践把路由表作为只读校验对象的取舍）。
        """
        best = None
        for destination in destinations:
            net = net_plan.network(destination)
            if net is None or (str(net.network_address) == "0.0.0.0" and net.prefixlen == 0):
                continue
            if net_plan.covers(destination, cidr) and (best is None or net.prefixlen > best[0]):
                best = (net.prefixlen, destination)
        return best[1] if best else None

    def _ensure_address_group(self, cidr):
        """把新网段确保进地址组（幂等）。

        安全组入站规则引用的是地址组而非子网，因此这一步承载「新网段即刻对内网互通生效」：
        一次成员写入让所有层级覆盖新地址空间。
        """
        group_id = self.config.address_group_id
        if not group_id:
            return {"updated": False, "reason": "未配置 ADDRESS_GROUP_ID"}
        group = self.api.show_address_group(group_id)
        members = list(group.get("ip_set") or [])
        if cidr in members:
            return {"updated": False, "reason": "已是成员", "id": group_id, "members": members}

        capacity = group.get("max_capacity")
        if capacity is not None and len(members) + 1 > int(capacity):
            raise ServiceError(
                "地址组容量不足：新增成员会超过 max_capacity，需调高部署栈的容量后重新交付",
                error_code="AddressGroupCapacityFull",
                body="id=%s members=%s max_capacity=%s pending=%s"
                     % (group_id, len(members), capacity, cidr))

        try:
            self.api.set_address_group_members(group_id, members + [cidr])
        except (TransportError, ServiceError) as error:
            observed = self._read_group_members(group_id)
            if observed and cidr in observed:
                self._log("warning", "address_group_written_after_error cidr=%s detail=%s",
                          cidr, error)
                return {"updated": True, "id": group_id, "members": observed,
                        "verified_after_error": True}
            raise
        self._log("info", "address_group_updated id=%s cidr=%s", group_id, cidr)
        return {"updated": True, "id": group_id, "members": members + [cidr]}

    def _read_group_members(self, group_id):
        try:
            return self.api.show_address_group(group_id).get("ip_set") or []
        except ExpansionError as error:
            self._log("warning", "address_group_readback_failed detail=%s", error)
            return None

    # ------------------------------------------------------------------
    # 子网
    # ------------------------------------------------------------------
    def _create_subnets(self, vpc_id, tiers, cidrs, subnets, execution_id):
        created = []
        failed = []
        # 已占用地址以云端现状为准，并在每次成功后追加，避免同轮内切出重叠子网
        occupied = [item.get("cidr") for item in subnets if item.get("cidr")]

        for tier in tiers:
            tier_names = net_plan.matching_names(tier.sample_name, subnets)
            names = net_plan.next_names(tier.sample_name, tier_names, tier.subnet_slots())
            baseline = self._baseline(tier, subnets)

            for index, zone in enumerate(tier.availability_zones):
                name = names[index] if index < len(names) else "%s-%d" % (
                    tier.sample_name, index + 1)
                context = {"tier": tier.tier_name, "name": name, "availability_zone": zone}

                if not net_plan.name_is_within_limit(name, _MAX_NAME_LENGTH):
                    failed.append(dict(context, cidr=None,
                                       reason="子网名称超过 %d 字符上限" % _MAX_NAME_LENGTH))
                    continue

                cidr, _ = net_plan.allocate_from_free(
                    net_plan.free_blocks(cidrs, occupied), tier.netmask)
                if not cidr:
                    failed.append(dict(context, cidr=None,
                                       reason="空闲地址不足：无法切出 /%d 网段" % tier.netmask))
                    continue

                item, failure = self._create_one(
                    vpc_id, tier, name, cidr, zone, baseline, execution_id)
                occupied.append((item or {}).get("cidr") or cidr)
                if item:
                    created.append(item)
                if failure:
                    failed.append(failure)

        return created, failed

    def _create_one(self, vpc_id, tier, name, cidr, zone, baseline, execution_id):
        """创建单个子网，在共享重试预算内重试。

        :returns: ``(创建结果或 None, 失败记录或 None)``——不抛异常，
                  单个层级失败不该让整条链路白跑（附加网段已经生效）。
        """
        spec = {
            "name": name,
            "cidr": cidr,
            "gateway_ip": net_plan.gateway_ip(cidr),
            "availability_zone": zone,
            "description": "自动扩容子网（层级 %s）" % tier.tier_name,
            "dhcp_enable": baseline.get("dhcp_enable", True),
            "primary_dns": baseline.get("primary_dns"),
            "secondary_dns": baseline.get("secondary_dns"),
            "dns_list": baseline.get("dns_list"),
            "tags": {
                "ManagedBy": "sample-automated-vpc-cidr-expansion",
                "Tier": tier.tier_name,
                "ExpansionExecution": execution_id,
                "ExpansionCidr": cidr,
            },
        }

        last_error = None
        attempts = 0
        for attempt in range(1, self.config.subnet_retry_attempts + 1):
            attempts = attempt
            try:
                return _subnet_view(self.api.create_subnet(vpc_id, spec), tier, zone), None
            except TransportError as error:
                # 超时后事实状态未知：先按名字回读，命中就当创建成功，绝不重发
                last_error = error
                observed = self._safe_subnet_exists(vpc_id, name)
                if observed:
                    self._log("warning", "subnet_created_after_timeout name=%s", name)
                    return _subnet_view(observed, tier, zone), None
            except ServiceError as error:
                last_error = error
                if _is_conflict(error):
                    observed = self._safe_subnet_exists(vpc_id, name)
                    if observed:
                        return _subnet_view(observed, tier, zone), None
                if not error.retryable_by_status():
                    break

            if attempt < self.config.subnet_retry_attempts and not self._budget_sleep(
                    self.config.subnet_retry_interval):
                self._log("warning", "subnet_retry_budget_exhausted name=%s attempt=%s",
                          name, attempt)
                break

        return None, {
            "tier": tier.tier_name,
            "name": name,
            "cidr": cidr,
            "availability_zone": zone,
            "attempts": attempts,
            "reason": _clip(str(last_error)),
        }

    def _budget_sleep(self, seconds):
        """子网阶段的共享等待预算：用完就不再等（详见模块 docstring 的时序取舍）。"""
        if self._budget_used + seconds > self._budget_seconds:
            return False
        self.sleeper(seconds)
        self._budget_used += seconds
        return True

    def _safe_subnet_exists(self, vpc_id, name):
        try:
            return self.api.subnet_exists(vpc_id, name)
        except ExpansionError as error:
            self._log("warning", "subnet_readback_failed name=%s detail=%s", name, error)
            return None

    def _baseline(self, tier, subnets):
        """从同层级的既有子网继承 DNS/DHCP 配置。

        委托没有授予云解析读权限（见 ``infra/iam.tf``），函数无法查询区域默认 DNS，
        因此以该层级的样例子网为基线；找不到基线时全部留空，
        由 VPC 侧套用区域默认解析地址。
        """
        wanted = set(net_plan.matching_names(tier.sample_name, subnets))
        for subnet in subnets:
            if subnet.get("name") not in wanted:
                continue
            return {
                "dhcp_enable": subnet.get("dhcp_enable", True),
                "primary_dns": subnet.get("primary_dns"),
                "secondary_dns": subnet.get("secondary_dns"),
                "dns_list": subnet.get("dnsList") or subnet.get("dns_list"),
            }
        return {}

    # ------------------------------------------------------------------
    # 留痕
    # ------------------------------------------------------------------
    def _record(self, result, request, plan, started_at, credentials):
        record = build_record(result, request, plan, self.config, started_at)
        payload = {
            "history_table": self.config.history_table,
            "history_written": self.history.write(record),
        }
        if credentials is not None:
            payload["history_object"] = self.history.write_object(record, credentials)
        else:
            payload["history_object"] = {"written": False, "key": None,
                                         "detail": "本次调用未提供凭据"}
        if not payload["history_written"]:
            self._log("error", "history_missing 云资源已改动但台账未落表，需人工补录 vpc_id=%s",
                      result.get("vpc_id"))
        return payload

    def _log(self, level, message, *args):
        _log(self.logger, level, message, *args)


def _subnet_view(subnet, tier, zone):
    """把创建响应压成留痕/通知需要的字段（响应里的 ``subnet`` 已在此前解开）。"""
    return {
        "id": subnet.get("id"),
        "name": subnet.get("name"),
        "cidr": subnet.get("cidr"),
        "availability_zone": subnet.get("availability_zone") or zone,
        "gateway_ip": subnet.get("gateway_ip"),
        "tier": tier.tier_name,
    }


def _cidrs(vpc):
    """主网段 + 已生效的附加网段（去重、保序）。"""
    collected = []
    for cidr in [vpc.get("cidr")] + list(vpc.get("extend_cidrs") or []):
        if cidr and str(cidr) not in collected:
            collected.append(str(cidr))
    return collected


def _is_conflict(error):
    text = " ".join([str(error), str(getattr(error, "status_code", "")),
                     str(getattr(error, "error_code", "")),
                     str(getattr(error, "body", ""))]).lower()
    return any(marker in text for marker in _CONFLICT_MARKERS)


def _is_client_error(error):
    """4xx（除 408/409/429）：等待与重试都不会改变结果，直接失败。"""
    status = getattr(error, "status_code", None)
    if status is None:
        return False
    return 400 <= int(status) < 500 and int(status) not in (408, 409, 429)


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_string_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item]
    return []


def _as_dict_list(value):
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, dict)]


def _clip(text, limit=300):
    text = str(text)
    return text if len(text) <= limit else text[:limit] + "..."


def _utc_now():
    """UTC 时间戳：``timestamp`` 是历史表的排序键，定宽字典序即时间序。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log(logger, level, message, *args):
    if logger is not None:
        getattr(logger, level, logger.info)(message, *args)
