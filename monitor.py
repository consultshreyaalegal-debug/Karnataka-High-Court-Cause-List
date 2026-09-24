from __future__ import annotations

import json
import os
import re
import sys
import time
from io import StringIO
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
BENCH_VALUES = {
    "Bengaluru Bench": "B",
    "Dharwad Bench": "D",
    "Kalaburagi Bench": "K",
}
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "7"))
FUZZY_CONFIRM = int(os.getenv("FUZZY_CONFIRM", "95"))
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


def goto_with_retries(page, url: str, attempts: int = 3):
    last_error = None
    for attempt in range(attempts):
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=60000)
            if response is None or response.status >= 500:
                status = response.status if response else "no response"
                raise RuntimeError(f"Court page returned HTTP {status}")
            if response.status >= 400:
                raise RuntimeError(f"Court page returned HTTP {response.status}")
            return response
        except Exception as exc:
            last_error = exc
            if attempt + 1 < attempts:
                page.wait_for_timeout(1000 * (attempt + 1))
    raise RuntimeError(f"Court page failed after {attempts} attempts: {last_error}") from last_error


def run_search_with_retries(search, *args, attempts: int = 2):
    last_error = None
    for attempt in range(attempts):
        try:
            return search(*args)
        except Exception as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(1)
    raise RuntimeError(f"Court search failed after {attempts} attempts: {last_error}") from last_error


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


def wait_for_result(page, label: str) -> None:
    deadline = time.monotonic() + 60
    result = page.locator("#listDisp")
    result.wait_for(state="visible", timeout=60000)
    while time.monotonic() < deadline:
        if result.inner_text().strip():
            return
        page.wait_for_timeout(250)
    raise RuntimeError(f"Court {label} returned no result within 60 seconds")


def run_advocate_search(page, bench: str, advocate_query: str, start: date, end: date) -> str:
    goto_with_retries(page, BASE_URL)

    advocate_tab = page.locator('a[data-toggle="tab"][href="#tab-1-content"]')
    if advocate_tab.count() != 1:
        raise RuntimeError("Verified advocate-search tab is missing")
    advocate_tab.click()
    form = page.locator('form:has(select[name="adv_bench"])')
    bench_select = form.locator('select[name="adv_bench"]')
    if bench_select.count() != 1:
        raise RuntimeError("Verified advocate-search bench selector is missing")
    bench_select.select_option(BENCH_VALUES[bench])
    form.locator('input[name="advName"]').wait_for(state="visible", timeout=30000)
    form.locator('input[name="advName"]').fill(advocate_query)
    form.locator('input[name="afromDt"]').fill(start.strftime("%d/%m/%Y"))
    form.locator('input[name="Regno"]').fill("")

    result = form.locator('input[name="getData"][value="GET LIST"]')
    if result.count() != 1:
        raise RuntimeError("Verified advocate-search GET LIST control is missing")
    with page.expect_response(
        lambda response: "causeListSearchAdvocateResp.php" in response.url,
        timeout=60000,
    ) as result_response:
        result.click()
    response = result_response.value
    if response.status >= 400:
        raise RuntimeError(f"Court advocate search returned HTTP {response.status}")
    wait_for_result(page, "advocate search")
    return page.content()


def run_general_advocate_search(page, bench: str, advocate_query: str, start: date, end: date) -> str:
    """Fetch the Court's full cause-list blocks for authoritative list headings."""
    goto_with_retries(page, BASE_URL)
    advocate_tab = page.locator('a[data-toggle="tab"][href="#tab-0-content"]')
    if advocate_tab.count() != 1:
        raise RuntimeError("Verified general-search tab is missing")
    advocate_tab.click()
    form = page.locator('form:has(select[name="bench"])')
    form.locator('select[name="bench"]').select_option(BENCH_VALUES[bench])
    form.locator('select[name="searchby"]').select_option("3")
    form.locator('input[name="advName"]').wait_for(state="visible", timeout=30000)
    form.locator('input[name="advName"]').fill(advocate_query)
    form.locator('input[name="fromDt"]').fill(start.strftime("%d/%m/%Y"))
    form.locator('input[name="toDt"]').fill(end.strftime("%d/%m/%Y"))
    button = form.locator('input[name="getData"][value="GET DETAILS"]')
    if button.count() != 1:
        raise RuntimeError("Verified general-search GET DETAILS control is missing")
    with page.expect_response(
        lambda response: "causeListSearchResp.php" in response.url,
        timeout=60000,
    ) as result_response:
        button.click()
    response = result_response.value
    if response.status >= 400:
        raise RuntimeError(f"Court general search returned HTTP {response.status}")
    wait_for_result(page, "general search")
    return page.content()


