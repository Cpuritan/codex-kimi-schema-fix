# Codex → Kimi Schema-Fix Middleware

> Fixes `HTTP 400: tools.function.parameters is not a valid moonshot flavored json schema` when routing OpenAI Codex to **Kimi For Coding (Moonshot)** models (k3 / k3-256k) through **CC Switch**.

🌐 **Language**: [**English**](README.md) | [**简体中文**](README.zh-CN.md)

---

## The Bug

After a Codex update, requests routed via CC Switch to Kimi started failing with:

```
CC Switch local proxy failed while handling Codex endpoint /responses.
Provider: Kimi For Coding; model: k3-256k;
upstream_status: HTTP 400;
cause: tools.function.parameters is not a valid moonshot flavored json schema,
details: <At path '$defs.__schema20': when using $ref, type should be defined
in the referenced schema instead of the parent schema>
```

### Root Cause

- Codex (desktop) dynamic tools (e.g. Plan mode `request_user_input`) now emit
  tool parameter schemas in **JSON Schema 2020-12** style: a `$defs` entry such
  as `__schema20` combines a `$ref` with sibling keywords:

  ```json
  { "$ref": "#/$defs/__schema7", "type": "string", "format": "uuid", "minLength": 1 }
  ```

- In JSON Schema 2020-12 this is legal (`$ref` is just an applicator keyword,
  siblings are allowed), so OpenAI's own API accepts it.
- Kimi/Moonshot validates tool schemas with its **"Moonshot Flavored JSON
  Schema"** validator (walle — a name used in the Moonshot community and in
  CC Switch's own code, not an official document name), which is **stricter
  than draft-07**: draft-07 only says that sibling keywords of `$ref`
  "MUST be ignored", while Kimi rejects the schema outright (OpenAPI 3.0
  likewise forbids `$ref` siblings). Its error message states that `type`
  should be defined in the referenced schema instead of the parent schema.
  It rejects the request with HTTP 400.
- CC Switch's proxy forwards the schema **unchanged**. Both upstream fixes —
  the Moonshot/Kimi schema normalization (PR #5125) and the `$ref`-sibling
  inlining (PR #6627) — were **still unmerged** when this project was written
  (2026-08-31), so simply updating CC Switch does not help yet.

Observed timeline (from CC Switch request logs): hundreds of successful requests
Aug 2–16, 2026; the first error appeared on Aug 31 — the same day the user
reported updating Codex (user-reported; the Codex update itself was not
independently verified). Successes and failures interleave — only requests that
include the dynamic tools fail; plain coding requests keep working.

## The Solution

A tiny local middleware that sits between CC Switch and Kimi and **fully
dereferences `$ref` / `$defs`** in every `tools[].function.parameters` before
the request reaches Kimi:

- inlines referenced `$defs` schemas (sibling keywords win, cycles protected)
- renames `definitions` → `$defs`
- injects top-level `type: "object"` when missing
- strips the duplicated `/v1` prefix that CC Switch appends to the base URL
- passes the original `Authorization` header through untouched
- relays streaming (SSE / chunked) responses without buffering

This is the same transformation as cc-switch PR #6627 (`inline_ref_siblings`),
applied at a layer you control — usable today, without waiting for a release.

## Architecture

```
Codex (Responses API)
   │  POST /responses
   ▼
CC Switch local proxy  (:15721)
   │  POST /v1/chat/completions   (base_url → http://127.0.0.1:8787)
   ▼
kimi_schema_fix.py    (127.0.0.1:8787)   ← dereferences $ref/$defs, strips /v1
   │  POST /chat/completions
   ▼
Kimi For Coding API  (https://api.kimi.com/coding/v1)
```

> Ports shown (15721 / 8787) are the author's instance; they may differ on your machine.

## Quick Start

1. **Run the middleware**:

   ```
   python kimi_schema_fix.py
   # or double-click start.bat
   ```

   Expected output:
   `listening on http://127.0.0.1:8787 -> https://api.kimi.com/coding/v1`

2. **Point CC Switch at it**: edit the *Kimi For Coding* provider and change
   `base_url` to `http://127.0.0.1:8787` (keep model / token / wire_api as-is),
   then fully restart CC Switch.

3. **Verify**: open `~/.cc-switch/logs/cc-switch.log` and look for
   `请求目标: http://127.0.0.1:8787/v1/chat/completions`. The middleware window
   prints the final upstream URL it forwards to. No more `moonshot flavored`
   400s.

## Files

| File | Purpose |
|---|---|
| `kimi_schema_fix.py` | The middleware (Python 3, stdlib + `requests`), with `--selfcheck` mode |
| `start.bat` | Windows launcher |
| `README.md` / `README.zh-CN.md` | This documentation (EN / CN) |

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| HTTP 400 `moonshot flavored json schema` | requests still bypass the middleware | check `base_url` in CC Switch; restart CC Switch |
| 404 `The requested resource was not found` | duplicated `/v1` in the forwarded path | use the current version of the script (auto-strips `/v1`) |
| connection refused / 502 | middleware not running or port mismatch | start `start.bat`; ports must match (default 8787) |
| port 8787 already in use | another program occupies it | change `LISTEN_PORT` in the script and the CC Switch `base_url` accordingly |

## Retiring This Workaround

When cc-switch PR #6627 is merged and released: stop the middleware, restore
`base_url` to `https://api.kimi.com/coding/v1`, and delete this folder. You can
also ask Moonshot to relax validation — the rejected schemas are valid per
JSON Schema 2020-12.

## Related

- [cc-switch issue #6614](https://github.com/farion1231/cc-switch/issues/6614)
- [cc-switch PR #6627 — inline $ref siblings in tool $defs for strict providers](https://github.com/farion1231/cc-switch/pull/6627)
- [cc-switch PR #5125 — normalize Moonshot/Kimi tool parameters schema](https://github.com/farion1231/cc-switch/pull/5125)
- [Moonshot forum — walle validator & $ref expansion](https://forum.moonshot.ai/t/critical-backend-error-service-unavailable-on-all-requests/427/2)

## License

MIT
