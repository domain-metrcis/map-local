"""Google Maps Pull-based Checker.

Each worker runs an ungoogled-chromium instance, pulls execution records from
the management pull endpoint:

  GET/POST /maps/   (maps.business-pool-queue)

For each domain it opens https://www.google.com/maps/search/<domain>, runs the
scrape script from maps.json in the page, extracts the business
name/address/website/rating/review_count, computes whether the listed website
matches the input domain, and posts the result back.

Unlike ahref-local there is NO cf-autoclick extension / master profile /
Cloudflare-Turnstile handling — Google Maps is not behind Turnstile. The only
browser dependency is ungoogled-chromium (vendored by tools/setup_vendor.sh).

Usage:
    python maps_checker.py [--api-url URL] [--headless] [--workers 5]
                           [--no-proxy] [--webshare-proxy]
                           [--chrome /path/to/chrome] [--proxies proxies.txt]
"""

import argparse
import atexit
import json
import os
import platform
import random
import shutil
import signal
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlparse

import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover
    from requests.packages.urllib3.util.retry import Retry  # type: ignore

WEBSHARE_API_KEY = os.environ.get("WEBSHARE_API_KEY", "9ulqjlekme1514dwfi7p6lwm9kmvygshn5brii5s")
WEBSHARE_PROXY_HOST = "p.webshare.io"
WEBSHARE_PROXY_PORT = "9999"
_WEBSHARE_AUTH_ID: Optional[int] = None

MAPS_JSON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "maps.json")
PROXIES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxies.txt")
DEFAULT_API_URL = "https://b-domain.articleinnovator.com/domain-metrics-management-service/api/v1"

ENDPOINT = "/maps/"
MAPS_SEARCH_URL = "https://www.google.com/maps/search/"

# Per-worker profile dirs created during this process — cleaned up on exit.
_CREATED_PROFILES: List[str] = []
_PROFILES_LOCK = threading.Lock()


# ----------------------------------------------------------------------------
# HTTP resilience
# ----------------------------------------------------------------------------

def _make_resilient_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=5, connect=5, read=5, status=5,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST", "PUT", "PATCH", "DELETE"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


_HTTP = _make_resilient_session()


# ----------------------------------------------------------------------------
# Webshare IP authorization
# ----------------------------------------------------------------------------

def webshare_get_my_ip() -> str:
    resp = requests.get("https://api.ipify.org", timeout=10)
    resp.raise_for_status()
    return resp.text.strip()


def webshare_authorize_ip(ip: str) -> int:
    global _WEBSHARE_AUTH_ID
    resp = requests.post(
        "https://proxy.webshare.io/api/v2/proxy/ipauthorization/",
        json={"ip_address": ip},
        headers={"Authorization": f"Token {WEBSHARE_API_KEY}"},
        timeout=15,
    )
    if resp.status_code == 400 and "already" in resp.text.lower():
        list_resp = requests.get(
            "https://proxy.webshare.io/api/v2/proxy/ipauthorization/",
            headers={"Authorization": f"Token {WEBSHARE_API_KEY}"},
            timeout=15,
        )
        list_resp.raise_for_status()
        for entry in list_resp.json().get("results", []):
            if entry.get("ip_address") == ip:
                _WEBSHARE_AUTH_ID = entry["id"]
                print(f"✅ IP {ip} already authorized (id={_WEBSHARE_AUTH_ID})", flush=True)
                return _WEBSHARE_AUTH_ID
        return 0
    resp.raise_for_status()
    _WEBSHARE_AUTH_ID = resp.json()["id"]
    print(f"✅ Authorized IP {ip} with Webshare (id={_WEBSHARE_AUTH_ID})", flush=True)
    return _WEBSHARE_AUTH_ID


def webshare_deauthorize_ip() -> None:
    global _WEBSHARE_AUTH_ID
    if not _WEBSHARE_AUTH_ID:
        return
    try:
        resp = requests.delete(
            f"https://proxy.webshare.io/api/v2/proxy/ipauthorization/{_WEBSHARE_AUTH_ID}/",
            headers={"Authorization": f"Token {WEBSHARE_API_KEY}"},
            timeout=15,
        )
        if resp.status_code == 204:
            print(f"✅ Deauthorized IP from Webshare (id={_WEBSHARE_AUTH_ID})", flush=True)
    except Exception as e:
        print(f"⚠️  Failed to deauthorize IP: {e}", flush=True)
    _WEBSHARE_AUTH_ID = None