def tables_from_html(html: str) -> list[pd.DataFrame]:
    from bs4 import BeautifulSoup

    def extract_case_number(value: str) -> str:
        match = re.search(
            r"\b(?:WP|RFA|RP|RSA|CRL\.?P|W\.A|MFA|COMAP|CCC|CP|W\.P)\s*[A-Z0-9./-]*\d+[A-Z0-9./-]*\b",
            value,
            re.I,
        )
        return match.group(0).strip() if match else ""

    soup = BeautifulSoup(html, "lxml")
    result = soup.select_one("#listDisp")
    if result is None:
        return []

    tables: list[pd.DataFrame] = []
    for table in result.select("table"):
        rows: list[list[str]] = []
        all_rows = table.select("tr")
        if not all_rows:
            continue

        header_map: dict[str, int] = {}
        for row_index, row in enumerate(all_rows):
            cells = row.find_all(["th", "td"], recursive=False)
            if not cells:
                continue
            values = [cell.get_text(" ", strip=True) for cell in cells]
            if not values:
                continue
            text_blob = " ".join(norm(v) for v in values if v)
            if any(token in text_blob for token in ("case number", "case no", "advocate", "petitioner", "respondent", "session", "item", "serial")):
                for col_index, value in enumerate(values):
                    lb = norm(value)
                    if "case no" in lb or "case number" in lb:
                        header_map["case_number"] = col_index
                    elif "case type" in lb:
                        header_map["case_type"] = col_index
                    elif "petitioner" in lb or "respondent" in lb or "parties" in lb or "cause title" in lb:
                        header_map["party"] = col_index
                    elif "advocate" in lb or "counsel" in lb or "party in person" in lb:
                        header_map["advocate"] = col_index
                    elif "session" in lb or "list type" in lb or "hearing" in lb:
                        header_map["session"] = col_index
                    elif "item" in lb or "sl no" in lb or "serial" in lb:
                        header_map["item"] = col_index
                if header_map:
                    for data_row in all_rows[row_index + 1 :]:
                        data_cells = data_row.find_all(["th", "td"], recursive=False)
                        if not data_cells:
                            continue
                        data_values = [cell.get_text(" ", strip=True) for cell in data_cells]
                        if len(data_values) <= max(header_map.values(), default=-1):
                            continue
                        case_number = data_values[header_map.get("case_number", -1)] if "case_number" in header_map else ""
                        if not case_number:
                            case_number = next((v for v in data_values if extract_case_number(v)), "")
                        if not extract_case_number(case_number):
                            continue

                        party_text = data_values[header_map["party"]] if "party" in header_map and header_map["party"] < len(data_values) else ""
                        session_text = data_values[header_map["session"]] if "session" in header_map and header_map["session"] < len(data_values) else ""
                        item_value = data_values[header_map["item"]] if "item" in header_map and header_map["item"] < len(data_values) else ""

                        advocate_cell = data_cells[header_map["advocate"]] if "advocate" in header_map and header_map["advocate"] < len(data_cells) else None
                        if advocate_cell is not None:
                            italic_text = " ".join(node.get_text(" ", strip=True) for node in advocate_cell.find_all("i"))
                            for italic in advocate_cell.find_all("i"):
                                italic.extract()
                            advocate_text = advocate_cell.get_text(" ", strip=True)
                            if italic_text and not advocate_text:
                                advocate_text = italic_text
                        else:
                            advocate_text = ""

                        rows.append([
                            "",
                            "",
                            "",
                            item_value,
                            case_number,
                            session_text,
                            party_text,
                            advocate_text,
                        ])
                    break

        if rows:
            tables.append(pd.DataFrame(rows, columns=[
                "Date List", "Hall No.", "List No.", "Item", "Case Number", "Session Type",
                "Petitioner/Respondent", "Advocate",
            ]))
            continue

        for row in all_rows:
            cells = row.find_all(["th", "td"], recursive=False)
            values = [cell.get_text(" ", strip=True) for cell in cells]
            if len(values) < 6:
                continue
            if len(values) >= 6 and norm(values[0]) == "date list" and "case no" in norm(values[4]):
                continue
            case_numbers = [extract_case_number(v) for v in values]
            case_index = next((i for i, v in enumerate(case_numbers) if v), None)
            if case_index is None:
                continue
            advocate_cell = cells[-1]
            party_text = " ".join(node.get_text(" ", strip=True) for node in advocate_cell.find_all("i"))
            for italic in advocate_cell.find_all("i"):
                italic.extract()
            advocate_text = advocate_cell.get_text(" ", strip=True)
            if not advocate_text:
                advocate_text = party_text

            session_text = values[1] if len(values) > 1 else ""
            rows.append([
                values[0] if len(values) > 0 else "",
                values[1] if len(values) > 1 else "",
                values[2] if len(values) > 2 else "",
                values[0] if len(values) > 0 else "",
                values[case_index] if case_index < len(values) else "",
                session_text,
                values[4] if len(values) > 4 else "",
                advocate_text,
            ])
        if rows:
            tables.append(pd.DataFrame(rows, columns=[
                "Date List", "Hall No.", "List No.", "Item", "Case Number", "Session Type",
                "Petitioner/Respondent", "Advocate",
            ]))
    return tables


