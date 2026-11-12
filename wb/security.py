"""安全：路径白名单、README 净化、CSV 注入防护.

原则（TECH_SPEC §7.7）：
- 一切来自 URL / 表单的路径，resolve 之后必须仍在允许的根目录内。
- README 渲染必须「markdown → nh3」两步，且显式声明 url_schemes，
  否则一次放宽属性就可能把 javascript: 放进来。
- 只在 hash 变化时重渲（不是「只渲一次」），既不浪费也不能显示过期内容。
"""

from __future__ import annotations

import hashlib
import html as _html
import os
import re
from typing import Any
from urllib.parse import quote

import markdown as md
import nh3

# README 里原生块级标签的 markdown="1" 映射。
# Python-Markdown 的 md_in_html 扩展**只认 markdown="1" 这个属性**才解析块内
# markdown；HTML 里手写的 <div align="center"> 不会带它，于是块内的 **粗体**
# 原样显示成字面星号（GitHub 不这样，用户会以为 README 坏了）。
# 渲染前在原文补这个属性，处理完由 md_in_html 吃掉，不会残留到输出（已实测）。
_MD_ATTR_TAGS = ("div", "p", "section", "article", "header", "footer", "details", "summary")
_MD_ATTR_RE = {
    t: re.compile(r"<%s\b(?![^>]*\bmarkdown\s*=\s*[\"'])([^>]*)>" % t, re.IGNORECASE)
    for t in _MD_ATTR_TAGS
}


def _inject_md_attr(raw: str) -> str:
    """给 README 里的原生块级标签补 markdown="1"。

    Python-Markdown 的 md_in_html 扩展只认这个属性才解析块内 markdown；
    HTML 里手写的 <div align="center"> 不会带它，块内 **粗体** 就会原样显示
    成字面星号。已带 markdown 属性的不重复注入（正则里用负向断言排除）。
    """
    for tag, pat in _MD_ATTR_RE.items():
        raw = pat.sub(lambda m: '<%s%s markdown="1">' % (tag, m.group(1)), raw)
    return raw


class SecurityError(ValueError):
    """路径越界 / 内容不安全 —— 必须显式报错，不能悄悄放过."""

# README 允许的标签与属性（白名单，宁少不多）
ALLOWED_TAGS = {
    "p", "h1", "h2", "h3", "h4", "ul", "ol", "li", "code", "pre", "blockquote",
    "a", "img", "table", "thead", "tbody", "tr", "th", "td", "strong", "em", "hr", "br",
    # README 普遍用 <div align="center"> 包 logo + 标题做居中排版；不放行的话
    # 标签被剥掉，标题会全部左对齐，跟原文件长得不一样。
    "div",
    # 折叠块是 README 常见用法，原生标签、无脚本，安全。
    "details", "summary",
}
ALLOWED_ATTRS = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title", "width", "height"},
    # 纯展示属性，只放行 align / width / height。
    # **绝不放 style / class / on\***：那会把 README 变成注入口子（详见 §7.7）。
    "div": {"align"},
    "p": {"align"},
    "h1": {"align"}, "h2": {"align"}, "h3": {"align"}, "h4": {"align"},
    # code 的 class 留出来给 ``` 语言高亮用（不做高亮，只保留语义）
    "code": {"class"},
}
# 必须显式声明；nh3 默认就挡 javascript:，但不能因为放宽属性把协议过滤丢掉
ALLOWED_SCHEMES = {"http", "https", "mailto"}

_JAVASCRIPT_RE = re.compile(r"javascript\s*:", re.IGNORECASE)

# 渲染口径版本号。渲染逻辑一升级就 +1：掺进 readme_hash 后，库里旧格式/旧口径的
# 缓存会自动失效并重渲，不会「配置改了但页面还是老样子」这种静默失效。
RENDER_VERSION = "v4"

# nh3.clean 输出的标签属性统一是双引号，这里只认它规范化后的形式。
# 分开匹配 img[src] 与 a[href]，避免把两类属性混着改。
_IMG_SRC_RE = re.compile(r'(<img\b[^>]*?\bsrc=")([^"]*)(")', re.IGNORECASE)
_A_HREF_RE = re.compile(r'(<a\b[^>]*?\bhref=")([^"]*)(")', re.IGNORECASE)
_EXPLICIT_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


# ------------------------------------------------------------ 路径白名单
def norm(path: str) -> str:
    """规范化路径：normcase + abspath + **realpath**.

    realpath 是防符号链接逃逸的关键 —— abspath 只做字符串拼接不解析链接，
    一个指向项目外（比如库文件或系统盘）的符号链接会被它放行。
    """
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def is_within(base: str, target: str) -> bool:
    """target 是否在 base 之内（含 base 本身）."""
    b = norm(base)
    t = norm(target)
    if b == t:
        return True
    return t.startswith(b + os.sep)


