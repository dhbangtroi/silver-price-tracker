import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

import gspread
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import requests
from bs4 import BeautifulSoup
from oauth2client.service_account import ServiceAccountCredentials

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None


CSV_COLUMNS = ["crawled_at", "metal", "category", "product", "sell_price", "buy_price", "url"]
LOCAL_TZ = ZoneInfo("Asia/Ho_Chi_Minh") if ZoneInfo else None

ANCARAT_URL = "https://giabac.ancarat.com"
DOJI_URL = "https://giabac.doji.vn"
DOJI_DATA_URL = f"{DOJI_URL}/data/DataBac9991Luong.txt"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8",
}

REQUEST_TIMEOUT = 25
SHEET_NAME = os.getenv("G_SHEET_NAME", "Silver_Prices")
WORKSHEET_NAME = os.getenv("G_WORKSHEET_NAME", "Sheet1")
CHART_PATH = Path("silver_price_trend.png")


def local_now() -> datetime:
    return datetime.now(LOCAL_TZ) if LOCAL_TZ else datetime.now()


def format_dt(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def clean_price(text: str) -> str:
    return "".join(char for char in text if char.isdigit())


def get_gspread_client() -> gspread.Client:
    raw_json = os.environ["G_SHEET_JSON"]
    credentials_data = json.loads(raw_json)
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    credentials = ServiceAccountCredentials.from_json_keyfile_dict(credentials_data, scope)
    return gspread.authorize(credentials)


def open_worksheet() -> gspread.Worksheet:
    spreadsheet = get_gspread_client().open(SHEET_NAME)
    try:
        return spreadsheet.worksheet(WORKSHEET_NAME)
    except gspread.WorksheetNotFound:
        return spreadsheet.sheet1


def ensure_header(sheet: gspread.Worksheet) -> List[str]:
    header = sheet.row_values(1)
    if not header:
        sheet.append_row(CSV_COLUMNS)
        return CSV_COLUMNS

    missing = [column for column in CSV_COLUMNS if column not in header]
    if missing:
        raise RuntimeError(
            f"Google Sheet header is missing required columns: {', '.join(missing)}. "
            f"Expected at least: {', '.join(CSV_COLUMNS)}"
        )
    return header


def append_rows_to_sheet(sheet: gspread.Worksheet, rows: List[Dict[str, str]]) -> None:
    if not rows:
        raise RuntimeError("No price rows were crawled; refusing to append an empty run.")

    header = ensure_header(sheet)
    values = [[row.get(column, "") for column in header] for row in rows]
    sheet.append_rows(values, value_input_option="USER_ENTERED")


def fetch_text(url: str) -> str:
    response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    response.encoding = "utf-8-sig"
    return response.text


def crawl_ancarat() -> List[Dict[str, str]]:
    html = fetch_text(ANCARAT_URL)
    soup = BeautifulSoup(html, "html.parser")
    crawled_at = format_dt(local_now())
    rows: List[Dict[str, str]] = []
    current_category = ""

    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if not cells:
                continue

            texts = [cell.get_text(" ", strip=True) for cell in cells]
            joined = " ".join(texts).upper()
            if "BAN RA" in joined or "MUA VAO" in joined or "SAN PHAM" in joined:
                continue

            if len(cells) == 1 or (len(cells) >= 3 and not texts[1] and not texts[2]):
                if texts[0]:
                    current_category = texts[0]
                continue

            if len(cells) < 3:
                continue

            product_cell = cells[0]
            product_name = product_cell.get_text(" ", strip=True)
            link = product_cell.find("a")
            product_url = urljoin(ANCARAT_URL, link["href"]) if link and link.get("href") else ANCARAT_URL
            sell_price = clean_price(cells[1].get_text(" ", strip=True))
            buy_price = clean_price(cells[2].get_text(" ", strip=True))

            if product_name and (sell_price or buy_price):
                rows.append(
                    {
                        "crawled_at": crawled_at,
                        "metal": "silver",
                        "category": current_category,
                        "product": product_name,
                        "sell_price": sell_price,
                        "buy_price": buy_price,
                        "url": product_url,
                    }
                )

    return rows


def fetch_doji_latest_record() -> Optional[Tuple[int, int, datetime]]:
    lines = [line.strip() for line in fetch_text(DOJI_DATA_URL).splitlines() if line.strip()]
    if not lines:
        return None

    parts = [part.strip() for part in lines[-1].split("|")]
    if len(parts) != 3:
        raise RuntimeError(f"Unexpected DOJI data row: {lines[-1]}")

    buy_price = int(parts[0])
    sell_price = int(parts[1])
    source_time = datetime.strptime(parts[2], "%H:%M:%S %d/%m/%Y")
    return buy_price, sell_price, source_time


def crawl_doji() -> List[Dict[str, str]]:
    latest_record = fetch_doji_latest_record()
    if latest_record is None:
        return []

    buy_price, sell_price, source_time = latest_record
    crawled_at = format_dt(source_time)
    products = [
        ("BAC DOJI 99.9 - 1 LUONG", 1),
        ("BAC DOJI 99.9 - 5 LUONG", 5),
    ]

    return [
        {
            "crawled_at": crawled_at,
            "metal": "silver",
            "category": "BANG GIA BAC DOJI",
            "product": product,
            "sell_price": str(sell_price * multiplier),
            "buy_price": str(buy_price * multiplier),
            "url": DOJI_URL,
        }
        for product, multiplier in products
    ]


def crawl_prices() -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    errors: List[str] = []

    for crawler_name, crawler in (("Ancarat", crawl_ancarat), ("DOJI", crawl_doji)):
        try:
            crawled_rows = crawler()
            print(f"{crawler_name}: crawled {len(crawled_rows)} rows")
            rows.extend(crawled_rows)
        except Exception as exc:
            errors.append(f"{crawler_name}: {exc}")
            print(f"{crawler_name}: failed - {exc}", file=sys.stderr)

    if not rows:
        raise RuntimeError("All crawlers failed or returned no rows: " + "; ".join(errors))

    return rows


def sheet_records(sheet: gspread.Worksheet) -> List[Dict[str, str]]:
    records = sheet.get_all_records()
    return [{str(key): value for key, value in record.items()} for record in records]


def generate_chart(records: List[Dict[str, str]], output_path: Path) -> Optional[Path]:
    if not records:
        return None

    df = pd.DataFrame(records)
    if "crawled_at" not in df or "sell_price" not in df or "product" not in df:
        return None

    df["crawled_at"] = pd.to_datetime(df["crawled_at"], errors="coerce")
    df["sell_price"] = pd.to_numeric(df["sell_price"], errors="coerce")
    df = df.dropna(subset=["crawled_at", "sell_price", "product"]).sort_values("crawled_at")
    if df.empty:
        return None

    latest_products = df.tail(40)["product"].drop_duplicates().tail(5).tolist()
    plot_df = df[df["product"].isin(latest_products)].copy()
    plot_df = plot_df.groupby("product", group_keys=False).tail(20)

    plt.figure(figsize=(12, 7))
    for product, product_df in plot_df.groupby("product"):
        plt.plot(
            product_df["crawled_at"],
            product_df["sell_price"],
            marker="o",
            linewidth=1.8,
            label=product[:45],
        )

    plt.title("Silver Sell Price Trend", fontsize=15, fontweight="bold")
    plt.xlabel("Crawled at")
    plt.ylabel("VND")
    plt.grid(True, linestyle="--", alpha=0.35)
    plt.legend(fontsize=8)
    plt.xticks(rotation=35, ha="right")
    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close()
    return output_path


def build_summary(rows: List[Dict[str, str]]) -> str:
    lines = [f"Silver price crawl completed at {format_dt(local_now())}", ""]
    for row in rows[:12]:
        sell = f"{int(row['sell_price']):,}" if row.get("sell_price") else "N/A"
        buy = f"{int(row['buy_price']):,}" if row.get("buy_price") else "N/A"
        lines.append(f"{row['product']}: sell {sell} VND | buy {buy} VND")

    if len(rows) > 12:
        lines.append(f"...and {len(rows) - 12} more rows")
    return "\n".join(lines)


def telegram_api(method: str, **kwargs) -> None:
    token = os.environ["TG_TOKEN"]
    response = requests.post(
        f"https://api.telegram.org/bot{token}/{method}",
        timeout=REQUEST_TIMEOUT,
        **kwargs,
    )
    response.raise_for_status()


def send_telegram_message(text: str) -> None:
    telegram_api(
        "sendMessage",
        data={
            "chat_id": os.environ["TG_CHAT_ID"],
            "text": text[:4096],
            "disable_web_page_preview": True,
        },
    )


def send_telegram_photo(image_path: Path, caption: str) -> None:
    with image_path.open("rb") as photo:
        telegram_api(
            "sendPhoto",
            data={"chat_id": os.environ["TG_CHAT_ID"], "caption": caption[:1024]},
            files={"photo": photo},
        )


def run_cloud_sync() -> None:
    for env_name in ("G_SHEET_JSON", "TG_TOKEN", "TG_CHAT_ID"):
        if not os.getenv(env_name):
            raise RuntimeError(f"Missing required environment variable: {env_name}")

    rows = crawl_prices()
    sheet = open_worksheet()
    append_rows_to_sheet(sheet, rows)
    records = sheet_records(sheet)
    chart_path = generate_chart(records, CHART_PATH)

    summary = build_summary(rows)
    send_telegram_message(summary)
    if chart_path:
        send_telegram_photo(chart_path, "Silver price trend from Google Sheets history")

    print(f"Appended {len(rows)} rows to {SHEET_NAME}/{sheet.title}")


if __name__ == "__main__":
    run_cloud_sync()