# ----------------------------------------------------------------------------
# Per-worker profile management (plain profiles — no extension)
# ----------------------------------------------------------------------------

def _create_profile(worker_id: int) -> str:
    profile_id = f"maps_w{worker_id}_{uuid.uuid4().hex[:8]}"
    dest = os.path.join(tempfile.gettempdir(), profile_id)
    os.makedirs(dest, exist_ok=True)
    with _PROFILES_LOCK:
        _CREATED_PROFILES.append(dest)
    return dest


def _remove_profile(path: Optional[str]) -> None:
    if not path:
        return
    try:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass
    with _PROFILES_LOCK:
        try:
            _CREATED_PROFILES.remove(path)
        except ValueError:
            pass


def _global_profile_cleanup() -> None:
    with _PROFILES_LOCK:
        paths = list(_CREATED_PROFILES)
        _CREATED_PROFILES.clear()
    for p in paths:
        try:
            shutil.rmtree(p, ignore_errors=True)
        except Exception:
            pass


atexit.register(_global_profile_cleanup)


def _install_signal_handlers() -> None:
    def _handler(signum, _frame):
        try:
            webshare_deauthorize_ip()
            _global_profile_cleanup()
        finally:
            sys.exit(128 + signum)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            pass


# ----------------------------------------------------------------------------
# Driver build
# ----------------------------------------------------------------------------

_DRIVER_BUILD_LOCK_PATH = "/tmp/uc_driver_build_maps.lock"
_driver_build_lock = threading.Lock()
UC_CACHE_DIR = os.path.expanduser("~/.local/share/undetected_chromedriver")
_UC_BIN_BACKUP = "/tmp/uc_chromedriver_maps.backup"


def _acquire_driver_build_lock(timeout: float = 60.0) -> Optional[Any]:
    try:
        import fcntl
    except ImportError:
        return None
    deadline = time.time() + timeout
    fh = open(_DRIVER_BUILD_LOCK_PATH, "w")
    while time.time() < deadline:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except BlockingIOError:
            time.sleep(0.5)
    fh.close()
    return None


def _release_driver_build_lock(fh: Optional[Any]) -> None:
    if fh is None:
        return
    try:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        fh.close()
    except Exception:
        pass


def _ensure_uc_binary_present() -> None:
    try:
        os.makedirs(UC_CACHE_DIR, exist_ok=True)
    except Exception:
        return
    canonical = os.path.join(UC_CACHE_DIR, "undetected_chromedriver")
    if os.path.exists(canonical):
        return
    if os.path.exists(_UC_BIN_BACKUP):
        try:
            shutil.copy2(_UC_BIN_BACKUP, canonical)
            os.chmod(canonical, 0o755)
        except Exception:
            pass


def _snapshot_uc_binary() -> None:
    canonical = os.path.join(UC_CACHE_DIR, "undetected_chromedriver")
    if not os.path.exists(canonical):
        return
    try:
        if os.path.exists(_UC_BIN_BACKUP) and os.path.getsize(canonical) == os.path.getsize(_UC_BIN_BACKUP):
            return
        shutil.copy2(canonical, _UC_BIN_BACKUP)
    except Exception:
        pass


def find_chrome_binary() -> Optional[str]:
    system = platform.system()
    candidates: List[str] = []
    if system == "Linux":
        script_dir = os.path.dirname(os.path.abspath(__file__))
        candidates = [
            os.path.join(script_dir, "vendor", "ungoogled-chromium", "chrome"),
            "/usr/bin/ungoogled-chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/google-chrome",
        ]
    elif system == "Darwin":
        candidates = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    elif system == "Windows":
        for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env, "")
            if base:
                candidates.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def detect_chrome_major(chrome_binary: Optional[str]) -> Optional[int]:
    import subprocess
    import re
    if not chrome_binary:
        return None
    try:
        out = subprocess.check_output([chrome_binary, "--version"], text=True, timeout=5)
        m = re.search(r"(\d+)\.", out)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def build_driver(worker_id: int, headless: bool, chrome_binary: Optional[str],
                 version_main: Optional[int], proxy: Optional[str] = None):
    """Build a uc.Chrome driver. Returns (driver, profile_path)."""
    import undetected_chromedriver as uc

    file_lock = _acquire_driver_build_lock(timeout=120.0)
    with _driver_build_lock:
        try:
            return _build_driver_locked(worker_id, headless, chrome_binary, version_main, proxy, uc)
        finally:
            _release_driver_build_lock(file_lock)


