# Codex → Kimi Schema 修复代理

> 解决通过 **CC Switch** 将 OpenAI Codex 路由到 **Kimi For Coding（Moonshot）** 模型（k3 / k3-256k）时出现的 `HTTP 400: tools.function.parameters is not a valid moonshot flavored json schema` 报错。

🌐 **语言**：[**简体中文**](README.zh-CN.md) | [**English**](README.md)

---

## 问题现象

Codex 更新之后，经 cc-switch 转发到 Kimi 的请求开始报错：

```
CC Switch local proxy failed while handling Codex endpoint /responses.
Provider: Kimi For Coding; model: k3-256k;
upstream_status: HTTP 400;
cause: tools.function.parameters is not a valid moonshot flavored json schema,
details: <At path '$defs.__schema20': when using $ref, type should be defined
in the referenced schema instead of the parent schema>
```

### 根本原因

- Codex（桌面端）更新后，内置动态工具（如 Plan 模式的 `request_user_input`）生成的
  工具参数 schema 变成了 **JSON Schema 2020-12** 风格：`$defs` 里的条目（如
  `__schema20`）把 `$ref` 和兄弟关键字写在一起：

  ```json
  { "$ref": "#/$defs/__schema7", "type": "string", "format": "uuid", "minLength": 1 }
  ```

- 在 2020-12 规范里这是合法写法（`$ref` 只是众多 applicator 之一，允许兄弟关键字），
  所以 OpenAI 官方 API 接受它。
- 但 Kimi/Moonshot 用自研的 **"Moonshot Flavored JSON Schema"** 校验器（walle——
  该名称出自 Moonshot 社区讨论与 cc-switch 代码，并非官方文档命名）校验工具
  schema，它**比 draft-07 更严格**：draft-07 只规定 `$ref` 的兄弟关键字
  "MUST be ignored"（忽略），而 Kimi 直接**拒绝**（OpenAPI 3.0 同样禁止 `$ref`
  带兄弟关键字），并在报错中要求 `type` 定义在被引用的 schema 里。因此直接返回
  HTTP 400。
- cc-switch 代理**原样转发**这份 schema，不做规范化。cc-switch 的两个上游修复——
  Moonshot/Kimi schema 规范化（PR #5125）与内联 `$ref` 兄弟关键字（PR #6627）——
  截至本项目编写时（2026-08-31）**均未合并**，所以单纯升级 cc-switch 暂时无效。

时间线证据（来自 cc-switch 请求日志）：2026 年 8 月 2 日–16 日期间数百次请求全部
成功；8 月 31 日出现第一条该报错——与用户报告的 Codex 更新同日（用户报告，
Codex 更新本身未独立验证）。成功与失败交错出现——只有携带动态工具的请求会失败，
普通编码请求不受影响。

## 解决方案

在 cc-switch 与 Kimi 之间加一个本地中转代理，在请求到达 Kimi 之前对
`tools[].function.parameters` 里的 `$ref` / `$defs` **做全量解引用**：

- 把被引用的 `$defs` schema 内联合并（兄弟关键字优先，循环引用有保护）
- `definitions` 自动重命名为 `$defs`
- 顶层缺 `type: "object"` 时自动补上
- 自动去掉 cc-switch 拼接在 base_url 后多余的 `/v1` 前缀（避免 404）
- 原始 `Authorization` 头原样透传，不改不存
- 流式响应（SSE / chunked）逐段透传，不做缓冲

这等价于 cc-switch PR #6627（`inline_ref_siblings`）的转换逻辑，但做在你自己
可控的一层——**今天就能用**，不用等官方发版。

## 架构

```
Codex（Responses API）
   │  POST /responses
   ▼
cc-switch 本地代理  (:15721)
   │  POST /v1/chat/completions   （base_url 改为 http://127.0.0.1:8787）
   ▼
kimi_schema_fix.py  (127.0.0.1:8787)   ← 解引用 $ref/$defs、去掉重复 /v1
   │  POST /chat/completions
   ▼
Kimi For Coding API  (https://api.kimi.com/coding/v1)
```

> 图中端口（15721 / 8787）为作者机器上的实际值，其他机器可能不同。

## 快速开始

1. **启动代理**：

   ```
   python kimi_schema_fix.py
   # 或直接双击 start.bat
   ```

   看到如下输出即成功：
   `listening on http://127.0.0.1:8787 -> https://api.kimi.com/coding/v1`

2. **修改 cc-switch**：编辑 *Kimi For Coding* provider，把 `base_url` 改为
   `http://127.0.0.1:8787`（模型、token、wire_api 等一律不动），然后**完全重启**
   cc-switch。

3. **验证**：打开 `~/.cc-switch/logs/cc-switch.log`，搜索 `请求目标`，应看到
   `请求目标: http://127.0.0.1:8787/v1/chat/completions`。代理窗口会打印它实际
   转发到 Kimi 的完整地址。不再出现 `moonshot flavored` 400 即成功。

## 文件说明

| 文件 | 用途 |
|---|---|
| `kimi_schema_fix.py` | 中转代理（Python 3，标准库 + `requests`），带 `--selfcheck` 自检模式 |
| `start.bat` | Windows 启动脚本 |
| `README.md` / `README.zh-CN.md` | 本文档（中 / 英） |

## 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| HTTP 400 `moonshot flavored json schema` | 请求没走代理 | 检查 cc-switch 的 `base_url`，并重启 cc-switch |
| 404 `The requested resource was not found` | 转发路径里 `/v1` 重复 | 使用当前版本脚本（自动去重 `/v1`） |
| 连接失败 / 502 | 代理没启动或端口不一致 | 启动 `start.bat`；两端口须一致（默认 8787） |
| 8787 端口被占用 | 其他程序占用 | 改脚本里的 `LISTEN_PORT`，cc-switch 的 `base_url` 同步改 |

## 何时可以下线此方案

cc-switch PR #6627 合并发布后：停掉代理 → `base_url` 改回
`https://api.kimi.com/coding/v1` → 删除本项目即可。也可以向 Moonshot 反馈放宽
校验——被拒的 schema 在 JSON Schema 2020-12 规范下是合法的。

## 相关链接

- [cc-switch issue #6614](https://github.com/farion1231/cc-switch/issues/6614)
- [cc-switch PR #6627 — inline $ref siblings in tool $defs for strict providers](https://github.com/farion1231/cc-switch/pull/6627)
- [cc-switch PR #5125 — normalize Moonshot/Kimi tool parameters schema](https://github.com/farion1231/cc-switch/pull/5125)
- [Moonshot 论坛 — walle 校验器与 $ref 展开](https://forum.moonshot.ai/t/critical-backend-error-service-unavailable-on-all-requests/427/2)

## 许可证

MIT
