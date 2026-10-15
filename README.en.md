# Warehouse Cost and Operating Analysis

[中文](README.md) · English

Reconstruct inventory movements with FIFO and explain storage costs by batch, age and applicable rate.

## Analytical approach and deliverables

- Separate FIFO queues by warehouse and material; reject an entire outbound movement when stock is insufficient.
- Reconcile opening stock plus receipts minus issues to closing stock, including cross-batch allocations.
- Deliver an Excel management report covering costs, inventory ageing and sensitivities.

The synthetic example produces storage fees of CNY 20,101.90 and closing inventory of 375 tonnes. These are demonstration outputs, not business savings.

## Reproduce

Run from the repository root with Python 3.12:

```powershell
python -m pip install -r requirements.txt
python -m warehouse_analysis.demo
python -m unittest discover -s tests -v
```

## Interpretation and limitations

Same-day ordering follows source order. Rate changes within a storage interval are not split automatically. The desktop interface is optional; the demo needs no real ERP files.

Business examples use synthetic data and contain no private company or customer records. Only the separately labelled World Bank case in the macro repository uses public economic observations. Refer to current source and CI for this update; older release tags and screenshots describe earlier snapshots.
