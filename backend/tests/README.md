# 派工调度检查

这些脚本直接使用应用代码和一次性 SQLite 数据库，不触碰演示数据。

```bash
cd backend
rm -f /tmp/t.db
DATABASE_URL=sqlite:////tmp/t.db SEED_DEMO=false ARTIFACT_DIR=/tmp/artifacts \
  python3 tests/test_scheduling.py      # 轮转、名额、恢复、凭据、快照、重启
DATABASE_URL=sqlite:////tmp/t.db SEED_DEMO=false ARTIFACT_DIR=/tmp/artifacts \
  python3 tests/test_worker.py          # 真实分析、续租、失联恢复、tick
DATABASE_URL=sqlite:////tmp/t.db SEED_DEMO=false ARTIFACT_DIR=/tmp/artifacts \
  python3 tests/test_dispatch_api.py    # 权限、名额接口、轨迹、历史结果与简报
DATABASE_URL=sqlite:////tmp/t.db SEED_DEMO=false ARTIFACT_DIR=/tmp/artifacts \
  python3 tests/test_concurrency.py     # 并发争抢名额、冒名提交、最终收敛
```

说明：这些检查只覆盖应用逻辑与接口层，使用 SQLite；容器内 PostgreSQL
行锁、Redis 心跳与 Nginx 反向代理需要在 `docker compose up` 后另行验证，
未执行的部分不应视为通过。
