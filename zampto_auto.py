#!/usr/bin/env python3
"""
Zampto Auto Renewal — CloakBrowser-based automation.

Logs in via Logto, checks server status, starts if stopped,
clicks renewal, waits for Cloudflare Turnstile, then pushes
results via Telegram Bot.
"""

import os
import re
import sys
import json
import time
import logging
from datetime import datetime, timezone

import requests
from cloakbrowser import CloakBrowser

# ── Config ──────────────────────────────────────────────────────────────

USERNAME = os.getenv("ZAMPTO_USERNAME", "").strip()
PASSWORD = os.getenv("ZAMPTO_PASSWORD", "").strip()
# 清洗 SERVER_ID，防止意外夾帶空格或換行符 (\n) 導致網址拼接出錯
SERVER_ID = "".join(os.getenv("ZAMPTO_SERVER_ID", "").strip().split())

# 獲取 TG 變數
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()

FORCE_RENEW = os.getenv("FORCE_RENEW", "false").lower() == "true"
DASHBOARD_URL = "https://zampto.net"

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("zampto")


# ── Telegram Notification ───────────────────────────────────────────────

def push_telegram(title: str, body: str):
    """
    發送 Telegram 機器人通知 (已修正 API 網址域名與路徑拼接問題)
    """
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log.warning("Telegram Bot not configured, skipping notification")
        return
    
    # 徹底防止 Token 拼接錯誤：過濾使用者可能填入的網址、bot字眼及斜線
    token = TG_BOT_TOKEN
    if "api.telegram.org" in token:
        token = token.split("/bot")[-1].split("/")
    if token.lower().startswith("bot"):
        token = token[3:]
    token = token.strip("/")
        
    # 修正：官方 API 域名為 api.telegram.org 且必須帶有 /bot 前綴
    url = f"https://telegram.org{token}/sendMessage"
    formatted_text = f"*{title}*\n\n{body}"
    
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": formatted_text,
        "parse_mode": "Markdown",
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        r.raise_for_status()
        log.info("Telegram notification sent successfully")
    except Exception as e:
        log.error("Telegram notification failed: %s", e)


# ── Helpers ─────────────────────────────────────────────────────────────

def wait_for(page, selector: str, timeout: float = 30.0, label: str = "element"):
    try:
        page.wait_for_selector(selector, timeout=timeout * 1000)
        log.info("Found %s: %s", label, selector)
        return True
    except Exception:
        log.warning("Timeout waiting for %s: %s", label, selector)
        return False


def screenshot(page, name: str, path: str = "./screenshots"):
    os.makedirs(path, exist_ok=True)
    filepath = os.path.join(path, name)
    page.screenshot(path=filepath)
    log.info("Screenshot saved: %s", filepath)
    return filepath


def parse_expiry(text: str):
    """Extract remaining time from a string like '1 day 23h 53m'."""
    text = text.strip()
    days = h = m = 0
    dm = re.search(r"(\d+)\s*day", text)
    hm = re.search(r"(\d+)\s*h", text)
    mm = re.search(r"(\d+)\s*m", text)
    if dm:
        days = int(dm.group(1))
    if hm:
        h = int(hm.group(1))
    if mm:
        m = int(mm.group(1))
    total_hours = days * 24 + h
    log.info("Parsed expiry: %d days %d hours %d min (Total: %d hours)", days, h, m, total_hours)
    return days, h, m, total_hours


def solve_cloudflare_turnstile_if_any(page):
    """通用 Cloudflare Turnstile 輔助處理器"""
    try:
        cf_iframe = page.query_selector('iframe[src*="://cloudflare.com"], iframe[src*="cloudflare-static"]')
        if cf_iframe:
            log.info("🛡️ Found Cloudflare Turnstile iframe. Executing safe human click simulation...")
            time.sleep(2)
            cf_frame = cf_iframe.content_frame()
            if cf_frame:
                checkbox = cf_frame.query_selector('input[type="checkbox"], .mark, #challenge-stage')
                if checkbox:
                    checkbox.click(position={"x": 25, "y": 25})
                else:
                    cf_iframe.click(position={"x": 40, "y": 40})
                
                log.info("⏳ Waiting for Turnstile Response token to update...")
                for _ in range(15):
                    time.sleep(1)
                    token_input = page.query_selector('input[name="cf-turnstile-response"]')
                    if token_input:
                        token_val = token_input.evaluate('el => el.value')
                        if token_val and len(token_val) > 10:
                            log.info("✅ Cloudflare Turnstile verified via token check!")
                            return True
    except Exception as e:
        log.debug("Turnstile optimizer skipped or not present: %s", e)
    return False


