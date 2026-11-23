"""分区归属投递治理测试。

完整复刻业务场景：批次 B4 三行初始归属（W1 两行、W2 一行），西区行已投递给
W1 且进入签收环节时改归 W2，验证：
- 归属只按可追查的批准记录生效，无权限目标不能消费明细；
- 在途旧子批随改派撤回，W1 迟到签收被拒；
- 更正重送给 W2，旧收据仍可回查；
- 确认/重放入仓按 仓库+分区+明细键 幂等，不重复入仓；
- 兼容历史已确认收讫；并发/重复确认不会让任何目标领到两份有效数据；
- 归属、子批、收据、入仓明细、数据投递清单可互相核对。
"""
from __future__ import annotations

import pytest

from app.services.partition_ownership import (
    AssignmentConflictError, OwnershipError, PartitionOwnershipService,
)
from app.store import store


BATCH = "B4"
W1 = "warehouse-W1"
W2 = "warehouse-W2"
EAST, SOUTH, WEST = "东区", "南区", "西区"
APPROVAL_V1 = "APPR-B4-V1"
APPROVAL_WEST_V2 = "APPR-B4-WEST-V2"

svc = PartitionOwnershipService()

_GOVERNED_TABLES = (
    "partition_ownership", "delivery_sub_batch",
    "delivery_receipt", "warehouse_intake", "data_delivery",
)


@pytest.fixture(autouse=True)
def _isolate_governed_tables():
    """每个用例前清空治理相关表，避免批次 B4 的数据在用例间串扰。"""
    snapshot = {name: list(store.rows(name)) for name in _GOVERNED_TABLES}
    for name in _GOVERNED_TABLES:
        store.rows(name).clear()
    yield
    for name, rows in snapshot.items():
        store.rows(name).clear()
        store.rows(name).extend(rows)


def _own_rows(batch_id: str = BATCH) -> list[dict]:
    return [r for r in store.rows("partition_ownership") if r.get("采集批次") == batch_id]


def _sub_rows(batch_id: str = BATCH) -> list[dict]:
    return [r for r in store.rows("delivery_sub_batch") if r.get("采集批次") == batch_id]


def _receipt_rows(batch_id: str = BATCH) -> list[dict]:
    return [r for r in store.rows("delivery_receipt") if r.get("采集批次") == batch_id]


def _intake_rows(batch_id: str = BATCH) -> list[dict]:
    return [r for r in store.rows("warehouse_intake") if r.get("采集批次") == batch_id]


def _register_initial_ownership() -> None:
    """三行初始归属：W1 拿东区、西区两行，W2 拿南区一行。"""
    svc.approve_ownership(BATCH, EAST, W1, APPROVAL_V1)
    svc.approve_ownership(BATCH, WEST, W1, APPROVAL_V1)
    svc.approve_ownership(BATCH, SOUTH, W2, APPROVAL_V1)


def test_initial_ownership_shape_is_w1_two_w2_one() -> None:
    _register_initial_ownership()

    active = [r for r in _own_rows() if r["生效状态"] == "生效中"]
    by_warehouse: dict[str, list[str]] = {}
    for row in active:
        by_warehouse.setdefault(row["归属仓库"], []).append(row["分区编码"])

    assert sorted(by_warehouse[W1]) == [EAST, WEST], "W1 初始应持有东区、西区两行"
    assert by_warehouse[W2] == [SOUTH], "W2 初始应持有南区一行"
    assert {r["版本号"] for r in active} == {1}, "初始归属均为 v1"
    assert all(r["批准单号"] == APPROVAL_V1 for r in active), "归属必须挂可追查批准单"


