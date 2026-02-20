# 遠端重啟部署方案

提供三種遠端重啟 bot 的方案，按簡單到完整排序。

---

## 方案 1：透過 Telegram `/restart` 命令 ⭐ 推薦

**最簡單的方案**，直接透過 TG 發送 `/restart` 重啟 bot。

### 使用方式

1. 確保 bot 已更新（包含 `/restart` 命令）
2. 在 Telegram 發送：`/restart`
3. Bot 會自動重啟（約 3-5 秒）

### 優點
- ✅ 最方便，無需 SSH
- ✅ 有權限控制（只有授權用戶可用）
- ✅ 有審計記錄

### 限制
- ⚠️ 如果 bot 完全卡死或崩潰，可能無法響應（需要配合方案 2/3）

---

## 方案 2：Systemd Service（Linux 生產環境）

**適合長期運行**，開機自啟 + 崩潰自動重啟。

### 安裝步驟

```bash
# 1. 編輯 service 檔案，修改用戶名和路徑
nano scripts/claude-telegram-bot.service
# 將 %YOUR_USERNAME% 替換為你的用戶名（如 jyw）

# 2. 複製到 systemd 目錄
sudo cp scripts/claude-telegram-bot.service /etc/systemd/system/

# 3. 重新載入 systemd
sudo systemctl daemon-reload

# 4. 啟用並啟動服務
sudo systemctl enable claude-telegram-bot
sudo systemctl start claude-telegram-bot

# 5. 檢查狀態
sudo systemctl status claude-telegram-bot
```

### 日常操作

```bash
# 重啟 bot
sudo systemctl restart claude-telegram-bot

# 停止 bot
sudo systemctl stop claude-telegram-bot

# 查看日誌
sudo journalctl -u claude-telegram-bot -f

# 查看最近 50 條日誌
sudo journalctl -u claude-telegram-bot -n 50
```

### 遠端操作（透過 SSH）

```bash
# SSH 登入後直接重啟
ssh user@your-server "sudo systemctl restart claude-telegram-bot"
```

### 優點
- ✅ 崩潰自動重啟（`Restart=always`）
- ✅ 開機自動啟動
- ✅ 完整的日誌管理（journalctl）
- ✅ 標準化管理方式

---

## 方案 3：監控腳本 + Cron（輕量級）

**適合簡單場景**，定期檢查 bot 是否運行，如果沒有則自動重啟。

### 安裝步驟

```bash
# 1. 賦予執行權限
chmod +x scripts/monitor.sh

# 2. 測試腳本
./scripts/monitor.sh

# 3. 添加到 crontab（每 5 分鐘檢查一次）
crontab -e

# 添加以下行：
*/5 * * * * /home/jyw/workspace/claude-code-telegram/scripts/monitor.sh
```

### 查看監控日誌

```bash
tail -f logs/monitor.log
```

### 優點
- ✅ 簡單，不需要 root 權限
- ✅ 崩潰自動重啟（最多延遲 5 分鐘）
- ✅ 適合已經使用 tmux 的場景

### 限制
- ⚠️ 不是即時重啟（取決於 cron 間隔）
- ⚠️ 需要 tmux

---

## 組合推薦方案

**最佳實踐**：組合使用方案 1 + 方案 2

1. **平時**：使用 `/restart` 命令快速重啟（無需 SSH）
2. **保底**：systemd 確保 bot 崩潰後自動恢復
3. **部署**：開機自動啟動，無人值守運行

### 完整部署流程

```bash
# 1. 安裝 systemd service
sudo cp scripts/claude-telegram-bot.service /etc/systemd/system/
sudo nano /etc/systemd/system/claude-telegram-bot.service  # 修改用戶名
sudo systemctl daemon-reload
sudo systemctl enable claude-telegram-bot
sudo systemctl start claude-telegram-bot

# 2. 確認運行
sudo systemctl status claude-telegram-bot

# 3. 在 Telegram 測試 /restart 命令
```

---

## 故障排查

### Bot 無法啟動

```bash
# 檢查 systemd 狀態
sudo systemctl status claude-telegram-bot

# 查看詳細日誌
sudo journalctl -u claude-telegram-bot -n 100 --no-pager

# 檢查環境變數
sudo systemctl show claude-telegram-bot | grep Environment

# 手動測試運行
cd /home/jyw/workspace/claude-code-telegram
uv run claude-telegram-bot
```

### /restart 命令無反應

1. 檢查是否為授權用戶（`ALLOWED_USERS`）
2. 查看 bot 日誌確認是否收到命令
3. 如果完全無響應，使用 systemd 或 SSH 重啟

### 無法遠端 SSH

```bash
# 確保 SSH 服務運行
sudo systemctl status sshd

# 檢查防火牆
sudo ufw status

# 允許 SSH（如果被阻止）
sudo ufw allow 22/tcp
```

---

## 安全建議

1. **限制 `/restart` 命令權限**
   - 確保 `ALLOWED_USERS` 僅包含可信用戶
   - 檢查審計日誌：`SELECT * FROM audit_log WHERE event_type='restart'`

2. **SSH 安全**
   - 使用 SSH 密鑰認證（禁用密碼）
   - 修改 SSH 預設端口
   - 配置防火牆僅允許特定 IP

3. **監控告警**
   - 配置 systemd 失敗通知：`OnFailure=notify-admin.service`
   - 使用 Telegram bot 發送重啟通知（已實現）
