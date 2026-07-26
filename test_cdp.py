from playwright.sync_api import sync_playwright

def test():
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        print("Connected to:", browser)
        print("Contexts:", browser.contexts)
        try:
            page = browser.contexts[0].new_page()
            page.goto("https://example.com")
            print("Title:", page.title())
            page.close()
        except Exception as e:
            print("Error new_page:", e)
        browser.disconnect()

test()
