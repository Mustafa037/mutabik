# Pipeline accuracy (3 receipts x 8 variants = 24 runs)

> Synthetic fictional receipts + synthetic degradations: a robustness check, not real-world accuracy.

| Field | Accuracy |
|---|---|
| merchant_name | 96% (23/24) |
| date | 96% (23/24) |
| subtotal_amount | 100% (24/24) |
| tax_amount | 100% (24/24) |
| total_amount | 100% (24/24) |
| currency | 100% (24/24) |
| category | 100% (24/24) |
| status | 100% (24/24) |

| Variant | Field accuracy |
|---|---|
| original | 100% |
| rotated 90 | 96% |
| rotated 180 | 100% |
| rotated 270 | 100% |
| blur | 96% |
| noise | 100% |
| dim light | 100% |
| skew 6 deg | 100% |

Mean time per receipt (CPU): 2.0 s
