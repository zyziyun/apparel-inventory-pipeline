PY := ./.venv/bin/python
# Postgres 可执行文件目录。优先用 PATH 上的，找不到再退回 homebrew 默认位置。
# 覆盖方式：make PG=/your/pg/bin up
PG ?= $(shell dirname $$(command -v pg_ctl 2>/dev/null) 2>/dev/null || echo /opt/homebrew/opt/postgresql@14/bin)
PGDATA := $(CURDIR)/pgdata
PGOPTS := -p 55432 -k /tmp

.PHONY: help up down status run traps guard eval demo

help:
	@echo "make up      启动本地 Postgres，端口 55432，不碰系统服务"
	@echo "make down    停掉它"
	@echo "make run     端到端：建表 → 造数 → 幂等加载 → 对账 → 验收"
	@echo "make traps   演示 5 个 SQL 坑，讲义 1.6"
	@echo "make guard   演示 AST 级 SQL 校验，讲义 2.7"
	@echo "make eval    演示结果比对，讲义 2.8"
	@echo "make demo    上面三个演示连着跑"
	@echo "make reference  下载课上要打开的开源参考文件"

up:
	@$(PG)/pg_ctl -D $(PGDATA) -o "$(PGOPTS)" -l $(PGDATA)/server.log start 2>/dev/null || true
	@sleep 1 && $(PG)/pg_isready -h /tmp -p 55432

down:
	@$(PG)/pg_ctl -D $(PGDATA) stop 2>/dev/null || true

status:
	@$(PG)/pg_isready -h /tmp -p 55432

run: up
	@$(PY) run_pipeline.py

traps: up
	@$(PY) demo/sql_traps.py

guard:
	@$(PY) demo/agent_guard.py

eval: up
	@$(PY) demo/eval_compare.py 2>/dev/null

demo: traps guard eval

reference:
	@bash scripts/fetch_reference.sh