def test_dispatch_and_delivery_list_sync() -> None:
    _register_initial_ownership()

    east = svc.dispatch_sub_batch(BATCH, EAST, ["e1", "e2"])
    assert east.warehouse == W1 and east.version == 1 and east.status == "待签收"

    # 归属确认结果同步到数据投递清单
    delivery = next(
        r for r in store.rows("data_delivery")
        if r.get("关联子批") == east.sub_batch_id
    )
    assert delivery["目标系统"] == W1
    assert delivery["投递状态"] == "投递中"
    assert EAST in delivery["分区策略"]

    # 重复编排同一归属版本幂等，不产生第二份子批
    again = svc.dispatch_sub_batch(BATCH, EAST, ["e1", "e2"])
    assert again.sub_batch_id == east.sub_batch_id
    assert len([s for s in _sub_rows() if s["分区编码"] == EAST]) == 1


def test_inflight_reassignment_blocks_w1_and_routes_correction_to_w2() -> None:
    """题面主场景：西区子批已发 W1 且进入签收环节，业务改归 W2。"""
    _register_initial_ownership()
    svc.dispatch_sub_batch(BATCH, EAST, ["e1"])
    west_v1 = svc.dispatch_sub_batch(BATCH, WEST, ["w1", "w2", "w3"])
    svc.dispatch_sub_batch(BATCH, SOUTH, ["s1"])

    # W1 对西区完成签收（进入签收环节，尚未确认收讫）
    receipt_w1 = svc.sign_for(west_v1.sub_batch_id, W1, note="西区首批送达")
    assert receipt_w1.status == "已签收"

    # 业务提交西区改归 W2：新批准记录生效，W1 旧版本被替换、在途子批撤回
    new_own = svc.approve_ownership(BATCH, WEST, W2, APPROVAL_WEST_V2)
    assert new_own.warehouse == W2 and new_own.version == 2
    assert new_own.supersedes == f"OWN-{BATCH}-{WEST}"

    west_subs = [s for s in _sub_rows() if s["分区编码"] == WEST]
    v1_sub = next(s for s in west_subs if s["版本号"] == 1)
    assert v1_sub["子批状态"] == "已撤回", "改派后 W1 在途子批必须撤回"

    # 改派后 W1 不再持有西区接收资格，迟到签收/确认都被拒绝
    with pytest.raises(OwnershipError):
        svc.sign_for(west_v1.sub_batch_id, W1)
    with pytest.raises(OwnershipError):
        svc.confirm_receipt(receipt_w1.receipt_id, W1)

    # W2 同样不能拿 W1 的旧收据确认（禁止未获权限目标消费明细）
    with pytest.raises(OwnershipError):
        svc.confirm_receipt(receipt_w1.receipt_id, W2)

    # 旧收据仍可回查，只是不可再确认入仓
    archived = next(r for r in _receipt_rows() if r["收据编号"] == receipt_w1.receipt_id)
    assert archived["收据状态"] == "已签收"

    # 按最新归属重送更正给 W2
    correction = svc.replay_correction(BATCH, WEST)
    assert correction.warehouse == W2 and correction.version == 2
    assert correction.is_correction and correction.status == "待签收"
    assert correction.record_keys == ["w1", "w2", "w3"], "更正应携带原分区明细"

    # 更正同步到投递清单，标记为更正且目标为 W2
    correction_delivery = next(
        r for r in store.rows("data_delivery")
        if r.get("关联子批") == correction.sub_batch_id
    )
    assert correction_delivery["目标系统"] == W2
    assert correction_delivery["更正标记"] == "是"

    # W2 完成签收并确认收讫
    rcp_w2 = svc.sign_for(correction.sub_batch_id, W2)
    receipt_w2, intakes = svc.confirm_receipt(rcp_w2.receipt_id, W2)
    assert receipt_w2.status == "已确认"
    assert {i.record_key for i in intakes} == {"w1", "w2", "w3"}

    # W1 对西区没有任何入仓明细；W2 拿到且仅拿到一份西区 v2 明细
    w1_west_intake = [
        r for r in _intake_rows()
        if r["归属仓库"] == W1 and r["分区编码"] == WEST
    ]
    assert w1_west_intake == [], "W1 未获权限，不能消费西区分区明细"
    w2_west_intake = [
        r for r in _intake_rows()
        if r["归属仓库"] == W2 and r["分区编码"] == WEST
    ]
    assert len(w2_west_intake) == 3
    assert {r["版本号"] for r in w2_west_intake} == {2}
    assert {r["收据编号"] for r in w2_west_intake} == {receipt_w2.receipt_id}


