"""Refresh data/feed.json for the SAF vs Jet A-1 dashboard.

Runs in GitHub Actions on weekday mornings (see .github/workflows/update-prices.yml),
or locally:  python scripts/update_feed.py

Every source is optional: if one can't be fetched or parsed, its old value is kept
and a warning is printed. Values in data/manual.json override the automatic ones
(use it for the SAF price, and for the carbon price if the automatic quote fails).
Standard library only.
"""

from __future__ import annotations

import csv
import io
import json
import re
import sys
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

SCRIPT_FILE: Path = Path(__file__).resolve()
SCRIPTS_DIR: Path = SCRIPT_FILE.parent
ROOT_DIR: Path = SCRIPTS_DIR.parent
FEED_FILE: Path = ROOT_DIR.joinpath("data", "feed.json")
MANUAL_OVERRIDES_FILE: Path = ROOT_DIR.joinpath("data", "manual.json")
USER_AGENT: str = "Mozilla/5.0 (compatible; saf-dashboard-price-updater)"

FeedDict = dict[str, Any]


def serialize_json_value(obj: Any) -> Any:
    if isinstance(obj, SeriesPoint):
        return obj.to_list()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def merge_series(
    old_raw: Sequence[SeriesPoint | Sequence[Any]],
    new_points: Sequence[SeriesPoint],
    keep: int = 250,
) -> list[SeriesPoint]:
    merged: dict[str, SeriesPoint] = {}
    for raw_item in old_raw:
        point = SeriesPoint.from_raw(raw_item)
        merged[point.period] = point
    for point in new_points:
        merged[point.period] = point

    ordered: list[SeriesPoint] = []
    for key in sorted(merged.keys()):
        ordered.append(merged[key])
    return ordered[-keep:]


def apply_manual_overrides(feed: FeedDict, overrides_file: Path) -> None:
    """5. Apply manual overrides from data/manual.json if present."""
    if not overrides_file.exists():
        return

    man: dict[str, Any] = json.loads(overrides_file.read_text(encoding="utf-8"))
    history: dict[str, Any] = feed["history"]

    saf: dict[str, Any] = man.get("saf") or {}
    if isinstance(saf.get("hefa_usd_t"), (int, float)):
        for key in ("hefa_usd_t", "period", "source", "url"):
            if key in saf:
                feed["saf"][key] = saf[key]

    car: dict[str, Any] = man.get("carbon") or {}
    if isinstance(car.get("eua_eur_t"), (int, float)):
        if str(car.get("date", "")) >= str(feed["carbon"].get("date", "")):
            for key in ("eua_eur_t", "date", "label", "source", "url"):
                if key in car:
                    feed["carbon"][key] = car[key]
            override_point = SeriesPoint(
                period=str(car["date"]), value=float(car["eua_eur_t"])
            )
            history["eua_daily"] = merge_series(
                history.get("eua_daily", []),
                [override_point],
                keep=250,
            )


@dataclass
class SeriesPoint:
    period: str
    value: float
    flag: int | None = None

    @classmethod
    def from_raw(cls, raw: SeriesPoint | Sequence[Any]) -> SeriesPoint:
        if isinstance(raw, SeriesPoint):
            return raw
        flag: int | None = int(raw[2]) if len(raw) > 2 else None
        return cls(period=str(raw[0]), value=float(raw[1]), flag=flag)

    def to_list(self) -> list[str | float | int]:
        if self.flag is not None:
            return [self.period, self.value, self.flag]
        return [self.period, self.value]


class RemoteDataSource(ABC):
    """Base class for remote price data sources."""

    timeout: int = 30

    def fetch_text(self, url: str) -> str:
        headers: dict[str, str] = {"User-Agent": USER_AGENT}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as response:
            raw_bytes: bytes = response.read()
            return raw_bytes.decode("utf-8", "replace")

    @abstractmethod
    def update(self, feed: FeedDict, today: str) -> None:
        """Fetch remote data and update the feed dictionary in-place."""


class IataSource(RemoteDataSource):
    """1. IATA global average jet fuel price (weekly)."""

    url: str = "https://www.iata.org/en/publications/economics/fuel-monitor/"

    def update(self, feed: FeedDict, today: str) -> None:
        raw_html: str = self.fetch_text(self.url)
        page: str = re.sub(r"<[^>]+>", " ", raw_html)
        page = re.sub(r"\s+", " ", page)

        pattern: str = (
            r"global average jet fuel price last week "
            r"(rose|fell|increased|decreased|was unchanged)"
            r"\s*(?:by\s*)?([\d.]+)?\s*%?.*?\$\s?([\d,]+\.?\d*)\s*/\s*bbl"
        )
        match: re.Match[str] | None = re.search(pattern, page, re.IGNORECASE)
        if match is None:
            raise ValueError("price sentence not found")

        val: float = float(match.group(3).replace(",", ""))
        raw_pct: str = match.group(2) or "0"
        direction: str = match.group(1).lower()
        sign: float = -1.0 if direction in ("fell", "decreased") else 1.0
        pct: float = float(raw_pct) * sign

        if not (20.0 <= val <= 600.0):
            raise ValueError(f"implausible {val}")

        iata: dict[str, Any] = feed["jet"]["iata"]
        history: dict[str, Any] = feed["history"]

        if abs(val - float(iata.get("usd_bbl", 0.0))) > 1e-6:
            weeks: list[Sequence[Any]] = history.get("iata_weekly", [])
            if weeks:
                last_date: date = date.fromisoformat(str(weeks[-1][0]))
            else:
                last_date = date.today() - timedelta(days=9)
            wk: date = last_date + timedelta(days=7)
            new_point = SeriesPoint(period=wk.isoformat(), value=val, flag=1)
            history["iata_weekly"] = merge_series(weeks, [new_point], keep=200)
            iata["period"] = f"latest week (≈ w/e {wk.day} {wk.strftime('%b %Y')})"

        iata.update(usd_bbl=val, wow_pct=pct, retrieved=today)


