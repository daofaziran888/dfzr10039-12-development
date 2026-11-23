"""分区投递治理路由：归属确认、按归属编排子批/更正、签收确认与对账。

通用增删查仍走 resource.py，这里只承载有业务判断的动作；规则全部在
PartitionOwnershipService 内，路由层不做业务判断。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.partition_ownership import (
    AssignmentConflictError, OwnershipError, PartitionOwnershipService,
)

router = APIRouter(prefix="/api/partition-delivery", tags=["分区投递治理"])
service = PartitionOwnershipService()


class OwnershipPayload(BaseModel):
    batch_id: str
    partition: str
    warehouse: str
    approval_id: str


class DispatchPayload(BaseModel):
    batch_id: str
    partition: str
    record_keys: list[str] = Field(default_factory=list)


class SignPayload(BaseModel):
    warehouse: str
    note: str = ""


class WarehousePayload(BaseModel):
    warehouse: str


def _fail(exc: OwnershipError) -> HTTPException:
    status = 409 if isinstance(exc, AssignmentConflictError) else 400
    return HTTPException(status_code=status, detail=str(exc))


@router.post("/ownerships")
def approve_ownership(payload: OwnershipPayload) -> dict[str, Any]:
    """登记/改派分区归属，必须带可追查的批准单号。"""
    try:
        ownership = service.approve_ownership(
            payload.batch_id, payload.partition, payload.warehouse,
            payload.approval_id,
        )
    except OwnershipError as exc:
        raise _fail(exc) from exc
    return {"ok": True, "data": service._ownership_dict(ownership)}


@router.post("/sub-batches")
def dispatch_sub_batch(payload: DispatchPayload) -> dict[str, Any]:
    """按当前生效归属为分区编排目标子批。"""
    try:
        sub = service.dispatch_sub_batch(payload.batch_id, payload.partition, payload.record_keys)
    except OwnershipError as exc:
        raise _fail(exc) from exc
    return {"ok": True, "data": service._sub_dict(sub)}


@router.post("/sub-batches/{sub_batch_id}/sign")
def sign_for(sub_batch_id: str, payload: SignPayload) -> dict[str, Any]:
    """目标仓库签收入库前收据；无权限或已撤回的子批拒绝签收。"""
    try:
        receipt = service.sign_for(sub_batch_id, payload.warehouse, payload.note)
    except OwnershipError as exc:
        raise _fail(exc) from exc
    return {"ok": True, "data": service._receipt_dict(receipt)}


@router.post("/receipts/{receipt_id}/confirm")
def confirm_receipt(receipt_id: str, payload: WarehousePayload) -> dict[str, Any]:
    """确认收讫并消费明细；入仓按仓库+分区+明细键幂等。"""
    try:
        receipt, intakes = service.confirm_receipt(receipt_id, payload.warehouse)
    except OwnershipError as exc:
        raise _fail(exc) from exc
    return {
        "ok": True,
        "data": service._receipt_dict(receipt),
        "new_intakes": [service._intake_dict(i) for i in intakes],
    }


@router.post("/batches/{batch_id}/partitions/{partition}/correction")
def replay_correction(batch_id: str, partition: str) -> dict[str, Any]:
    """归属改派后按最新生效归属重送更正子批。"""
    try:
        sub = service.replay_correction(batch_id, partition)
    except OwnershipError as exc:
        raise _fail(exc) from exc
    return {"ok": True, "data": service._sub_dict(sub)}


@router.get("/batches/{batch_id}/reconcile")
def reconcile_batch(batch_id: str) -> dict[str, Any]:
    """核对批次内归属、子批、收据与实际入仓明细。"""
    return service.reconcile_batch(batch_id)
