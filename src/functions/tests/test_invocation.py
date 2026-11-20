"""扩容指令的事件信封解析与凭据来源。"""

import json

import pytest
from helpers import (PLACEHOLDER_ACCESS_KEY, PLACEHOLDER_PROJECT_ID, PLACEHOLDER_SECRET_KEY,
                    PLACEHOLDER_SECURITY_TOKEN, VPC_ID, FakeContext)
from handler_errors import ConfigError
from invocation import (Credentials, execution_id, function_name, parse_event,
                        resolve_credentials)

MESSAGE = json.dumps({"vpc_id": VPC_ID, "reason": "utilization", "utilization": 87.5})


def payload(**overrides):
    item = {"vpc_id": VPC_ID, "reason": "utilization", "utilization": 87.5}
    item.update(overrides)
    return item


# ----------------------------------------------------------------------
# 五种事件结构
# ----------------------------------------------------------------------
def test_envelope_one_standard_smn_record():
    """形态 1：``record[0].smn.message`` 里的 JSON 字符串（消息服务触发）。"""
    event = {"record": [{"smn": {"message": MESSAGE, "topic_urn": "urn:smn:a:b:c"}}]}
    request = parse_event(event)
    assert request.vpc_id == VPC_ID
    assert request.utilization == pytest.approx(87.5)
    assert request.reason == "utilization"


def test_envelope_two_records_sns_capitalized():
    """形态 2：``Records[0].Sns.Message``（大写字段的兼容形态）。"""
    event = {"Records": [{"Sns": {"Message": json.dumps(payload(vpc_id="vpc-alt-0002"))}}]}
    assert parse_event(event).vpc_id == "vpc-alt-0002"


def test_envelope_three_top_level_smn_block():
    """形态 3：顶层 ``smn.message``。"""
    event = {"smn": {"message": json.dumps(payload(vpc_id="vpc-alt-0003"))}}
    assert parse_event(event).vpc_id == "vpc-alt-0003"


def test_envelope_four_bare_message_string():
    """形态 4：控制台直接发布时拿到的裸 ``message`` 字符串。"""
    for field in ("message", "Message", "content"):
        event = {field: json.dumps(payload(vpc_id="vpc-alt-0004"))}
        assert parse_event(event).vpc_id == "vpc-alt-0004", field


def test_envelope_five_inline_fields():
    """形态 5：手工联调时直接把字段塞进事件。"""
    assert parse_event(payload()).vpc_id == VPC_ID
    assert parse_event({"vpcId": "vpc-alt-0005"}).vpc_id == "vpc-alt-0005"
    assert parse_event({"VpcId": "vpc-alt-0006"}).vpc_id == "vpc-alt-0006"


def test_envelope_message_as_dict_is_accepted():
    """``message`` 已是字典（某些投递形态会预先解好）也要认。"""
    assert parse_event({"message": payload(vpc_id="vpc-alt-0007")}).vpc_id == "vpc-alt-0007"


def test_event_given_as_json_text_and_as_bytes():
    assert parse_event(MESSAGE).vpc_id == VPC_ID
    assert parse_event(MESSAGE.encode("utf-8")).vpc_id == VPC_ID
    assert parse_event(json.dumps({"message": MESSAGE})).vpc_id == VPC_ID


def test_record_block_can_be_a_single_dict():
    event = {"record": {"smn": {"message": MESSAGE}}}
    assert parse_event(event).vpc_id == VPC_ID


def test_missing_vpc_id_is_a_config_error_with_the_seen_keys():
    """解析不出 VPC 标识时报错要给出看到过的字段名，便于排障。"""
    with pytest.raises(ConfigError) as error:
        parse_event({"message": {"reason": "utilization", "trigger_type": "TIMER"}})
    assert error.value.fields["keys"] == ["reason", "trigger_type"]
    with pytest.raises(ConfigError):
        parse_event({"record": [{"smn": {"message": json.dumps({"reason": "utilization"})}}]})
    with pytest.raises(ConfigError):
        parse_event({})
    with pytest.raises(ConfigError):
        parse_event("plain text, not json")


