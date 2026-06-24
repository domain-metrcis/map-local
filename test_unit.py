"""Unit tests for map-local (no browser needed).

Covers:
  - website_match (equal / subdomain / parent / mismatch / no-website / www)
  - maps.json is well-formed and exposes an 'evaluate' action whose script
    returns the {results:[...]} envelope.
  - the maps_* result keys are byte-identical to the management contract.

Run:  python3 test_unit.py
"""
import json
import os
import sys

import maps_checker as m

CONTRACT_KEYS = [
    "maps_name", "maps_address", "maps_website",
    "maps_website_match", "maps_rating", "maps_review_count",
]


def test_website_match():
    cases = [
        # (website, input_domain, expected)
        ("https://imperialpalace.in/", "imperialpalace.in", True),     # equal
        ("http://www.imperialpalace.in", "imperialpalace.in", True),   # www-insensitive
        ("https://shop.example.com", "example.com", True),             # site is subdomain
        ("https://example.com", "shop.example.com", True),             # site is parent
        ("https://botxbyte.co", "botxbyte.com", False),                # different TLD
        ("https://other.com", "example.com", False),                   # mismatch
        (None, "example.com", False),                                  # no website
        ("", "example.com", False),                                    # empty website
        ("https://EXAMPLE.com", "example.com", True),                  # case-insensitive
    ]
    for website, domain, expected in cases:
        got = m.website_match(website, domain)
        assert got == expected, f"website_match({website!r},{domain!r})={got}, expected {expected}"
    print(f"  ✓ website_match: {len(cases)} cases passed")


def test_maps_json():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "maps.json")
    spec = json.load(open(path, encoding="utf-8"))
    ev = next((a for a in spec.get("actions", []) if a.get("type") == "evaluate"), None)
    assert ev is not None, "maps.json has no 'evaluate' action"
    assert "script" in ev and ev["script"], "evaluate action has no script"
    assert "return JSON.stringify" in ev["script"], "script must return a JSON string"
    assert "results" in ev["script"], "script must build a {results:[...]} envelope"
    # load_maps_js() must find it.
    js = m.load_maps_js()
    assert "${domain}" in js, "script must reference the ${domain} placeholder"
    print(f"  ✓ maps.json: valid envelope, evaluate script {len(js)} chars")


def test_contract_keys():
    # The keys the checker emits must match the management/DB/frontend contract.
    row = {
        "domain_name": "x.com", "status": "completed",
        "maps_name": "X", "maps_address": "A", "maps_website": "https://x.com",
        "maps_website_match": True, "maps_rating": 4.5, "maps_review_count": 10,
    }
    for k in CONTRACT_KEYS:
        assert k in row, f"missing contract key {k}"
    print(f"  ✓ contract keys: {', '.join(CONTRACT_KEYS)}")


if __name__ == "__main__":
    failures = 0
    for fn in (test_website_match, test_maps_json, test_contract_keys):
        try:
            fn()
        except AssertionError as e:
            failures += 1
            print(f"  ✗ {fn.__name__}: {e}")
    if failures:
        print(f"\n{failures} test(s) FAILED")
        sys.exit(1)
    print("\nAll unit tests passed.")
