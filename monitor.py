from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from rapidfuzz import fuzz, process
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

BASE_URL = "https://judiciary.karnataka.gov.in/causelistSearch.php"
ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
ADVOCATE_GROUPS: dict[str, list[str]] = CONFIG["advocates"]
QUERY_SEEDS: list[str] = CONFIG.get("query_seeds") or [v[0] for v in ADVOCATE_GROUPS.values()]
BENCHES = ["Bengaluru Bench", "Dharwad Bench", "Kalaburagi Bench"]
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "7"))
FUZZY_CONFIRM = int(os.getenv("FUZZY_CONFIRM", "88"))
FUZZY_UNCERTAIN = int(os.getenv("FUZZY_UNCERTAIN", "74"))

HEADINGS = {
    "PRELIMINARY HEARING",
    "PRELIMINARY HEARING (READY IN NOTICE)",
    "ADMISSION",
    "ORDERS",
    "FURTHER HEARING",
    "FINAL HEARING",
    "REGULAR HEARING",
    "HEARING-IA",
    "HEARING - IA",
    "NOTICE",
    "FOR ORDERS",
    "DIRECTION",
    "COMPLIANCE",
    "NON-COMPLIANCE OF OFFICE-OBJNS FOR 3RD TIME",
    "FRESH MATTER/S",
}


def norm(s: str) -> str:
    s = str(s or "")
    s = s.replace("\u00a0", " ")
    s = re.sub(r"[\u2018\u2019\u201c\u201d]", "'", s)
    s = re.sub(r"[^A-Za-z0-9]+", " ", s).strip().lower()
    return re.sub(r"\s+", " ", s)