def _build_driver_locked(worker_id: int, headless: bool, chrome_binary: Optional[str],
                         version_main: Optional[int], proxy: Optional[str], uc):
    opts = uc.ChromeOptions()
    opts.page_load_strategy = "eager"
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--disable-infobars")
    opts.add_argument("--lang=en-US")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument(f"--remote-debugging-port={_free_port()}")
    opts.add_argument("--password-store=basic")
    opts.add_argument("--use-mock-keychain")

    profile = _create_profile(worker_id)
    print(f"  [worker-{worker_id}] Profile: {profile}", flush=True)

    if chrome_binary:
        opts.binary_location = chrome_binary

    if proxy:
        parts = proxy.split(":")
        if len(parts) == 4:
            ip, port, user, passwd = parts
            ext_zip = os.path.join(tempfile.gettempdir(), f"proxy_ext_maps_w{worker_id}.zip")
            with zipfile.ZipFile(ext_zip, "w") as zp:
                zp.writestr("manifest.json", json.dumps({
                    "version": "1.0.0", "manifest_version": 2, "name": "Proxy Auth",
                    "permissions": ["proxy", "tabs", "unlimitedStorage", "storage",
                                    "<all_urls>", "webRequest", "webRequestBlocking"],
                    "background": {"scripts": ["background.js"]},
                    "minimum_chrome_version": "22.0.0",
                }))
                zp.writestr("background.js", f"""
                    var config = {{mode: "fixed_servers", rules: {{
                        singleProxy: {{scheme: "http", host: "{ip}", port: parseInt({port})}},
                        bypassList: ["localhost"]
                    }}}};
                    chrome.proxy.settings.set({{value: config, scope: "regular"}}, function(){{}});
                    chrome.webRequest.onAuthRequired.addListener(
                        function(details) {{ return {{authCredentials: {{username: "{user}", password: "{passwd}"}}}}; }},
                        {{urls: ["<all_urls>"]}}, ['blocking']
                    );
                """)
            opts.add_extension(ext_zip)
        elif len(parts) == 2:
            opts.add_argument(f"--proxy-server=http://{proxy}")

    _ensure_uc_binary_present()
    driver = uc.Chrome(options=opts, headless=headless, use_subprocess=True,
                       version_main=version_main, user_data_dir=profile)
    driver.set_page_load_timeout(30)
    driver.set_script_timeout(45)
    _snapshot_uc_binary()
    return driver, profile


# ----------------------------------------------------------------------------
# Maps scrape
# ----------------------------------------------------------------------------

def load_maps_js() -> str:
    with open(MAPS_JSON_PATH, "r", encoding="utf-8") as fh:
        spec = json.load(fh)
    action = next((a for a in spec.get("actions", []) if a.get("type") == "evaluate"), None)
    if not action or "script" not in action:
        raise RuntimeError(f"{MAPS_JSON_PATH} missing the 'evaluate' action / script")
    return action["script"]


def _host(value: str) -> str:
    """Normalize a domain/URL to a bare host (lowercase, no scheme/www/path)."""
    v = (value or "").strip().lower()
    if "://" in v:
        v = urlparse(v).netloc or v
    v = v.split("/")[0]
    if v.startswith("www."):
        v = v[4:]
    return v


def website_match(website: Optional[str], domain: str) -> bool:
    site = _host(website or "")
    inp = _host(domain or "")
    if not site or not inp:
        return False
    return site == inp or site.endswith("." + inp) or inp.endswith("." + site)


def _prime_consent(driver) -> None:
    """Set a Google consent cookie so Maps doesn't serve the EU consent wall."""
    try:
        driver.get("https://www.google.com/?hl=en")
        time.sleep(1)
        for val in ("YES+", "YES+cb"):
            try:
                driver.add_cookie({"name": "CONSENT", "value": val, "domain": ".google.com"})
            except Exception:
                pass
    except Exception:
        pass


