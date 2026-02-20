# 🚀 遠端重啟快速上手指南

## TL;DR - 三種方案選擇

| 方案 | 適用場景 | 難度 | 推薦指數 |
|------|---------|------|---------|
| **1. `/restart` 命令** | 日常使用，快速重啟 | ⭐ 簡單 | ⭐⭐⭐⭐⭐ |
| **2. Systemd Service** | Linux 生產環境，長期運行 | ⭐⭐ 中等 | ⭐⭐⭐⭐⭐ |
| **3. 監控腳本** | 輕量級自動恢復 | ⭐ 簡單 | ⭐⭐⭐ |

**推薦組合**：方案 1 (日常) + 方案 2 (保底)

---

## 📋 方案 1：Telegram `/restart` 命令

### 最快上手（1 分鐘）

```bash
# 1. 確保代碼已更新
git pull  # 如果需要

# 2. 重啟 bot（讓新代碼生效）
make run  # 或 systemd-restart（如果已安裝）
```

### 使用方式

在 Telegram 發送：
```
/restart
```

Bot 會回覆確認並在 3-5 秒內重啟。

**注意**：如果 bot 完全卡死，這個命令可能無效，需要用方案 2/3。

---

## 📋 方案 2：Systemd Service（推薦生產環境）

### 自動化安裝（2 分鐘）

```bash
# 一鍵安裝（會自動配置路徑和用戶名）
./scripts/install-systemd.sh
```

腳本會：
1. ✅ 自動替換用戶名和路徑
2. ✅ 安裝 systemd service
3. ✅ 啟用開機自啟
4. ✅ 詢問是否立即啟動

### 日常使用

```bash
# 重啟 bot（最常用）
make systemd-restart

# 查看狀態
make systemd-status

# 查看日誌
make systemd-logs

# 停止 bot
make systemd-stop

# 啟動 bot
make systemd-start
```

### 遠端 SSH 重啟

```bash
# 從任何地方重啟（需要 SSH 權限）
ssh jyw@your-server "sudo systemctl restart claude-telegram-bot"
```

---

## 📋 方案 3：監控腳本

### 安裝（2 分鐘）

```bash
# 1. 測試腳本
./scripts/monitor.sh

# 2. 添加到 crontab（每 5 分鐘檢查一次）
crontab -e

# 添加以下行：
*/5 * * * * /home/jyw/workspace/claude-code-telegram/scripts/monitor.sh
```

### 查看監控日誌

```bash
tail -f logs/monitor.log
```

---

## 🎯 最佳實踐：組合方案

### 推薦配置

```bash
# 1. 安裝 systemd（保底，自動恢復）
./scripts/install-systemd.sh

# 2. 確認運行
make systemd-status

# 3. 在 Telegram 測試 /restart
# 發送：/restart
```

### 為什麼這樣最好？

- ✅ **平時**：用 `/restart` 快速重啟（無需 SSH）
- ✅ **崩潰**：systemd 自動恢復（10 秒內）
- ✅ **開機**：自動啟動，無人值守
- ✅ **日誌**：`journalctl` 完整記錄

---

## ❓ 常見問題

### Q1: `/restart` 命令沒反應？

**原因**：
1. 可能不是授權用戶（檢查 `ALLOWED_USERS`）
2. Bot 完全崩潰了

**解決**：
```bash
# 使用 systemd 強制重啟
make systemd-restart

# 或 SSH 登入手動重啟
ssh your-server
cd /home/jyw/workspace/claude-code-telegram
make run
```

### Q2: Systemd 服務無法啟動？

**診斷步驟**：
```bash
# 1. 檢查狀態
make systemd-status

# 2. 查看詳細日誌
sudo journalctl -u claude-telegram-bot -n 100

# 3. 測試手動運行
cd /home/jyw/workspace/claude-code-telegram
uv run claude-telegram-bot
```

**常見問題**：
- 路徑錯誤：檢查 service 檔案中的 `WorkingDirectory`
- 環境變數缺失：檢查 `.env` 檔案是否存在
- uv 路徑錯誤：執行 `which uv` 確認路徑

### Q3: 如何確認 bot 是否在運行？

```bash
# 方法 1：systemd 狀態
make systemd-status

# 方法 2：查看進程
ps aux | grep claude-telegram-bot

# 方法 3：Telegram 測試
# 發送任意消息給 bot，看是否有回應
```

### Q4: 如何完全卸載 systemd service？

```bash
# 1. 停止並禁用服務
sudo systemctl stop claude-telegram-bot
sudo systemctl disable claude-telegram-bot

# 2. 刪除 service 檔案
sudo rm /etc/systemd/system/claude-telegram-bot.service

# 3. 重新載入 systemd
sudo systemctl daemon-reload
```

---

## 🔒 安全建議

### 1. 限制 `/restart` 權限

確保 `.env` 中 `ALLOWED_USERS` 僅包含可信用戶：
```bash
ALLOWED_USERS=123456789,987654321  # 你的 Telegram User ID
```

### 2. SSH 安全加固

```bash
# 使用 SSH 密鑰（禁用密碼登入）
ssh-keygen -t ed25519
ssh-copy-id jyw@your-server

# 編輯 /etc/ssh/sshd_config
sudo nano /etc/ssh/sshd_config
# 設置：
# PasswordAuthentication no
# PermitRootLogin no

sudo systemctl restart sshd
```

### 3. 查看重啟審計日誌

```bash
# 在 bot 數據庫中查詢
sqlite3 data/bot.db "SELECT * FROM audit_log WHERE event_type='restart' ORDER BY timestamp DESC LIMIT 10;"
```

---

## 📊 監控和告警

### 添加 Telegram 通知（當 bot 重啟時）

Bot 已經會在重啟時記錄審計日誌。如果需要主動通知，可以在 systemd service 中添加：

```bash
# 編輯 service 檔案
sudo nano /etc/systemd/system/claude-telegram-bot.service

# 在 [Service] 區塊添加：
ExecStopPost=/usr/bin/curl -X POST "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/sendMessage" \
  -d "chat_id=<YOUR_CHAT_ID>" \
  -d "text=⚠️ Bot stopped at $(date)"
```

---

## 📞 需要幫助？

詳細文檔：`scripts/DEPLOY_README.md`

常用命令速查：
```bash
make help              # 查看所有命令
make systemd-restart   # 快速重啟
make systemd-logs      # 查看日誌
```
