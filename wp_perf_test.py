#!/usr/bin/env python3
"""
WordPress performance test runner.

Features:
- Samples random frontend pages from sitemap(s), always including homepage.
- Runs multiple test iterations for cached and uncached frontend requests.
- Optionally logs into wp-admin and tests admin pages + sampled frontend pages.
- Prints a speed breakdown and can export JSON results.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


DEFAULT_SITEMAP_CANDIDATES = (
    "/sitemap.xml",
    "/wp-sitemap.xml",
    "/sitemap_index.xml",
)


@dataclass
class RequestResult:
    category: str
    run: int
    url: str
    status_code: int
    duration_ms: float
    response_bytes: int
    ok: bool
    final_url: str
    error: Optional[str] = None


def normalize_base_url(base_url: str) -> str:
    parsed = urllib.parse.urlparse(base_url.strip())
    if not parsed.scheme:
        base_url = "https://" + base_url.strip()
        parsed = urllib.parse.urlparse(base_url)
    if not parsed.netloc:
        raise ValueError(f"Invalid base URL: {base_url}")
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/", "", "", ""))


def build_opener(insecure: bool = False) -> urllib.request.OpenerDirector:
    cookie_jar = CookieJar()
    handlers: List[urllib.request.BaseHandler] = [urllib.request.HTTPCookieProcessor(cookie_jar)]
    if insecure:
        import ssl

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


def safe_join(base_url: str, path_or_url: str) -> str:
    return urllib.parse.urljoin(base_url, path_or_url)


def same_domain(url: str, base_url: str) -> bool:
    return urllib.parse.urlparse(url).netloc == urllib.parse.urlparse(base_url).netloc


def fetch_text(
    opener: urllib.request.OpenerDirector,
    url: str,
    timeout: float,
    headers: Optional[Dict[str, str]] = None,
) -> Tuple[int, str]:
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    with opener.open(req, timeout=timeout) as resp:
        data = resp.read()
        encoding = resp.headers.get_content_charset() or "utf-8"
        text = data.decode(encoding, errors="replace")
        return resp.getcode(), text


def parse_sitemap_xml(xml_text: str) -> Tuple[List[str], List[str]]:
    """
    Returns (url_entries, sitemap_entries).
    """
    root = ET.fromstring(xml_text)
    tag = root.tag.lower()
    url_entries: List[str] = []
    sitemap_entries: List[str] = []

    if tag.endswith("urlset"):
        for loc in root.findall(".//{*}url/{*}loc"):
            if loc.text:
                url_entries.append(loc.text.strip())
    elif tag.endswith("sitemapindex"):
        for loc in root.findall(".//{*}sitemap/{*}loc"):
            if loc.text:
                sitemap_entries.append(loc.text.strip())
    return url_entries, sitemap_entries


def discover_sitemap_urls(
    opener: urllib.request.OpenerDirector,
    base_url: str,
    timeout: float,
    max_sitemap_files: int = 30,
    max_url_entries: int = 3000,
) -> List[str]:
    discovered_urls: Set[str] = set()
    queue: List[str] = [safe_join(base_url, p) for p in DEFAULT_SITEMAP_CANDIDATES]
    seen_sitemaps: Set[str] = set()

    while queue and len(seen_sitemaps) < max_sitemap_files and len(discovered_urls) < max_url_entries:
        sitemap_url = queue.pop(0)
        if sitemap_url in seen_sitemaps:
            continue
        seen_sitemaps.add(sitemap_url)
        try:
            status, xml_text = fetch_text(opener, sitemap_url, timeout=timeout)
            if status >= 400:
                continue
            urls, nested_sitemaps = parse_sitemap_xml(xml_text)
            for u in urls:
                if same_domain(u, base_url):
                    discovered_urls.add(u)
                    if len(discovered_urls) >= max_url_entries:
                        break
            for nested in nested_sitemaps:
                if same_domain(nested, base_url) and nested not in seen_sitemaps:
                    queue.append(nested)
        except (ET.ParseError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
            continue

    return sorted(discovered_urls)


def sample_frontend_pages(all_urls: Sequence[str], base_url: str, sample_size: int) -> List[str]:
    homepage = base_url
    pool = [u for u in all_urls if u.rstrip("/") != homepage.rstrip("/")]
    picked = random.sample(pool, min(len(pool), max(0, sample_size - 1))) if pool else []
    sampled = [homepage] + picked
    deduped: List[str] = []
    seen: Set[str] = set()
    for url in sampled:
        key = url.rstrip("/")
        if key not in seen:
            seen.add(key)
            deduped.append(url)
    return deduped[:sample_size] if sample_size > 0 else [homepage]


def add_no_cache_query(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    qs.append(("no-cache", str(random.randint(100000, 999999999))))
    new_query = urllib.parse.urlencode(qs, doseq=True)
    return urllib.parse.urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment)
    )


def request_once(
    opener: urllib.request.OpenerDirector,
    url: str,
    timeout: float,
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, object]:
    start = time.perf_counter()
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read()
            duration_ms = (time.perf_counter() - start) * 1000
            return {
                "status_code": int(resp.getcode()),
                "duration_ms": duration_ms,
                "response_bytes": len(body),
                "ok": 200 <= int(resp.getcode()) < 400,
                "final_url": resp.geturl(),
                "error": None,
            }
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
        except Exception:
            body = b""
        duration_ms = (time.perf_counter() - start) * 1000
        return {
            "status_code": int(exc.code),
            "duration_ms": duration_ms,
            "response_bytes": len(body),
            "ok": False,
            "final_url": url,
            "error": str(exc),
        }
    except Exception as exc:  # noqa: BLE001
        duration_ms = (time.perf_counter() - start) * 1000
        return {
            "status_code": 0,
            "duration_ms": duration_ms,
            "response_bytes": 0,
            "ok": False,
            "final_url": url,
            "error": str(exc),
        }


def detect_wordpress(opener: urllib.request.OpenerDirector, base_url: str, timeout: float) -> bool:
    checks = [
        safe_join(base_url, "/wp-login.php"),
        safe_join(base_url, "/wp-json/"),
    ]
    for url in checks:
        result = request_once(opener, url, timeout=timeout)
        if result["status_code"] in {200, 301, 302, 303}:
            return True
    return False


def login_wp_admin(
    opener: urllib.request.OpenerDirector,
    base_url: str,
    username: str,
    password: str,
    timeout: float,
) -> bool:
    login_url = safe_join(base_url, "/wp-login.php")
    admin_url = safe_join(base_url, "/wp-admin/")

    # Prime test cookie expected by wp-login.
    try:
        request_once(opener, login_url, timeout=timeout)
    except Exception:  # noqa: BLE001
        return False

    payload = urllib.parse.urlencode(
        {
            "log": username,
            "pwd": password,
            "wp-submit": "Log In",
            "redirect_to": admin_url,
            "testcookie": "1",
        }
    ).encode("utf-8")
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    req = urllib.request.Request(login_url, data=payload, headers=headers, method="POST")
    try:
        with opener.open(req, timeout=timeout) as resp:
            final_url = resp.geturl()
            body = resp.read().decode("utf-8", errors="ignore").lower()
            if "/wp-admin/" in final_url:
                return True
            if "dashboard" in body and "wp-admin" in body:
                return True
            return False
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore").lower()
        if "dashboard" in body and "wp-admin" in body:
            return True
        return False
    except Exception:  # noqa: BLE001
        return False


def run_category(
    opener: urllib.request.OpenerDirector,
    category: str,
    run_id: int,
    urls: Iterable[str],
    timeout: float,
    transform_url=None,
    headers: Optional[Dict[str, str]] = None,
) -> List[RequestResult]:
    rows: List[RequestResult] = []
    for original_url in urls:
        target_url = transform_url(original_url) if transform_url else original_url
        result = request_once(opener, target_url, timeout=timeout, headers=headers)
        rows.append(
            RequestResult(
                category=category,
                run=run_id,
                url=target_url,
                status_code=int(result["status_code"]),
                duration_ms=float(result["duration_ms"]),
                response_bytes=int(result["response_bytes"]),
                ok=bool(result["ok"]),
                final_url=str(result["final_url"]),
                error=result["error"] if isinstance(result["error"], str) else None,
            )
        )
    return rows


def percentile(values: Sequence[float], p: float) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * p
    lower = int(pos)
    upper = min(lower + 1, len(sorted_vals) - 1)
    weight = pos - lower
    return sorted_vals[lower] * (1 - weight) + sorted_vals[upper] * weight


def build_summary(rows: Sequence[RequestResult]) -> List[Dict[str, object]]:
    grouped: Dict[str, List[RequestResult]] = defaultdict(list)
    for row in rows:
        grouped[row.category].append(row)

    output: List[Dict[str, object]] = []
    for category in sorted(grouped):
        group = grouped[category]
        durations = [r.duration_ms for r in group]
        ok_count = sum(1 for r in group if r.ok)
        output.append(
            {
                "category": category,
                "count": len(group),
                "success_count": ok_count,
                "success_rate": round((ok_count / len(group)) * 100, 2) if group else 0.0,
                "avg_ms": round(statistics.mean(durations), 2) if durations else 0.0,
                "median_ms": round(statistics.median(durations), 2) if durations else 0.0,
                "p95_ms": round(percentile(durations, 0.95), 2) if durations else 0.0,
                "min_ms": round(min(durations), 2) if durations else 0.0,
                "max_ms": round(max(durations), 2) if durations else 0.0,
            }
        )
    return output


def print_summary_table(summary: Sequence[Dict[str, object]]) -> None:
    if not summary:
        print("\nNo request data collected.")
        return
    print("\nPerformance summary by category")
    print("-" * 112)
    print(
        f"{'Category':36} {'Count':>5} {'Success%':>8} {'Avg(ms)':>10} {'Median':>10} {'P95':>10} {'Min':>10} {'Max':>10}"
    )
    print("-" * 112)
    for row in summary:
        print(
            f"{str(row['category']):36} "
            f"{int(row['count']):>5} "
            f"{float(row['success_rate']):>8.2f} "
            f"{float(row['avg_ms']):>10.2f} "
            f"{float(row['median_ms']):>10.2f} "
            f"{float(row['p95_ms']):>10.2f} "
            f"{float(row['min_ms']):>10.2f} "
            f"{float(row['max_ms']):>10.2f}"
        )
    print("-" * 112)


def print_slowest(rows: Sequence[RequestResult], limit: int = 10) -> None:
    if not rows:
        return
    slowest = sorted(rows, key=lambda r: r.duration_ms, reverse=True)[:limit]
    print(f"\nTop {len(slowest)} slowest requests")
    print("-" * 132)
    print(f"{'Category':34} {'Run':>4} {'Status':>6} {'Time(ms)':>10} {'URL'}")
    print("-" * 132)
    for row in slowest:
        print(
            f"{row.category:34} {row.run:>4} {row.status_code:>6} {row.duration_ms:>10.2f} {row.url}"
        )
    print("-" * 132)


def remove_query_params(url: str, params_to_drop: Set[str]) -> str:
    parsed = urllib.parse.urlparse(url)
    query_items = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    filtered = [(k, v) for (k, v) in query_items if k not in params_to_drop]
    new_query = urllib.parse.urlencode(filtered, doseq=True)
    return urllib.parse.urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment)
    )


def build_per_page_delta(
    rows: Sequence[RequestResult],
    cached_category: str,
    uncached_category: str,
    uncached_drop_params: Optional[Set[str]] = None,
) -> List[Dict[str, object]]:
    """
    Build per-page delta table:
      delta_ms = avg_uncached_ms - avg_cached_ms
      delta_pct = delta_ms / avg_cached_ms * 100
    """
    cached_by_page: Dict[str, List[RequestResult]] = defaultdict(list)
    uncached_by_page: Dict[str, List[RequestResult]] = defaultdict(list)

    for row in rows:
        if row.category == cached_category:
            cached_by_page[row.url].append(row)
        elif row.category == uncached_category:
            normalized_url = (
                remove_query_params(row.url, uncached_drop_params)
                if uncached_drop_params
                else row.url
            )
            uncached_by_page[normalized_url].append(row)

    pages = sorted(set(cached_by_page.keys()) & set(uncached_by_page.keys()))
    delta_rows: List[Dict[str, object]] = []
    for page in pages:
        cached_group_ok = [r.duration_ms for r in cached_by_page[page] if r.ok]
        uncached_group_ok = [r.duration_ms for r in uncached_by_page[page] if r.ok]
        if not cached_group_ok or not uncached_group_ok:
            continue
        avg_cached = statistics.mean(cached_group_ok)
        avg_uncached = statistics.mean(uncached_group_ok)
        delta_ms = avg_uncached - avg_cached
        delta_pct = (delta_ms / avg_cached * 100.0) if avg_cached else 0.0
        delta_rows.append(
            {
                "url": page,
                "cached_category": cached_category,
                "uncached_category": uncached_category,
                "cached_ok_count": len(cached_group_ok),
                "uncached_ok_count": len(uncached_group_ok),
                "avg_cached_ms": round(avg_cached, 2),
                "avg_uncached_ms": round(avg_uncached, 2),
                "delta_ms": round(delta_ms, 2),
                "delta_pct": round(delta_pct, 2),
            }
        )
    delta_rows.sort(key=lambda item: float(item["delta_ms"]), reverse=True)
    return delta_rows


def print_per_page_delta_table(title: str, delta_rows: Sequence[Dict[str, object]], limit: int = 10) -> None:
    if not delta_rows:
        return
    shown = list(delta_rows)[:limit]
    print(f"\nPer-page delta ({title}) [uncached - cached], top {len(shown)} by delta")
    print("-" * 154)
    print(
        f"{'Delta(ms)':>10} {'Delta(%)':>10} {'CachedAvg':>11} {'UncachedAvg':>13} {'CachedN':>8} {'UncachedN':>10} URL"
    )
    print("-" * 154)
    for row in shown:
        print(
            f"{float(row['delta_ms']):>10.2f} "
            f"{float(row['delta_pct']):>10.2f} "
            f"{float(row['avg_cached_ms']):>11.2f} "
            f"{float(row['avg_uncached_ms']):>13.2f} "
            f"{int(row['cached_ok_count']):>8} "
            f"{int(row['uncached_ok_count']):>10} "
            f"{str(row['url'])}"
        )
    print("-" * 154)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run WordPress performance checks for cached/uncached pages and optional wp-admin routes."
    )
    parser.add_argument("--base-url", required=True, help="WordPress site base URL (example: https://example.com)")
    parser.add_argument("--runs", type=int, default=3, help="How many times to repeat each test set (default: 3)")
    parser.add_argument(
        "--sample-size",
        type=int,
        default=10,
        help="How many frontend pages to test (homepage is always included, default: 10)",
    )
    parser.add_argument("--timeout", type=float, default=20.0, help="HTTP timeout in seconds (default: 20)")
    parser.add_argument("--seed", type=int, default=None, help="Optional random seed for repeatable sampling")
    parser.add_argument("--username", default=None, help="WordPress admin username (optional)")
    parser.add_argument("--password", default=None, help="WordPress admin password (optional)")
    parser.add_argument("--output", default=None, help="Optional path to write JSON report")
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Skip TLS certificate verification (useful for staging with self-signed certs)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.sample_size < 1:
        print("sample-size must be >= 1", file=sys.stderr)
        return 2
    if args.runs < 1:
        print("runs must be >= 1", file=sys.stderr)
        return 2

    if args.seed is not None:
        random.seed(args.seed)

    try:
        base_url = normalize_base_url(args.base_url)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    opener = build_opener(insecure=args.insecure)
    is_wp = detect_wordpress(opener, base_url, timeout=args.timeout)
    if not is_wp:
        print(
            "Warning: site does not look like a reachable WordPress install (wp-login/wp-json checks failed). "
            "Continuing anyway."
        )

    sitemap_urls = discover_sitemap_urls(opener, base_url, timeout=args.timeout)
    sampled_pages = sample_frontend_pages(sitemap_urls, base_url, sample_size=args.sample_size)
    if not sampled_pages:
        sampled_pages = [base_url]

    print(f"Base URL: {base_url}")
    print(f"Discovered sitemap URLs: {len(sitemap_urls)}")
    print(f"Sampled frontend pages ({len(sampled_pages)}):")
    for page in sampled_pages:
        print(f"  - {page}")

    results: List[RequestResult] = []

    for run_id in range(1, args.runs + 1):
        results.extend(
            run_category(
                opener,
                category="frontend_uncached",
                run_id=run_id,
                urls=sampled_pages,
                timeout=args.timeout,
                transform_url=add_no_cache_query,
            )
        )
        results.extend(
            run_category(
                opener,
                category="frontend_cached",
                run_id=run_id,
                urls=sampled_pages,
                timeout=args.timeout,
            )
        )

    admin_tests_skipped_reason: Optional[str] = None
    logged_in = False
    if args.username and args.password:
        logged_in = login_wp_admin(opener, base_url, args.username, args.password, timeout=args.timeout)
        if not logged_in:
            admin_tests_skipped_reason = "login_failed"
    else:
        admin_tests_skipped_reason = "missing_credentials"

    if logged_in:
        admin_pages = [
            safe_join(base_url, "/wp-admin/"),
            safe_join(base_url, "/wp-admin/plugins.php"),
            safe_join(base_url, "/wp-admin/edit.php"),
        ]
        for run_id in range(1, args.runs + 1):
            results.extend(
                run_category(
                    opener,
                    category="admin_core_pages",
                    run_id=run_id,
                    urls=admin_pages,
                    timeout=args.timeout,
                )
            )
            results.extend(
                run_category(
                    opener,
                    category="frontend_logged_in_cached",
                    run_id=run_id,
                    urls=sampled_pages,
                    timeout=args.timeout,
                )
            )
            results.extend(
                run_category(
                    opener,
                    category="frontend_logged_in_uncached_headers",
                    run_id=run_id,
                    urls=sampled_pages,
                    timeout=args.timeout,
                    headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
                )
            )
    else:
        print("\nSkipping wp-admin checks.")
        if admin_tests_skipped_reason == "missing_credentials":
            print("Reason: username/password were not provided.")
        elif admin_tests_skipped_reason == "login_failed":
            print("Reason: login failed with provided credentials.")

    summary = build_summary(results)
    frontend_delta = build_per_page_delta(
        results,
        cached_category="frontend_cached",
        uncached_category="frontend_uncached",
        uncached_drop_params={"no-cache"},
    )
    logged_in_frontend_delta = build_per_page_delta(
        results,
        cached_category="frontend_logged_in_cached",
        uncached_category="frontend_logged_in_uncached_headers",
        uncached_drop_params=None,
    )
    print_summary_table(summary)
    print_slowest(results, limit=10)
    print_per_page_delta_table("frontend logged-out", frontend_delta, limit=10)
    print_per_page_delta_table("frontend logged-in", logged_in_frontend_delta, limit=10)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "config": {
            "runs": args.runs,
            "sample_size": args.sample_size,
            "timeout": args.timeout,
            "seed": args.seed,
            "insecure": args.insecure,
            "admin_checks_attempted": bool(args.username and args.password),
            "admin_checks_executed": logged_in,
        },
        "sampled_pages": sampled_pages,
        "summary": summary,
        "per_page_deltas": {
            "frontend_logged_out": frontend_delta,
            "frontend_logged_in": logged_in_frontend_delta,
        },
        "results": [asdict(row) for row in results],
    }

    if args.output:
        output_path = Path(args.output)
        output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nSaved JSON report: {output_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
