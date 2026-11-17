# 实地调研加权透视

加权透视汇总的真实业务分析小型项目。完整模拟客户、痛点、上下游、算法公式与一天工作剧本见 [BUSINESS.md](BUSINESS.md)。

## 模块与业务流程
数据接入（JSON/CSV、字段验证、批次指纹）→ 参数化分析（异步 Python worker）→ 结果复核（指标、图形、明细、解释、批准）→ 定时分发（本地收件箱、CSV/JSON、SHA256）；另有业务总览和审计轨迹。
三个贯穿全栈的实体：Dataset 数据批次、Job 分析任务、Report 分发计划；辅助 User、Audit、Delivery。派工调度是该链路上的独立一层，见下。

## 派工调度（按发起人轮转）
连续提交会挤占他人，因此排队不按任务先来后到，而是按**发起人**轮转：

- 每名发起人每轮领取一项，个人任务仍按受理顺序处理；本轮无任务的发起人跳过，不阻塞他人。
- 新来者加入当前循环的末尾，不会插到更早提交者之前。
- 管理员可设**全站运行名额**（`SITE_CAPACITY`，1–8，默认 1）。调低名额不打断在途分析，只有空位出现才派下一项。
- 派工记录（`job_events`）、轮转游标（`scheduler_state`）与任务领取在同一事务中写入，派工依据与领取结果始终一致。
- 轮转进度与未完任务持久化在 `scheduler_state` / `jobs`，重启后从原游标继续；中断任务转为「待恢复」，恢复后回到原轮次重试，不会重复派工，也不会额外多给发起人一轮。
- 每个领取带独立凭据与租约；执行者失联后任务转「待恢复」，失联者的晚回结果因凭据不符被拒收并留痕。
- 「派工调度台」（`#/dispatch`）可查等待次序、占位任务、轮转位置与每一次派工的依据；任务详情页给出从排队到占位、完成或待恢复的完整轨迹。历史成功结果与简报照常可用。

## 快速启动
```bash
cp .env.example .env
docker compose up -d --build --wait
docker compose exec -T backend python -m scripts.seed_demo
```
访问 http://localhost:18218；后端文档 http://localhost:19218/docs；数据库宿主端口 17718。
`SEED_DEMO=true` 自动幂等 seed；不覆盖用户修改。手动 seed 命令可重复执行。

## 演示账号与权限
|账号|密码|真实权限|
|---|---|---|
|admin@demo.local|Demo123!|接入、执行、批准、分发、暂停、读取与下载|
|analyst@demo.local|Demo123!|接入、执行、读取与下载；不能批准或安排分发|
|viewer@demo.local|Demo123!|只读与下载|

## 技术与本地开发
Python 3.12 / FastAPI / SQLAlchemy / NumPy / SciPy / scikit-learn；PostgreSQL 16；Redis 7；Nginx；原生 HTML CSS JavaScript ES modules（用户指定）。后端所有校验、持久化和算法在服务端。无前端打包步骤。
后端：安装 backend/requirements.txt，设置 DATABASE_URL、REDIS_URL、JWT_SECRET、ARTIFACT_DIR 后在 backend/ 执行 `python -m migrations.initial`、`python -m scripts.seed_demo`、`uvicorn src.main:app`，另开 `python -m src.worker`。前端使用带 /api 反向代理的 Nginx，直接打开 index.html 不支持 API。

## 目录与工程约定
backend/src/{models,schemas,services,routes,controllers,middlewares,constants,config,utils}；backend/scripts/seed_demo.py；backend/migrations/initial.py。
frontend/src/{api,stores,types,components/common,hooks,pages,router,utils,constants}。
Role 枚举：admin/analyst/viewer；JobStatus：queued/running/recovering/completed/failed。后端 backend/src/constants/enums.py，前端 frontend/src/constants/enums.js；路由权限在 middlewares/auth.py；页面按相同角色控制操作入口。派工逻辑集中在 backend/src/services/scheduling.py，调度台页面为 frontend/src/pages/dispatch.js。

## 环境变量与 Docker
COMPOSE_PROJECT_NAME=survey-pivot；FRONTEND_PORT=18218；BACKEND_PORT=19218；DB_PORT=17718；DB_PASSWORD、JWT_SECRET 见 .env.example；SEED_DEMO 默认 true。共享命名产物卷供后端下载与 worker 写入，数据库/Redis 各持久卷。派工相关：`SITE_CAPACITY` 初始全站名额（1–8，默认 1，管理员可在调度台调整）、`JOB_LEASE_SECONDS` 领取租约秒数（默认 30）。
恰好五服务均有健康检查。Redis 队列只负责唤醒；worker 以 SQL 中的排队状态为耐久依据按轮转派工，并写入本地收件箱文件。周期 60 秒至 30 天。分发结果为批准分析的固定快照；若需新数据重新计算并建立新计划。当前不连接邮件或企业聊天。
错误响应、请求编号、Redis 每 IP 每分钟 240 次限流、JWT 和数据库操作审计。演示密钥与密码仅供本机模拟；生产部署前配置独立密钥与账号。

## 数据与验收
所有数据为固定随机种子生成的模拟业务数据，不能作为实际生产决策证据。项目独立算法、输入边界及业务局限详见 BUSINESS.md。测试与截图统一存放 ../../test-artifacts/gb-1019/，源码不含日志/截图/测试报告。
清理本项目演示数据：`docker compose down -v --remove-orphans`（会删除本项目命名卷）。

## License
MIT，见 LICENSE。
