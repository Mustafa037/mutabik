"""HTTP API with a real database and image pre-processing but a fake OCR engine.

The OCR model is replaced by a stub that "reads" a fixed list of rows, so these
tests are fast and need neither PaddleOCR nor model downloads.
"""

from collections.abc import Iterator
from typing import Any

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.api import routes
from app.main import app
from app.services.ocr_engine import OCRResult
from tests.helpers import make_ocr

MARKET_ROWS = [
    ["ORNEK MARKET A.S"],
    ["TARIH: 04.11.2026"],
    ["EKMEK", "12,50"],
    ["ARA TOPLAM", "96,80"],
    ["KDV %10", "9,68"],
    ["TOPLAM", "*106,48 TL"],
]
CAFE_ROWS = [
    ["CORNER CAFE"],
    ["Date: 2026-10-02"],
    ["Latte x2", "8.00"],
    ["Subtotal", "$10.00"],
    ["Tax 8%", "$0.80"],
    ["TOTAL", "$10.80"],
]
MISMATCH_ROWS = [
    ["YILDIZ KIRTASIYE LTD. STI."],
    ["TARIH: 02.11.2026"],
    ["DEFTER", "75,00"],
    ["KDV MATRAHI", "100,00"],
    ["KDV %20", "20,00"],
    ["TOPLAM", "*130,00 TL"],
]


class FakeEngine:
    """Returns the rows set by the test instead of running PaddleOCR."""

    def __init__(self) -> None:
        self.rows: list[list[str]] = MARKET_ROWS

    async def recognize_async(self, image: np.ndarray) -> OCRResult:
        return make_ocr(self.rows)


@pytest.fixture(scope="module")
def engine() -> FakeEngine:
    return FakeEngine()


@pytest.fixture(scope="module")
def client(engine: FakeEngine) -> Iterator[TestClient]:
    routes.get_ocr_engine = lambda *_: engine  # type: ignore[assignment]
    with TestClient(app) as test_client:
        yield test_client


def _receipt_png() -> bytes:
    """A plain white 'paper' on a dark table: enough for the real pre-processor."""
    image = np.full((900, 700, 3), 40, dtype=np.uint8)
    cv2.rectangle(image, (100, 80), (600, 820), (245, 245, 245), -1)
    for y in range(140, 780, 50):
        cv2.putText(image, "ITEM 12,50", (140, y), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (20, 20, 20), 2)
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return encoded.tobytes()


def _upload(client: TestClient, engine: FakeEngine, rows: list[list[str]]) -> dict[str, Any]:
    engine.rows = rows
    response = client.post("/api/v1/extract-receipt", files={"file": ("r.png", _receipt_png(), "image/png")})
    assert response.status_code == 201, response.text
    return response.json()


def test_health(client: TestClient) -> None:
    body = client.get("/api/v1/health").json()
    assert body["status"] == "ok" and body["database"] is True


def test_extract_verified_receipt_is_categorised(client: TestClient, engine: FakeEngine) -> None:
    body = _upload(client, engine, MARKET_ROWS)
    receipt = body["receipt"]
    assert receipt["merchant_name"] == "ORNEK MARKET A.S"
    assert receipt["date"] == "2026-11-04"
    assert (receipt["subtotal_amount"], receipt["tax_amount"], receipt["total_amount"]) == (96.8, 9.68, 106.48)
    assert receipt["currency"] == "TRY"
    assert receipt["category"] == "grocery"
    assert receipt["reconciliation_status"] == "verified"
    assert body["boxes"]


def test_extract_mismatch_is_flagged(client: TestClient, engine: FakeEngine) -> None:
    body = _upload(client, engine, MISMATCH_ROWS)
    assert body["receipt"]["reconciliation_status"] == "flagged_for_review"
    assert body["receipt"]["category"] == "stationery"
    assert "uyuşmuyor" in body["reconciliation"]["message"]


def test_extract_rejects_non_image(client: TestClient) -> None:
    response = client.post("/api/v1/extract-receipt", files={"file": ("a.txt", b"hello", "text/plain")})
    assert response.status_code == 422


def test_extract_rejects_empty_file(client: TestClient) -> None:
    response = client.post("/api/v1/extract-receipt", files={"file": ("a.png", b"", "image/png")})
    assert response.status_code == 400


