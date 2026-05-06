"""Cloud silver price crawler and Telegram chart notifier.

This script crawls Ancarat and DOJI silver prices, appends the rows to a
Google Sheet, reads a ``filter`` worksheet containing tracked products and
investment prices, then sends Telegram notifications with three charts:
this month, this quarter, and this year.

The investment gap is calculated as ``buy_price - investment_price``. A
negative gap means the current buy price is below the investment price.

Environment Variables
---------------------
G_SHEET_JSON : str
    Full Google service account JSON key content.
TG_TOKEN : str
    Telegram bot token.
TG_CHAT_ID : str
    Telegram chat, group, or channel identifier.
G_SHEET_NAME : str, optional
    Spreadsheet name. Defaults to ``Silver_Prices``.
G_WORKSHEET_NAME : str, optional
    Price history worksheet name. Defaults to ``Sheet1``.
G_FILTER_WORKSHEET_NAME : str, optional
    Product filter worksheet name. Defaults to ``filter``.
"""

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
FILTER_COLUMNS = ["product", "investment_price", "investment_date"]
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
FILTER_WORKSHEET_NAME = os.getenv("G_FILTER_WORKSHEET_NAME", "filter")


def local_now() -> datetime:
    """Return the current local crawler timestamp.

    Returns
    -------
    datetime
        Current time in Asia/Ho_Chi_Minh when ``zoneinfo`` is available,
        otherwise the system local time.
    """
    return datetime.now(LOCAL_TZ) if LOCAL_TZ else datetime.now()


def format_dt(value: datetime) -> str:
    """Format a datetime for sheet storage.

    Parameters
    ----------
    value : datetime
        Datetime value to format.

    Returns
    -------
    str
        Timestamp formatted as ``YYYY-MM-DD HH:MM:SS``.
    """
    return value.strftime("%Y-%m-%d %H:%M:%S")


def clean_price(text: str) -> str:
    """Extract price digits from source text.

    Parameters
    ----------
    text : str
        Raw price text from a source page.

    Returns
    -------
    str
        Digits-only price string, or an empty string if no digits exist.
    """
    return "".join(char for char in text if char.isdigit())


def get_gspread_client() -> gspread.Client:
    """Create an authenticated Google Sheets client.

    Returns
    -------
    gspread.Client
        Authorized gspread client using ``G_SHEET_JSON``.

    Raises
    ------
    KeyError
        If ``G_SHEET_JSON`` is missing.
    json.JSONDecodeError
        If ``G_SHEET_JSON`` is not valid JSON.
    """
    raw_json = os.environ["G_SHEET_JSON"]
    credentials_data = json.loads(raw_json)
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    credentials = ServiceAccountCredentials.from_json_keyfile_dict(credentials_data, scope)
    return gspread.authorize(credentials)


def open_spreadsheet() -> gspread.Spreadsheet:
    """Open the configured Google spreadsheet.

    Returns
    -------
    gspread.Spreadsheet
        Spreadsheet named by ``G_SHEET_NAME``.
    """
    return get_gspread_client().open(SHEET_NAME)


def open_price_worksheet(spreadsheet: gspread.Spreadsheet) -> gspread.Worksheet:
    """Open the price history worksheet.

    Parameters
    ----------
    spreadsheet : gspread.Spreadsheet
        Open spreadsheet object.

    Returns
    -------
    gspread.Worksheet
        Configured price worksheet, or the first worksheet as fallback.
    """
    try:
        return spreadsheet.worksheet(WORKSHEET_NAME)
    except gspread.WorksheetNotFound:
        return spreadsheet.sheet1


