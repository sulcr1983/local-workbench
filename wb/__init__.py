"""Supersu 工作台 · 应用工厂.

约定（见 docs/TECH_SPEC.md §4）：
- 这里只做「创建 app + 注册蓝图 + 注入模板默认值」，不写业务逻辑。
- 模板与静态目录用绝对路径，避免被 Flask 的 root_path 推断带到 wb/ 里去。
"""

from __future__ import annotations

import os

from flask import Flask, render_template

# 项目根目录（wb/ 的上一级）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

APP_NAME = "Supersu 工作台"

# T2 阶段：界面开关还没进库，先给默认值；T3 建库后由 ui_cfg 表提供
DEFAULT_UI = {"show_overview": False}


def _is_api() -> bool:
    """当前请求是不是走 /api/ 的接口调用.

    统一口径：接口一律回 JSON，页面一律回 HTML。前端十几个地方是裸
    `r.json()`，后端若吐 Werkzeug 英文错误页，UI 会静默失败 ——
    这条规则以前只落在 413 上，404/405/500 都漏了。
    """
    from flask import request

    return request.path.startswith("/api/")


def _api_error(message: str, status: int):
    from flask import jsonify

    return jsonify({"ok": False, "error": message}), status



def create_app() -> Flask:
    """创建并配置 Flask 应用."""
    from . import config, db

    config.setup_logging()
    db.init_db()  # 建库 / 校验 / 迁移；失败抛 DBError，由启动入口负责报错退出

    app = Flask(
        __name__,
        template_folder=os.path.join(ROOT, "templates"),
        static_folder=os.path.join(ROOT, "static"),
    )

    # ---------------------------------------------------------- 请求体上限
    # Werkzeug 3.1 的 MAX_FORM_MEMORY_SIZE 默认只有 500KB，超了直接 413 并把
    # Werkzeug 原生 HTML 错误页吐回来 —— 前端 r.json() 解析失败，用户只看到
    # "服务返回的不是合法 JSON"。截图走 base64 传，一张 400KB 的图编码后 533KB
    # 就撞线了，所以必须显式放开。
    #
    # 内存上限给 12MB：8MB 图 base64 后约 10.7MB，再留点余量。
    # MAX_CONTENT_LENGTH 给 16MB 兜底，防止有人往这个口子灌大文件。
    app.config.update(
        MAX_FORM_MEMORY_SIZE=12 * 1024 * 1024,
        MAX_CONTENT_LENGTH=16 * 1024 * 1024,
    )

    from .blueprints import api, audit, engine, expiries, pages, projects, servers, settings, welcome
    from .paging import page_window

    app.register_blueprint(pages.bp)
    app.register_blueprint(projects.bp)
    app.register_blueprint(servers.bp)
    app.register_blueprint(expiries.bp)
    app.register_blueprint(engine.bp)
    app.register_blueprint(settings.bp)
    app.register_blueprint(audit.bp)
    app.register_blueprint(api.bp)
    app.register_blueprint(welcome.bp)

    # 模板里直接用 page_window(page, pages) 画省略号
    app.jinja_env.globals["page_window"] = page_window

    # 项目详情页「仓库」按钮：git remote → 可点链接，认不出就空着
    from .scan import repo_url

    app.jinja_env.filters["repo_url"] = repo_url

    @app.context_processor
    def inject_defaults() -> dict:
        """模板里放心用 ui / counts / rail，缺省不炸."""
        from flask import request

        from . import db
        from .blueprints.settings import load_ui

        # 写死 8787 会在换端口（WB_PORT）时显示错的地址，按实际请求取
        try:
            host = request.host or "127.0.0.1:8787"
        except RuntimeError:  # 没有请求上下文（离线渲染模板）时兜底
            host = "127.0.0.1:8787"

        try:
            ui = load_ui()
        except Exception:  # noqa: BLE001 - 库还没建好时也不能让页面 500
            ui = dict(DEFAULT_UI)

        try:
            counts = {
                "projects": db.scalar("SELECT COUNT(*) FROM project", default=0),
                "servers": db.scalar("SELECT COUNT(*) FROM server", default=0),
                "expiries": db.scalar(
                    "SELECT COUNT(*) FROM expiry WHERE expire IS NOT NULL AND expire <= date('now','localtime','+30 day')",
                    default=0,
                ),
            }
        except Exception:  # noqa: BLE001
            counts = {}

        return {
            "ui": ui,
            "counts": counts,
            "rail": None,
            "crumb_sub": f"本地 · {host}",
        }

    @app.errorhandler(404)
    def not_found(_err):
        if _is_api():
            return _api_error("这个接口不存在，刷新页面再试一次。", 404)
        return (
            render_template(
                "placeholder.html",
                page_title="找不到页面",
                page_code="404 / NOT FOUND",
                page_sub="这个地址不存在，或者对应页面还没做。",
                stage="404",
                crumb="404",
                active="",
                status=404,
            ),
            404,
        )

    @app.errorhandler(405)
    def method_not_allowed(_err):
        """请求方式不对：直接拿浏览器打开只收 POST 的接口就会撞上."""
        if _is_api():
            return _api_error("这个接口不接受当前的请求方式。", 405)
        return (
            render_template(
                "placeholder.html",
                page_title="打开方式不对",
                page_code="405 / METHOD NOT ALLOWED",
                page_sub="这个地址要用页面上的按钮操作，直接在地址栏回车是打不开的。",
                stage="405",
                crumb="405",
                active="",
                status=405,
            ),
            405,
        )

    @app.errorhandler(413)
    def too_large(_err):
        """请求体超限.

        接口必须回 JSON —— 前端 `r.json()` 拿到 Werkzeug 的 HTML 错误页会炸成
        "服务返回的不是合法 JSON"，用户完全不知道是图太大。页面请求仍走 HTML。
        """
        from flask import jsonify, request

        limit_mb = app.config.get("MAX_CONTENT_LENGTH", 0) // 1024 // 1024
        msg = f"这次提交的数据超过上限（约 {limit_mb}MB）。截图请压小一点再试。"
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": msg}), 413
        return (
            render_template(
                "placeholder.html",
                page_title="提交的内容太大",
                page_code="413 / TOO LARGE",
                page_sub=msg,
                stage="413",
                crumb="413",
                active="",
                status=413,
            ),
            413,
        )

    @app.errorhandler(500)
    def server_error(err):
        """页面永远给完整 HTML；堆栈进日志，界面上只给一个编号."""
        import logging
        import uuid

        err_id = uuid.uuid4().hex[:8]
        logging.getLogger("wb").exception("未处理异常 #%s", err_id, exc_info=err)
        if _is_api():
            return _api_error(f"服务器出错（编号 {err_id}），详情见日志。", 500)
        return (
            render_template(
                "error.html",
                page_title="出错了",
                page_code="500",
                crumb="出错了",
                active="",
                err_id=err_id,
                counts={},
            ),
            500,
        )

    return app