def test_list_filters_and_pagination(client: TestClient, engine: FakeEngine) -> None:
    _upload(client, engine, CAFE_ROWS)
    everything = client.get("/api/v1/expenses", params={"page_size": 100}).json()
    assert everything["total"] >= 3

    usd = client.get("/api/v1/expenses", params={"currency": "USD"}).json()
    assert usd["items"] and all(item["currency"] == "USD" for item in usd["items"])

    market = client.get("/api/v1/expenses", params={"merchant": "market"}).json()  # case-insensitive
    assert market["items"] and all("MARKET" in item["merchant_name"] for item in market["items"])

    food = client.get("/api/v1/expenses", params={"category": "food_drink"}).json()
    assert food["items"] and all(item["category"] == "food_drink" for item in food["items"])

    ranged = client.get("/api/v1/expenses", params={"date_from": "2026-11-01", "date_to": "2026-11-30"}).json()
    assert all("2026-11-01" <= item["date"] <= "2026-11-30" for item in ranged["items"])

    one = client.get("/api/v1/expenses", params={"page_size": 1}).json()
    assert len(one["items"]) == 1 and one["total_pages"] == one["total"]


def test_list_rejects_unknown_param_and_bad_range(client: TestClient) -> None:
    assert client.get("/api/v1/expenses", params={"nope": 1}).status_code == 422
    bad = client.get("/api/v1/expenses", params={"date_from": "2026-12-01", "date_to": "2026-01-01"})
    assert bad.status_code == 422


def test_like_wildcards_are_escaped(client: TestClient) -> None:
    assert client.get("/api/v1/expenses", params={"merchant": "%"}).json()["total"] == 0


def test_patch_recomputes_status_and_category(client: TestClient, engine: FakeEngine) -> None:
    receipt = _upload(client, engine, MISMATCH_ROWS)["receipt"]
    assert receipt["reconciliation_status"] == "flagged_for_review"
    fixed = client.patch(f"/api/v1/expenses/{receipt['id']}", json={"total_amount": 120, "category": "other"})
    assert fixed.status_code == 200
    assert fixed.json()["reconciliation_status"] == "verified"
    assert fixed.json()["category"] == "other"


def test_patch_validation_and_404(client: TestClient) -> None:
    assert client.patch("/api/v1/expenses/1", json={}).status_code == 422
    assert client.patch("/api/v1/expenses/1", json={"total_amount": "abc"}).status_code == 422
    assert client.patch("/api/v1/expenses/1", json={"category": "nonsense"}).status_code == 422
    assert client.patch("/api/v1/expenses/999999", json={"total_amount": 1}).status_code == 404


def test_image_endpoint(client: TestClient, engine: FakeEngine) -> None:
    receipt_id = _upload(client, engine, MARKET_ROWS)["receipt"]["id"]
    response = client.get(f"/api/v1/expenses/{receipt_id}/image")
    assert response.status_code == 200 and response.headers["content-type"] == "image/jpeg"
    assert client.get("/api/v1/expenses/999999/image").status_code == 404


def test_delete_removes_row_and_image(client: TestClient, engine: FakeEngine) -> None:
    receipt_id = _upload(client, engine, MARKET_ROWS)["receipt"]["id"]
    assert client.delete(f"/api/v1/expenses/{receipt_id}").status_code == 204
    assert client.get(f"/api/v1/expenses/{receipt_id}/image").status_code == 404
    assert client.delete(f"/api/v1/expenses/{receipt_id}").status_code == 404


def test_stats(client: TestClient, engine: FakeEngine) -> None:
    _upload(client, engine, MARKET_ROWS)
    stats = client.get("/api/v1/stats").json()
    assert stats["total_receipts"] == stats["verified_count"] + stats["flagged_count"] >= 3
    currencies = {row["currency"]: row for row in stats["totals_by_currency"]}
    assert {"TRY", "USD"} <= set(currencies)
    assert currencies["USD"]["total_amount"] == 10.8  # currencies are never mixed
    assert {row["category"] for row in stats["category_totals"]} >= {"grocery", "food_drink"}
    assert any(row["month"] == "2026-11" for row in stats["monthly_totals"])
    assert all(row["merchant_name"] for row in stats["top_merchants"])