def open_filter_worksheet(spreadsheet: gspread.Spreadsheet) -> gspread.Worksheet:
    """Open the product filter worksheet.

    Parameters
    ----------
    spreadsheet : gspread.Spreadsheet
        Open spreadsheet object.

    Returns
    -------
    gspread.Worksheet
        Worksheet containing ``product``, ``investment_price``, and
        ``investment_date`` columns.

    Raises
    ------
    RuntimeError
        If the configured filter worksheet is missing.
    """
    try:
        return spreadsheet.worksheet(FILTER_WORKSHEET_NAME)
    except gspread.WorksheetNotFound as exc:
        raise RuntimeError(
            f'Missing Google Sheet tab "{FILTER_WORKSHEET_NAME}". '
            f"Create it with columns: {', '.join(FILTER_COLUMNS)}"
        ) from exc


def ensure_header(sheet: gspread.Worksheet) -> List[str]:
    """Ensure the price worksheet has required columns.

    Parameters
    ----------
    sheet : gspread.Worksheet
        Price history worksheet.

    Returns
    -------
    list of str
        Existing or newly-created header row.

    Raises
    ------
    RuntimeError
        If an existing header row is missing required price columns.
    """
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
    """Append crawled price rows to Google Sheets.

    Parameters
    ----------
    sheet : gspread.Worksheet
        Price history worksheet.
    rows : list of dict
        Crawled rows matching ``CSV_COLUMNS``.

    Raises
    ------
    RuntimeError
        If ``rows`` is empty.
    """
    if not rows:
        raise RuntimeError("No price rows were crawled; refusing to append an empty run.")

    header = ensure_header(sheet)
    values = [[row.get(column, "") for column in header] for row in rows]
    sheet.append_rows(values, value_input_option="USER_ENTERED")


def fetch_text(url: str) -> str:
    """Fetch text content from a URL.

    Parameters
    ----------
    url : str
        HTTP URL to fetch.

    Returns
    -------
    str
        Response body decoded as UTF-8 with BOM handling.

    Raises
    ------
    requests.RequestException
        If the request fails or returns an error status.
    """
    response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    response.encoding = "utf-8-sig"
    return response.text


def crawl_ancarat() -> List[Dict[str, str]]:
    """Crawl silver prices from Ancarat.

    Returns
    -------
    list of dict
        Parsed Ancarat price rows with product, buy price, sell price, and URL.
    """
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
    """Fetch the latest raw DOJI silver price record.

    Returns
    -------
    tuple of int, int, datetime or None
        ``buy_price``, ``sell_price``, and source timestamp. Returns ``None``
        when the source file has no rows.

    Raises
    ------
    RuntimeError
        If the DOJI row format is unexpected.
    ValueError
        If numeric prices or source timestamp cannot be parsed.
    """
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
    """Crawl silver prices from DOJI.

    Returns
    -------
    list of dict
        Parsed DOJI price rows for 1 lượng and 5 lượng products.
    """
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
    """Run all configured crawlers.

    Returns
    -------
    list of dict
        Combined crawled price rows from Ancarat and DOJI.

    Raises
    ------
    RuntimeError
        If all crawlers fail or return no rows.
    """
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
    """Read worksheet records as string-key dictionaries.

    Parameters
    ----------
    sheet : gspread.Worksheet
        Worksheet to read.

    Returns
    -------
    list of dict
        Google Sheet records with normalized string keys.
    """
    records = sheet.get_all_records()
    return [{str(key): value for key, value in record.items()} for record in records]


def parse_number(value: object) -> Optional[float]:
    """Parse a spreadsheet number value.

    Parameters
    ----------
    value : object
        Spreadsheet value such as ``2,909,000`` or ``2909000``.

    Returns
    -------
    float or None
        Parsed numeric value. Returns ``None`` for blank or non-numeric input.
    """
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    negative = text.startswith("-")
    digits = "".join(char for char in text if char.isdigit())
    if not digits:
        return None

    number = float(digits)
    return -number if negative else number


