# Weather Data Source Audit

Pipeline running since ~2026-05-18. Audit started 2026-05-21.

Row counts as of 2026-05-21:

| Source       | Rows    | Files | Table              | Notes |
|--------------|---------|-------|--------------------|-------|
| METAR        | 2,336   | 25    | observations       |       |
| CLI          | 1,740   | 9     | observations       |       |
| CLIMO        | 7,320   | 1     | static/climatology |       |
| GFS_MOS      | 11,285  | 24    | daily_forecasts    |       |
| NWS_FORECAST | 40,785  | 34    | daily_forecasts    |       |
| NBM          | 44,640  | 34    | daily_forecasts    |       |
| GFS_LAMP     | 85,690  | 23    | hourly_forecasts   |       |
| HRRR         | 10,060  | 8     | hourly_forecasts   |       |
| GEFS         | 286,680 | 25    | hourly_forecasts   |       |
| ECMWF_DET    | MISSING | —     | hourly_forecasts   |       |
| ECMWF_ENS    | MISSING | —     | hourly_forecasts   |       |
| KALSHI_SNAP  | 50,852  | 33    | market/snapshots   |       |
| KALSHI_RES   | 15,642  | 9     | market/results     |       |

---

## 1. METAR

**Status:** OK with caveats

**Notes:**
- First ~day of data (May 19 through early May 20) is noisy — pipeline was restarting frequently, poll times are irregular and some runs were very short. Treat this period as unreliable.
- From ~May 20 17:30Z onwards, polls align cleanly to 10-minute boundaries.
- Some 10-minute slots are missing even in the clean period. Each missing slot is a poll that returned 0 rows. No errors or warnings in logs, so the likely cause is IEM returning an empty response for the 2-hour window (data processing latency on their end). The source catches empty responses silently without logging.
- Normal flush produces 120 rows = 6 polls × 20 stations. This is consistent throughout the clean period.

---

## 2. CLI

**Status:** Bug fixed — data going forward is correct; historical rows have null precipitation

**Notes:**
- Shares the `observations` table with METAR but only populates high_f, low_f, avg_f, precip_in, snow_in. All METAR-specific fields (dewpoint, wind, etc.) are intentionally null.
- **Bug (fixed 2026-05-21):** Precipitation was null for all 1,740 historical rows. The regex expected the value on the same line as "PRECIPITATION (IN)" but the actual format puts it on the next line under "TODAY". Fixed to handle both multi-line and inline formats.
- Historical parquet rows cannot be backfilled — nulls will remain for the first ~3 days of data.
- Mid-May precipitation where present: Denver 0.30", Chicago 0.39", trace in Austin and Houston. Zero for most other stations — consistent with dry conditions.

---

## 3. CLIMO

**Status:** OK — re-fetched 2026-05-22 with corrected schema

**Notes:**
- One-time download from NCEI 1991-2020 daily normals. 20 stations × 366 days = 7,320 rows.
- Originally stored `record_high_f` / `record_low_f` which were always null — those columns don't exist in the 1991-2020 normals dataset.
- **Fixed 2026-05-22:** replaced with `stddev_high_f` / `stddev_low_f` (DLY-TMAX-STDDEV / DLY-TMIN-STDDEV), which are fully populated and directly useful for modeling temperature variability.
- All-time records are not available in this dataset — would require a separate GHCND source.

---

## 4. GFS_MOS

**Status:**
**Notes:**

---

## 5. NWS_FORECAST

**Status:**
**Notes:**

---

## 6. NBM

**Status:**
**Notes:**

---

## 7. GFS_LAMP

**Status:**
**Notes:**

---

## 8. HRRR

**Status:** Bug fixed — all historical data deleted and will re-accumulate

**Notes:**
- **Bug (fixed 2026-05-22):** All 20 stations returned identical temperatures (49.05°F) for every forecast hour. HRRR's native Lambert Conformal grid uses 0-360 longitude (225°–299° for CONUS), but station longitudes were passed in -180 to 180 format. The nearest-neighbor search computed huge distances for all stations and converged on the same wrong grid point.
- Fix: `lon = station.lon % 360` before the distance calculation, consistent with GEFS and ECMWF.
- All historical HRRR parquet data deleted — it was entirely wrong.

---

## 9. GEFS

**Status:**
**Notes:**

---

## 10. ECMWF_DET

**Status:** Bug fixed — no historical data, will accumulate going forward

**Notes:**
- **Bug (fixed 2026-05-22):** Always produced 0 rows. Two issues:
  1. Herbie's subset extraction doesn't work for ECMWF's `.index` format — `H.xarray(VAR_TEMP)` threw FileNotFoundError, silently caught, returning empty dict.
  2. Longitude was converted with `% 360` but ECMWF oper uses -180 to 180.
- Fix: download full GRIB with `H.download()`, open with `cfgrib.open_datasets()` directly, use `station.lon` as-is. Full oper GRIB is ~140MB per fhr — acceptable.
- Variables available: `t2m`, `d2m`, `u10`, `v10`.

---

## 11. ECMWF_ENS

**Status:** Bug fixed — no historical data, will accumulate going forward

**Notes:**
- **Bug (fixed 2026-05-22):** Always produced 0 rows. Same Herbie subset extraction failure as ECMWF_DET plus wrong longitude (`% 360` instead of -180 to 180).
- Original architecture was fatally slow: 51 members × N fhrs individual downloads. Full ENFO GRIB is 6.5GB per fhr — impractical.
- Fix: parse ECMWF's `.index` JSON to find only `2t` byte offsets, fetch 50 range requests in parallel (10 workers), open concatenated result with cfgrib. Each fhr downloads ~32MB instead of 6.5GB.
- All 50 perturbed members (1-50) per fhr. Control member (0) omitted.
- Poll time: ~104s for 16 fhrs × 50 members = 16,000 rows. Acceptable at `min_poll_interval_sec=3600`.
- Must eagerly load numpy arrays before deleting temp file — cfgrib is lazy.

---

## 12. KALSHI_MARKETS (snapshots + results)

**Status:** Snapshots OK. Results had a gap (May 18–22) — fixed 2026-05-22.

**Notes:**
- market_snapshots: 468 rows per poll (39 series × 12 contracts each), looks correct.
- market_results: one row per finalized market, emitted once.
- **Bug (fixed 2026-05-22):** Results stopped being written after May 19. Race condition: `_seen_tickers.json` was saved immediately when tickers were added to `_emitted_results` during `poll()`, before the rows were written to parquet by the scheduler. If the pipeline restarted between those two steps, tickers were permanently marked "seen" with no parquet rows.
- Fix: `_save_seen()` now called via `confirm_results_written()` only after `storage.write()` succeeds in the scheduler.
- Recovery: removed 936 missing tickers from `_seen_tickers.json` so pipeline can re-emit them.
