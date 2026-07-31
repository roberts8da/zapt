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
import asyncio
from datetime import datetime, timezone

import requests
# 這裡修改了導入方式：使用最新官方標準的 launch 函數
from cloakbrowser import launch

# ── Config ──────────────────────────────────────────────────────────────

USERNAME = os.getenv("ZAMPTO_USERNAME", "")
PASSWORD = os.getenv("ZAMPTO_PASSWORD", "")
SERVER_ID = os.getenv("ZAMPTO_SERVER_ID", "")

# 已更換為 TG 相關環境變數
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")

FORCE_RENEW = os.getenv("FORCE_RENEW", "false").lower() == "true"
DASHBOARD_URL = "https://zampto.net"

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("zampto")


# ── Telegram Notification ───────────────────────────────────────────────

def push_telegram(title: str, body: str):
    """
    將原來的 WxPusher 改為 Telegram 機器人推送
    """
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log.warning("Telegram Bot not configured, skipping notification")
        return
    
    url = f"https://telegram.org{TG_BOT_TOKEN}/sendMessage"
    
    # 組合標題與內文，使用 Markdown 格式美化
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

async def wait_for(page, selector: str, timeout: float = 30.0, label: str = "element"):
    try:
        await page.wait_for_selector(selector, timeout=timeout * 1000)
        log.info("Found %s: %s", label, selector)
        return True
    except Exception:
        log.warning("Timeout waiting for %s: %s", label, selector)
        return False


async def screenshot(page, name: str, path: str = "./screenshots"):
    os.makedirs(path, exist_ok=True)
    filepath = os.path.join(path, name)
    await page.screenshot(path=filepath)
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
    log.info("Parsed expiry: %d days %d hours %d min", days, h, m)
    return days, h, m, total_hours


# ── Main automation ─────────────────────────────────────────────────────