# ── Main automation ─────────────────────────────────────────────────────

def main():
    if not all([USERNAME, PASSWORD, SERVER_ID]):
        log.error("Missing required env vars: ZAMPTO_USERNAME, ZAMPTO_PASSWORD, ZAMPTO_SERVER_ID")
        sys.exit(1)

    log.info("=== Zampto Auto Renewal ===")
    log.info("Server ID: %s  |  Force: %s", SERVER_ID, FORCE_RENEW)

    report = {
        "server_id": SERVER_ID,
        "status": "unknown",
        "action": "none",
        "expiry": None,
        "error": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    browser = None
    try:
        # ── 1. Launch CloakBrowser ──────────────────────────────────────
        browser = CloakBrowser(headless=True, geoip=True)
        page = browser.get_page()
        page.set_viewport_size({"width": 1280, "height": 720})

        # ── 2. Navigate to Zampto Dashboard ─────────────────────────────
        log.info("Navigating to %s", DASHBOARD_URL)
        page.goto(DASHBOARD_URL, wait_until="networkidle")
        time.sleep(2)
        screenshot(page, "01_dashboard.png")

        # ── 3. Detect login page & handle Logto ─────────────────────────
        if "login" in page.url.lower() or "auth" in page.url.lower():
            log.info("Login page detected")
            screenshot(page, "02_login.png")

            # Step 1: Enter email/username
            email_sel = "input[name='email'], input[type='email'], input[name='username']"
            if wait_for(page, email_sel, 15, "email input"):
                email_input = page.query_selector(email_sel)
                email_input.fill(USERNAME)
                email_input.press("Enter")
                time.sleep(1)

            # Step 2: Enter password
            if wait_for(page, "input[type='password']", 15, "password input"):
                pwd_input = page.query_selector("input[type='password']")
                pwd_input.fill(PASSWORD)
                
                # 登入前嘗試點擊潛在的登入頁 CF 盾
                solve_cloudflare_turnstile_if_any(page)
                
                pwd_input.press("Enter")
                log.info("Credentials submitted. Waiting for automatic OAuth callback...")
                
                # 核心優化點：強制等待 URL 自動重定向回儀表板首頁，確保 Token 寫入完全，杜絕 404
                try:
                    page.wait_for_url("https://zampto.net/**", timeout=20000)
                    log.info("✅ OAuth redirection complete. Secured Page URL: %s", page.url)
                except Exception:
                    log.warning("Redirect timeout. Proceeding to target routing anyway...")
                
                time.sleep(3)

            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            screenshot(page, "03_post_login.png")
        else:
            log.info("Already logged in, skipping auth block.")

        # ── 4. Navigate to server detail（移出 if-else 分支，杜絕 404） ──
        server_url = f"{DASHBOARD_URL}/server?id={SERVER_ID}"
        log.info("Redirecting to target server detail page: %s", server_url)
        
        try:
            page.goto(server_url, wait_until="networkidle", timeout=25000)
        except Exception:
            page.goto(server_url, wait_until="load")
            
        time.sleep(4)
        screenshot(page, "04_server_detail.png")

        # 404 雙重安全自我修復機制
        is_404 = page.query_selector("text=/404|not found/i") is not None or "404" in page.url.lower()
        if is_404 or "login" in page.url.lower():
            log.warning("⚠️ Access restriction or 404 page encountered! Executing hard refresh to lock state...")
            time.sleep(2)
            page.reload(wait_until="networkidle")
            time.sleep(4)
            screenshot(page, "04_server_detail_retry.png")

        # ── 5. Determine server status ─────────────────────────────────
        status_text = ""
        for cls in ["status-running", "status-stopped", "status-starting", "status-stopping"]:
            el = page.query_selector(f".{cls}")
            if el:
                status_text = el.inner_text()
                break
        
        # 修正：將相容性差的 text=/Regex/i 替換為標準 XPath 寫法
        if not status_text:
            el = page.query_selector("//*[contains(translate(text(), 'RUNNING', 'running'), 'running') or contains(translate(text(), 'STOPPED', 'stopped'), 'stopped')]")
            if el:
                status_text = el.inner_text()

        is_running = "running" in status_text.lower() if status_text else False
        report["status"] = "running" if is_running else "stopped"
        log.info("Server status: %s", report["status"])

        # ── 6. Start server if stopped ──────────────────────────────────
        if not is_running:
            log.info("Server is stopped — clicking Start")
            start_btn = page.query_selector("button:has-text('Start'), button:has-text('start'), .btn-start")
            if start_btn:
                start_btn.click()
                time.sleep(3)
                try:
                    page.wait_for_load_state("networkidle", timeout=20000)
                except Exception:
                    pass
                screenshot(page, "05_server_started.png")
                report["action"] = "started"
                log.info("Server start clicked")
            else:
                log.warning("Start button not found")

        # ── 7. Check expiry & handle renewal ────────────────────────────
        # 修正：向外擴大獲取範圍（/..），確保完整讀取包含時間的文字區塊，防止解析錯誤
        expiry_el = page.query_selector("//*[contains(text(), 'Expiry') or contains(text(), 'Renew') or contains(text(), '到期') or contains(text(), '剩余')]/..")
        if not expiry_el:
            expiry_el = page.query_selector("body")  # 保底

        if expiry_el:
            expiry_text = expiry_el.inner_text()
            report["expiry"] = expiry_text.split('\n')[0]  # 簡化記錄防通知過長
            log.info("Expiry info section loaded.")

            days, hours, mins, total_h = parse_expiry(expiry_text)

            # 保底防護：若匹配完全失敗且非強制續期，預設進行安全續期點擊
            if total_h == 0 and not FORCE_RENEW and "day" not in expiry_text.lower() and "h" not in expiry_text.lower():
                log.warning("Numeric expiry values not found. Defaulting to true for verification.")
                should_renew = True
            else:
                should_renew = FORCE_RENEW or total_h < 48

            if should_renew:
                log.info("Initiating renewal (days=%d, hours=%d, force=%s)", days, hours, FORCE_RENEW)
                report["action"] = "renewed"

                renew_btn = page.query_selector(
                    "button:has-text('Renew'), button:has-text('Renewal'), "
                    "button:has-text('续期'), .renew-btn, .btn-renew"
                )
                if renew_btn:
                    renew_btn.click()
                    time.sleep(3)

                    log.info("Waiting for Cloudflare Turnstile on dialog...")
                    wait_for(page, "[data-sitekey], .cf-turnstile, iframe[src*='cloudflare']", 30, "turnstile")
                    
                    # 處理續期對話框中可能二次出現的 Turnstile 盾
                    solve_cloudflare_turnstile_if_any(page)
                    time.sleep(5)

                    confirm = page.query_selector("button:has-text('Confirm'), button:has-text('OK'), button:has-text('确定')")
                    if confirm:
                        confirm.click()
                        time.sleep(3)

                    try:
                        page.wait_for_load_state("networkidle", timeout=20000)
                    except Exception:
                        pass
                    screenshot(page, "06_after_renew.png")

                    expiry_el2 = page.query_selector("//*[contains(text(), 'Expiry') or contains(text(), '到期')]/..")
                    if expiry_el2:
                        new_expiry = expiry_el2.inner_text()
                        log.info("New expiry text: %s", new_expiry)
                        report["expiry"] = new_expiry.split('\n')[0]
                else:
                    log.warning("Renew button not found")
                    report["action"] = "renew-failed"
                    report["error"] = "Renew button not found on page"
            else:
                log.info("No renewal needed (total_hours=%d)", total_h)
                report["action"] = "skipped"
        else:
            log.warning("Expiry element not found")
            report["error"] = "Expiry element not found"

        screenshot(page, "07_final.png")

    except Exception as e:
        report["error"] = str(e)
        log.exception("Automation failed: %s", e)
    finally:
        if browser:
            browser.close()

    # ── 8. Build & send notification（改回 Telegram 模組） ────────────────
    status_icon = "🟢" if report["status"] == "running" else "🔴"
    action_icon = {
        "started": "▶️",
        "renewed": "✅",
        "skipped": "⏭️",
        "renew-failed": "❌",
        "none": "—",
    }.get(report["action"], "❓")

    body_lines = [
        f"🖥️ **Zampto Server Report**",
        f"",
        f"**Server ID:** `{SERVER_ID}`",
        f"**Status:** {status_icon} {report['status'].title()}",
        f"**Action:** {action_icon} {report['action']}",
    ]
    if report.get("expiry"):
        body_lines.append(f"**Expiry Info:** `{str(report['expiry']).strip()}`")
    if report.get("error"):
        body_lines.append(f"**⚠️ Error:** {report['error']}")
    body_lines.append(f"")
    body_lines.append(f"_Generated: {report['timestamp']}_")

    body = "\n".join(body_lines)
    log.info("--- Report ---\n%s", body)
    
    # 呼叫 TG 推送
    push_telegram("🖥️ Zampto Server Report", body)

    with open("./screenshots/report.json", "w") as f:
        json.dump(report, f, indent=2)
    log.info("Report saved to ./screenshots/report.json")


if __name__ == "__main__":
    main()