def test_csv_export_turkish_and_english(client: TestClient) -> None:
    tr = client.get("/api/v1/expenses/export.csv")
    assert tr.status_code == 200 and tr.headers["content-type"].startswith("text/csv")
    assert "attachment" in tr.headers["content-disposition"]
    assert tr.content.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM for Excel
    lines = tr.content.decode("utf-8-sig").splitlines()
    assert lines[0].split(";")[:3] == ["id", "date", "merchant"]
    assert any("106,48" in line for line in lines)  # decimal comma

    en = client.get("/api/v1/expenses/export.csv", params={"locale": "en", "currency": "USD"})
    body = en.content.decode("utf-8-sig")
    assert "10.80" in body and "106" not in body


def test_csv_export_neutralises_formulas(client: TestClient, engine: FakeEngine) -> None:
    receipt = _upload(client, engine, MARKET_ROWS)["receipt"]
    client.patch(f"/api/v1/expenses/{receipt['id']}", json={"merchant_name": "=HYPERLINK(1)"})
    body = client.get("/api/v1/expenses/export.csv").content.decode("utf-8-sig")
    assert "'=HYPERLINK(1)" in body and ";=HYPERLINK" not in body


# ---------------------------------------------------------------------------
# Real Turkish till-receipt layout: rates on the item lines, only TOPKDV + TOPLAM printed
# ---------------------------------------------------------------------------
BAKKAL_ROWS = [
    ["$AHINTEPE BAKKAL"],
    ["TARIH  : 01/02/2024"],
    ["OZMO FU FIGURLU 23G", "%01  *13,50"],
    ["TADPI PILIC BAGET KG", "%01  *102,27"],
    ["FINISH QUANTUM OZEL", "%20  *259,00"],
    ["ARKO DEGERLI YAGLAR", "$20  *104,50"],
    ["TOPKDV", "*46,60"],
    ["TOPLAM", "*427,42"],
]


def test_missing_subtotal_is_implied_from_the_rates(client: TestClient, engine: FakeEngine) -> None:
    body = _upload(client, engine, BAKKAL_ROWS)
    receipt = body["receipt"]
    assert receipt["currency"] == "TRY"  # "$AHINTEPE" is not a dollar sign
    assert receipt["date"] == "2024-02-01"  # day first, because the receipt is Turkish
    assert receipt["reconciliation_status"] == "verified"
    assert receipt["reconciliation_method"] == "implied_rate"
    assert receipt["subtotal_amount"] == 380.82
    assert body["reconciliation"]["method"] == "implied_rate"


def test_patching_an_implied_receipt_does_not_make_it_trivially_true(client: TestClient, engine: FakeEngine) -> None:
    receipt = _upload(client, engine, BAKKAL_ROWS)["receipt"]
    bad = client.patch(f"/api/v1/expenses/{receipt['id']}", json={"tax_amount": 300}).json()
    assert bad["reconciliation_status"] == "flagged_for_review"
    assert bad["subtotal_amount"] is None and bad["reconciliation_method"] is None
    back = client.patch(f"/api/v1/expenses/{receipt['id']}", json={"tax_amount": 46.60}).json()
    assert back["reconciliation_status"] == "verified" and back["reconciliation_method"] == "implied_rate"


def test_editing_the_subtotal_makes_the_method_manual(client: TestClient, engine: FakeEngine) -> None:
    receipt = _upload(client, engine, MARKET_ROWS)["receipt"]
    assert receipt["reconciliation_method"] == "printed"
    fixed = client.patch(f"/api/v1/expenses/{receipt['id']}", json={"subtotal_amount": 96.8}).json()
    assert fixed["reconciliation_status"] == "verified" and fixed["reconciliation_method"] == "manual"


def test_derived_subtotal_from_rate_rows(client: TestClient, engine: FakeEngine) -> None:
    rows = [["ACME KIRTASIYE"], ["TARIH 05.11.2026"], ["KDV %20", "20,00"], ["TOPLAM", "120,00"]]
    receipt = _upload(client, engine, rows)["receipt"]
    assert receipt["reconciliation_method"] == "derived_from_rate" and receipt["subtotal_amount"] == 100.0