def load_filter_records(sheet: gspread.Worksheet) -> List[Dict[str, str]]:
    """Load and validate product filter records.

    Parameters
    ----------
    sheet : gspread.Worksheet
        Filter worksheet.

    Returns
    -------
    list of dict
        Filter records containing product names and investment data.

    Raises
    ------
    RuntimeError
        If the filter worksheet is empty or missing required columns.
    """
    records = sheet_records(sheet)
    if not records:
        raise RuntimeError(
            f'The "{FILTER_WORKSHEET_NAME}" tab is empty. '
            f"Add rows with columns: {', '.join(FILTER_COLUMNS)}"
        )

    header = set(records[0].keys())
    missing = [column for column in FILTER_COLUMNS if column not in header]
    if missing:
        raise RuntimeError(
            f'The "{FILTER_WORKSHEET_NAME}" tab is missing columns: {", ".join(missing)}. '
            f"Expected columns: {', '.join(FILTER_COLUMNS)}"
        )

    return records


def prepare_chart_data(
    price_records: List[Dict[str, str]], filter_records: List[Dict[str, str]]
) -> pd.DataFrame:
    """Join price history with filter investments for charting.

    Parameters
    ----------
    price_records : list of dict
        Price history rows from the main worksheet.
    filter_records : list of dict
        Rows from the ``filter`` worksheet with ``product``,
        ``investment_price``, and ``investment_date``.

    Returns
    -------
    pandas.DataFrame
        Chart-ready rows filtered to selected products. Includes numeric
        ``sell_price``, ``buy_price``, and ``gap``. Gap is calculated as
        ``buy_price - investment_price``.
    """
    if not price_records:
        return pd.DataFrame()

    prices = pd.DataFrame(price_records)
    filters = pd.DataFrame(filter_records)
    if prices.empty or filters.empty:
        return pd.DataFrame()

    prices["product"] = prices["product"].astype(str).str.strip()
    filters["product"] = filters["product"].astype(str).str.strip()
    filters["_filter_order"] = range(len(filters))
    prices["crawled_at"] = pd.to_datetime(prices["crawled_at"], errors="coerce")
    prices["sell_price"] = prices["sell_price"].apply(parse_number)
    prices["buy_price"] = prices["buy_price"].apply(parse_number)
    filters["investment_price"] = filters["investment_price"].apply(parse_number)
    filters["investment_date"] = pd.to_datetime(
        filters["investment_date"], errors="coerce", dayfirst=True
    )

    filters = filters.dropna(subset=["product", "investment_price"])
    filters = filters[filters["product"] != ""].drop_duplicates("product", keep="last")

    chart_df = prices.merge(
        filters[["product", "investment_price", "investment_date", "_filter_order"]],
        on="product",
        how="inner",
    )
    chart_df = chart_df.dropna(subset=["crawled_at", "sell_price", "buy_price", "investment_price"])
    chart_df["gap"] = chart_df["buy_price"] - chart_df["investment_price"]
    return chart_df.sort_values(["_filter_order", "product", "crawled_at"])


