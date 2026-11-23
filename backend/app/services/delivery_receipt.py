"""签收收据领域服务：记录目标仓库对子批的签收与确认凭证，支持旧收据留档回查。"""
from __future__ import annotations

from app.services.base import BaseModuleService


class DeliveryReceiptService(BaseModuleService):
    MODULE = "delivery_receipt"
    ENTITY = "签收收据"
    OPERATION_TERM = "签收"
    LIST_FIELDS = [
        "收据编号", "子批编号", "采集批次", "分区编码", "归属仓库",
        "版本号", "收据状态", "签收时间", "备注",
    ]
    REQUIRED_FIELDS = ["收据编号", "子批编号", "归属仓库"]
    STATUS_ORDER = ["已签收", "已确认", "已驳回"]
    ACTION_RULES = {"驳回收据": "已驳回"}
    NEGATIVE_ACTIONS = ["驳回收据"]
