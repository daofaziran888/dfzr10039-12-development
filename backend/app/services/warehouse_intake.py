"""仓库实际入仓明细服务：按仓库+分区+明细键幂等落库，支撑投递对账。"""
from __future__ import annotations

from app.services.base import BaseModuleService


class WarehouseIntakeService(BaseModuleService):
    MODULE = "warehouse_intake"
    ENTITY = "入仓明细"
    OPERATION_TERM = "入仓"
    LIST_FIELDS = [
        "入仓编号", "归属仓库", "采集批次", "分区编码", "明细键",
        "版本号", "收据编号", "子批编号", "入仓时间",
    ]
    REQUIRED_FIELDS = ["入仓编号", "归属仓库", "采集批次", "分区编码", "明细键"]
    STATUS_ORDER = ["已入仓"]
    ACTION_RULES: dict[str, str] = {}
    NEGATIVE_ACTIONS: list[str] = []