def start_of_quarter(value: datetime) -> datetime:
    """Return the first timestamp of the quarter containing a date.

    Parameters
    ----------
    value : datetime
        Date inside the desired quarter.

    Returns
    -------
    datetime
        Date reset to the first day of the quarter at midnight.
    """
    quarter_month = ((value.month - 1) // 3) * 3 + 1
    return value.replace(month=quarter_month, day=1, hour=0, minute=0, second=0, microsecond=0)


def filter_period(df: pd.DataFrame, period_name: str, now: datetime) -> pd.DataFrame:
    """Filter chart data to a named reporting period.

    Parameters
    ----------
    df : pandas.DataFrame
        Chart-ready data from :func:`prepare_chart_data`.
    period_name : str
        Period name. Supported values are ``month``, ``quarter``, and
        ``year``.
    now : datetime
        Current local datetime used to determine period boundaries.

    Returns
    -------
    pandas.DataFrame
        Data whose ``crawled_at`` timestamp is inside the selected period.

    Raises
    ------
    ValueError
        If ``period_name`` is unsupported.
    """
    if df.empty:
        return df

    if period_name == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif period_name == "quarter":
        start = start_of_quarter(now)
    elif period_name == "year":
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        raise ValueError(f"Unknown chart period: {period_name}")

    period_df = df[df["crawled_at"] >= pd.Timestamp(start)]
    return period_df.copy()


def format_vnd_short(value: float) -> str:
    """Format a VND value for compact chart labels.

    Parameters
    ----------
    value : float
        Numeric value in VND.

    Returns
    -------
    str
        Short label using ``K`` or ``M`` suffixes.
    """
    sign = "-" if value < 0 else ""
    abs_value = abs(value)
    if abs_value >= 1_000_000:
        return f"{sign}{abs_value / 1_000_000:.3f}M"
    if abs_value >= 1_000:
        return f"{sign}{abs_value / 1_000:.0f}K"
    return f"{sign}{abs_value:.0f}"


def generate_gap_chart(df: pd.DataFrame, period_title: str, output_path: Path) -> Optional[Path]:
    """Generate a buy-price investment gap chart.

    Parameters
    ----------
    df : pandas.DataFrame
        Period-filtered chart data. Must include ``product``, ``crawled_at``,
        ``sell_price``, ``buy_price``, ``investment_price``,
        ``investment_date``, and ``gap``.
    period_title : str
        Human-readable period label for the chart title.
    output_path : pathlib.Path
        PNG output path.

    Returns
    -------
    pathlib.Path or None
        Path to the generated chart, or ``None`` when there is no data.

    Notes
    -----
    The gap line and shaded area use ``gap = buy_price - investment_price``.
    Negative values mean the current buy price is lower than the investment
    price.
    """
    if df.empty:
        return None

    products = df["product"].drop_duplicates().tolist()
    fig_height = max(4.5, 3.1 * len(products))
    fig, axes = plt.subplots(
        len(products),
        1,
        figsize=(14, fig_height),
        sharex=True,
        squeeze=False,
    )
    fig.suptitle(f"Silver Price And Investment Gap - {period_title}", fontsize=18, fontweight="bold", color="#e83e8c")

    for axis, product in zip(axes.flatten(), products):
        product_df = df[df["product"] == product].sort_values("crawled_at")
        investment_price = product_df["investment_price"].iloc[-1]
        investment_date = product_df["investment_date"].iloc[-1]

        axis.plot(
            product_df["crawled_at"],
            product_df["sell_price"],
            marker="o",
            color="#f26b2d",
            linewidth=2,
            label="sell_price",
        )
        axis.plot(
            product_df["crawled_at"],
            product_df["buy_price"],
            marker="o",
            color="#0b2aa3",
            linewidth=2,
            label="buy_price",
        )
        axis.axhline(
            investment_price,
            color="#f26b2d",
            linewidth=2,
            linestyle="-",
            label="investment_price",
        )

        gap_positive = product_df["gap"] >= 0
        axis.fill_between(
            product_df["crawled_at"],
            product_df["buy_price"],
            investment_price,
            where=gap_positive,
            color="#7bd88f",
            alpha=0.25,
            interpolate=True,
            label="positive_gap",
        )
        axis.fill_between(
            product_df["crawled_at"],
            product_df["buy_price"],
            investment_price,
            where=~gap_positive,
            color="#f05ab7",
            alpha=0.22,
            interpolate=True,
            label="negative_gap",
        )

        if pd.notna(investment_date):
            axis.axvline(investment_date, color="#f26b2d", linestyle=":", linewidth=1.5, alpha=0.85)
            axis.text(
                investment_date,
                investment_price,
                f" investment: {investment_date.strftime('%d/%m/%Y')}",
                fontsize=8,
                color="#f26b2d",
                va="bottom",
            )

        latest = product_df.iloc[-1]
        axis.annotate(
            f"sell {format_vnd_short(latest['sell_price'])}\nbuy {format_vnd_short(latest['buy_price'])}\ngap {format_vnd_short(latest['gap'])}",
            xy=(latest["crawled_at"], latest["buy_price"]),
            xytext=(8, 10),
            textcoords="offset points",
            fontsize=9,
            color="#0b2aa3",
            bbox={"boxstyle": "round,pad=0.25", "fc": "#e5e9f7", "ec": "none", "alpha": 0.9},
        )
        axis.annotate(
            f"investment {format_vnd_short(investment_price)}",
            xy=(latest["crawled_at"], investment_price),
            xytext=(8, -18),
            textcoords="offset points",
            fontsize=9,
            color="#9c6a18",
            bbox={"boxstyle": "round,pad=0.25", "fc": "#ead6bd", "ec": "none", "alpha": 0.9},
        )

        axis.set_title(product, loc="left", fontsize=11)
        axis.set_ylabel("VND")
        axis.grid(True, linestyle="--", alpha=0.3)

        gap_axis = axis.twinx()
        gap_axis.plot(
            product_df["crawled_at"],
            product_df["gap"],
            color="#e83e8c",
            marker="o",
            linestyle=":",
            linewidth=1.8,
            label="gap",
        )
        gap_axis.axhline(0, color="#999999", linewidth=0.8, alpha=0.6)
        gap_axis.set_ylabel("gap")
        axis.right_ax = gap_axis

    handles, labels = axes.flatten()[0].get_legend_handles_labels()
    gap_handles, gap_labels = axes.flatten()[0].right_ax.get_legend_handles_labels() if hasattr(axes.flatten()[0], "right_ax") else ([], [])
    fig.legend(
        handles[:3] + gap_handles[:1],
        labels[:3] + gap_labels[:1],
        loc="upper center",
        ncol=4,
        bbox_to_anchor=(0.5, 0.97),
    )
    fig.autofmt_xdate(rotation=25)
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(output_path, dpi=150)
    plt.close(fig)
    return output_path


def log_period_counts(period_title: str, df: pd.DataFrame) -> None:
    """Log product point counts for a chart period.

    Parameters
    ----------
    period_title : str
        Human-readable chart period name.
    df : pandas.DataFrame
        Period-filtered chart data.
    """
    if df.empty:
        print(f"{period_title}: no matching filtered product rows")
        return

    counts = df.groupby("product")["crawled_at"].count()
    formatted_counts = ", ".join(f"{product}={count}" for product, count in counts.items())
    print(f"{period_title}: chart data points by product: {formatted_counts}")


def generate_period_charts(chart_df: pd.DataFrame) -> List[Path]:
    """Generate month, quarter, and year gap charts.

    Parameters
    ----------
    chart_df : pandas.DataFrame
        Chart-ready data from :func:`prepare_chart_data`.

    Returns
    -------
    list of pathlib.Path
        Paths to generated PNG chart files.
    """
    if chart_df.empty:
        print(f'No matching products found between "{WORKSHEET_NAME}" and "{FILTER_WORKSHEET_NAME}"')
        return []

    now = local_now().replace(tzinfo=None)
    chart_specs = [
        ("month", "This Month", Path("silver_gap_this_month.png")),
        ("quarter", "This Quarter", Path("silver_gap_this_quarter.png")),
        ("year", "This Year", Path("silver_gap_this_year.png")),
    ]

    chart_paths: List[Path] = []
    for period_name, title, path in chart_specs:
        period_df = filter_period(chart_df, period_name, now)
        log_period_counts(title, period_df)
        chart_path = generate_gap_chart(period_df, title, path)
        if chart_path:
            chart_paths.append(chart_path)

    return chart_paths


def format_vnd(value: object) -> str:
    """Format a numeric VND value with thousands separators.

    Parameters
    ----------
    value : object
        Numeric value or parseable spreadsheet value.

    Returns
    -------
    str
        Formatted VND value, or ``N/A`` when the value cannot be parsed.
    """
    number = parse_number(value)
    if number is None:
        return "N/A"
    return f"{int(number):,} VND"


def build_summary(chart_df: pd.DataFrame) -> str:
    """Build the filtered Telegram text summary.

    Parameters
    ----------
    chart_df : pandas.DataFrame
        Chart-ready data filtered to products listed in the ``filter`` sheet.

    Returns
    -------
    str
        Telegram-ready summary text containing sell price, buy price,
        investment price, and ``buy_price - investment_price`` gap for each
        tracked product.
    """
    lines = [f"Filtered silver price gap at {format_dt(local_now())}", ""]
    if chart_df.empty:
        lines.append(f'No products from "{FILTER_WORKSHEET_NAME}" matched price history.')
        return "\n".join(lines)

    latest_rows = chart_df.groupby("product", sort=False, group_keys=False).tail(1)
    for _, row in latest_rows.iterrows():
        gap = row["gap"]
        gap_marker = "above" if gap >= 0 else "below"
        crawled_at = row["crawled_at"].strftime("%Y-%m-%d %H:%M")
        lines.append(str(row["product"]))
        lines.append(f"Sell: {format_vnd(row['sell_price'])}")
        lines.append(f"Buy: {format_vnd(row['buy_price'])}")
        lines.append(f"Investment: {format_vnd(row['investment_price'])}")
        lines.append(f"Gap: {format_vnd(gap)} ({gap_marker} investment)")
        lines.append(f"Latest: {crawled_at}")
        lines.append("")

    if lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def telegram_api(method: str, **kwargs) -> None:
    """Call a Telegram Bot API method.

    Parameters
    ----------
    method : str
        Telegram API method name, such as ``sendMessage``.
    **kwargs
        Keyword arguments passed to :func:`requests.post`.

    Raises
    ------
    requests.RequestException
        If Telegram returns a request or HTTP error.
    """
    token = os.environ["TG_TOKEN"]
    response = requests.post(
        f"https://api.telegram.org/bot{token}/{method}",
        timeout=REQUEST_TIMEOUT,
        **kwargs,
    )
    response.raise_for_status()


def send_telegram_message(text: str) -> None:
    """Send a text message to the configured Telegram target.

    Parameters
    ----------
    text : str
        Message text. Telegram's 4096-character limit is enforced.
    """
    telegram_api(
        "sendMessage",
        data={
            "chat_id": os.environ["TG_CHAT_ID"],
            "text": text[:4096],
            "disable_web_page_preview": True,
        },
    )


def send_telegram_photo(image_path: Path, caption: str) -> None:
    """Send a photo to the configured Telegram target.

    Parameters
    ----------
    image_path : pathlib.Path
        Local PNG file to upload.
    caption : str
        Telegram photo caption. Telegram's 1024-character limit is enforced.
    """
    with image_path.open("rb") as photo:
        telegram_api(
            "sendPhoto",
            data={"chat_id": os.environ["TG_CHAT_ID"], "caption": caption[:1024]},
            files={"photo": photo},
        )


def run_cloud_sync() -> None:
    """Run the full cloud sync workflow.

    The workflow crawls prices, appends them to Google Sheets, reads the
    product filter tab, generates month/quarter/year investment gap charts,
    and sends Telegram notifications.

    Raises
    ------
    RuntimeError
        If required environment variables are missing or required sheet tabs
        cannot be loaded.
    """
    for env_name in ("G_SHEET_JSON", "TG_TOKEN", "TG_CHAT_ID"):
        if not os.getenv(env_name):
            raise RuntimeError(f"Missing required environment variable: {env_name}")

    rows = crawl_prices()
    spreadsheet = open_spreadsheet()
    price_sheet = open_price_worksheet(spreadsheet)
    filter_sheet = open_filter_worksheet(spreadsheet)
    append_rows_to_sheet(price_sheet, rows)
    records = sheet_records(price_sheet)
    filter_records = load_filter_records(filter_sheet)
    chart_df = prepare_chart_data(records, filter_records)
    chart_paths = generate_period_charts(chart_df)

    summary = build_summary(chart_df)
    send_telegram_message(summary)
    for chart_path in chart_paths:
        send_telegram_photo(chart_path, chart_path.stem.replace("_", " ").title())

    print(f"Appended {len(rows)} rows to {SHEET_NAME}/{price_sheet.title}")
    print(f"Generated {len(chart_paths)} filtered investment gap charts")


if __name__ == "__main__":
    run_cloud_sync()
