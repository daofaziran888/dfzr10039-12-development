# 虫巢工坊 · 分布式爬虫调度与反爬对抗平台

面向大规模网页采集的分布式爬虫调度、代理池管理、反爬策略对抗、内容抽取清洗与采集结果交付平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 采集站点 | `crawl_site` | 目标站点 | 站点编号、站点域名、站点类型 |
| 代理池 | `proxy_pool` | 代理节点 | 节点编号、代理地址、代理类型 |
| 反爬对抗 | `anti_block` | 对抗策略 | 策略编号、目标站点、对抗类型 |
| 采集任务 | `crawl_job` | 采集作业 | 作业编号、目标站点、入口URL |
| URL去重 | `url_dedup` | 去重指纹 | 指纹编号、源URL、指纹算法 |
| 内容抽取 | `content_extract` | 抽取规则 | 规则编号、目标站点、XPath选择器 |
| 字段清洗 | `field_clean` | 清洗规则 | 规则编号、目标字段、清洗类型 |
| 批次存储 | `batch_store` | 采集批次 | 批次编号、来源作业、数据行数 |
| 增量采集 | `incr_crawl` | 增量标记 | 标记编号、目标站点、增量字段 |
| 解析日志 | `parse_log` | 解析记录 | 记录编号、来源URL、解析规则 |
| 数据投递 | `data_delivery` | 投递任务 | 任务编号、数据来源、目标系统 |
| 分区归属 | `partition_ownership` | 分区归属 | 归属编号、采集批次、分区编码、归属仓库、版本号、批准单号 |
| 目标子批 | `delivery_sub_batch` | 目标子批 | 子批编号、采集批次、分区编码、归属仓库、版本号、收据编号 |
| 签收收据 | `delivery_receipt` | 签收收据 | 收据编号、子批编号、分区编码、归属仓库、版本号 |
| 入仓明细 | `warehouse_intake` | 入仓明细 | 入仓编号、归属仓库、分区编码、明细键、版本号、收据编号 |
| 质量抽样 | `quality_sample` | 抽样计划 | 计划编号、目标批次、抽样比例 |
| 站点监控 | `site_monitor` | 监控指标 | 指标编号、目标站点、监控类型 |
| 验证码识别 | `robot_verify` | 识别记录 | 记录编号、目标站点、验证码类型 |
| 采集限速 | `crawl_limit` | 限速策略 | 策略编号、目标站点、请求间隔 |
| 模板库 | `template_lib` | 采集模板 | 模板编号、站点类型、模板名称 |
| 调度编排 | `scheduler` | 调度计划 | 计划编号、关联作业、Cron表达式 |
| 采集计费 | `crawl_cost` | 用量记录 | 记录编号、采集站点、计费周期 |
| 数据归档 | `data_archive` | 归档策略 | 策略编号、数据范围、归档周期 |
| 差异检测 | `diff_check` | 差异记录 | 记录编号、目标站点、检测页面 |
| 导出任务 | `export_task` | 导出记录 | 记录编号、数据来源、导出格式 |
| 操作审计 | `audit_log` | 审计记录 | 记录编号、操作模块、操作人 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 分区投递治理

同一采集批次的不同分区可由不同仓库负责，治理规则集中在
`backend/app/services/partition_ownership.py`，专用动作挂在
`/api/partition-delivery`：

- **归属按批准记录生效**：`POST /ownerships` 必须带可追查的批准单号；同一分区
  改派时旧版本置「已替换」、新版本递增，在途（待签收/已签收未确认）旧子批自动撤回。
- **按归属编排投递**：`POST /sub-batches` 只为当前生效归属仓库生成目标子批，并把
  归属确认结果同步到数据投递清单 `data_delivery`；`POST .../correction` 按最新
  归属重送更正子批。
- **接收权限校验**：`/sign` 签收、`/receipts/{id}/confirm` 确认都会校验仓库对
  分区的接收资格，未获权限的目标无法签收或消费明细；撤回子批的旧收据留档可回查
  但不可再确认入仓。
- **入仓幂等**：实际入仓明细按 `仓库+批次+分区+明细键` 去重，确认重放与更正
  重送都不会重复入仓；历史已确认收讫及其明细在改派后继续保留。
- **对账**：`GET /batches/{batch_id}/reconcile` 交叉核对归属、子批、收据版本与
  实际入仓明细，输出每个目标实际接收的分区/版本/明细及不一致项。

完整场景见 `backend/tests/test_partition_delivery.py`（批次 B4：W1 两行、W2 一行，
西区在途改派 W2 的签收冲突、更正重放与幂等对账）。

领域：数据工程
子方向：数据采集、爬虫与RPA