async def run_automation():
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
        # ── 1. Launch CloakBrowser (使用最新非同步官方調用規範) ─────────────────
        browser = await launch(headless=True, geoip=True)
        page = await browser.new_page()

        # ── 2. Navigate to Zampto Dashboard ─────────────────────────────
        log.info("Navigating to %s", DASHBOARD_URL)
        await page.goto(DASHBOARD_URL, wait_until="networkidle")
        await asyncio.sleep(2)
        await screenshot(page, "01_dashboard.png")

        # ── 3. Detect login page & handle Logto ─────────────────────────
        if "login" in page.url.lower():
            log.info("Login page detected")
            await screenshot(page, "02_login.png")

            # Step 1: Enter email/username
            if await wait_for(page, "input[name='email'], input[type='email'], input[name='username']", 15, "email input"):
                email_input = await page.query_selector("input[name='email'], input[type='email'], input[name='username']")
                await email_input.fill(USERNAME)
                await email_input.press("Enter")
                await asyncio.sleep(1)

            # Step 2: Enter password
            if await wait_for(page, "input[type='password']", 15, "password input"):
                pwd_input = await page.query_selector("input[type='password']")
                await pwd_input.fill(PASSWORD)
                await pwd_input.press("Enter")
                await asyncio.sleep(3)

            # Check for 2FA / OTP
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            await screenshot(page, "03_post_login.png")

            # ── 4. Navigate to server detail ────────────────────────────
            server_url = f"{DASHBOARD_URL}/server?id={SERVER_ID}"
            await page.goto(server_url, wait_until="networkidle")
            await asyncio.sleep(2)
            await screenshot(page, "04_server_detail.png")
        else:
            # Already logged in, go directly to server
            server_url = f"{DASHBOARD_URL}/server?id={SERVER_ID}"
            await page.goto(server_url, wait_until="networkidle")
            await asyncio.sleep(2)
            await screenshot(page, "04_server_detail.png")

        # ── 5. Determine server status ─────────────────────────────────
        status_text = ""
        for cls in ["status-running", "status-stopped", "status-starting", "status-stopping"]:
            el = await page.query_selector(f".{cls}")
            if el:
                status_text = await el.inner_text()
                break
        if not status_text:
            el = await page.query_selector("text=/Running|Stopped|Starting|Stopping/i")
            if el:
                status_text = await el.inner_text()

        is_running = "running" in status_text.lower() if status_text else False
        report["status"] = "running" if is_running else "stopped"
        log.info("Server status: %s", report["status"])

        # ── 6. Start server if stopped ──────────────────────────────────
        if not is_running:
            log.info("Server is stopped — clicking Start")
            start_btn = await page.query_selector("button:has-text('Start'), button:has-text('start'), .btn-start")
            if start_btn:
                await start_btn.click()
                await asyncio.sleep(3)
                try:
                    await page.wait_for_load_state("networkidle", timeout=20000)
                except Exception:
                    pass
                await screenshot(page, "05_server_started.png")
                report["action"] = "started"
                log.info("Server start clicked")
            else:
                log.warning("Start button not found")

        # ── 7. Check expiry & handle renewal ────────────────────────────
        expiry_el = await page.query_selector("text=/Expiry|Renew|到期|剩余/i")
        if expiry_el:
            expiry_text = await expiry_el.inner_text()
            report["expiry"] = expiry_text
            log.info("Expiry info: %s", expiry_text)

            days, hours, mins, total_h = parse_expiry(expiry_text)

            should_renew = FORCE_RENEW or total_h < 48
            if should_renew:
                log.info("Initiating renewal (days=%d, hours=%d, force=%s)", days, hours, FORCE_RENEW)
                report["action"] = "renewed"

                renew_btn = await page.query_selector(
                    "button:has-text('Renew'), button:has-text('Renewal'), "
                    "button:has-text('续期'), .renew-btn, .btn-renew"
                )
                if renew_btn:
                    await renew_btn.click()
                    await asyncio.sleep(2)

                    # Wait for Cloudflare Turnstile
                    log.info("Waiting for Cloudflare Turnstile...")
                    await wait_for(page, "[data-sitekey], .cf-turnstile", 30, "turnstile")
                    await asyncio.sleep(8)

                    # Confirm renewal if dialog appears
                    confirm = await page.query_selector("button:has-text('Confirm'), button:has-text('OK'), button:has-text('确定')")
                    if confirm:
                        await confirm.click()
                        await asyncio.sleep(3)

                    try:
                        await page.wait_for_load_state("networkidle", timeout=20000)
                    except Exception:
                        pass
                    await screenshot(page, "06_after_renew.png")

                    # Re-read expiry
                    expiry_el2 = await page.query_selector("text=/Expiry|到期/i")
                    if expiry_el2:
                        new_expiry = await expiry_el2.inner_text()
                        log.info("New expiry: %s", new_expiry)
                        report["expiry"] = new_expiry
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

        await screenshot(page, "07_final.png")

    except Exception as e:
        report["error"] = str(e)
        log.exception("Automation failed: %s", e)
    finally:
        if browser:
            await browser.close()

    # ── 8. Build & send notification ────────────────────────────────────
    status_icon = "🟢" if report["status"] == "running" else "🔴"
    action_icon = {
        "started": "▶️",
        "renewed": "✅",
        "skipped": "⏭️",
        "renew-failed": "❌",
        "none": "—",
    }.get(report["action"], "❓")

    body_lines = [
        f"🖥️ Zampto Server Report",
        f"",
        f"Server ID: {SERVER_ID}",
        f"Status: {status_icon} {report['status'].title()}",
        f"Action: {action_icon} {report['action']}",
    ]

    if report.get("expiry"):
        body_lines.append(f"Expiry: {report['expiry']}")
    if report.get("error"):
        body_lines.append(f"⚠️ Error: {report['error']}")

    body_lines.append(f"")
    body_lines.append(f"Generated: {report['timestamp']}")

    body = "\n".join(body_lines)
    log.info("--- Report ---\n%s", body)

    push_telegram("🖥️ Zampto Server Report", body)

    with open("./screenshots/report.json", "w") as f:
        json.dump(report, f, indent=2)
    log.info("Report saved to ./screenshots/report.json")


if __name__ == "__main__":
    asyncio.run(run_automation())
