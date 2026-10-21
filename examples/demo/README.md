# Demo outputs (no network needed)

Three JSON files for a presenter who has no network, or no connected account, and wants to show what an answer looks like.

> **Synthetic demo data, shop name Demo Shop.** Every figure, query, product and id in these files is invented. None of it comes from a real account. Do not present it as a client's results.

| File | Shaped like the output of | Ask the assistant |
|---|---|---|
| `ga4_report.json` | `ga4_report` with `sessionDefaultChannelGroup` and the metrics `sessions`, `ecommercePurchases`, `purchaseRevenue` | "Sessions, orders and revenue by channel over the last 28 days." |
| `gsc_performance.json` | `gsc_performance` with the `query` dimension | "Queries where we rank between 5 and 15 with more than 100 impressions." |
| `merchant_product_issues.json` | `merchant_product_issues` with `only_with_issues` left at `true` | "Which products are disapproved for Shopping ads, grouped by issue?" |

## Shape

Each file has the keys the real tool returns today ([docs/TOOLS.md](../../docs/TOOLS.md)), plus the fields every read tool adds: `client_id`, `fetched_at` and `freshness`.

- `ga4_report.json`: `rows`, `row_count`, `truncated`, `totals`, `metrics` (unit and definition of each), `currency`, `date_range` (`data_complete` is `false`, so `settling_from` and `settling_note` are present), `property_id`.
- `gsc_performance.json`: `rows`, `truncated`, `totals` and `totals_note` (the totals include anonymized queries, so they exceed the sum of the rows), `metrics`, `date_range` in Pacific Time.
- `merchant_product_issues.json`: `products` with their `issues`, `scanned`, `truncated`.

The only key that does not exist in a real answer is the first one, `_demo`, which carries the label above. It is there so the files cannot be mistaken for real data.

## How to use them in a demo

1. Open a file in an editor or paste it into the conversation.
2. Say it is synthetic data that shows the shape of an answer.
3. Read the fields that matter: `totals` is the real total even when `rows` is cut, `date_range.data_complete` tells whether the last days can still change, `freshness` says how stale the source is.
4. With a network and a connected account, replace this step with the real tool call.

## Check the files

```bash
python -c "import json,glob; [json.load(open(f, encoding='utf-8')) for f in glob.glob('examples/demo/*.json')]; print('valid JSON')"
```
