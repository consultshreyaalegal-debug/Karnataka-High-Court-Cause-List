# Karnataka High Court Cause List Monitor

GitHub Actions monitor for the Karnataka High Court Cause List Search page:
https://judiciary.karnataka.gov.in/causelistSearch.php

## Schedule

The workflow runs **once per hour at 6:00 PM, 7:00 PM, 8:00 PM, 9:00 PM and 10:00 PM IST** every day.

```cron
0 18-22 * * *
```

The workflow also supports **Run workflow** for a manual test.

## Final / tentative rule

The monitor first reads the Court page's bench update indicator.

- A bench showing a date/time such as `17-09-2026 @ 08:13 PM` is treated as **FINAL / latest posted**.
- A bench showing **Final Cause List Pending** is treated as **TENTATIVE**.
- If neither can be determined, the status is **UNKNOWN** and the run summary records the source text for manual review.

Tentative matches are never put into the confirmed result file. They are written to `results/latest_tentative.*`.

## Benches

- Bengaluru Bench
- Dharwad Bench
- Kalaburagi Bench

## Advocate names

The configured groups and OCR/spelling variants are in `config.json`.

## Main output

`results/latest_matches.xlsx` contains exactly these columns:

1. Court Hall & Bench (Name of Judges)
2. Item / Serial Number & Session Type
3. Case Number
4. Case Type
5. Petitioner V/s Respondent
6. Advocate Name & Variant Matched

Manual-review and status files are also produced:

- `latest_uncertain.*`
- `latest_tentative.*`
- `bench_status.json`
- `bench_status.md`
- `run_summary.json`
