# Git 流程与门禁

> 一句话：**这个仓库可以随便 commit，但绝不允许直接 push。**
> 对外发布只有一条出口——`scripts/publish_public.py`。

## 为什么

本地仓库的历史里躺着真实服务器 IP、主机名、私钥路径和私有项目名。Git 的历史是不可撤销的：一旦推到公开仓库，即使立刻删掉，fork、缓存、搜索引擎都留着。所以：

- 历史**不对外**，只留在本机；
- 对外只发「当前代码 + 一份脱敏」，在导出副本里重新建一个单提交仓库。

这不是流程洁癖，是「推错一次就永久泄露」的代价决定的。

## 三层门禁

| 层 | 位置 | 拦什么 | 触发时机 |
|---|---|---|---|
| 提交前 | `.githooks/pre-commit` | 私钥、API Token、数据库、打包产物、>5MB 文件 | `git commit` |
| 提交信息 | `.githooks/commit-msg` | `wip` / `update` / `修改` 这类等于没写的信息 | `git commit` |
| 推送前 | `.githooks/pre-push` | **一切直接推送**，只放行发布脚本 | `git push` |

三层都由 `scripts/git_guard.py` 实现，规则集中在一个文件里，改规则只需改一处。

### pre-commit 的分寸

它只拦「不该进版本库的东西」，**不拦隐私词**。原因很实在：主仓库本来就私有，如果日常 `git commit` 里带个 `demo-collector` 就被拦，人会立刻学会加 `--no-verify`，门禁就废了。

隐私词的拦截放在发布阶段（`publish_public.py` 的审查环节），那里才是真正会泄露的地方。

想在本机也严格起来：

```bash
WB_STRICT=1 git commit -m "feat: ..."   # 隐私词也拦
```

### pre-push 的硬规则

只有同时满足三个条件才放行：

1. 由发布脚本触发（环境变量 `WB_PUBLISH_RUN=1`）；
2. 目标地址等于 `.wb-publish.json` 里的 `repo_url`；
3. 只推 `main` 分支，不推 tag。

其余一律拒绝，并在终端告诉你去用哪个命令。

## 日常开发

```bash
git add <files>
git commit -m "fix(readme): 本地相对图片 404，改走 /readme-asset 路由"
```

就这样，不需要 push。本地提交本身就是你想要的那份备份。

提交信息格式：`<类型>(<范围>): <一句话说明>`
类型：`feat` `fix` `refactor` `docs` `test` `chore` `release`

## 对外发布

```bash
python scripts/publish_public.py              # 干跑：构建 + 审查 + 报告，不推送
python scripts/publish_public.py --apply      # 确认无误后真推
python scripts/publish_public.py --keep       # 保留构建目录，人工翻一遍
```

它做五件事：

1. **从 HEAD 导出**（`git archive`）——未提交的改动、并行任务的半成品天然进不去；
2. **替换对外素材**：README、.gitignore、截图都取自 `docs/public/`，与内部文档解耦；
3. **脱敏**：按 `.wb-publish.json` 的 `scrub` 规则逐条替换（真实 IP → `203.0.113.x`，真实主机 → `*.test`，私有项目名 → Demo）；
4. **审查**：脱敏后再全量扫一遍禁用词、密钥、数据库、大文件。**任何一处命中就中止，不推**；
5. **重建仓库**：在临时目录 `git init`，用发布副本里自己的 hook 再校验一次，单次提交，推送。

### 推上去之后：tag 与 Release 附件

`git push` **不许推 tag**（pre-push 白名单只有 `refs/heads/main`）。发布副本每次都是全新
单提交仓库、没有共同祖先，tag 若跟着它走会指向一个「下次发布就被覆盖」的提交，
反而更容易错位。所以 tag 和附件走 GitHub API，人工/脚本各做一次：

```text
PATCH /repos/{owner}/{repo}/git/refs/tags/v0.2.2   {"sha":"<main 的 sha>","force":true}
POST  https://uploads.github.com/repos/{owner}/{repo}/releases/{release_id}/assets?name=workbench-0.2.2.zip
```

两条都要**回来验**：tag 用 `git ls-remote --tags` 看 sha；附件下载回来算 sha256，与本地
`release/*.sha256` 比。只看「上传返回 201」不算验过。

2026-11-12 的教训：main 已经推到新快照，tag 还指着上一个快照，Release 页面点进去是旧代码；
附件一个都没有。三处都对上才算发布完成 —— **main / tag / 附件**。

审查不通过时它会告诉你是哪个文件哪一行，两条路：补 `scrub` 规则，或把该文件移出 `include_*` 白名单。

## 三个需要知道的边界

**1. `--no-verify` 能绕过 hook。** 这是 git 的设计，任何本地 hook 都挡不住故意的绕过。所以真正的保险是：本机不配置指向公开仓库的常驻 remote（`git remote -v` 应该是空的），发布只走脚本。想更硬就在 GitHub 上给 `main` 开分支保护，禁止直接推送。

**2. hook 是本地配置，不进版本库。** 换机器、重新 clone 之后必须重新安装：

```bash
python scripts/install_hooks.py          # 安装
python scripts/install_hooks.py --check  # 体检（hook 在不在、白名单配没配）
```

**3. `.wb-publish.json` 不对外发布。** 它的 `scrub` 规则里写着私有项目名，发出去等于把清单交出去。所以它只在本地存在，发布包里没有——公开克隆拿到的是「没有白名单 = 谁都不许推」的安全默认值。

## 救急

| 场景 | 做法 |
|---|---|
| 确认是误判，非要提交 | `WB_GUARD_ALLOW=1 git commit ...`（会打印放行提示） |
| 想连隐私词一起拦 | `WB_STRICT=1 git commit ...` |
| 门禁失灵自检 | `python scripts/install_hooks.py --check` |
| 发布审查报错 | 看报告的行号，补 `scrub` 规则或移出白名单，**不要**用 `--no-verify` 硬推 |