def test_replay_and_duplicate_confirm_are_idempotent() -> None:
    """重放更正与并发/重复确认都不会重复入仓。"""
    _register_initial_ownership()
    sub = svc.dispatch_sub_batch(BATCH, SOUTH, ["s1", "s2"])
    receipt = svc.sign_for(sub.sub_batch_id, W2)

    _, first_intakes = svc.confirm_receipt(receipt.receipt_id, W2)
    assert len(first_intakes) == 2

    # 模拟并发/重放的第二次确认：无新增入仓，明细不重复
    _, second_intakes = svc.confirm_receipt(receipt.receipt_id, W2)
    assert second_intakes == []
    south_intake = [
        r for r in _intake_rows()
        if r["归属仓库"] == W2 and r["分区编码"] == SOUTH
    ]
    assert sorted(r["明细键"] for r in south_intake) == ["s1", "s2"]

    # 更正重送后再确认，同一仓库+分区+明细键仍只保留一份
    svc.approve_ownership(BATCH, SOUTH, W2, "APPR-B4-SOUTH-V2")
    correction = svc.replay_correction(BATCH, SOUTH)
    rcp = svc.sign_for(correction.sub_batch_id, W2)
    _, corrected = svc.confirm_receipt(rcp.receipt_id, W2)
    assert corrected == [], "相同明细重放更正不能重复入仓"
    assert len([
        r for r in _intake_rows()
        if r["归属仓库"] == W2 and r["分区编码"] == SOUTH
    ]) == 2


def test_historical_confirmed_receipt_remains_compatible_after_reassignment() -> None:
    """W1 已确认收讫后才改派：历史收讫与明细保留，旧收据可回查，不重复入仓。"""
    batch = "B4-HIST"
    svc.approve_ownership(batch, WEST, W1, "APPR-HIST-V1")
    sub = svc.dispatch_sub_batch(batch, WEST, ["h1", "h2"])
    receipt = svc.sign_for(sub.sub_batch_id, W1)
    confirmed, intakes = svc.confirm_receipt(receipt.receipt_id, W1)
    assert confirmed.status == "已确认" and len(intakes) == 2

    # 改派 W2
    svc.approve_ownership(batch, WEST, W2, "APPR-HIST-V2")

    # 历史确认与 W1 已入仓明细继续保留、可回查
    kept_intake = [
        r for r in store.rows("warehouse_intake")
        if r["采集批次"] == batch and r["归属仓库"] == W1
    ]
    assert sorted(r["明细键"] for r in kept_intake) == ["h1", "h2"]
    kept_receipt = next(
        r for r in store.rows("delivery_receipt")
        if r["采集批次"] == batch and r["收据编号"] == receipt.receipt_id
    )
    assert kept_receipt["收据状态"] == "已确认"

    # W2 走更正链路拿到自己的有效投递；W1 不会因重放再入仓一次
    correction = svc.replay_correction(batch, WEST)
    rcp = svc.sign_for(correction.sub_batch_id, W2)
    _, w2_intakes = svc.confirm_receipt(rcp.receipt_id, W2)
    assert {i.record_key for i in w2_intakes} == {"h1", "h2"}

    # 两个目标各自只有一份有效入仓明细，没有任何目标领到两份
    w1_count = len([
        r for r in store.rows("warehouse_intake")
        if r["采集批次"] == batch and r["归属仓库"] == W1
    ])
    w2_count = len([
        r for r in store.rows("warehouse_intake")
        if r["采集批次"] == batch and r["归属仓库"] == W2
    ])
    assert (w1_count, w2_count) == (2, 2)


