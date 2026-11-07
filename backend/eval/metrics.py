"""Receipt-level metrics: how often the system is right *and knows it*.

Field accuracy alone hides the point of this project. What matters:

* **Doğru-otomatik oranı** - receipts whose critical fields are all correct *and* that the system
  marked ``verified`` (no human needed).
* **Yakalama oranı** - of the receipts with a wrong critical field, the share that was flagged for
  review. The missed ones (wrong but ``verified``) are the dangerous errors and are listed by name.
* **Gereksiz inceleme** - correct receipts that were flagged anyway (costs the user time).

Critical fields are the ones that end up in the books: total, tax, date, currency (only those that
the label file contains).
"""

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Sequence

__all__ = ["CRITICAL_FIELDS", "Record", "condition_of", "is_verified", "render_receipt_metrics", "summarize"]

CRITICAL_FIELDS = ("total_amount", "tax_amount", "date", "currency")


@dataclass(frozen=True)
class Record:
    """Outcome of one receipt read.

    Attributes:
        file: File name.
        condition: Capture condition (``iyi``, ``kotu``, ``egik``... or a synthetic variant name).
        wrong: Critical fields that were read wrongly (or not at all).
        verified: Whether the system marked the receipt ``verified``.
    """

    file: str
    condition: str
    wrong: tuple[str, ...]
    verified: bool
    expect_flag: bool = False  # the receipt itself does not add up: review is the right outcome


def condition_of(filename: str) -> str:
    """``migros__kotu.jpg`` -> ``kotu``; names without ``__`` -> ``genel``."""
    stem = filename.rsplit(".", 1)[0]
    return stem.split("__", 1)[1] if "__" in stem else "genel"


def is_verified(status: Any) -> bool:
    """``True`` for ``verified`` given as a string or an enum."""
    return str(getattr(status, "value", status)) == "verified"


def summarize(records: Sequence[Record]) -> dict[str, Any]:
    """Counts and rates over ``records`` (rates are ``None`` when their denominator is zero)."""
    total = len(records)
    wrong = [r for r in records if r.wrong]
    right = [r for r in records if not r.wrong]
    auto_correct = [r for r in right if r.verified]
    caught = [r for r in wrong if not r.verified]
    missed = [r for r in wrong if r.verified]
    false_alarms = [r for r in right if not r.verified and not r.expect_flag]

    def rate(part: int, whole: int) -> Any:
        return part / whole if whole else None

    return {
        "n": total,
        "auto_correct": len(auto_correct),
        "auto_correct_rate": rate(len(auto_correct), total),
        "wrong": len(wrong),
        "caught": len(caught),
        "catch_rate": rate(len(caught), len(wrong)),
        "missed": [(r.file, r.wrong) for r in missed],
        "false_alarms": len(false_alarms),
        "false_alarm_rate": rate(len(false_alarms), len([r for r in right if not r.expect_flag])),
    }


def _pct(value: Any) -> str:
    return "-" if value is None else f"{100 * value:.0f}%"


def render_receipt_metrics(records: Sequence[Record]) -> str:
    """Markdown block with the receipt-level metrics, overall and per capture condition."""
    if not records:
        return ""
    overall = summarize(records)
    lines = [
        "## Fiş düzeyinde sonuç",
        "",
        "| Ölçü | Değer |",
        "|---|---|",
        f"| Doğru-otomatik oranı (doğru **ve** mutabık) | {_pct(overall['auto_correct_rate'])} ({overall['auto_correct']}/{overall['n']}) |",
        f"| Yakalama oranı (hatalı fişin incelemeye düşmesi) | {_pct(overall['catch_rate'])} ({overall['caught']}/{overall['wrong']}) |",
        f"| Gereksiz inceleme (doğru ama işaretli) | {_pct(overall['false_alarm_rate'])} ({overall['false_alarms']}) |",
        f"| **Kaçan hata** (yanlış ama mutabık) | **{len(overall['missed'])}** |",
        "",
    ]
    if overall["missed"]:
        lines += ["Kaçan hatalar (öncelikli düzeltilecekler):", ""]
        lines += [f"- `{name}`: yanlış alan(lar) {', '.join(fields)}" for name, fields in overall["missed"]]
        lines.append("")
    by_condition: dict[str, list[Record]] = defaultdict(list)
    for record in records:
        by_condition[record.condition].append(record)
    if len(by_condition) > 1:
        lines += ["| Koşul | Fiş | Doğru-otomatik | Yakalama | Kaçan |", "|---|---|---|---|---|"]
        for condition, group in sorted(by_condition.items()):
            part = summarize(group)
            lines.append(
                f"| {condition} | {part['n']} | {_pct(part['auto_correct_rate'])} | {_pct(part['catch_rate'])} | {len(part['missed'])} |"
            )
        lines.append("")
    return "\n".join(lines)