# ---------------------------------------------------------------------------
# Merchant identity, learned categories, merging
# ---------------------------------------------------------------------------
def _shop_rows(name: str, total: str = "106,48") -> list[list[str]]:
    return [[name], ["TARIH: 04.11.2026"], ["ARA TOPLAM", "96,80"], ["KDV %10", "9,68"], ["TOPLAM", total]]


def test_spelling_variants_share_a_merchant_in_the_stats(client: TestClient, engine: FakeEngine) -> None:
    for name in ("KUZEY MANAV A.S", "KUZEY MANAV A.S.", "Kuzey Manav A.Ş."):
        assert _upload(client, engine, _shop_rows(name))["receipt"]["merchant_key"] == "KUZEY MANAV"
    stats = client.get("/api/v1/stats").json()
    kuzey = [row for row in stats["top_merchants"] if row["merchant_key"] == "KUZEY MANAV"]
    assert len(kuzey) == 1 and kuzey[0]["receipt_count"] == 3
    listed = {m["merchant_key"]: m for m in client.get("/api/v1/merchants").json()}
    assert listed["KUZEY MANAV"]["receipt_count"] == 3


def test_one_misread_letter_does_not_create_a_new_merchant(client: TestClient, engine: FakeEngine) -> None:
    _upload(client, engine, _shop_rows("ISTIKLAL BUYUK MARKET"))
    again = _upload(client, engine, _shop_rows("ISTIKLAL BUYUK MARKFT"))["receipt"]
    assert again["merchant_key"] == "ISTIKLAL BUYUK MARKET"


def test_correcting_a_category_is_learned_for_the_merchant(client: TestClient, engine: FakeEngine) -> None:
    first = _upload(client, engine, _shop_rows("ZENGIN HIRDAVAT"))["receipt"]
    older = _upload(client, engine, _shop_rows("ZENGIN HIRDAVAT"))["receipt"]
    assert first["category"] is None or first["category"] == "other"
    client.patch(f"/api/v1/expenses/{first['id']}", json={"category": "electronics"})

    items = {i["id"]: i for i in client.get("/api/v1/expenses", params={"merchant_key": "ZENGIN HIRDAVAT"}).json()["items"]}
    assert items[older["id"]]["category"] == "electronics"  # undecided receipts of the merchant follow
    newer = _upload(client, engine, _shop_rows("Zengin Hirdavat A.S."))["receipt"]
    assert newer["category"] == "electronics"  # and so do future ones
    assert {m["merchant_key"]: m for m in client.get("/api/v1/merchants").json()}["ZENGIN HIRDAVAT"]["category"] == "electronics"


def test_explicit_categories_of_other_receipts_are_not_overwritten(client: TestClient, engine: FakeEngine) -> None:
    a = _upload(client, engine, _shop_rows("TEK KARAR LTD"))["receipt"]
    b = _upload(client, engine, _shop_rows("TEK KARAR LTD"))["receipt"]
    client.patch(f"/api/v1/expenses/{b['id']}", json={"category": "health"})
    client.patch(f"/api/v1/expenses/{a['id']}", json={"category": "clothing"})
    items = {i["id"]: i for i in client.get("/api/v1/expenses", params={"merchant_key": "TEK KARAR"}).json()["items"]}
    assert items[b["id"]]["category"] == "health"


def test_manual_merge_of_two_merchants(client: TestClient, engine: FakeEngine) -> None:
    _upload(client, engine, _shop_rows("DEMIR CELIK TEKNIK"))
    _upload(client, engine, _shop_rows("D. CELIK"))
    merged = client.post("/api/v1/merchants/merge", json={"from_key": "D CELIK", "into_key": "DEMIR CELIK TEKNIK"})
    assert merged.status_code == 200 and merged.json()["moved"] == 1
    keys = {m["merchant_key"] for m in client.get("/api/v1/merchants").json()}
    assert "D CELIK" not in keys and "DEMIR CELIK TEKNIK" in keys
    same = client.post("/api/v1/merchants/merge", json={"from_key": "X", "into_key": "X"})
    assert same.status_code == 422
    unknown = client.post("/api/v1/merchants/merge", json={"from_key": "NOPE NOPE", "into_key": "DEMIR CELIK TEKNIK"})
    assert unknown.status_code == 404