def scrape_domain(driver, domain: str) -> Dict[str, Any]:
    """Open Maps for the domain, run maps.json, extract the listing fields."""
    t0 = time.time()
    row: Dict[str, Any] = {
        "domain_name": domain, "status": "error",
        "maps_name": None, "maps_address": None, "maps_website": None,
        "maps_website_match": False, "maps_rating": None, "maps_review_count": None,
    }
    try:
        try:
            driver.get(f"{MAPS_SEARCH_URL}{quote(domain)}?hl=en")
        except Exception:
            pass

        time.sleep(2)

        js_payload = load_maps_js().replace("${domain}", domain)
        kickoff_js = f"""
            window.__mapsResult = undefined;
            window.__mapsError = undefined;
            (async function() {{
                try {{
                    var r = await (async function() {{ {js_payload} }})();
                    window.__mapsResult = r;
                }} catch (err) {{
                    window.__mapsError = JSON.stringify({{error:String(err&&err.message||err)}});
                }}
            }})();
        """
        driver.execute_script(kickoff_js)

        poll_deadline = time.time() + 45
        while time.time() < poll_deadline:
            try:
                done_raw = driver.execute_script(
                    "return JSON.stringify({r:window.__mapsResult,e:window.__mapsError})"
                )
                done = json.loads(done_raw) if done_raw else {}
            except Exception:
                time.sleep(1)
                continue

            if done.get("r") is not None:
                raw = done["r"]
                parsed = json.loads(raw) if isinstance(raw, str) else raw
                results = parsed.get("results") if isinstance(parsed, dict) else None
                if results and isinstance(results, list):
                    r = results[0]
                    name = r.get("name") or r.get("maps_name")
                    website = r.get("website") or r.get("maps_website")
                    rating = r.get("rating") if r.get("rating") is not None else r.get("maps_rating")
                    reviews = r.get("review_count") if r.get("review_count") is not None else r.get("maps_review_count")
                    status = r.get("status")
                    row["maps_name"] = name
                    row["maps_address"] = r.get("address") or r.get("maps_address")
                    row["maps_website"] = website
                    row["maps_website_match"] = website_match(website, domain)
                    row["maps_rating"] = rating
                    row["maps_review_count"] = reviews
                    row["status"] = status or ("completed" if (name or website) else "notfound")
                break

            if done.get("e") is not None:
                row["error"] = done["e"]
                break

            time.sleep(1.0)
    except Exception as e:
        row["error"] = f"{type(e).__name__}: {e}"
    finally:
        row["elapsed_seconds"] = round(time.time() - t0, 2)
        row["finished_at"] = datetime.now(timezone.utc).isoformat()
    return row


# ----------------------------------------------------------------------------
# Pull / post
# ----------------------------------------------------------------------------

_print_lock = threading.Lock()


def tprint(msg):
    with _print_lock:
        print(msg)


def pull_domain(api_url: str) -> Optional[Dict[str, Any]]:
    try:
        resp = _HTTP.get(f"{api_url}{ENDPOINT}", timeout=(10, 30))
        if resp.status_code == 204:
            return None
        resp.raise_for_status()
        data = resp.json()
        if data.get("success"):
            return data["data"]
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 204:
            return None
    except Exception as e:
        tprint(f"  [pull] error after retries: {e}")
    return None


def post_result(api_url: str, execution_record: Dict, result: Dict) -> bool:
    try:
        resp = _HTTP.post(
            f"{api_url}{ENDPOINT}",
            json={"execution_record": execution_record, "result": result},
            timeout=(10, 60),
        )
        resp.raise_for_status()
        ok = resp.json().get("success", False)
        if not ok:
            _buffer_failed_post(execution_record, result)
        return ok
    except Exception as e:
        tprint(f"  [post] error after retries: {e}; buffering result for {result.get('domain_name')}")
        _buffer_failed_post(execution_record, result)
        return False


_buffer_lock = threading.Lock()


def _post_buffer_path() -> str:
    return os.environ.get("MAPS_POST_BUFFER", "/tmp/map-local_pending_posts.jsonl")


