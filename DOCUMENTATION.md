# Google Maps Business-Listing Checker — Pull-Based Local Browser Processing

## Overview

Local script that pulls domains from the server queue, looks each up on Google
Maps in parallel ungoogled-chromium instances, and posts results back. No tunnel
or port-forwarding needed — the script makes outbound HTTP calls only.

For each domain it returns: business name, address, website, whether the listed
website matches the domain (true/false), rating, and review count.

---

## Local vendored setup

Run once after cloning:

```bash
bash tools/setup_vendor.sh
```

This downloads ungoogled-chromium (portable build) into
`vendor/ungoogled-chromium/`. It is gitignored — it lives in your local clone
only. `maps_checker.py` auto-discovers it at runtime, so no `--chrome` flag is
needed.

> Unlike `ahref-local`, there is **no cf-autoclick extension / master profile**:
> Google Maps is not behind Cloudflare Turnstile, so the browser binary alone is
> enough.

---

## Architecture (Pull Approach)

```
Server (Kubernetes)
  Campaign → maps.business-pool-queue        (0-replica worker; no in-cluster consumer)
       ↑                       ↓
  workflow.domain-final-queue  GET /maps/    (pop a domain)
       ↑                       ↓
  POST /maps/  ←──── returns execution record
         ↑                     ↓
         │      YOUR LAPTOP     │
         │   N parallel Chrome  │
         └──────────────────────┘
```

---

## Commands

```bash
# Default (proxies on if proxies.txt present)
.venv/bin/python maps_checker.py

# No proxies (local IP)
.venv/bin/python maps_checker.py --no-proxy

# N instances, headless
.venv/bin/python maps_checker.py --workers 3 --no-proxy --headless

# Webshare rotating proxy
.venv/bin/python maps_checker.py --workers 1 --webshare-proxy
```

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--workers N` | 5 | Parallel browser instances |
| `--no-proxy` | off | Disable proxies (local IP) |
| `--headless` | off | Headless browsers |
| `--webshare-proxy` | off | Webshare rotating proxy (IP auth) |
| `--api-url URL` | (production) | Management service API URL |
| `--chrome PATH` | auto | Chrome/Chromium binary path |
| `--proxies FILE` | proxies.txt | Proxy list (`ip:port:user:pass` per line) |

---

## How It Works

1. Launch N browser instances (each with its own proxy if enabled).
2. Each worker polls `GET /maps/` for a domain.
3. Open `https://www.google.com/maps/search/<domain>`, wait for the place panel
   to render, run `maps.json`'s scrape script, extract the fields.
4. Compute `maps_website_match` (listed website host vs the input domain).
5. Post the result to `POST /maps/`, which routes to `workflow.domain-final-queue`.
6. Loop.

---

## Services Involved

| Service | Role |
|---------|------|
| **domain-metrics-management-service** | `GET /maps/` pops from queue; `POST /maps/` routes results to the next step |
| **domain-metrics-orchestration-service** | Worker scaled to 0 (pull-based; `maps.business-pool-queue` ∈ PULL_BASED_QUEUES) |
| **map-local (this script)** | Pulls domains, scrapes Google Maps, posts results |

---

## Stopping

`Ctrl+C` gracefully shuts down all browser instances.
