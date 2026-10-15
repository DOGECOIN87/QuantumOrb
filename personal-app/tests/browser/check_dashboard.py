"""Optional real-browser release check. Run against a loopback fixture server."""

import argparse
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--browser", default="/usr/bin/google-chrome")
    parser.add_argument(
        "--screenshots", type=Path, default=Path("/tmp/quantumorb-dashboard-review")
    )
    args = parser.parse_args()
    args.screenshots.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=args.browser if Path(args.browser).is_file() else None,
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            viewport={"width": 1440, "height": 1050}, reduced_motion="reduce", accept_downloads=True
        )
        page = context.new_page()
        errors = []
        external = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "request",
            lambda request: (
                external.append(request.url) if not request.url.startswith(args.base_url) else None
            ),
        )
        page.goto(f"{args.base_url}/?source=demo", wait_until="networkidle")
        expect(page.locator("#desk-events")).to_have_text("96")
        expect(page.locator("#toggle-motion")).to_have_text("MOTION OFF")
        expect(page.locator(".intro")).to_have_count(0)
        expect(page.locator("#data-notice")).to_contain_text("SAMPLE WORKSPACE")
        assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
        page.screenshot(path=str(args.screenshots / "desktop.png"), full_page=True)
        page.get_by_role("link", name="TOKENS", exact=True).click()
        expect(page.locator("#token-rows tr")).to_have_count(15)
        page.get_by_role("button", name="NEXT →", exact=True).click()
        expect(page.locator("#token-rows tr")).to_have_count(3)
        page.get_by_role("button", name="← PREVIOUS", exact=True).click()
        page.locator("#lifecycle-filter").select_option("pool_active")
        expect(page.locator("#tokens-total")).to_have_text("3 TOKENS")
        page.locator("#lifecycle-filter").select_option("")
        expect(page.locator("#tokens-total")).to_have_text("18 TOKENS")
        page.locator("#token-rows .address-button").first.click()
        expect(page.locator("#detail-dialog")).to_be_visible()
        page.get_by_role("button", name="EVIDENCE", exact=True).click()
        expect(page.locator("#detail-evidence")).to_contain_text("Partial / missing history")
        page.get_by_role("button", name="RELATIONSHIPS", exact=True).click()
        expect(page.locator(".graph-visual")).to_be_visible()
        with page.expect_download() as download:
            page.get_by_role("button", name="↓ EXPORT TOKEN GRAPH").click()
        exported = json.loads(Path(download.value.path()).read_text())
        assert exported["source"] == "sample_data" and exported["redacted"]
        node_ids = {item["id"] for item in exported["nodes"]}
        assert all(
            edge[key] in node_ids
            for edge in exported["edges"]
            for key in ("source_node_id", "target_node_id")
        )
        page.screenshot(path=str(args.screenshots / "token-graph.png"))
        page.keyboard.press("Escape")
        expect(page.locator("#detail-dialog")).not_to_be_visible()
        page.get_by_role("link", name="ACTIVITY", exact=True).click()
        page.locator("#finality-filter").select_option("provisional")
        expect(page.locator("#activity-total")).to_have_text("11 EVENTS")
        page.get_by_role("link", name="TRADERS", exact=True).click()
        expect(page.locator("#wallet-rows tr")).to_have_count(15)
        page.locator("#wallet-rows .row-action").first.click()
        expect(page.locator("#page-activity")).to_be_visible()
        expect(page.locator("#activity-search")).not_to_have_value("")
        page.locator("#activity-search").fill("22222222222222222")
        expect(page.locator("#activity-total")).to_have_text("0 EVENTS")
        page.locator("#activity-search").fill("<script>")
        expect(page.locator("#activity-filter-message")).to_contain_text("Base58")
        page.goto(f"{args.base_url}/?source=demo#agents", wait_until="networkidle")
        expect(page.locator(".agent-card")).to_have_count(6)
        expect(page.locator("#agent-executor")).to_contain_text("Execution is disabled")
        page.goto(f"{args.base_url}/?source=demo#system", wait_until="networkidle")
        expect(page.locator("#system-facts")).to_contain_text("not read")
        page.goto(f"{args.base_url}/?source=demo#guide", wait_until="networkidle")
        expect(page.locator("#guide-heading")).to_be_visible()

        # This local server must have the three checked-in Scout fixtures replayed
        # and an offline analysis for their shared mint (README instructions).
        page.goto(f"{args.base_url}/?source=local#tokens", wait_until="networkidle")
        expect(page.locator("#token-rows tr")).to_have_count(1)
        page.locator("#token-rows .address-button").first.click()
        expect(page.locator("#detail-content")).to_contain_text(
            "7LSsEoJGhLeZzGvDofTdNg7M3JttxQqGWNLo6vWMpump"
        )
        page.get_by_role("button", name="EVIDENCE", exact=True).click()
        expect(page.locator("#detail-evidence")).to_contain_text("Partial / missing history")
        page.keyboard.press("Escape")
        assert not errors, errors
        assert not external, external

        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(f"{args.base_url}/?source=demo", wait_until="networkidle")
        assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
        page.screenshot(path=str(args.screenshots / "mobile.png"), full_page=True)
        page.get_by_role("button", name="Open navigation").click()
        page.get_by_role("link", name="TOKENS", exact=True).click()
        expect(page.locator("#page-tokens")).to_be_visible()
        assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
        page.locator("#token-rows .address-button").first.click()
        expect(page.locator("#detail-dialog")).to_be_visible()
        page.keyboard.press("Escape")

        # HTTP failures must stay unavailable; the UI must not manufacture zeros.
        page.route(
            "**/api/dashboard",
            lambda route: route.fulfill(
                status=503, content_type="application/json", body='{"detail":"unavailable"}'
            ),
        )
        page.goto(f"{args.base_url}/?source=local", wait_until="networkidle")
        expect(page.locator("#data-notice")).to_contain_text("Dashboard unavailable")
        expect(page.locator("#desk-events")).to_have_text("—")
        page.get_by_role("button", name="TRY SAMPLE DATA", exact=True).click()
        expect(page.locator("#desk-events")).to_have_text("96")
        expect(page.locator("#data-notice")).to_contain_text("SAMPLE WORKSPACE")
        assert not errors, errors
        browser.close()
    print(
        "PASS: desktop/mobile, local SQLite data, sample isolation, filters, pagination, "
        "evidence graphs, downloads, reduced motion and unavailable-state recovery."
    )


if __name__ == "__main__":
    main()
