# 工具通配符配置指南

## 概述

Bot 現在支援在 `CLAUDE_ALLOWED_TOOLS` 和 `CLAUDE_DISALLOWED_TOOLS` 中使用通配符模式，方便批量允許或禁止工具。

## 通配符語法

使用 Unix shell 風格的通配符（fnmatch）：

| 通配符 | 說明 | 範例 |
|--------|------|------|
| `*` | 匹配任意數量的任意字符 | `mcp__*` 匹配所有 MCP 工具 |
| `?` | 匹配單一字符 | `Read?` 匹配 `Read1`, `ReadA` |
| `[seq]` | 匹配序列中的任意字符 | `[RW]ead` 匹配 `Read`, `Wead` |
| `[!seq]` | 匹配不在序列中的任意字符 | `[!R]ead` 不匹配 `Read` |

## 常見使用案例

### 1. 允許所有 MCP 工具

```bash
# .env
CLAUDE_ALLOWED_TOOLS=Read,Write,Edit,Bash,mcp__*
```

這會允許：
- ✅ `mcp__sqlite__list_tables`
- ✅ `mcp__sqlite__query`
- ✅ `mcp__filesystem__read`
- ✅ `mcp__tiered-prompts__list_rules`
- ✅ 任何其他 `mcp__` 開頭的工具

### 2. 允許特定 MCP server 的所有工具

```bash
# 只允許 sqlite MCP server
CLAUDE_ALLOWED_TOOLS=Read,Write,mcp__sqlite__*

# 只允許 tiered-prompts MCP server
CLAUDE_ALLOWED_TOOLS=Read,Write,mcp__tiered-prompts__*
```

### 3. 禁止特定模式的工具

```bash
# 禁止所有 Web 相關工具
CLAUDE_DISALLOWED_TOOLS=Web*,*Fetch*

# 這會禁止：
# - WebFetch
# - WebSearch
# - 任何包含 "Fetch" 的工具
```

### 4. 組合使用

```bash
# 允許所有基本工具 + 特定 MCP servers
CLAUDE_ALLOWED_TOOLS=Read,Write,Edit,Bash,Glob,Grep,Task,mcp__sqlite__*,mcp__filesystem__*

# 禁止危險操作
CLAUDE_DISALLOWED_TOOLS=*Delete*,*Remove*,*Drop*
```

## MCP 工具命名規則

MCP 工具的命名格式：`mcp__<server_name>__<tool_name>`

範例：
- `mcp__sqlite__list_tables` → SQLite MCP server 的 list_tables 工具
- `mcp__filesystem__read` → Filesystem MCP server 的 read 工具
- `mcp__tiered-prompts__create_rule` → Tiered-prompts MCP server 的 create_rule 工具

## 測試工具配置

使用測試腳本驗證你的通配符配置：

```bash
# 運行測試
python3 scripts/test-tool-matching.py
```

或手動測試：

```python
import fnmatch

def matches(tool_name, pattern):
    return fnmatch.fnmatch(tool_name.lower(), pattern.lower())

# 測試
matches("mcp__sqlite__list_tables", "mcp__*")           # True
matches("mcp__sqlite__list_tables", "mcp__sqlite__*")   # True
matches("Read", "read")                                 # True (大小寫不敏感)
```

## 診斷工具阻止問題

### 1. 查看實際使用的工具名稱

```bash
# 查看數據庫記錄
sqlite3 data/bot.db "SELECT DISTINCT tool_name FROM tool_usage ORDER BY tool_name;"

# 查看日誌
grep "Tool call received from SDK" logs/*.log
```

### 2. 測試特定工具是否被允許

```bash
# 臨時啟用 debug 日誌
make run-debug

# 在 Telegram 執行會觸發該工具的操作
# 查看日誌中的 "Tool not allowed" 訊息
```

### 3. 臨時禁用驗證（診斷用）

```bash
# .env
DISABLE_TOOL_VALIDATION=true
```

## 安全建議

### ✅ 推薦做法

```bash
# 明確列出核心工具 + 使用通配符允許 MCP
CLAUDE_ALLOWED_TOOLS=Read,Write,Edit,Bash,Glob,Grep,Task,mcp__*
```

### ⚠️ 謹慎使用

```bash
# 允許所有工具（僅開發環境）
CLAUDE_ALLOWED_TOOLS=*

# 或乾脆禁用驗證
DISABLE_TOOL_VALIDATION=true
```

### ❌ 避免

```bash
# 過於寬鬆的模式
CLAUDE_ALLOWED_TOOLS=*  # 允許一切
CLAUDE_DISALLOWED_TOOLS=  # 不禁止任何東西
```

## 常見問題

### Q: 通配符是否大小寫敏感？

A: **否**。所有匹配都是大小寫不敏感的。`Read` = `read` = `READ`

### Q: 可以使用正則表達式嗎？

A: **否**。目前只支援 Unix shell 風格的通配符（`*`, `?`, `[]`），不支援正則表達式。

### Q: 通配符的優先級如何？

A:
1. 如果設置了 `CLAUDE_ALLOWED_TOOLS`，工具必須匹配其中至少一個模式
2. 然後檢查 `CLAUDE_DISALLOWED_TOOLS`，如果匹配則禁止
3. Disallow 優先級高於 Allow

### Q: 如何允許所有工具但禁止特定的？

A:
```bash
# 允許所有
CLAUDE_ALLOWED_TOOLS=*

# 但禁止危險操作
CLAUDE_DISALLOWED_TOOLS=*Delete*,*Drop*,rm
```

## 實戰範例

### 範例 1：SQLite MCP Server

```bash
# 只允許 SQLite 查詢，禁止修改操作
CLAUDE_ALLOWED_TOOLS=Read,Write,mcp__sqlite__list_*,mcp__sqlite__query
CLAUDE_DISALLOWED_TOOLS=mcp__sqlite__execute,mcp__sqlite__*_table
```

### 範例 2：開發環境 vs 生產環境

```bash
# 開發環境（寬鬆）
CLAUDE_ALLOWED_TOOLS=*
CLAUDE_DISALLOWED_TOOLS=

# 生產環境（嚴格）
CLAUDE_ALLOWED_TOOLS=Read,Grep,Glob,mcp__sqlite__list_*,mcp__sqlite__query
CLAUDE_DISALLOWED_TOOLS=Write,Edit,Bash,*Delete*,*Drop*
```

## 更新日誌

- **2026-02-20**: 新增通配符支援
  - 支援 `*`, `?`, `[]` 模式
  - 大小寫不敏感匹配
  - 適用於 `CLAUDE_ALLOWED_TOOLS` 和 `CLAUDE_DISALLOWED_TOOLS`
