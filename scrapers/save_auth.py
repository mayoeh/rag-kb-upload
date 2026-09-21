#!/usr/bin/env python3
"""
Automate NetBadge login in headless mode — no display required.
Run this on the HPC server. You will be prompted for your UVA
computing ID and password; then approve the Duo push on your phone.

Usage:
    python scrapers/save_auth.py
"""

import getpass
import json
from playwright.sync_api import sync_playwright

TARGET = "https://rci.hpc.virginia.edu/wiki/Special:AllPages"
OUTPUT = "scrapers/auth_cookies.json"

USERNAME_SEL = "input[name='j_username'], input[name='username'], input[id='username']"
PASSWORD_SEL = "input[name='j_password'], input[name='password'], input[id='password']"

print("=" * 50)
print("UVA NetBadge Login (headless)")
print("=" * 50)
username = input("UVA Computing ID : ")
password = getpass.getpass("Password         : ")
print()
print("Launching headless browser ...")


def fill_credentials(page, username, password, label=""):
    """Fill username/password and submit by pressing Enter on the password field."""
    page.locator(USERNAME_SEL).first.fill(username)
    filled = page.locator(USERNAME_SEL).first.input_value()
    print(f"  {label}Username filled : '{filled}'")
    page.locator(PASSWORD_SEL).first.fill(password)
    # Press Enter on the password field — submits the password form only,
    # not the certificate-login button which is a separate form/button.
    page.locator(PASSWORD_SEL).first.press("Enter")
    page.wait_for_load_state("domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)
    print(f"  {label}URL after submit : {page.url}")


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx  = browser.new_context()
    page = ctx.new_page()

    page.goto(TARGET, timeout=30000)
    page.wait_for_load_state("domcontentloaded", timeout=20000)
    page.wait_for_timeout(2000)

    # ── Step 1: handle certificate-auth failure page ───────────────────────
    for _ in range(4):
        if page.locator("#mw-content-text").count() > 0:
            break  # already on the wiki

        if "my.policy" in page.url or "Certificate Login Failed" in page.content():
            print("  Certificate auth failed page — clicking Continue ...")
            page.locator(
                "a:has-text('Continue'), input[value='Continue'], button:has-text('Continue')"
            ).first.click()
            page.wait_for_load_state("domcontentloaded", timeout=20000)
            page.wait_for_timeout(2000)
            print(f"  Now at: {page.url}")

        if page.locator(USERNAME_SEL).count() > 0:
            fill_credentials(page, username, password, label="[login] ")
            break

    # ── Step 2: handle Duo 2FA ─────────────────────────────────────────────
    if page.locator("#mw-content-text").count() == 0:
        page.wait_for_timeout(3000)
        print(f"  Pre-Duo URL   : {page.url}")
        print(f"  Pre-Duo title : {page.title()}")
        body = page.locator("body").inner_text()
        print(f"  Pre-Duo body  :\n{body[:1000]}\n")
        page.screenshot(path="scrapers/debug_duo.png")
        print("  Screenshot → scrapers/debug_duo.png")

        # Duo Universal Prompt sends the push automatically — just wait for approval.
        # (No button click needed; the push arrives on the phone automatically.)
        print()
        print("Duo push sent automatically — approve it on your phone (up to 90 seconds) ...")

    # ── Step 3: wait for wiki — print status every 5 s ───────────────────
    import time
    print("Waiting for wiki to load ...", flush=True)
    deadline = time.time() + 120
    while time.time() < deadline:
        cur_url = page.url
        # Success: on the wiki (any page, with or without #mw-content-text)
        if "rci.hpc.virginia.edu/wiki/" in cur_url and \
           "shibidp" not in cur_url and "duosecurity" not in cur_url:
            break
        print(f"  [{int(deadline - time.time())}s left] URL: {page.url} | title: {page.title()}", flush=True)
        # Handle any "Trust this device?" or "Stay signed in?" prompt
        for btn_text in ["Trust this browser", "Stay signed in", "Yes", "Continue", "Proceed"]:
            btn = page.locator(f"button:has-text('{btn_text}'), input[value='{btn_text}']")
            if btn.count() > 0:
                print(f"  Clicking intermediate button: '{btn_text}'")
                btn.first.click()
                page.wait_for_load_state("domcontentloaded", timeout=15000)
                break
        page.wait_for_timeout(5000)
    else:
        print(f"[ERROR] Timed out. Final URL: {page.url}")
        print(f"Final title: {page.title()}")
        print(f"Final body:\n{page.locator('body').inner_text()[:1000]}")
        page.screenshot(path="scrapers/debug_final.png")
        browser.close()
        raise SystemExit(1)
    print("Login successful!")

    cookies = ctx.cookies()
    with open(OUTPUT, "w") as f:
        json.dump(cookies, f, indent=2)

    print(f"Saved {len(cookies)} cookies → {OUTPUT}")
    browser.close()
