"""分区归属领域服务。

围绕「同一采集批次的不同分区由不同仓库负责」这一场景，落地四条硬规则：

1. 归属判定只按可追查的批准记录生效（无批准单不得登记归属），同一分区改派
   时旧版本置「已替换」、新版本递增，整条归属链可回查；
2. 只有当前对分区持有生效归属、或持有该分区在途有效子批的仓库才能签收/确认，
   未获权限的目标一律拒绝消费明细；
3. 实际入仓明细按 (仓库, 批次, 分区, 记录键) 幂等，确认与重放更正都不会
   重复入仓；
4. 子批、更正同步写入数据投递清单，目标实际接收的分区、版本、收据、入仓
   明细可互相核对（reconcile_batch）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.models import (
    DeliveryReceipt, DeliverySubBatch, PartitionOwnership, WarehouseIntake,
)
from app.services.base import BaseModuleService
from app.store import store


class OwnershipError(Exception):
    """归属/接收权限类业务冲突，路由层转成 409。"""


class AssignmentConflictError(OwnershipError):
    """新旧目标对同一分区的接收资格冲突，且不存在可依据的批准记录。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PartitionOwnershipService(BaseModuleService):
    """通用资源骨架 + 分区投递治理引擎。"""

    MODULE = "partition_ownership"
    ENTITY = "分区归属"
    OPERATION_TERM = "归属确认"
    LIST_FIELDS = [
        "归属编号", "采集批次", "分区编码", "归属仓库", "版本号",
        "批准单号", "生效状态", "生效时间", "替换归属",
    ]
    REQUIRED_FIELDS = ["采集批次", "分区编码", "归属仓库", "批准单号"]
    STATUS_ORDER = ["生效中", "已替换"]
    ACTION_RULES = {"确认归属": "生效中", "标记替换": "已替换"}
    NEGATIVE_ACTIONS = []

    # 运行态表名
    T_SUB = "delivery_sub_batch"
    T_RECEIPT = "delivery_receipt"
    T_INTAKE = "warehouse_intake"
    T_DELIVERY = "data_delivery"

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    def _seq(self) -> int:
        all_rows = (
            store.rows(self.MODULE)
            + store.rows(self.T_SUB)
            + store.rows(self.T_RECEIPT)
            + store.rows(self.T_INTAKE)
        )
        return max((int(r.get("id", 0)) for r in all_rows), default=0) + 1

    @staticmethod
    def _decode_keys(raw: Any) -> list[str]:
        if isinstance(raw, list):
            return [str(k) for k in raw]
        return [part.strip() for part in str(raw or "").split(",") if part.strip()]

    @staticmethod
    def _find_active(batch_id: str, partition: str) -> dict[str, Any] | None:
        for row in store.rows("partition_ownership"):
            if (row.get("采集批次") == batch_id
                    and row.get("分区编码") == partition
                    and row.get("生效状态") == "生效中"):
                return row
        return None

    def _find_sub_batch(self, sub_batch_id: str) -> dict[str, Any] | None:
        for row in store.rows(self.T_SUB):
            if row.get("子批编号") == sub_batch_id:
                return row
        return None

    def _find_receipt(self, receipt_id: str) -> dict[str, Any] | None:
        for row in store.rows(self.T_RECEIPT):
            if row.get("收据编号") == receipt_id:
                return row
        return None

    def _can_receive(self, warehouse: str, batch_id: str, partition: str) -> bool:
        """接收资格：当前生效归属，或持有该分区在途（未撤回/未驳回）子批。"""
        active = self._find_active(batch_id, partition)
        if active is not None and active.get("归属仓库") == warehouse:
            return True
        for row in store.rows(self.T_SUB):
            if (row.get("采集批次") == batch_id
                    and row.get("分区编码") == partition
                    and row.get("归属仓库") == warehouse
                    and row.get("子批状态") in ("待签收", "已签收")):
                return True
        return False

    def _sync_delivery_list(self, sub: dict[str, Any]) -> None:
        """子批/更正的归属确认结果同步到数据投递清单（按子批编号幂等 upsert）。"""
        sub_status = str(sub.get("子批状态", "待签收"))
        delivery_status = {
            "待签收": "投递中",
            "已签收": "投递中",
            "已确认": "已完成",
            "已撤回": "投递失败",
            "已驳回": "投递失败",
        }.get(sub_status, "投递中")
        values = {
            "任务编号": sub["子批编号"],
            "数据来源": f'{sub["采集批次"]}:{sub["分区编码"]}:v{sub["版本号"]}',
            "目标系统": sub["归属仓库"],
            "投递格式": "JSON",
            "投递频率": "on_partition_ownership",
            "分区策略": f'by_partition/{sub["分区编码"]}',
            "最近投递": _now(),
            "投递状态": delivery_status,
            "关联子批": sub["子批编号"],
            "关联批准单": sub.get("批准单号", ""),
            "更正标记": "是" if sub.get("更正标记") == "是" else "否",
        }
        rows = store.rows(self.T_DELIVERY)
        for row in rows:
            if row.get("任务编号") == sub["子批编号"]:
                row.update(values)
                row["status"] = delivery_status
                row["abnormal"] = delivery_status == "投递失败"
                return
        rows.append({
            "id": self._seq(),
            **values,
            "status": delivery_status,
            "pending": delivery_status != "已完成",
            "abnormal": delivery_status == "投递失败",
        })

    # ------------------------------------------------------------------
    # 1. 归属确认：只按可追查的批准记录生效
    # ------------------------------------------------------------------
    def approve_ownership(
        self, batch_id: str, partition: str, warehouse: str, approval_id: str,
    ) -> PartitionOwnership:
        if not str(approval_id or "").strip():
            raise AssignmentConflictError(
                f"分区 {partition} 改派仓库 {warehouse} 缺少可追查的批准记录，禁止生效"
            )

        previous = self._find_active(batch_id, partition)
        version = 1
        supersedes = ""
        if previous is not None:
            if previous.get("归属仓库") == warehouse:
                # 同目标重复确认：幂等返回当前生效版本
                return PartitionOwnership.from_seed_row(previous)
            version = int(previous.get("版本号", 1)) + 1
            supersedes = str(previous.get("归属编号", ""))
            previous["生效状态"] = "已替换"
            previous["status"] = "已替换"
            # 在途（待签收或已签收但未确认收讫）的旧目标子批立即撤回，
            # 杜绝新旧目标同时持有接收资格；已确认的历史收讫保留兼容。
            for sub in store.rows(self.T_SUB):
                if (sub.get("采集批次") == batch_id
                        and sub.get("分区编码") == partition
                        and sub.get("子批状态") in ("待签收", "已签收")
                        and sub.get("归属仓库") != warehouse):
                    sub["子批状态"] = "已撤回"
                    sub["status"] = "已撤回"
                    self._sync_delivery_list(sub)

        ownership_id = (
            f"OWN-{batch_id}-{partition}-v{version}"
            if previous is not None else f"OWN-{batch_id}-{partition}"
        )
        row = {
            "id": self._seq(),
            "归属编号": ownership_id,
            "采集批次": batch_id,
            "分区编码": partition,
            "归属仓库": warehouse,
            "版本号": version,
            "批准单号": approval_id,
            "生效状态": "生效中",
            "生效时间": _now(),
            "替换归属": supersedes,
            "status": "生效中",
            "pending": False,
            "abnormal": False,
        }
        store.rows(self.MODULE).append(row)
        return PartitionOwnership.from_seed_row(row)

    # ------------------------------------------------------------------
    # 2. 投递按分区归属清单编排目标子批
    # ------------------------------------------------------------------
    def dispatch_sub_batch(
        self, batch_id: str, partition: str, record_keys: list[str],
    ) -> DeliverySubBatch:
        active = self._find_active(batch_id, partition)
        if active is None:
            raise OwnershipError(
                f"批次 {batch_id} 分区 {partition} 尚无生效归属，无法编排投递子批"
            )
        warehouse = str(active["归属仓库"])
        version = int(active["版本号"])

        # 同一归属版本已投过则幂等返回，避免重复子批
        for sub in store.rows(self.T_SUB):
            if (sub.get("采集批次") == batch_id
                    and sub.get("分区编码") == partition
                    and int(sub.get("版本号", 0)) == version
                    and sub.get("子批状态") != "已撤回"):
                return self._to_sub_batch(sub)

        sub_batch_id = f"SUB-{batch_id}-{partition}-v{version}"
        row = {
            "id": self._seq(),
            "子批编号": sub_batch_id,
            "采集批次": batch_id,
            "分区编码": partition,
            "归属仓库": warehouse,
            "版本号": version,
            "批准单号": active.get("批准单号", ""),
            "明细键": ",".join(record_keys),
            "子批状态": "待签收",
            "收据编号": "",
            "更正标记": "否",
            "创建时间": _now(),
            "status": "待签收",
            "pending": True,
            "abnormal": False,
        }
        store.rows(self.T_SUB).append(row)
        self._sync_delivery_list(row)
        return self._to_sub_batch(row)

    # ------------------------------------------------------------------
    # 3. 签收：未获权限的目标不能签收
    # ------------------------------------------------------------------
    def sign_for(self, sub_batch_id: str, warehouse: str, note: str = "") -> DeliveryReceipt:
        sub = self._find_sub_batch(sub_batch_id)
        if sub is None:
            raise OwnershipError(f"子批 {sub_batch_id} 不存在")
        if sub.get("子批状态") == "已撤回":
            raise OwnershipError(
                f"子批 {sub_batch_id} 已随归属改派撤回，仓库 {warehouse} 的签收被拒绝"
            )
        batch_id = str(sub["采集批次"])
        partition = str(sub["分区编码"])
        if not self._can_receive(warehouse, batch_id, partition):
            raise OwnershipError(
                f"仓库 {warehouse} 未持有分区 {partition} 的接收权限，禁止签收"
            )
        # 重复签收幂等：返回既有收据
        existing_id = sub.get("收据编号")
        if existing_id:
            existing = self._find_receipt(str(existing_id))
            if existing is not None:
                return DeliveryReceipt(
                    receipt_id=str(existing["收据编号"]),
                    sub_batch_id=str(existing["子批编号"]),
                    batch_id=str(existing["采集批次"]),
                    partition=str(existing["分区编码"]),
                    warehouse=str(existing["归属仓库"]),
                    version=int(existing["版本号"]),
                    status=str(existing["收据状态"]),
                    recorded_at=str(existing["签收时间"]),
                    note=str(existing.get("备注", "")),
                )

        receipt_id = f"RCP-{sub_batch_id}"
        row = {
            "id": self._seq(),
            "收据编号": receipt_id,
            "子批编号": sub_batch_id,
            "采集批次": batch_id,
            "分区编码": partition,
            "归属仓库": warehouse,
            "版本号": int(sub["版本号"]),
            "收据状态": "已签收",
            "签收时间": _now(),
            "备注": note,
            "status": "已签收",
            "pending": True,
            "abnormal": False,
        }
        store.rows(self.T_RECEIPT).append(row)
        sub["子批状态"] = "已签收"
        sub["status"] = "已签收"
        sub["收据编号"] = receipt_id
        self._sync_delivery_list(sub)
        return DeliveryReceipt(
            receipt_id=receipt_id, sub_batch_id=sub_batch_id, batch_id=batch_id,
            partition=partition, warehouse=warehouse, version=int(sub["版本号"]),
            status="已签收", recorded_at=row["签收时间"], note=note,
        )

    # ------------------------------------------------------------------
    # 4. 确认收讫并消费明细：入仓幂等，重放不重复
    # ------------------------------------------------------------------
    def confirm_receipt(
        self, receipt_id: str, warehouse: str,
    ) -> tuple[DeliveryReceipt, list[WarehouseIntake]]:
        receipt_row = self._find_receipt(receipt_id)
        if receipt_row is None:
            raise OwnershipError(f"收据 {receipt_id} 不存在")
        if receipt_row.get("归属仓库") != warehouse:
            raise OwnershipError(
                f"收据 {receipt_id} 不属于仓库 {warehouse}，禁止确认消费"
            )
        sub = self._find_sub_batch(str(receipt_row["子批编号"]))
        if sub is None:
            raise OwnershipError(f"收据 {receipt_id} 对应的子批不存在")
        if sub.get("子批状态") == "已撤回":
            raise OwnershipError(
                f"子批 {sub['子批编号']} 已撤回，旧收据 {receipt_id} 仅留档不可确认入仓"
            )
        batch_id = str(sub["采集批次"])
        partition = str(sub["分区编码"])
        if not self._can_receive(warehouse, batch_id, partition):
            raise OwnershipError(
                f"仓库 {warehouse} 已失去分区 {partition} 的接收权限，禁止确认消费明细"
            )

        existing_keys = {
            (str(r["归属仓库"]), str(r["采集批次"]),
             str(r["分区编码"]), str(r["明细键"]))
            for r in store.rows(self.T_INTAKE)
        }
        version = int(sub["版本号"])
        record_keys = self._decode_keys(sub.get("明细键"))
        new_intakes: list[WarehouseIntake] = []
        for key in record_keys:
            identity = (warehouse, batch_id, partition, key)
            if identity in existing_keys:
                # 重放/重复确认：同一明细只入仓一次
                continue
            intake_id = f"INT-{batch_id}-{partition}-{key}"
            row = {
                "id": self._seq(),
                "入仓编号": intake_id,
                "归属仓库": warehouse,
                "采集批次": batch_id,
                "分区编码": partition,
                "明细键": key,
                "版本号": version,
                "收据编号": receipt_id,
                "子批编号": sub["子批编号"],
                "入仓时间": _now(),
                "status": "已入仓",
                "pending": False,
                "abnormal": False,
            }
            store.rows(self.T_INTAKE).append(row)
            existing_keys.add(identity)
            new_intakes.append(WarehouseIntake(
                intake_id=intake_id, warehouse=warehouse, batch_id=batch_id,
                partition=partition, record_key=key, version=version,
                receipt_id=receipt_id, sub_batch_id=str(sub["子批编号"]),
                intake_at=row["入仓时间"],
            ))

        receipt_row["收据状态"] = "已确认"
        receipt_row["status"] = "已确认"
        receipt_row["pending"] = False
        sub["子批状态"] = "已确认"
        sub["status"] = "已确认"
        self._sync_delivery_list(sub)
        return DeliveryReceipt(
            receipt_id=receipt_id,
            sub_batch_id=str(receipt_row["子批编号"]),
            batch_id=str(receipt_row["采集批次"]),
            partition=str(receipt_row["分区编码"]),
            warehouse=warehouse,
            version=int(receipt_row["版本号"]),
            status="已确认",
            recorded_at=str(receipt_row["签收时间"]),
            note=str(receipt_row.get("备注", "")),
        ), new_intakes

    # ------------------------------------------------------------------
    # 5. 更正重送：按最新生效归属给新目标补发，旧收据留档可回查
    # ------------------------------------------------------------------
    def replay_correction(
        self, batch_id: str, partition: str,
    ) -> DeliverySubBatch:
        active = self._find_active(batch_id, partition)
        if active is None:
            raise OwnershipError(
                f"批次 {batch_id} 分区 {partition} 无生效归属，无法重送更正"
            )
        related = [
            s for s in store.rows(self.T_SUB)
            if s.get("采集批次") == batch_id and s.get("分区编码") == partition
        ]
        if not related:
            raise OwnershipError(
                f"分区 {partition} 没有历史子批，缺少更正所需明细"
            )
        latest = max(related, key=lambda s: int(s.get("id", 0)))
        record_keys = self._decode_keys(latest.get("明细键"))
        retry = sum(1 for s in related) + 1
        sub_batch_id = (
            f"SUB-{batch_id}-{partition}-v{active['版本号']}-R{retry}"
        )
        row = {
            "id": self._seq(),
            "子批编号": sub_batch_id,
            "采集批次": batch_id,
            "分区编码": partition,
            "归属仓库": str(active["归属仓库"]),
            "版本号": int(active["版本号"]),
            "批准单号": active.get("批准单号", ""),
            "明细键": ",".join(record_keys),
            "子批状态": "待签收",
            "收据编号": "",
            "更正标记": "是",
            "创建时间": _now(),
            "status": "待签收",
            "pending": True,
            "abnormal": False,
        }
        store.rows(self.T_SUB).append(row)
        self._sync_delivery_list(row)
        return self._to_sub_batch(row)

    # ------------------------------------------------------------------
    # 6. 对账：归属 / 子批 / 收据 / 实际入仓明细互相核对
    # ------------------------------------------------------------------
    def reconcile_batch(self, batch_id: str) -> dict[str, Any]:
        ownerships = [
            PartitionOwnership.from_seed_row(r)
            for r in store.rows(self.MODULE) if r.get("采集批次") == batch_id
        ]
        subs = [
            self._to_sub_batch(r)
            for r in store.rows(self.T_SUB) if r.get("采集批次") == batch_id
        ]
        receipts = [
            self._to_receipt(r)
            for r in store.rows(self.T_RECEIPT) if r.get("采集批次") == batch_id
        ]
        intakes = [
            WarehouseIntake(
                intake_id=str(r["入仓编号"]), warehouse=str(r["归属仓库"]),
                batch_id=str(r["采集批次"]), partition=str(r["分区编码"]),
                record_key=str(r["明细键"]), version=int(r["版本号"]),
                receipt_id=str(r["收据编号"]), sub_batch_id=str(r["子批编号"]),
                intake_at=str(r.get("入仓时间", "")),
            )
            for r in store.rows(self.T_INTAKE) if r.get("采集批次") == batch_id
        ]

        sub_by_id = {s.sub_batch_id: s for s in subs}
        receipt_by_id = {r.receipt_id: r for r in receipts}
        inconsistencies: list[str] = []

        # 生效归属必须有对应有效子批；撤回/驳回子批不算有效投递
        for own in ownerships:
            if own.status != "生效中":
                continue
            valid = [
                s for s in subs
                if s.partition == own.partition and s.warehouse == own.warehouse
                and s.version == own.version and s.status not in ("已撤回", "已驳回")
            ]
            if not valid:
                inconsistencies.append(
                    f"分区 {own.partition} 生效归属 {own.warehouse} v{own.version} 缺少有效子批"
                )

        # 收据必须挂在有效子批上，且仓库/分区/版本一致
        for receipt in receipts:
            sub = sub_by_id.get(receipt.sub_batch_id)
            if sub is None:
                inconsistencies.append(f"收据 {receipt.receipt_id} 找不到对应子批")
                continue
            if sub.status in ("已撤回", "已驳回") and receipt.status == "已确认":
                inconsistencies.append(
                    f"收据 {receipt.receipt_id} 对应子批已 {sub.status}，不应存在确认入仓"
                )
            if (sub.warehouse != receipt.warehouse
                    or sub.partition != receipt.partition
                    or sub.version != receipt.version):
                inconsistencies.append(
                    f"收据 {receipt.receipt_id} 与子批 {sub.sub_batch_id} 的仓库/分区/版本不一致"
                )

        # 入仓明细必须有收据背书，且仓库/分区/版本与收据一致
        for intake in intakes:
            receipt = receipt_by_id.get(intake.receipt_id)
            if receipt is None:
                inconsistencies.append(
                    f"入仓明细 {intake.intake_id} 缺少收据 {intake.receipt_id} 背书"
                )
                continue
            if (intake.warehouse != receipt.warehouse
                    or intake.partition != receipt.partition
                    or intake.version != receipt.version):
                inconsistencies.append(
                    f"入仓明细 {intake.intake_id} 与收据 {receipt.receipt_id} 的仓库/分区/版本不一致"
                )

        # 入仓幂等核对：同一 仓库+分区+明细键 只能出现一次
        seen: set[tuple[str, str, str]] = set()
        for intake in intakes:
            identity = (intake.warehouse, intake.partition, intake.record_key)
            if identity in seen:
                inconsistencies.append(
                    f"明细 {intake.record_key} 在仓库 {intake.warehouse} 分区 {intake.partition} 重复入仓"
                )
            seen.add(identity)

        # 每个目标实际入仓明细汇总
        per_warehouse: dict[str, dict[str, Any]] = {}
        for intake in intakes:
            bucket = per_warehouse.setdefault(intake.warehouse, {
                "warehouse": intake.warehouse, "partitions": {},
            })
            part = bucket["partitions"].setdefault(
                intake.partition, {"version": intake.version, "record_keys": []},
            )
            part["record_keys"].append(intake.record_key)

        return {
            "batch_id": batch_id,
            "consistent": not inconsistencies,
            "inconsistencies": inconsistencies,
            "ownerships": [self._ownership_dict(o) for o in ownerships],
            "sub_batches": [self._sub_dict(s) for s in subs],
            "receipts": [self._receipt_dict(r) for r in receipts],
            "intakes": [self._intake_dict(i) for i in intakes],
            "warehouse_intake": list(per_warehouse.values()),
        }

    # ------------------------------------------------------------------
    # 序列化辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _to_sub_batch(row: dict[str, Any]) -> DeliverySubBatch:
        return DeliverySubBatch(
            sub_batch_id=str(row["子批编号"]),
            batch_id=str(row["采集批次"]),
            partition=str(row["分区编码"]),
            warehouse=str(row["归属仓库"]),
            version=int(row["版本号"]),
            approval_id=str(row.get("批准单号", "")),
            record_keys=PartitionOwnershipService._decode_keys(row.get("明细键")),
            status=str(row["子批状态"]),
            receipt_id=str(row.get("收据编号", "")),
            is_correction=row.get("更正标记") == "是",
            created_at=str(row.get("创建时间", "")),
        )

    @staticmethod
    def _to_receipt(row: dict[str, Any]) -> DeliveryReceipt:
        return DeliveryReceipt(
            receipt_id=str(row["收据编号"]),
            sub_batch_id=str(row["子批编号"]),
            batch_id=str(row["采集批次"]),
            partition=str(row["分区编码"]),
            warehouse=str(row["归属仓库"]),
            version=int(row["版本号"]),
            status=str(row["收据状态"]),
            recorded_at=str(row.get("签收时间", "")),
            note=str(row.get("备注", "")),
        )

    @staticmethod
    def _ownership_dict(o: PartitionOwnership) -> dict[str, Any]:
        return {
            "ownership_id": o.ownership_id, "batch_id": o.batch_id,
            "partition": o.partition, "warehouse": o.warehouse,
            "version": o.version, "approval_id": o.approval_id,
            "status": o.status, "effective_at": o.effective_at,
            "supersedes": o.supersedes,
        }

    @staticmethod
    def _sub_dict(s: DeliverySubBatch) -> dict[str, Any]:
        return {
            "sub_batch_id": s.sub_batch_id, "batch_id": s.batch_id,
            "partition": s.partition, "warehouse": s.warehouse,
            "version": s.version, "approval_id": s.approval_id,
            "record_keys": list(s.record_keys), "status": s.status,
            "receipt_id": s.receipt_id, "is_correction": s.is_correction,
        }

    @staticmethod
    def _receipt_dict(r: DeliveryReceipt) -> dict[str, Any]:
        return {
            "receipt_id": r.receipt_id, "sub_batch_id": r.sub_batch_id,
            "batch_id": r.batch_id, "partition": r.partition,
            "warehouse": r.warehouse, "version": r.version,
            "status": r.status, "recorded_at": r.recorded_at, "note": r.note,
        }

    @staticmethod
    def _intake_dict(i: WarehouseIntake) -> dict[str, Any]:
        return {
            "intake_id": i.intake_id, "warehouse": i.warehouse,
            "batch_id": i.batch_id, "partition": i.partition,
            "record_key": i.record_key, "version": i.version,
            "receipt_id": i.receipt_id, "sub_batch_id": i.sub_batch_id,
        }