def html_text(html: str) -> str:
    # Use pandas tables separately; this body text is for contextual extraction.
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    return "\n".join(x.strip() for x in soup.get_text("\n").splitlines() if x.strip())


def general_list_metadata(html: str) -> dict[str, dict[str, str]]:
    """Extract list headings from the Court's full cause-list blocks."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    metadata: dict[str, dict[str, str]] = {}
    hall = list_no = judge = session = ""
    for row in soup.select("#listDisp tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        text = clean_advocate_text(row.get_text(" ", strip=True))
        if not text:
            continue
        hall_match = re.search(r"COURT HALL NO\s*:\s*(.*?)(?:\s+CAUSE LIST NO\b|$)", text, re.I)
        if hall_match:
            hall = hall_match.group(1).strip()
            list_match = re.search(r"CAUSE LIST NO\.?\s*[:.]?\s*(\S+)", text, re.I)
            list_no = list_match.group(1) if list_match else ""
            session = ""
            continue
        if len(cells) == 1 and re.search(r"^(?:THE HON|HON'BLE|HON`BLE)", text, re.I):
            judge = re.sub(r"^.*?\b(?:JUSTICE|JUDGE|REGISTRAR)\s+", "", text, flags=re.I).strip()
            continue
        if len(cells) == 1 and not re.search(
            r"^(?:IN THE HIGH COURT|ON |PHYSICAL |AT |BEFORE|\(To get|\(For |JOIN VC|WEBSITE:|PUBLISHED|PRINTED)",
            text,
            re.I,
        ):
            if not re.search(r"^(?:Sl\.No\.?|Case No\.)", text, re.I):
                session = text
            continue
        if len(cells) < 5 or not re.search(r"\b(?:WP|RFA|RP|CRL|W\.A|MFA|COMAP|CCC|CP)\b", text, re.I):
            continue
        case_text = clean_advocate_text(cells[1].get_text(" ", strip=True))
        case_match = re.search(
            r"\b(?:WP|RFA|RP|RSA|CRL\.?P|W\.A|MFA|COMAP|CCC|CP)\s*[A-Z0-9./-]*\d+[A-Z0-9./-]*",
            case_text,
            re.I,
        )
        case_number = case_match.group(0).strip() if case_match else ""
        serial = clean_advocate_text(cells[0].get_text(" ", strip=True))
        if case_number and serial:
            metadata[case_number] = {
                "serial": serial,
                "session": session or (f"Cause List No. {list_no}" if list_no else ""),
                "hall": hall,
                "judge": judge,
            }
    return metadata


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
        r"(Bengaluru|Bangalore|Dharwad|Kalaburagi|Kalburagi)\s*:\s*", re.I
    )

    for bench, aliases in BENCH_ALIASES.items():
        snippet = ""
        for i, line in enumerate(lines):
            matches = list(bench_pattern.finditer(line))
            for match_index, m in enumerate(matches):
                label = m.group(1).lower()
                if not any(label == a.lower() for a in aliases):
                    continue
                end = matches[match_index + 1].start() if match_index + 1 < len(matches) else len(line)
                snippet = line[m.end():end].strip()
                if not snippet and i + 1 < len(lines):
                    nxt = lines[i + 1]
                    if not bench_pattern.search(nxt):
                        snippet = nxt
                break
            if snippet:
                break

        low = snippet.lower()
        pending = (
            "final cause list pending" in low
            or "wait for final" in low
            or "awaiting final" in low
        )
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


def _column_index(columns, patterns: tuple[str, ...]) -> int | None:
    for index, column in enumerate(columns):
        label = norm(column)
        if any(pattern in label for pattern in patterns):
            return index
    return None


def row_records(df: pd.DataFrame, page_lines: list[str], bench: str, query: str) -> tuple[list[dict], list[dict]]:
    confirmed, uncertain = [], []
    df = df.fillna("").astype(str)
    columns = [str(column) for column in df.columns]
    advocate_columns = [
        index for index, column in enumerate(columns)
        if any(token in norm(column) for token in ("advocate", "counsel", "party in person"))
    ]
    if not advocate_columns:
        return confirmed, uncertain
    item_index = _column_index(columns, ("item", "serial", "sl no", "sr no"))
    session_index = _column_index(columns, ("session", "list type", "hearing"))
    case_number_index = _column_index(columns, ("case no", "case number"))
    case_type_index = _column_index(columns, ("case type",))
    party_index = _column_index(columns, ("petitioner", "respondent", "parties", "cause title"))
    for _, row in df.iterrows():
        vals = [clean_advocate_text(v) for v in row.tolist()]
        row_text = " | ".join(vals)
        candidates = [vals[index] for index in advocate_columns]
        match = max((match_advocate(c) for c in candidates), key=lambda x: x[1])
        group, score, variant = match
        if not group or score < FUZZY_UNCERTAIN:
            continue
        case_number = vals[case_number_index] if case_number_index is not None else ""
        if not case_number:
            for v in vals:
                m = re.search(r"\b(?:WP|RFA|RP|CRL\.?P|W\.A|MFA|COMAP|CCC|CP|W\.P)\s*[A-Z0-9./-]*\d+[A-Z0-9./-]*\b", v, re.I)
                if m:
                    case_number = m.group(0).strip()
                    break
        if not case_number:
            continue

        ctx = find_case_context(page_lines, case_number)
        party_text = vals[party_index] if party_index is not None else row_text
        petitioner, respondent = extract_party_pair(party_text)
        if not petitioner or not respondent:
            # Try a small page-text context around the first case occurrence.
            for line in page_lines:
                if case_number.replace(" ", "").lower() in line.replace(" ", "").lower():
                    petitioner, respondent = extract_party_pair(line)
                    if petitioner and respondent:
                        break
        party_pair = f"{petitioner} V/s {respondent}" if petitioner and respondent else ""
        item = vals[item_index] if item_index is not None else ctx.get("list_no", "")
        session = vals[session_index] if session_index is not None else ctx.get("session", "")
        rec = {
            "Court Hall & Bench (Name of Judges)": f"{bench} | Court Hall {vals[1] if len(vals) > 1 else ''} | {ctx.get('judges','')}".strip(" |"),
            "Item / Serial Number & Session Type": " - ".join(part for part in (item, session) if part) or f"Search date seed: {query}",
            "Case Number": case_number,
            "Case Type": vals[case_type_index] if case_type_index is not None else "",
            "Petitioner V/s Respondent": party_pair,
            "Advocate Name & Variant Matched": f"{variant} (matched to {group}, score {score})",
            "Bench": bench,
            "Search Seed": query,
        }
        (confirmed if score >= FUZZY_CONFIRM else uncertain).append(rec)
    return confirmed, uncertain


def enrich_case_details(page, html: str, records: list[dict]) -> None:
    """Fill required party, classification, and judge fields from Court case links."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    details = {}
    for link in soup.select("#listDisp a[href*='causelistcasestatus']"):
        case_number = clean_advocate_text(link.get_text(" ", strip=True))
        match = re.search(r'causelistcasestatus\("([^"]+)', link.get("href", ""))
        if not case_number or not match:
            continue
        response = None
        last_error = None
        for attempt in range(3):
            try:
                response = page.request.get(
                    f"{BASE_URL.rsplit('/', 1)[0]}/casestatushck.php?params={match.group(1)}",
                    timeout=60000,
                )
                break
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    page.wait_for_timeout(1000 * (attempt + 1))
        if response is None:
            raise RuntimeError(f"Case-status request failed after 3 attempts: {last_error}")
        if response.status >= 400:
            raise RuntimeError(f"Case-status request returned HTTP {response.status}")
        case_soup = BeautifulSoup(response.text(), "lxml")
        def value(element_id: str) -> str:
            node = case_soup.select_one(f"#{element_id}")
            return clean_advocate_text(node.get_text(" ", strip=True)) if node else ""
        details[case_number] = {
            "petitioner": value("petitioner"),
            "respondent": value("respondent"),
            "case_type": value("classification"),
            "judge": value("judge"),
        }
    for record in records:
        detail = details.get(record.get("Case Number", ""), {})
        if detail.get("petitioner") and detail.get("respondent"):
            record["Petitioner V/s Respondent"] = f"{detail['petitioner']} V/s {detail['respondent']}"
        record["Case Type"] = detail.get("case_type", record.get("Case Type", ""))
        if detail.get("judge"):
            record["Court Hall & Bench (Name of Judges)"] += f" | {detail['judge']}"


