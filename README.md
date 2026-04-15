# PerfTest

WordPress performance test script for:
- Frontend pages with cache bypass (`?no-cache=<random>`)
- Frontend pages with normal cached requests
- Optional logged-in checks for wp-admin pages and the same frontend pages

## Script

`wp_perf_test.py`

## Requirements

- Python 3.9+
- Target site should be a WordPress site

No third-party Python packages are required.

## What it does

1. Discovers URLs from common sitemap entry points:
   - `/sitemap.xml`
   - `/wp-sitemap.xml`
   - `/sitemap_index.xml`
   - network timeouts on individual sitemap files are skipped (non-fatal), so one slow sitemap does not abort the whole run
2. Randomly samples 10 frontend pages (configurable), always including homepage.
3. Runs multiple iterations (configurable) of:
   - `frontend_uncached`: appends `?no-cache=<random>`
   - `frontend_cached`: normal request
4. If `--username` and `--password` are provided and login succeeds, it also tests:
   - Dashboard: `/wp-admin/`
   - Plugins: `/wp-admin/plugins.php`
   - Posts: `/wp-admin/edit.php`
   - Same sampled frontend pages while logged in:
     - cached
     - uncached-like (via `Cache-Control: no-cache` and `Pragma: no-cache` headers)
5. Prints a speed breakdown (avg, median, p95, min, max) by category and the slowest requests.
6. Prints per-page delta tables for frontend:
   - logged-out delta: `frontend_uncached - frontend_cached`
   - logged-in delta: `frontend_logged_in_uncached_headers - frontend_logged_in_cached`

If credentials are not provided, wp-admin checks are skipped automatically.

## Usage

### Basic run (frontend only)

```bash
python3 wp_perf_test.py --base-url https://example.com
```

### Custom runs and output report

```bash
python3 wp_perf_test.py \
  --base-url https://example.com \
  --runs 5 \
  --sample-size 10 \
  --output report.json
```

### With wp-admin login checks

```bash
python3 wp_perf_test.py \
  --base-url https://example.com \
  --username admin \
  --password 'your-password' \
  --runs 3 \
  --output report.json
```

### Helpful options

- `--timeout 20` request timeout in seconds
- `--seed 123` deterministic random sampling
- `--insecure` disable TLS verification for self-signed staging certs

## Output

Console output includes:
- sampled URLs
- per-category performance summary
- top slowest requests

Optional JSON report (`--output`) includes full request-level details for automation.
It also includes `per_page_deltas` for direct cached vs uncached comparison per URL.