def test_ownership_change_without_approval_is_rejected() -> None:
    """无批准记录的归属变更禁止生效，新旧资格冲突不得落库。"""
    batch = "B4-NOAPPR"
    svc.approve_ownership(batch, EAST, W1, "APPR-NOAPPR-V1")
    before = len([r for r in _own_rows(batch)])

    with pytest.raises(AssignmentConflictError):
        svc.approve_ownership(batch, EAST, W2, "")

    active = next(
        r for r in store.rows("partition_ownership")
        if r["采集批次"] == batch and r["生效状态"] == "生效中"
    )
    assert active["归属仓库"] == W1, "无批准单时原归属必须保持有效"
    assert len([r for r in _own_rows(batch)]) == before


def test_unauthorized_warehouse_cannot_sign() -> None:
    """未获权限的目标不能签收到任何子批。"""
    batch = "B4-AUTH"
    svc.approve_ownership(batch, EAST, W1, "APPR-AUTH-V1")
    sub = svc.dispatch_sub_batch(batch, EAST, ["a1"])

    with pytest.raises(OwnershipError):
        svc.sign_for(sub.sub_batch_id, W2)

    # 没有任何归属时编排子批也必须拒绝
    with pytest.raises(OwnershipError):
        svc.dispatch_sub_batch(batch, "北区", ["n1"])


def test_reconcile_cross_checks_partition_version_receipt_and_intake() -> None:
    """端到端对账：实际接收的分区、版本、收据、入仓明细互相核对一致。"""
    _register_initial_ownership()
    east = svc.dispatch_sub_batch(BATCH, EAST, ["e1", "e2"])
    south = svc.dispatch_sub_batch(BATCH, SOUTH, ["s1"])
    west = svc.dispatch_sub_batch(BATCH, WEST, ["w1", "w2"])

    r_east = svc.sign_for(east.sub_batch_id, W1)
    svc.confirm_receipt(r_east.receipt_id, W1)
    r_south = svc.sign_for(south.sub_batch_id, W2)
    svc.confirm_receipt(r_south.receipt_id, W2)
    svc.sign_for(west.sub_batch_id, W1)

    # 西区改派 + 更正到 W2
    svc.approve_ownership(BATCH, WEST, W2, APPROVAL_WEST_V2)
    correction = svc.replay_correction(BATCH, WEST)
    r_west = svc.sign_for(correction.sub_batch_id, W2)
    svc.confirm_receipt(r_west.receipt_id, W2)

    report = svc.reconcile_batch(BATCH)
    assert report["consistent"], f"对账应一致，差异: {report['inconsistencies']}"

    # 每个目标实际入仓的分区/版本/明细
    per_warehouse = {item["warehouse"]: item for item in report["warehouse_intake"]}
    assert set(per_warehouse[W1]["partitions"][EAST]["record_keys"]) == {"e1", "e2"}
    assert per_warehouse[W1]["partitions"][EAST]["version"] == 1
    assert set(per_warehouse[W2]["partitions"][SOUTH]["record_keys"]) == {"s1"}
    assert set(per_warehouse[W2]["partitions"][WEST]["record_keys"]) == {"w1", "w2"}
    assert per_warehouse[W2]["partitions"][WEST]["version"] == 2

    # W1 名下不含西区入仓；全批次没有重复入仓
    assert WEST not in per_warehouse.get(W1, {}).get("partitions", {})
    identities = [
        (i["warehouse"], i["partition"], i["record_key"]) for i in report["intakes"]
    ]
    assert len(identities) == len(set(identities))

    # 投递清单中每个子批都有对应行，撤回/更正状态可区分
    tracked = {
        r["关联子批"]: r
        for r in store.rows("data_delivery") if r.get("关联子批")
    }
    for sub in report["sub_batches"]:
        row = tracked[sub["sub_batch_id"]]
        assert row["目标系统"] == sub["warehouse"]
        assert f'v{sub["version"]}' in row["数据来源"]
    assert tracked[west.sub_batch_id]["投递状态"] == "投递失败"  # 已撤回
    assert tracked[correction.sub_batch_id]["更正标记"] == "是"
