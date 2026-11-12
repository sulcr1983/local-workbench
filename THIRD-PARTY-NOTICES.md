# 第三方组件声明

本项目的分发包（`release/workbench-*.zip`）包含以下第三方组件。分发时**必须**随包附带本文件。

| 组件 | 版本 | 许可证 | 用途 | 分发义务 |
|---|---|---|---|---|
| **Tabler** | 1.7.x | MIT | 前端 UI 基座（`vendor/tabler/`） | 保留版权与许可声明 |
| **paramiko** | 3.5.x | **LGPL-2.1** | SSH 连接（密码/密钥/跳板机） | 见下方专条 |
| Flask / Jinja2 / Waitress | 3.x | BSD-3 / BSD-3 / ZPL-2.1 | HTTP 与模板（T1 起引入） | 保留版权与许可声明 |
| requests | 2.32.x | Apache-2.0 | RDAP / Webhook | 保留 LICENSE 与 NOTICE |
| APScheduler | 3.10.x | MIT | 定时任务 | 保留版权与许可声明 |
| markdown | 3.7.x | BSD-3 | README 渲染 | 保留版权与许可声明 |
| nh3 | 0.2.x | MIT | HTML 净化 | 保留版权与许可声明 |
| tencentcloud-sdk-python-cvm | 3.x | Apache-2.0 | 腾讯云实例到期 | 保留 LICENSE 与 NOTICE |
| alibabacloud_ecs20140526 | 3.x | Apache-2.0 | 阿里云实例到期 | 保留 LICENSE 与 NOTICE |
| huaweicloudsdkecs | 3.x | Apache-2.0 | 华为云实例到期 | 保留 LICENSE 与 NOTICE |

## paramiko（LGPL-2.1）专条

paramiko 以 **LGPL-2.1** 发布。本项目的合规边界：

- **自用（不分发）**：无义务。
- **分发（含把目录拷给他人、发布 PyInstaller 打包产物、上传到公开仓库）**：必须
  1. 随包附带 paramiko 的 LICENSE 文本与**可替换的源码**（未修改原样提供即可，例如 `pip download paramiko` 得到的 sdist）；
  2. 允许接收方用自己编译/修改的 paramiko 替换本包内的该库；
  3. 在文档中声明本项目使用了 paramiko。

  **不需要**把本项目整体改为 GPL——LGPL 允许与 MIT 等项目结合，代价仅是保持该库可替换。

## Tabler（MIT）

`vendor/tabler/tabler.min.css` 与 `tabler.min.js` 为原样引入，未修改。版权归 Tabler 作者所有，许可文本见 <https://github.com/tabler/tabler/blob/main/LICENSE>。
