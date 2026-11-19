"""VPC 级分布式锁：取锁/放锁的判定口径与重复触发护栏。

锁是「同一 VPC 同时只有一次扩展」的唯一保证，因此这里既断言条件写的报文，
也断言「锁是否过期」只按写入的绝对时间戳判定，不依赖状态存储自身的过期能力。
"""

import pytest
from expansion_lock import STATUS_LOCKED, STATUS_RELEASED, ExpansionLock
from handler_errors import LockUnavailable, StateStoreError
from helpers import VPC_ID, FakeStore

TABLE = "demo-locks"
NOW = 1760000000
HOLDER = "exec-demo-0001"


def lock(store=None, holder=HOLDER, ttl=900, vpc_id=VPC_ID):
    return ExpansionLock(store or FakeStore(tables=[TABLE]), TABLE, vpc_id, holder,
                         ttl_seconds=ttl)


def locked_row(holder="someone-else", ttl=NOW + 300, status=STATUS_LOCKED):
    return FakeStore(rows={TABLE: {VPC_ID: {
        "vpc_id": VPC_ID, "lock_holder": holder, "status": status, "lock_ttl": ttl}}})


# ----------------------------------------------------------------------
# 只读判定
# ----------------------------------------------------------------------
def test_absent_record_is_not_locked():
    result = lock().check(NOW)
    assert result == {"is_locked": False, "lock_holder": None, "reason": "no_record"}


def test_released_record_is_not_locked():
    result = lock(locked_row(status=STATUS_RELEASED)).check(NOW)
    assert result["is_locked"] is False
    assert result["reason"] == "status_%s" % STATUS_RELEASED


def test_a_status_that_is_not_the_locked_token_is_treated_as_free():
    result = lock(locked_row(status="")).check(NOW)
    assert result["is_locked"] is False and result["reason"] == "status_unknown"


def test_held_lock_reports_the_absolute_expiry_it_read():
    result = lock(locked_row(ttl=NOW + 60)).check(NOW)
    assert result["is_locked"] is True
    assert result["lock_holder"] == "someone-else"
    assert result["reason"] == "held_until_%d" % (NOW + 60)


@pytest.mark.parametrize("ttl", [NOW, NOW - 1, "1760000000", None, "", "not-a-number"])
def test_expiry_is_judged_by_explicit_comparison_not_by_the_store(ttl):
    """TTL 到点即视为未占用；接口侧的过期能力不可依赖。"""
    result = lock(locked_row(ttl=ttl)).check(NOW)
    assert result["is_locked"] is False
    assert result["reason"] == "expired"


# ----------------------------------------------------------------------
# 取锁
# ----------------------------------------------------------------------
def test_acquire_writes_an_absolute_expiry_under_a_condition():
    store = FakeStore(tables=[TABLE])
    assert lock(store).acquire(NOW) is True
    put = store.puts[-1]
    assert put["table"] == TABLE
    assert put["item"]["vpc_id"] == VPC_ID
    assert put["item"]["status"] == STATUS_LOCKED
    assert put["item"]["lock_holder"] == HOLDER
    # 绝对时间戳而不是「写入后 900 秒」：读侧才能自己判定过期
    assert put["item"]["lock_ttl"] == NOW + 900
    assert put["item"]["locked_at"].endswith("Z")
    assert put["condition"] == \
        "attribute_not_exists(vpc_id) OR #status <> :locked OR #ttl < :now"
    assert put["values"] == {":locked": STATUS_LOCKED, ":now": NOW}


def test_acquire_refuses_a_lock_held_by_another_execution():
    store = locked_row(holder="someone-else", ttl=NOW + 60)
    with pytest.raises(LockUnavailable) as error:
        lock(store).acquire(NOW)
    assert error.value.fields["vpc_id"] == VPC_ID
    assert error.value.fields["lock_holder"] == "someone-else"
    assert error.value.retryable is False
    # 没拿到锁就一次写都不发
    assert store.puts == []


def test_acquire_allows_the_same_execution_to_take_the_lock_again():
    """同一执行编号重投：上一轮没放锁也允许继续，避免把自己锁死。"""
    store = locked_row(holder=HOLDER, ttl=NOW + 60)
    assert lock(store).acquire(NOW) is True
    assert len(store.puts) == 1


def test_expired_lock_is_taken_over_by_the_new_execution():
    store = locked_row(holder="someone-else", ttl=NOW - 1)
    assert lock(store, holder="exec-2").acquire(NOW) is True
    assert store.rows[TABLE][VPC_ID]["lock_holder"] == "exec-2"
    assert store.rows[TABLE][VPC_ID]["lock_ttl"] == NOW + 900


def test_a_racing_write_loses_the_condition_and_becomes_lock_unavailable():
    """条件未命中说明有人抢先：这是「让下一个投递周期」而不是链路故障。"""
    class RacingStore(FakeStore):
        def put_item(self, table_name, item, **kwargs):
            from state_store import ConditionNotMet

            self.calls.append(("put_item", table_name))
            raise ConditionNotMet("条件写未命中", table=table_name)

    store = RacingStore(tables=[TABLE])
    with pytest.raises(LockUnavailable) as error:
        lock(store).acquire(NOW)
    assert "抢先" in error.value.message
    assert error.value.fields["lock_holder"] is None


def test_ttl_is_coerced_to_an_integer():
    store = FakeStore(tables=[TABLE])
    lock(store, ttl="1200").acquire(NOW)
    assert store.puts[-1]["item"]["lock_ttl"] == NOW + 1200


# ----------------------------------------------------------------------
# 放锁
# ----------------------------------------------------------------------
def test_release_without_acquire_touches_nothing():
    store = FakeStore(tables=[TABLE])
    assert lock(store).release() == {"released": False, "reason": "not_acquired"}
    assert store.updates == []


def test_release_marks_the_row_released_under_a_holder_condition():
    store = FakeStore(tables=[TABLE])
    subject = lock(store)
    subject.acquire(NOW)
    assert subject.release() == {"released": True, "reason": "released"}
    update = store.updates[-1]
    assert update["table"] == TABLE
    assert update["expression"] == "SET #status = :released, released_at = :now"
    # 只有持有者放得动这把锁：误放别人的锁会把两次执行搅在一起
    assert update["condition"] == "lock_holder = :holder"
    assert update["values"][":released"] == STATUS_RELEASED
    assert update["values"][":holder"] == HOLDER
    assert store.rows[TABLE][VPC_ID]["status"] == STATUS_RELEASED
    assert subject.acquired is False


def test_release_by_a_different_holder_is_reported_not_raised():
    store = locked_row(holder=HOLDER, ttl=NOW + 60)
    store.rows[TABLE][VPC_ID]["lock_holder"] = "someone-else"
    subject = lock(store)
    subject.acquired = True
    assert subject.release() == {"released": False, "reason": "holder_mismatch"}


def test_an_unreachable_store_does_not_turn_a_finished_expansion_into_a_failure():
    store = FakeStore(tables=[TABLE], raise_on={"update_item": StateStoreError("存储不可达")})
    subject = lock(store)
    subject.acquired = True
    result = subject.release()
    assert result["released"] is False
    assert result["reason"] == "state_store_error"
    assert result["detail"]


def test_a_released_lock_can_be_acquired_again():
    """一次完整执行之后，下一次投递能正常取锁：重复触发不会永久卡死。"""
    store = FakeStore(tables=[TABLE])
    subject = lock(store)
    subject.acquire(NOW)
    subject.release()
    assert lock(store, holder="exec-2").acquire(NOW + 10) is True
    assert len(store.puts) == 2
