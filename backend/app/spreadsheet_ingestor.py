"""Spreadsheet ingestion for site updates.

Accepts CSV or XLSX file content and converts each data row into a
SiteUpdateCreate object ready to be stored via the existing
site_updates.create_site_update() function.

Design principles
-----------------
* No FastAPI dependency — pure Python, fully unit-testable.
* No database access — persistence is the caller responsibility.
* Feeds into the existing site_updates -> AI processing pipeline.
* One bad row does NOT abort the whole import; errors are collected
  and returned alongside the successful rows.

Supported column-name variations
---------------------------------
Headers are normalised to lowercase and non-alphanumeric chars collapsed
before matching, so "Update ID", "update_id", "UpdateID" all resolve to
source_update_id.

Required canonical fields (any listed alias is accepted):
  source_update_id : "update id", "update_id", "id", "update no", "uid", etc.
  reported_on      : "date", "reported on", "report date", "update date", etc.
  location         : "location", "loc", "site", "area", "zone", etc.
  raw_update       : "raw update", "update", "description", "desc",
                     "remarks", "notes", "comment", "observation", etc.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from typing import Any

from app.site_updates import SiteUpdateCreate


# ---------------------------------------------------------------------------
# Column name aliases
# ---------------------------------------------------------------------------

def _normalise(header: str) -> str:
    """Lowercase + collapse all non-alphanumeric runs to a single space."""
    return re.sub(r"[^a-z0-9]+", " ", header.lower()).strip()


# Maps canonical field name -> set of normalised aliases
_ALIASES: dict[str, set[str]] = {
    "source_update_id": {
        "update id", "updateid", "update id", "update no", "uid",
        "sr no", "sr", "serial no", "s no", "update number", "id",
    },
    "reported_on": {
        "date", "reported on", "reported on date", "report date",
        "update date", "reportedon", "reported date", "observation date",
    },
    "location": {
        "location", "loc", "site", "area", "zone",
        "site location", "work location",
    },
    "raw_update": {
        "raw update", "raw update text", "update", "description", "desc",
        "remarks", "notes", "comment", "observation", "site update",
        "update text", "details", "work done",
    },
}

# Build reverse map: normalised alias -> canonical field name
_ALIAS_LOOKUP: dict[str, str] = {}
for _canonical, _aliases in _ALIASES.items():
    for _alias in _aliases:
        _ALIAS_LOOKUP[_normalise(_alias)] = _canonical


def _resolve_header(raw_header: str) -> str | None:
    """Return the canonical field name for a raw spreadsheet header, or None."""
    return _ALIAS_LOOKUP.get(_normalise(raw_header))


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class RowError:
    """A validation error for a single spreadsheet row."""
    row_number: int        # 1-based (header = row 1, first data row = 2)
    source_update_id: str  # raw cell value or empty string if missing
    message: str


@dataclass
class SpreadsheetImportResult:
    """Outcome of parsing one spreadsheet file."""
    filename: str
    rows_accepted: list[SiteUpdateCreate] = field(default_factory=list)
    row_errors: list[RowError] = field(default_factory=list)

    @property
    def accepted_count(self) -> int:
        return len(self.rows_accepted)

    @property
    def error_count(self) -> int:
        return len(self.row_errors)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_spreadsheet(
    content: bytes,
    filename: str,
) -> SpreadsheetImportResult:
    """Parse a CSV or XLSX file and return validated rows plus row-level errors.

    Parameters
    ----------
    content:  Raw file bytes.
    filename: Original filename (used for source_reference and format detection).

    Returns
    -------
    SpreadsheetImportResult with rows_accepted and row_errors.
    """
    result = SpreadsheetImportResult(filename=filename)
    lower_name = filename.lower()

    if lower_name.endswith(".xlsx") or lower_name.endswith(".xls"):
        rows_raw = _read_xlsx(content, result)
    else:
        rows_raw = _read_csv(content, result)

    if rows_raw is None:
        return result

    _validate_and_collect(rows_raw, filename, result)
    return result


# ---------------------------------------------------------------------------
# File readers
# ---------------------------------------------------------------------------

def _read_csv(
    content: bytes,
    result: SpreadsheetImportResult,
) -> list[tuple[int, dict[str, str]]] | None:
    """Decode and parse CSV bytes. Returns None on fatal decode error."""
    try:
        text = content.decode("utf-8-sig")  # strip BOM if present
    except UnicodeDecodeError:
        try:
            text = content.decode("latin-1")
        except Exception as exc:
            result.row_errors.append(RowError(
                row_number=1,
                source_update_id="",
                message=f"Could not decode file as UTF-8 or Latin-1: {exc}",
            ))
            return None

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        result.row_errors.append(RowError(
            row_number=1,
            source_update_id="",
            message="File appears to be empty or has no header row.",
        ))
        return None

    col_map = _build_column_map(list(reader.fieldnames))
    missing = _check_required_columns(col_map)
    if missing:
        result.row_errors.append(RowError(
            row_number=1,
            source_update_id="",
            message=(
                f"Missing required column(s): {', '.join(sorted(missing))}. "
                f"Found headers: {list(reader.fieldnames)}"
            ),
        ))
        return None

    rows: list[tuple[int, dict[str, str]]] = []
    for i, raw_row in enumerate(reader, start=2):  # row 1 = header
        canonical_row: dict[str, str] = {}
        for orig_header, canonical in col_map.items():
            canonical_row[canonical] = (raw_row.get(orig_header) or "").strip()
        rows.append((i, canonical_row))

    return rows


def _read_xlsx(
    content: bytes,
    result: SpreadsheetImportResult,
) -> list[tuple[int, dict[str, str]]] | None:
    """Parse XLSX bytes using openpyxl. Returns None on fatal error."""
    try:
        import openpyxl
    except ImportError:
        result.row_errors.append(RowError(
            row_number=1,
            source_update_id="",
            message=(
                "openpyxl is required for XLSX support. "
                "Run: pip install openpyxl>=3.1"
            ),
        ))
        return None

    try:
        wb = openpyxl.load_workbook(
            io.BytesIO(content), read_only=True, data_only=True
        )
    except Exception as exc:
        result.row_errors.append(RowError(
            row_number=1,
            source_update_id="",
            message=f"Could not open XLSX file: {exc}",
        ))
        return None

    ws = wb.active
    raw_rows = list(ws.iter_rows(values_only=True))
    wb.close()

    if not raw_rows:
        result.row_errors.append(RowError(
            row_number=1,
            source_update_id="",
            message="Worksheet is empty.",
        ))
        return None

    # First row = headers
    header_row = [str(c).strip() if c is not None else "" for c in raw_rows[0]]
    col_map = _build_column_map(header_row)
    missing = _check_required_columns(col_map)
    if missing:
        result.row_errors.append(RowError(
            row_number=1,
            source_update_id="",
            message=(
                f"Missing required column(s): {', '.join(sorted(missing))}. "
                f"Found headers: {header_row}"
            ),
        ))
        return None

    # Build header -> column index map for required canonical fields
    header_index: dict[str, int] = {}
    for idx, h in enumerate(header_row):
        canonical = col_map.get(h)
        if canonical and canonical not in header_index:
            header_index[canonical] = idx

    rows: list[tuple[int, dict[str, str]]] = []
    for row_num, raw_row in enumerate(raw_rows[1:], start=2):
        canonical_row: dict[str, str] = {}
        for canonical, idx in header_index.items():
            cell = raw_row[idx] if idx < len(raw_row) else None
            canonical_row[canonical] = str(cell).strip() if cell is not None else ""
        rows.append((row_num, canonical_row))

    return rows


# ---------------------------------------------------------------------------
# Column mapping helpers
# ---------------------------------------------------------------------------

def _build_column_map(headers: list[str]) -> dict[str, str]:
    """Return {original_header: canonical_field} for recognised headers only.

    If two headers resolve to the same canonical field, the first one wins.
    """
    col_map: dict[str, str] = {}
    seen_canonical: set[str] = set()
    for h in headers:
        canonical = _resolve_header(h)
        if canonical and canonical not in seen_canonical:
            col_map[h] = canonical
            seen_canonical.add(canonical)
    return col_map


_REQUIRED_FIELDS = frozenset({"source_update_id", "reported_on", "location", "raw_update"})


def _check_required_columns(col_map: dict[str, str]) -> set[str]:
    """Return the set of required canonical fields not present in col_map."""
    present = set(col_map.values())
    return _REQUIRED_FIELDS - present


# ---------------------------------------------------------------------------
# Row validation
# ---------------------------------------------------------------------------

def _validate_and_collect(
    rows: list[tuple[int, dict[str, str]]],
    filename: str,
    result: SpreadsheetImportResult,
) -> None:
    """Validate each row; append to rows_accepted or row_errors."""
    for row_number, canonical_row in rows:
        uid = canonical_row.get("source_update_id", "")

        # Skip completely blank rows silently
        if not any(v for v in canonical_row.values()):
            continue

        try:
            payload = SiteUpdateCreate(
                source_update_id=uid,
                reported_on=canonical_row.get("reported_on", ""),
                location=canonical_row.get("location", ""),
                raw_update=canonical_row.get("raw_update", ""),
                source_reference=filename,
            )
            result.rows_accepted.append(payload)
        except Exception as exc:
            result.row_errors.append(RowError(
                row_number=row_number,
                source_update_id=uid,
                message=_extract_error_message(exc),
            ))


def _extract_error_message(exc: Exception) -> str:
    """Produce a concise human-readable error string from any exception."""
    try:
        errs = exc.errors()  # type: ignore[attr-defined]
        parts = [
            f"{'/'.join(str(loc) for loc in e['loc'])}: {e['msg']}"
            for e in errs
        ]
        return "; ".join(parts)
    except (AttributeError, TypeError):
        return str(exc)
