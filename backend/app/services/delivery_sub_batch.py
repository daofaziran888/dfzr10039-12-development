"""目标子批领域服务：投递按分区归属清单为每个仓库/分区编排，更正以新版本重送。"""
from __future__ import annotations

from app.services.base import BaseModuleService


class DeliverySubBatchService(BaseModuleService):
    MODULE = "delivery_sub_batch"
    ENTITY = "目标子批"
    OPERATION_TERM = "编排"
    LIST_FIELDS = [
        "子批编号", "采集批次", "分区编码", "归属仓库", "版本号",
        "批准单号", "明细键", "子批状态", "收据编号", "更正标记", "创建时间",
    ]
    REQUIRED_FIELDS = ["子批编号", "采集批次", "分区编码", "归属仓库"]
    STATUS_ORDER = ["待签收", "已签收", "已确认", "已撤回", "已驳回"]
    ACTION_RULES: dict[str, str] = {}
    NEGATIVE_ACTIONS: list[str] = []