def compact(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", norm(s))


def best_match(text: str) -> tuple[str | None, int, str | None]:
    n = norm(text)
    c = compact(text)
    best = (None, 0, None)
    for group, variants in ADVOCATE_GROUPS.items():
        choices = variants + [group]
        for v in choices:
            nv = norm(v)
            score = max(fuzz.ratio(n, nv), fuzz.partial_ratio(n, nv)) if n and nv else 0
            # Short names require a little stricter treatment.
            if len(compact(v)) < 8:
                score = fuzz.ratio(c, compact(v))
            if score > best[1]:
                best = (group, int(score), v)
    return best


def match_advocate(text: str) -> tuple[str | None, int, str | None]:
    """Match the advocate column/text, conservatively.

    We score individual name fragments as well as the full cell because the Court
    sometimes emits names with initials, punctuation, or OCR spacing differences.
    """
    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    direct = best_match(raw)
    if direct[1] >= FUZZY_UNCERTAIN:
        return direct

    words = raw.split()
    candidates = []
    for width in range(min(6, len(words)), 1, -1):
        for i in range(0, len(words) - width + 1):
            candidates.append(" ".join(words[i : i + width]))
    best = direct
    for chunk in candidates[:80]:
        bm = best_match(chunk)
        if bm[1] > best[1]:
            best = bm
    return best


def choose_select(page, predicate) -> Any:
    for sel in page.locator("select").all():
        try:
            opts = sel.locator("option").all_text_contents()
            if any(predicate(o) for o in opts):
                return sel
        except Exception:
            continue
    raise RuntimeError("Could not identify required select field")


def choose_input(page, patterns: list[str], exclude_hidden=True):
    for inp in page.locator("input").all():
        try:
            if exclude_hidden and (inp.get_attribute("type") or "").lower() in {"hidden", "submit", "button", "image", "checkbox", "radio"}:
                continue
            blob = " ".join(
                [
                    inp.get_attribute("name") or "",
                    inp.get_attribute("id") or "",
                    inp.get_attribute("placeholder") or "",
                    inp.get_attribute("aria-label") or "",
                ]
            ).lower()
            if any(p.lower() in blob for p in patterns):
                return inp
        except Exception:
            continue
    return None


def set_date_input(inp, dt: date):
    if not inp:
        raise RuntimeError("Date input was not found")
    typ = (inp.get_attribute("type") or "text").lower()
    val = dt.strftime("%Y-%m-%d") if typ == "date" else dt.strftime("%d/%m/%Y")
    inp.fill(val)


def click_get_details(page):
    candidates = [
        page.get_by_role("button", name=re.compile(r"GET DETAILS|GET LIST", re.I)),
        page.locator('input[type="submit"][value*="GET" i]'),
        page.locator('button:has-text("GET DETAILS")'),
        page.locator('button:has-text("GET LIST")'),
    ]
    for loc in candidates:
        try:
            if loc.count():
                loc.first.click()
                return
        except Exception:
            pass
    raise RuntimeError("Could not find GET DETAILS / GET LIST button")


def run_advocate_search(page, bench: str, advocate_query: str, start: date, end: date) -> str:
    page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(700)

    bench_select = choose_select(page, lambda x: "bengaluru" in x.lower() or "dharwad" in x.lower() or "kalaburagi" in x.lower() or "kalburagi" in x.lower())
    # Match court spelling used by the live select.
    target = "kalburagi bench" if "kalaburagi" in bench.lower() else bench.lower()
    options = bench_select.locator("option").all_text_contents()
    chosen = next((o for o in options if target in o.lower()), None)
    if chosen is None:
        chosen = next((o for o in options if bench.lower().split()[0] in o.lower()), None)
    if chosen is None:
        raise RuntimeError(f"Bench option not found: {bench}; options={options}")
    bench_select.select_option(label=chosen)

    search_by = choose_select(page, lambda x: x.strip().lower() == "advocate" or "advocate" == x.strip().lower())
    search_by.select_option(label=next(o for o in search_by.locator("option").all_text_contents() if o.strip().lower() == "advocate"))

    # The page can dynamically reveal the advocate field after selecting Advocate.
    page.wait_for_timeout(400)
    adv = choose_input(page, ["advocate"])
    if adv is None:
        raise RuntimeError("Advocate name input was not found")
    adv.fill(advocate_query)

    date_inputs = []
    for inp in page.locator("input").all():
        try:
            typ = (inp.get_attribute("type") or "text").lower()
            blob = " ".join([inp.get_attribute("name") or "", inp.get_attribute("id") or "", inp.get_attribute("placeholder") or ""]).lower()
            if typ == "date" or "dd/mm/yyyy" in blob or "date" in blob:
                if inp.is_visible():
                    date_inputs.append(inp)
        except Exception:
            pass
    # Deduplicate by DOM identity is awkward; just keep the first two usable inputs.
    if len(date_inputs) < 2:
        # Fall back to visible text inputs that look like date controls from nearby placeholders.
        for inp in page.locator('input[type="text"]').all():
            try:
                if inp.is_visible() and inp not in date_inputs:
                    ph = (inp.get_attribute("placeholder") or "").lower()
                    if "dd/mm/yyyy" in ph:
                        date_inputs.append(inp)
            except Exception:
                pass
    if len(date_inputs) < 2:
        raise RuntimeError("Could not locate both cause-list date fields")

    set_date_input(date_inputs[0], start)
    set_date_input(date_inputs[1], end)

    click_get_details(page)
    try:
        page.wait_for_load_state("networkidle", timeout=45000)
    except PlaywrightTimeoutError:
        page.wait_for_timeout(2500)
    return page.content()


def tables_from_html(html: str) -> list[pd.DataFrame]:
    try:
        return pd.read_html(html)
    except ValueError:
        return []


def html_text(html: str) -> str:
    # Use pandas tables separately; this body text is for contextual extraction.
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    return "\n".join(x.strip() for x in soup.get_text("\n").splitlines() if x.strip())


BENCH_ALIASES = {
    "Bengaluru Bench": ["Bengaluru", "Bangalore"],
    "Dharwad Bench": ["Dharwad"],
    "Kalaburagi Bench": ["Kalaburagi", "Kalburagi"],
}
TIMESTAMP_RE = re.compile(r"\b(\d{1,2}-\d{1,2}-\d{4})\s*@\s*(\d{1,2}:\d{2}\s*(?:AM|PM))\b", re.I)


def read_bench_update_status(page) -> dict[str, dict[str, str]]:
    """Read the Court's visible bench update indicator.

    User-defined interpretation:
      * a displayed date/time stamp = FINAL / latest posted list;
      * 'Final Cause List Pending' = TENTATIVE;
      * otherwise UNKNOWN.

    The parser deliberately isolates each bench's own status text so that a
    pending status for one bench does not accidentally affect another bench.
    """
    text = page.locator("body").inner_text(timeout=30000)
    lines = [re.sub(r"\s+", " ", x).strip() for x in text.splitlines() if x.strip()]
    statuses: dict[str, dict[str, str]] = {}

    bench_pattern = re.compile(
        r"^(Bengaluru|Bangalore|Dharwad|Kalaburagi|Kalburagi)\s*:\s*(.*)$", re.I
    )

    for bench, aliases in BENCH_ALIASES.items():
        snippet = ""
        for i, line in enumerate(lines):
            m = bench_pattern.match(line)
            if not m:
                continue
            label = m.group(1).lower()
            if not any(label == a.lower() for a in aliases):
                continue
            snippet = m.group(2).strip()
            if not snippet and i + 1 < len(lines):
                # Some renderings place the value on the following line.
                nxt = lines[i + 1]
                if not bench_pattern.match(nxt):
                    snippet = nxt
            break

        low = snippet.lower()
        pending = "final cause list pending" in low
        m = TIMESTAMP_RE.search(snippet)
        if pending:
            status = "TENTATIVE"
        elif m:
            status = "FINAL"
        else:
            status = "UNKNOWN"

        statuses[bench] = {
            "status": status,
            "updated_at": f"{m.group(1)} @ {m.group(2)}" if m else "",
            "source_text": snippet,
        }
    return statuses


def find_case_context(lines: list[str], case_number: str) -> dict[str, str]:
    joined = norm(case_number)
    idxs = [i for i, line in enumerate(lines) if joined and joined in norm(line)]
    if not idxs:
        return {}
    idx = idxs[0]
    back = lines[max(0, idx - 70) : idx + 3]
    court_hall = ""
    list_no = ""
    session = ""
    judges = ""
    for line in reversed(back):
        m = re.search(r"COURT\s*HALL\s*(?:NO\s*)?[:\-]?\s*([A-Z0-9A-Z\-/ ]+)", line, re.I)
        if m and not court_hall:
            court_hall = m.group(1).strip()
        m = re.search(r"CAUSE\s*LIST\s*NO\.?\s*[:\-]?\s*([A-Z0-9]+)", line, re.I)
        if m and not list_no:
            list_no = m.group(1).strip()
        u = re.sub(r"\s+", " ", line).strip().upper()
        if not session and (u in HEADINGS or any(h in u for h in HEADINGS)):
            session = line.strip()
    for j in range(max(0, idx - 30), idx):
        if lines[j].strip().upper() == "BEFORE":
            judge_lines = []
            for z in range(j + 1, min(idx, j + 9)):
                s = lines[z].strip()
                if not s:
                    continue
                if s.upper() in {"COURT HALL", "CAUSE LIST"}:
                    break
                judge_lines.append(s)
                if len(judge_lines) >= 4:
                    break
            judges = " ".join(judge_lines)
    return {"court_hall": court_hall, "list_no": list_no, "session": session, "judges": judges}


def extract_party_pair(text: str) -> tuple[str, str]:
    clean = re.sub(r"\s+", " ", text.replace("\u00a0", " ")).strip()
    # Normalize common labels but retain names.
    m = re.search(r"PET\s*:\s*(.*?)\s+RES\s*:\s*(.*)$", clean, re.I)
    if m:
        return m.group(1).strip(" -|"), m.group(2).strip(" -|")
    # Short cause-list format can use PET./RESP.
    m = re.search(r"PET\.?\s*:?\s*(.*?)\s+RES(?:P)?\.?\s*:?\s*(.*)$", clean, re.I)
    if m:
        return m.group(1).strip(" -|"), m.group(2).strip(" -|")
    return "", ""


def clean_advocate_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip(" -|\n\t")


def row_records(df: pd.DataFrame, page_lines: list[str], bench: str, query: str) -> tuple[list[dict], list[dict]]:
    confirmed, uncertain = [], []
    # Make string columns easy to inspect.
    df = df.fillna("").astype(str)
    for _, row in df.iterrows():
        vals = [clean_advocate_text(v) for v in row.tolist()]
        row_text = " | ".join(vals)
        # Look through individual cells and the row as a whole, but prefer advocate-labelled cells.
        candidates = vals + [row_text]
        match = max((match_advocate(c) for c in candidates), key=lambda x: x[1])
        group, score, variant = match
        if not group or score < FUZZY_UNCERTAIN:
            continue
        # Avoid false positives from the page's generic heading / navigation text.
        if not any(re.search(r"\b(?:WP|RFA|RP|CRL\.?P|W\.A|MFA|COMAP|CCC|CP|W\.P)\b", v, re.I) for v in vals):
            continue
        case_number = ""
        for v in vals:
            m = re.search(r"\b(?:WP|RFA|RP|CRL\.?P|W\.A|MFA|COMAP|CCC|CP|W\.P)\s*[A-Z0-9./-]*\d+[A-Z0-9./-]*\b", v, re.I)
            if m:
                case_number = m.group(0).strip()
                break
        if not case_number:
            continue

        ctx = find_case_context(page_lines, case_number)
        petitioner, respondent = extract_party_pair(row_text)
        if not petitioner or not respondent:
            # Try a small page-text context around the first case occurrence.
            for line in page_lines:
                if case_number.replace(" ", "").lower() in line.replace(" ", "").lower():
                    petitioner, respondent = extract_party_pair(line)
                    if petitioner and respondent:
                        break
        party_pair = f"{petitioner} V/s {respondent}" if petitioner and respondent else ""
        rec = {
            "Court Hall & Bench (Name of Judges)": f"{bench} | Court Hall {ctx.get('court_hall','')} | {ctx.get('judges','')}".strip(" |"),
            "Item / Serial Number & Session Type": f"List {ctx.get('list_no','')}; {ctx.get('session','')}; Date search seed: {query}",
            "Case Number": case_number,
            "Case Type": ctx.get("session", ""),
            "Petitioner V/s Respondent": party_pair,
            "Advocate Name & Variant Matched": f"{variant} (matched to {group}, score {score})",
            "Bench": bench,
            "Search Seed": query,
        }
        (confirmed if score >= FUZZY_CONFIRM else uncertain).append(rec)
    return confirmed, uncertain


def dedupe(records: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for r in records:
        key = (
            r.get("Bench"),
            r.get("Case Number"),
            r.get("Petitioner V/s Respondent"),
            r.get("Advocate Name & Variant Matched", "").split(" (matched")[0],
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def save_results(records: list[dict], uncertain: list[dict], tentative: list[dict], statuses: dict[str, dict[str, str]]):
    RESULTS.mkdir(parents=True, exist_ok=True)
    columns = [
        "Court Hall & Bench (Name of Judges)",
        "Item / Serial Number & Session Type",
        "Case Number",
        "Case Type",
        "Petitioner V/s Respondent",
        "Advocate Name & Variant Matched",
    ]
    df = pd.DataFrame(dedupe(records), columns=columns)
    du = pd.DataFrame(dedupe(uncertain), columns=columns)
    dt = pd.DataFrame(dedupe(tentative), columns=columns)
    df.to_excel(RESULTS / "latest_matches.xlsx", index=False)
    df.to_csv(RESULTS / "latest_matches.csv", index=False)
    du.to_excel(RESULTS / "latest_uncertain.xlsx", index=False)
    du.to_csv(RESULTS / "latest_uncertain.csv", index=False)
    dt.to_excel(RESULTS / "latest_tentative.xlsx", index=False)
    dt.to_csv(RESULTS / "latest_tentative.csv", index=False)
    (RESULTS / "bench_status.json").write_text(json.dumps(statuses, indent=2), encoding="utf-8")

    def md(frame: pd.DataFrame, title: str) -> str:
        lines = [f"# {title}", ""]
        if frame.empty:
            lines.append("No matches.")
        else:
            lines.append(frame.to_markdown(index=False))
        return "\n".join(lines) + "\n"

    (RESULTS / "latest_matches.md").write_text(md(df, "Confirmed Karnataka High Court Advocate Matches"), encoding="utf-8")
    (RESULTS / "latest_uncertain.md").write_text(md(du, "Uncertain / Manual Review Matches"), encoding="utf-8")
    (RESULTS / "latest_tentative.md").write_text(md(dt, "Tentative Cause List Matches - Final Cause List Pending"), encoding="utf-8")
    status_lines = ["# Cause List Posting Status", ""]
    for b, st in statuses.items():
        status_lines.append(f"- **{b}:** {st.get("status", "UNKNOWN")} {st.get("updated_at", "")}".rstrip())
    (RESULTS / "bench_status.md").write_text("\n".join(status_lines) + "\n", encoding="utf-8")


def main():
    start = date.today()
    end = start + timedelta(days=max(0, DAYS_AHEAD - 1))
    confirmed_all: list[dict] = []
    uncertain_all: list[dict] = []
    tentative_all: list[dict] = []
    errors: list[str] = []
    statuses: dict[str, dict[str, str]] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1200})
        page = context.new_page()
        page.set_default_timeout(30000)

        for bench in BENCHES:
            try:
                page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(500)
                status_map = read_bench_update_status(page)
                statuses[bench] = status_map.get(bench, {"status": "UNKNOWN", "updated_at": "", "source_text": ""})
            except Exception as exc:
                statuses[bench] = {"status": "UNKNOWN", "updated_at": "", "source_text": ""}
                errors.append(f"{bench}: status read failed: {type(exc).__name__}: {exc}")

            for query in QUERY_SEEDS:
                try:
                    html = run_advocate_search(page, bench, query, start, end)
                    text = html_text(html)
                    lines = text.splitlines()
                    tables = tables_from_html(html)
                    page_conf, page_unc = [], []
                    for table in tables:
                        c, u = row_records(table, lines, bench, query)
                        page_conf.extend(c)
                        page_unc.extend(u)
                    # Fallback to raw page text if no tables are available.
                    if not tables and text:
                        bm = match_advocate(text)
                        if bm[1] >= FUZZY_UNCERTAIN:
                            errors.append(f"{bench} / {query}: no result tables; possible page-text-only match, inspect run logs")
                    if statuses.get(bench, {}).get("status") == "TENTATIVE":
                        tentative_all.extend(page_conf)
                        tentative_all.extend(page_unc)
                    else:
                        confirmed_all.extend(page_conf)
                        uncertain_all.extend(page_unc)
                except Exception as exc:
                    errors.append(f"{bench} / {query}: {type(exc).__name__}: {exc}")
        browser.close()

    save_results(confirmed_all, uncertain_all, tentative_all, statuses)
    summary = {
        "run_date": str(start),
        "search_end_date": str(end),
        "confirmed_count": len(dedupe(confirmed_all)),
        "uncertain_count": len(dedupe(uncertain_all)),
        "tentative_count": len(dedupe(tentative_all)),
        "bench_status": statuses,
        "errors": errors,
    }
    (RESULTS / "run_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    # Don't fail the workflow just because one bench/query was unavailable; do fail on total inability.
    if not confirmed_all and not uncertain_all and not tentative_all and len(errors) >= len(BENCHES) * len(QUERY_SEEDS):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
