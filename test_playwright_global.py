from playwright.sync_api import sync_playwright

GLOBAL_BROWSER = None
GLOBAL_PW = None

def get_browser():
    global GLOBAL_BROWSER, GLOBAL_PW
    if GLOBAL_BROWSER is None:
        GLOBAL_PW = sync_playwright().start()
        GLOBAL_BROWSER = GLOBAL_PW.chromium.launch_persistent_context(
            user_data_dir="./test_profile",
            headless=False
        )
    return GLOBAL_BROWSER

b = get_browser()
p = b.new_page()
p.goto("https://example.com")
print(p.title())