def resolve_under(base: str, rel: str) -> str | None:
    """把 rel 拼到 base 下并校验；越界返回 None（不做任何修正性猜测）."""
    if rel is None:
        return None
    rel = rel.replace("/", os.sep).strip()
    if rel.startswith(("\\", "/")) or re.match(r"^[a-zA-Z]:", rel):
        return None  # 绝对路径不接受，rel 只能是相对的
    if any(part == ".." for part in rel.split(os.sep)):
        return None
    target = os.path.abspath(os.path.join(base, rel))
    return target if is_within(base, target) else None


def safe_filename(name: str) -> bool:
    """截图之类的单文件名：不许带目录、不许奇怪字符."""
    if not name or len(name) > 120:
        return False
    if name in (".", ".."):
        return False
    return not re.search(r'[\\/:*?"<>|\x00-\x1f]', name)


# ------------------------------------------------------------ README 渲染
def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()


def readme_digest(raw: str) -> str:
    """README 缓存键 = 渲染器版本 + 原文。

    旧库里存的是纯原文 sha1；升级渲染逻辑后这个键必然不同，于是所有项目
    首访自动重渲，不需要写迁移脚本，也不会静默显示过期 HTML。
    """
    return sha1(f"{RENDER_VERSION}|{raw}")


def _is_local_relative(url: str) -> bool:
    """判断这条 src/href 是不是「项目内的相对路径」，需要改写."""
    if not url:
        return False
    low = url.lower()
    if low.startswith(("//", "/", "#", "data:", "javascript:", "mailto:",
                       "http:", "https:", "ftp:", "file:")):
        return False
    # 本地文件没有 query 语义；fragment 保留在最终 URL 上即可
    path = url.split("#", 1)[0].split("?", 1)[0]
    if not path or path.startswith(("/", "\\")):
        return False
    if _EXPLICIT_SCHEME_RE.match(path):  # 任何显式协议（含 C: 盘符）
        return False
    # 穿越路径不改写，也绝不送进资源路由（保持原样，浏览器自己 404）
    if ".." in path.replace("\\", "/").split("/"):
        return False
    return True


def _asset_url(asset_base: str, url: str) -> str:
    path = url.split("#", 1)[0].split("?", 1)[0].replace("\\", "/")
    frag = url.split("#", 1)[1] if "#" in url else ""
    out = "/readme-asset/%s?f=%s" % (
        quote(asset_base, safe="/"),
        quote(path, safe="/"),
    )
    if frag:
        out += "#" + quote(frag, safe="")
    return out


def _rewrite_local_assets(cleaned: str, asset_base: str) -> str:
    """把本地相对图片/链接指向 /readme-asset；外链、锚点、绝对路径一律不动."""
    def _sub(m: re.Match[str]) -> str:
        url = m.group(2)
        if not _is_local_relative(url):
            return m.group(0)
        return m.group(1) + _html.escape(_asset_url(asset_base, url), quote=True) + m.group(3)

    cleaned = _IMG_SRC_RE.sub(_sub, cleaned)
    return _A_HREF_RE.sub(_sub, cleaned)


def render_markdown(raw: str, asset_base: str | None = None) -> str:
    """markdown → HTML → nh3 净化 → 本地相对资源改写.

    asset_base 传项目 rel；不传时（纯函数场景）只做净化，不改写 URL。
    """
    try:
        html = md.markdown(
            _inject_md_attr(raw),
            extensions=["fenced_code", "tables", "sane_lists", "md_in_html"],
            output_format="html",
        )
    except Exception:  # noqa: BLE001 - markdown 解析器对畸形输入偶发异常
        html = "<p>README 解析失败，原文可能存在不支持的语法。</p>"

    cleaned = nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        url_schemes=ALLOWED_SCHEMES,
        link_rel="noopener noreferrer",
    )

    if asset_base:
        cleaned = _rewrite_local_assets(cleaned, asset_base)

    # 后置断言放最后：万一哪天配置被改宽了、或改写出意外内容，这里兜住
    if _JAVASCRIPT_RE.search(cleaned):
        cleaned = _JAVASCRIPT_RE.sub("", cleaned)
    return cleaned


def readme_needs_render(raw: str, cached_hash: str | None) -> bool:
    return readme_digest(raw) != (cached_hash or "")


# ------------------------------------------------------------ CSV 注入
CSV_TRIGGER = ("=", "+", "@", "\t", "\r")
CSV_DASH_DANGER = ("=", "+", "|", "(", ")")


def csv_safe(value: Any) -> str:
    """Excel 公式注入防护.

    口径见 TECH_SPEC §7.7：
    - `= + @ \\t \\r` 开头一律前置单引号
    - `-` **不一律转义**（-2、-v2 是合法值），只有同时含 = + | ( ) 才转义
    """
    s = "" if value is None else str(value)
    if s.startswith(CSV_TRIGGER):
        return "'" + s
    if s.startswith("-") and any(ch in s for ch in CSV_DASH_DANGER):
        return "'" + s
    return s
