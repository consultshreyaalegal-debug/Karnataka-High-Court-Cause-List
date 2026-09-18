# GitHub setup

1. Create a **private** GitHub repository, for example:
   `karnataka-hc-causelist-monitor`
2. Upload the contents of this folder to the repository root, preserving:
   `.github/workflows/cause-list-monitor.yml`
3. Commit to the repository's **default branch**.
4. Open **Actions** → **Karnataka HC Cause List Monitor**.
5. Click **Run workflow** once to test it.
6. Thereafter GitHub Actions will run automatically at:
   - 6:00 PM IST
   - 7:00 PM IST
   - 8:00 PM IST
   - 9:00 PM IST
   - 10:00 PM IST

No Python installation is required on your Mac. GitHub installs Python, dependencies and Chromium on the hosted runner.

## Important

GitHub scheduled workflows can occasionally be delayed during periods of high Actions load, especially around the start of an hour. This repository deliberately uses the exact hourly times requested: 6 PM through 10 PM IST.

The Court page is checked before advocate searches are run. A timestamp beside a bench is treated as the latest/final posting; `Final Cause List Pending` routes any discovered matches into the tentative output instead.