def test_non_json_body_falls_through_to_the_bare_fields():
    """``message`` 是不可解析文本、但顶层带 vpc_id 时仍按顶层字段处理。"""
    request = parse_event({"message": "just text", "vpc_id": VPC_ID})
    assert request.vpc_id == VPC_ID


def test_utilization_is_optional_and_tolerant():
    """巡检侧数值缺失时留 None，让扩容函数用云端现状自行计算。"""
    assert parse_event({"vpc_id": VPC_ID}).utilization is None
    assert parse_event({"vpc_id": VPC_ID, "utilization": "88"}).utilization == 88.0
    assert parse_event({"vpc_id": VPC_ID, "utilization": "n/a"}).utilization is None


def test_envelope_source_and_execution_id_are_recorded():
    request = parse_event(payload(source="smn"), FakeContext(request_id="exec-42"))
    assert request.envelope == "smn"
    assert request.execution_id == "exec-42"
    assert request.raw_event["vpc_id"] == VPC_ID
    assert request.to_dict()["reason"] == "utilization"


def test_execution_id_falls_back_to_a_generated_one():
    """上下文没有请求编号时生成一个，且每次调用都不同。"""
    generated = execution_id(None)
    assert len(generated) == 32
    assert generated != execution_id(None)
    assert execution_id(FakeContext(request_id="exec-fixed")) == "exec-fixed"


def test_function_name_defaults_for_direct_invocation():
    assert function_name(None) == "vpc-cidr-expansion"
    assert function_name(FakeContext(name="vpc-cidr-monitor")) == "vpc-cidr-monitor"


# ----------------------------------------------------------------------
# 凭据来源
# ----------------------------------------------------------------------
def test_credentials_come_from_the_invocation_context():
    credentials = resolve_credentials(FakeContext(), environ={})
    assert credentials.source == "agency-context"
    assert credentials.access_key == PLACEHOLDER_ACCESS_KEY
    assert credentials.secret_key == PLACEHOLDER_SECRET_KEY
    assert credentials.security_token == PLACEHOLDER_SECURITY_TOKEN
    assert credentials.project_id == PLACEHOLDER_PROJECT_ID
    # 掩码视图只保留首尾字符，中间一律打星
    assert credentials.redacted()["access_key"].count("****") == 1
    assert PLACEHOLDER_SECRET_KEY not in repr(credentials)


def test_credentials_fall_back_to_local_placeholder_pair():
    credentials = resolve_credentials(None, environ={
        "HW_ACCESS_KEY": PLACEHOLDER_ACCESS_KEY,
        "HW_SECRET_KEY": PLACEHOLDER_SECRET_KEY,
        "HW_PROJECT_ID": PLACEHOLDER_PROJECT_ID,
    })
    assert credentials.source == "environment"
    assert credentials.project_id == PLACEHOLDER_PROJECT_ID


def test_missing_credentials_fail_loudly_without_default():
    with pytest.raises(ConfigError) as error:
        resolve_credentials(FakeContext(with_credentials=False), environ={})
    assert error.value.fields["missing"] == ["access_key", "secret_key"]


def test_project_id_can_be_recovered_from_the_function_urn():
    class _UrnContext(FakeContext):
        def getSecurityAccessKey(self):
            return PLACEHOLDER_ACCESS_KEY

        def getSecuritySecretKey(self):
            return PLACEHOLDER_SECRET_KEY

        def getProjectId(self):
            return None

        function_urn = "urn:fss:cn-north-4:project-from-urn:function:standard:vpc:latest"

    credentials = resolve_credentials(_UrnContext(), environ={})
    assert credentials.project_id == "project-from-urn"


def test_credentials_repr_never_leaks_the_secret():
    credentials = Credentials(PLACEHOLDER_ACCESS_KEY, "super-secret-value",
                              "token-value", PLACEHOLDER_PROJECT_ID, "agency-context")
    rendered = repr(credentials)
    assert "super-secret-value" not in rendered
    assert "token-value" not in rendered
    assert credentials.redacted()["secret_key"] == "<masked>"