class FredSource(RemoteDataSource):
    """Shared base for FRED public CSV series."""

    def fetch_series(self, series_id: str) -> list[SeriesPoint]:
        url: str = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
        text: str = self.fetch_text(url)
        points: list[SeriesPoint] = []
        for row in csv.reader(io.StringIO(text)):
            if len(row) < 2 or not re.match(r"\d{4}-\d{2}-\d{2}", row[0]):
                continue
            try:
                points.append(SeriesPoint(period=row[0], value=float(row[1])))
            except ValueError:
                pass  # '.' marks a missing day
        return points


class FredDailySource(FredSource):
    """2a. EIA US Gulf Coast jet fuel spot, daily (via FRED)."""

    def update(self, feed: FeedDict, today: str) -> None:
        daily: list[SeriesPoint] = []
        for point in self.fetch_series("DJFUELUSGULF"):
            if 0.3 <= point.value <= 15.0:
                daily.append(point)

        if daily:
            latest: SeriesPoint = daily[-1]
            feed["jet"]["usgc"].update(date=latest.period, usd_gal=latest.value)
            history: dict[str, Any] = feed["history"]
            history["usgc_daily"] = merge_series(
                history.get("usgc_daily", []), daily[-120:], keep=120
            )


class FredMonthlySource(FredSource):
    """2b. EIA US Gulf Coast jet fuel spot, monthly (via FRED)."""

    def update(self, feed: FeedDict, today: str) -> None:
        monthly: list[SeriesPoint] = []
        for point in self.fetch_series("MJFUELUSGULF"):
            if point.period >= "2024-01-01":
                monthly.append(SeriesPoint(period=point.period[:7], value=point.value))

        if monthly:
            history: dict[str, Any] = feed["history"]
            history["usgc_monthly"] = merge_series(
                history.get("usgc_monthly", []), monthly, keep=240
            )


class EcbSource(RemoteDataSource):
    """3. ECB euro reference rates."""

    url: str = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"

    def update(self, feed: FeedDict, today: str) -> None:
        xml_text: str = self.fetch_text(self.url)
        time_match: re.Match[str] | None = re.search(
            r"time=['\"](\d{4}-\d{2}-\d{2})['\"]", xml_text
        )
        if time_match is None:
            raise ValueError("ECB reference date not found")

        rates: dict[str, float] = {}
        for match in re.finditer(
            r"currency=['\"]([A-Z]{3})['\"]\s+rate=['\"]([\d.]+)['\"]", xml_text
        ):
            rates[match.group(1)] = float(match.group(2))

        if "USD" not in rates or "GBP" not in rates:
            raise ValueError("USD or GBP rate missing from ECB feed")

        feed["fx"].update(
            usd_per_eur=rates["USD"],
            gbp_per_eur=rates["GBP"],
            date=time_match.group(1),
        )


class InvestingEuaSource(RemoteDataSource):
    """4. EU ETS allowance futures quote (best effort)."""

    url: str = "https://www.investing.com/commodities/european-union-allowance-eua-year-futures"

    def update(self, feed: FeedDict, today: str) -> None:
        page: str = self.fetch_text(self.url)
        match: re.Match[str] | None = re.search(
            r'data-test="instrument-price-last"[^>]*>\s*([\d.,]+)', page
        )
        if match is None:
            raise ValueError("price not found")

        val: float = float(match.group(1).replace(",", ""))
        if not (20.0 <= val <= 300.0):
            raise ValueError(f"implausible {val}")

        feed["carbon"].update(
            eua_eur_t=val, date=today, source="Investing.com (EEX contract)"
        )
        history: dict[str, Any] = feed["history"]
        history["eua_daily"] = merge_series(
            history.get("eua_daily", []),
            [SeriesPoint(period=today, value=val)],
            keep=250,
        )


def main() -> None:
    feed: FeedDict = json.loads(FEED_FILE.read_text(encoding="utf-8"))
    feed.setdefault("history", {})
    today: str = date.today().isoformat()

    sources: list[RemoteDataSource] = [
        IataSource(),
        FredDailySource(),
        FredMonthlySource(),
        EcbSource(),
        InvestingEuaSource(),
    ]

    for source in sources:
        try:
            source.update(feed, today)
        except Exception as exc:  # noqa: BLE001
            print(
                f"::warning title={type(source).__name__}::{exc}",
                file=sys.stderr,
                flush=True,
            )

    apply_manual_overrides(feed, MANUAL_OVERRIDES_FILE)

    feed["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    feed["updated_by"] = "GitHub Action"
    FEED_FILE.write_text(
        json.dumps(feed, ensure_ascii=False, indent=1, default=serialize_json_value),
        encoding="utf-8",
    )
    print("feed updated", feed["updated_at"], flush=True)


if __name__ == "__main__":
    main()