def _buffer_failed_post(execution_record: Dict, result: Dict) -> None:
    try:
        with _buffer_lock:
            with open(_post_buffer_path(), "a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "execution_record": execution_record,
                    "result": result,
                }) + "\n")
    except Exception as e:
        print(f"  [buffer] FATAL: cannot write post buffer: {e}", flush=True)


def _flush_pending_posts(api_url: str, max_per_run: int = 50) -> int:
    buf_path = _post_buffer_path()
    if not os.path.exists(buf_path):
        return 0
    flushed = 0
    remaining: List[str] = []
    with _buffer_lock:
        try:
            with open(buf_path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
        except Exception:
            return 0
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if flushed >= max_per_run:
                remaining.append(line)
                continue
            try:
                obj = json.loads(line)
                resp = _HTTP.post(
                    f"{api_url}{ENDPOINT}",
                    json={"execution_record": obj["execution_record"], "result": obj["result"]},
                    timeout=(10, 60),
                )
                if resp.status_code < 400 and resp.json().get("success"):
                    flushed += 1
                    continue
            except Exception:
                pass
            remaining.append(line)
        try:
            with open(buf_path, "w", encoding="utf-8") as fh:
                for ln in remaining:
                    fh.write(ln + "\n")
        except Exception:
            pass
    return flushed


def _start_buffer_flusher(api_url: str) -> threading.Thread:
    def loop():
        while True:
            time.sleep(30)
            try:
                n = _flush_pending_posts(api_url)
                if n:
                    tprint(f"  [flusher] re-sent {n} buffered posts")
            except Exception as e:
                tprint(f"  [flusher] error: {e}")
    t = threading.Thread(target=loop, daemon=True, name="maps-post-flusher")
    t.start()
    return t


# ----------------------------------------------------------------------------
# Worker
# ----------------------------------------------------------------------------

def _is_driver_alive(driver) -> bool:
    if driver is None:
        return False
    try:
        return driver.execute_script("return 1") == 1
    except Exception:
        return False


def worker_loop(worker_id: int, proxy: Optional[str], api_url: str, headless: bool,
                chrome_bin: Optional[str], version_main: Optional[int]):
    proxy_short = proxy.split(":")[0] if proxy else "local"
    tprint(f"  [W{worker_id}] Starting with proxy {proxy_short}...")

    driver = None
    profile_path: Optional[str] = None
    processed = 0

    def start_browser():
        nonlocal driver, profile_path
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
            time.sleep(2)
        if profile_path:
            _remove_profile(profile_path)
        last_err = None
        for attempt in range(10):
            try:
                driver, profile_path = build_driver(worker_id, headless, chrome_bin, version_main, proxy)
                if _is_driver_alive(driver):
                    break
                raise RuntimeError("driver built but is_alive() returned False")
            except Exception as e:
                last_err = e
                tprint(f"  [W{worker_id}] driver build attempt {attempt+1}/10 failed: {e}")
                try:
                    if driver:
                        driver.quit()
                except Exception:
                    pass
                driver = None
                if profile_path:
                    _remove_profile(profile_path)
                    profile_path = None
                time.sleep(min(5 * (2 ** attempt), 60))
        if driver is None:
            raise RuntimeError(f"driver build failed after 10 attempts: {last_err}")
        _prime_consent(driver)
        try:
            driver.get("about:blank")
        except Exception:
            pass
        tprint(f"  [W{worker_id}] Browser ready (proxy: {proxy_short})")

    try:
        start_browser()
        last_healthcheck = time.time()

        while True:
            now = time.time()
            if now - last_healthcheck > 60:
                if not _is_driver_alive(driver):
                    tprint(f"  [W{worker_id}] healthcheck failed — rebuilding browser")
                    start_browser()
                last_healthcheck = now

            record = pull_domain(api_url)
            if record is None:
                time.sleep(3 + random.random() * 2)
                continue

            domain = record.get("domain_name", "unknown")
            execution_id = str(record.get("execution_id", "?"))[:8]
            tprint(f"  [W{worker_id}] Got: {domain} (exec: {execution_id})")

            try:
                result = scrape_domain(driver, domain)
                mark = "OK" if result["status"] == "completed" else result["status"].upper()
                tprint(f"  [W{worker_id}] [{mark}] {domain} "
                       f"site={result.get('maps_website', '-')} "
                       f"match={result.get('maps_website_match')} "
                       f"rating={result.get('maps_rating', '-')} "
                       f"reviews={result.get('maps_review_count', '-')} "
                       f"({result['elapsed_seconds']:.1f}s)")
                try:
                    driver.get("about:blank")
                except Exception:
                    start_browser()
            except Exception as e:
                tprint(f"  [W{worker_id}] Error: {e.__class__.__name__}: {e}. Restarting browser...")
                try:
                    start_browser()
                except Exception as e2:
                    tprint(f"  [W{worker_id}] FATAL: cannot rebuild browser: {e2}")
                    raise
                result = {"domain_name": domain, "status": "error", "error": str(e)}

            post_result(api_url, record, result)
            processed += 1

            if processed > 0 and processed % 50 == 0:
                tprint(f"  [W{worker_id}] processed {processed} — recycling browser")
                try:
                    start_browser()
                except Exception as e:
                    tprint(f"  [W{worker_id}] recycle failed (continuing): {e}")

    except KeyboardInterrupt:
        pass
    except Exception as e:
        tprint(f"  [W{worker_id}] FATAL: {e}")
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        if profile_path:
            _remove_profile(profile_path)
        tprint(f"  [W{worker_id}] Stopped. Processed: {processed}")


def main():
    p = argparse.ArgumentParser(description="Google Maps Checker — Parallel Pull Mode.")
    p.add_argument("--api-url", default=DEFAULT_API_URL)
    p.add_argument("--headless", action="store_true")
    p.add_argument("--workers", type=int, default=5, help="Number of parallel browser instances")
    p.add_argument("--chrome", help="Path to chrome/chromium binary")
    p.add_argument("--proxies", default=PROXIES_PATH, help="Path to proxies file")
    p.add_argument("--no-proxy", action="store_true", help="Disable proxies")
    p.add_argument("--webshare-proxy", action="store_true",
                   help="Use Webshare rotating proxy (p.webshare.io:9999 with IP auth)")
    args = p.parse_args()

    _install_signal_handlers()

    if not os.path.isfile(MAPS_JSON_PATH):
        print(f"[FATAL] maps.json not found at {MAPS_JSON_PATH}", file=sys.stderr)
        sys.exit(2)

    proxies: List[str] = []
    if not args.no_proxy and os.path.exists(args.proxies):
        with open(args.proxies) as f:
            proxies = [line.strip() for line in f if line.strip()]

    if args.webshare_proxy:
        my_ip = webshare_get_my_ip()
        print(f"[*] Public IP: {my_ip}", flush=True)
        webshare_authorize_ip(my_ip)
        atexit.register(webshare_deauthorize_ip)
        proxies = [f"{WEBSHARE_PROXY_HOST}:{WEBSHARE_PROXY_PORT}"]
        print(f"[*] Webshare proxy: {proxies[0]}", flush=True)
        time.sleep(3)

    if not args.no_proxy and not args.webshare_proxy and not proxies:
        print("[WARN] No proxies found. Running all instances on local IP.")

    chrome_bin = args.chrome or find_chrome_binary()
    version_main = detect_chrome_major(chrome_bin)

    print(f"[*] Google Maps Checker - Parallel Pull Mode")
    print(f"[*] Endpoint: {args.api_url}{ENDPOINT}")
    print(f"[*] Chrome: {chrome_bin}")
    print(f"[*] Workers: {args.workers} | Proxy: {'disabled' if args.no_proxy or not proxies else f'{len(proxies)} loaded'}")
    print(f"[*] Launching {args.workers} browser instances...\n")

    initial = _flush_pending_posts(args.api_url, max_per_run=200)
    if initial:
        print(f"[*] Recovered {initial} buffered posts from previous run")
    _start_buffer_flusher(args.api_url)

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = []
        for i in range(args.workers):
            proxy = proxies[i % len(proxies)] if proxies else None
            futures.append(executor.submit(
                worker_loop, i, proxy, args.api_url, args.headless, chrome_bin, version_main
            ))
            time.sleep(10)
        try:
            for f in futures:
                f.result()
        except KeyboardInterrupt:
            print("\n[*] Shutting down...")


if __name__ == "__main__":
    main()
