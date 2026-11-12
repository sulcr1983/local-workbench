# 贡献指南

## 提 Issue

- **Bug**：说明系统版本、Python 版本、复现步骤，以及 `data/workbench.log` 里的相关报错
- **功能建议**：先说清要解决的**实际问题**，而不是直接给方案。这个项目刻意保持简单，功能不是越多越好

## 提 PR

1. **新增依赖要先讨论**。项目不追求零依赖，但每个依赖要能回答「它替代了多少自建代码、为什么更简单」（见 `docs/TECH_SPEC.md` §2 依赖表）
2. 改动前先开 Issue，避免白做
3. 前端改动请附**有头浏览器截图**，并说明检查过 `getBoundingClientRect`
4. 代码风格跟着现有的写，不引入格式化工具的强约束
5. 提交前跑 `python tests/run_all.py`，**退出码必须为 0 且 SKIP 0**

## 开发

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe app.py      # 或直接双击 start.bat
python tests/run_all.py              # 测试；唯一允许得出"全绿"的入口
python scripts/build_dist.py         # 打发布包到 release/
```

代码结构：

| 位置 | 说明 |
|---|---|
| `app.py` | 入口：查端口、起 Waitress、退出收尾（不上业务，≤150 行） |
| `wb/` | 业务包（`__init__.py` 应用工厂 + db / scan / ssh / expiry / notify / scheduler） |
| `wb/blueprints/` | 路由层：只取参数、调服务、渲染模板，不写业务分支 |
| `templates/` `static/` | Jinja2 模板与自有 css/js（自造组件类名一律 `wb-` 前缀） |
| `static/vendor/tabler/` | Tabler 编译产物，**不要手改** |
| `data/` | 运行时数据（配置 / 数据库 / 日志），不入库不打包 |

## 提交与发布

**这个仓库只能 commit，不能 push。** 直接 `git push` 会被 pre-push 拦下，因为本地历史里有真实服务器地址和私有项目名，推出去撤不回来。

- 日常：`git add` + `git commit` 就够了，本地提交就是备份
- 对外：`python scripts/publish_public.py`（先干跑看报告，再加 `--apply`）
- 换机器后先装门禁：`python scripts/install_hooks.py`

完整规则见 [docs/GIT_FLOW.md](docs/GIT_FLOW.md)。

## 不要做的事

- **不要加前端构建链**（Vite / React / npm）——会毁掉「双击就能跑」
- **不要加 Docker / 需要常驻服务的中间件**——这是本机小工具
- **不要引 Django / SQLAlchemy / Celery**——8 张表、两个定时任务，用不上
- **不要把 `data/` 或 `config.json` 提交进 git**——里面有真实密码
- **不要在没确认的情况下改用户磁盘上的文件**（项目代码只读，不改）
- **不要用 `@unittest.skip` 绕过失败**——必须登记 `tests/skip_allowlist.json`，且 SKIP 不算全绿
- **不要用非贪婪正则截取 HTML 片段**——必须做标签配对（曾因此吃掉整个 `<section>`）

## 数据库变更

改动 `docs/TECH_SPEC.md` §5.1 的 schema 时：

1. `meta.schema_version` +1
2. 在 `wb/db.py` 的 `MIGRATIONS` 加一版迁移 SQL（一个事务）
3. 补一条迁移用例（旧版本数据能升上来）
4. 同步 `config.example.json`（若涉及导入导出字段）
