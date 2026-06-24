# map-local

Pull-based local Google Maps business-listing checker. Pulls domains from the
server queue, looks each up on Google Maps in ungoogled-chromium, extracts the
business name / address / website / rating / review count (and whether the
listed website matches the domain), and posts the result back.

See `DOCUMENTATION.md` for setup and usage.

Quick start:

```bash
bash tools/setup_vendor.sh                       # one-time: download ungoogled-chromium
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python maps_checker.py --workers 1 --no-proxy --headless
```