def apply_list_metadata(records: list[dict], metadata: dict[str, dict[str, str]]) -> None:
    for record in records:
        detail = metadata.get(record.get("Case Number", ""))
        if not detail:
            continue
        session = detail.get("session", "")
        serial = detail.get("serial", "")
        if serial and session:
            record["Item / Serial Number & Session Type"] = f"Item {serial} - {session}"
        elif serial:
            record["Item / Serial Number & Session Type"] = f"Item {serial}"
        if detail.get("hall"):
            record["Court Hall & Bench (Name of Judges)"] = (
                f"{record['Bench']} | Court Hall {detail['hall']}"
                + (f" | {detail['judge']}" if detail.get("judge") else "")
            )


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


def run_local_test() -> int:
    """Exercise output routing and table parsing without a browser or network."""
    fixture = ROOT / "fixtures" / "local_result.html"
    html = fixture.read_text(encoding="utf-8")
    lines = html_text(html).splitlines()
    statuses = {
        "Bengaluru Bench": {"status": "FINAL", "updated_at": "18-09-2026 @ 07:10 PM", "source_text": "18-09-2026 @ 07:10 PM"},
        "Dharwad Bench": {"status": "TENTATIVE", "updated_at": "", "source_text": "Final Cause List Pending"},
        "Kalaburagi Bench": {"status": "FINAL", "updated_at": "18-09-2026 @ 07:16 PM", "source_text": "18-09-2026 @ 07:16 PM"},
    }
    confirmed, uncertain, tentative = [], [], []
    for bench in BENCHES:
        found, review = [], []
        for table in tables_from_html(html):
            current, manual = row_records(table, lines, bench, "local-test")
            found.extend(current)
            review.extend(manual)
        if statuses[bench]["status"] == "TENTATIVE":
            tentative.extend(found + review)
        else:
            confirmed.extend(found)
            uncertain.extend(review)
    save_results(confirmed, uncertain, tentative, statuses)
    summary = {
        "run_date": str(date.today()),
        "search_end_date": str(date.today()),
        "confirmed_count": len(dedupe(confirmed)),
        "uncertain_count": len(dedupe(uncertain)),
        "tentative_count": len(dedupe(tentative)),
        "bench_status": statuses,
        "errors": [],
        "mode": "local-test",
    }
    (RESULTS / "run_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


def main():
    if "--local-test" in sys.argv:
        return run_local_test()
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
                    html = run_search_with_retries(run_advocate_search, page, bench, query, start, end)
                    text = html_text(html)
                    lines = text.splitlines()
                    tables = tables_from_html(html)
                    page_conf, page_unc = [], []
                    for table in tables:
                        c, u = row_records(table, lines, bench, query)
                        page_conf.extend(c)
                        page_unc.extend(u)
                    enrich_case_details(page, html, page_conf + page_unc)
                    general_html = run_search_with_retries(run_general_advocate_search, page, bench, query, start, end)
                    apply_list_metadata(page_conf + page_unc, general_list_metadata(general_html))
                    # Fallback to raw page text if no tables are available.
                    if not tables:
                        result_text = norm(text)
                        if not result_text:
                            errors.append(f"{bench} / {query}: #listDisp is empty")
                        elif "no records found" not in result_text and "advocate name should not be more than 2 words" not in result_text:
                            errors.append(f"{bench} / {query}: unexpected #listDisp HTML without result table")
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
    # Partial results are unsafe to publish because a failed bench can look like no match.
    if errors or any(status.get("status") == "UNKNOWN" for status in statuses.values()):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
