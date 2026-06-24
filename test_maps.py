"""Standalone live test: run the maps.json scrape against one domain.

Builds a single ungoogled-chromium instance (reusing maps_checker's driver +
scrape logic), scrapes the given domain, and prints the extracted fields +
website_match. Use --debug to dump the place-panel HTML so you can verify /
iterate the selectors in maps.json against the live Google Maps DOM.

Run:
  bash tools/setup_vendor.sh                 # one-time: download ungoogled-chromium
  python3 test_maps.py botxbyte.com
  python3 test_maps.py imperialpalace.in --headless
  python3 test_maps.py imperialpalace.in --debug      # dump panel HTML
"""
import sys

import maps_checker as m


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if not args:
        print("usage: python3 test_maps.py <domain> [--headless] [--debug]")
        sys.exit(1)
    domain = args[0]
    headless = "--headless" in flags
    debug = "--debug" in flags

    chrome_bin = m.find_chrome_binary()
    version_main = m.detect_chrome_major(chrome_bin)
    print(f"[*] chrome: {chrome_bin}")
    print(f"[*] scraping: {domain}")

    driver, profile = m.build_driver(0, headless=headless, chrome_binary=chrome_bin,
                                     version_main=version_main, proxy=None)
    try:
        m._prime_consent(driver)
        row = m.scrape_domain(driver, domain)
        print("  ---- result ----")
        for k in ("status", "maps_name", "maps_address", "maps_website",
                  "maps_website_match", "maps_rating", "maps_review_count",
                  "elapsed_seconds", "error"):
            if k in row:
                print(f"  {k:18}: {row.get(k)}")

        if debug:
            try:
                html = driver.execute_script(
                    "var m=document.querySelector('div[role=\"main\"]');"
                    "return m ? m.outerHTML.slice(0, 20000) : document.body.innerHTML.slice(0, 20000);"
                )
                with open("test-maps-panel.html", "w", encoding="utf-8") as fh:
                    fh.write(html or "")
                print("  [debug] wrote test-maps-panel.html (panel HTML, 20k chars)")
            except Exception as e:
                print(f"  [debug] could not dump panel: {e}")
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        m._remove_profile(profile)


if __name__ == "__main__":
    main()