def test_renaming_the_merchant_updates_the_key(client: TestClient, engine: FakeEngine) -> None:
    receipt = _upload(client, engine, _shop_rows("YANLIS OKUNAN AD"))["receipt"]
    fixed = client.patch(f"/api/v1/expenses/{receipt['id']}", json={"merchant_name": "Doğru Market A.Ş."}).json()
    assert fixed["merchant_key"] == "DOGRU MARKET"


# ---------------------------------------------------------------------------
# Payment cross-check (real e-arsiv slip: "*94.00" was read as "494.00")
# ---------------------------------------------------------------------------
E_ARSIV_ROWS = [
    ["E-ARSIV FATURA BILGI FISI"],
    ["11.05.2024 19:01", "NO : 280"],
    ["KEO1 HAHASI 85 G", "220", "94.00"],
    ["TOPKOV", "15.67"],
    ["TOPLAH", "494.00"],
    ["Nakit", "*204.00"],
    ["PARA USTU", "*-110.00"],
]


def test_misread_star_in_the_total_is_corrected_over_http(client: TestClient, engine: FakeEngine) -> None:
    body = _upload(client, engine, E_ARSIV_ROWS)
    receipt = body["receipt"]
    assert receipt["total_amount"] == 94.0 and receipt["tax_amount"] == 15.67
    assert receipt["currency"] == "TRY" and receipt["reconciliation_status"] == "verified"
    assert body["reconciliation"]["payment_matches"] is True
    assert any("494,00" in warning for warning in body["warnings"])


def test_editing_the_total_against_the_payment_flags_the_receipt(client: TestClient, engine: FakeEngine) -> None:
    receipt = _upload(client, engine, E_ARSIV_ROWS)["receipt"]
    wrong = client.patch(f"/api/v1/expenses/{receipt['id']}", json={"total_amount": 494}).json()
    assert wrong["reconciliation_status"] == "flagged_for_review"  # cash - change says 94.00
    right = client.patch(f"/api/v1/expenses/{receipt['id']}", json={"total_amount": 94}).json()
    assert right["reconciliation_status"] == "verified"


# ---------------------------------------------------------------------------
# GET /expenses/{id} (reason for the status) and the mobile PWA files under /m/
# ---------------------------------------------------------------------------
def test_get_expense_explains_a_flagged_receipt(client: TestClient, engine: FakeEngine) -> None:
    receipt = _upload(client, engine, MISMATCH_ROWS)["receipt"]
    detail = client.get(f"/api/v1/expenses/{receipt['id']}").json()
    assert detail["receipt"]["id"] == receipt["id"]
    assert detail["receipt"]["reconciliation_status"] == "flagged_for_review"
    assert detail["reconciliation"]["status"] == "flagged_for_review"
    assert detail["reconciliation"]["message"]


def test_get_expense_reflects_a_correction(client: TestClient, engine: FakeEngine) -> None:
    receipt = _upload(client, engine, MISMATCH_ROWS)["receipt"]
    client.patch(f"/api/v1/expenses/{receipt['id']}", json={"total_amount": 120})
    detail = client.get(f"/api/v1/expenses/{receipt['id']}").json()
    assert detail["receipt"]["reconciliation_status"] == "verified"
    assert detail["reconciliation"]["status"] == "verified"


def test_get_unknown_expense_is_404(client: TestClient) -> None:
    assert client.get("/api/v1/expenses/999999").status_code == 404


def test_mobile_pwa_files_are_served(client: TestClient) -> None:
    page = client.get("/m/")
    assert page.status_code == 200 and "manifest.webmanifest" in page.text
    assert client.get("/m").status_code == 200  # redirects to /m/
    manifest = client.get("/m/manifest.webmanifest").json()
    assert manifest["display"] == "standalone" and manifest["start_url"] == "./"
    for icon in manifest["icons"]:
        assert client.get(f"/m/{icon['src']}").status_code == 200
    assert "javascript" in client.get("/m/sw.js").headers["content-type"]
    assert client.get("/m/app.js").status_code == 200 and client.get("/m/app.css").status_code == 200
