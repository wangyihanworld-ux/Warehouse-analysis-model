
# Requirements and evidence

| Business need | Implementation | Acceptance evidence |
|---|---|---|
| Track the oldest available lot | core/fifo_engine.py::_rebuild_fifo | Cross-lot allocation test |
| Reject infeasible issue requests | FIFO anomalies + summary.py | Requested = accepted + rejected; opening + inbound - accepted = closing |
| Reconstruct each historical month | calculation.py::run_engine | Sold-out material retained in January snapshot |
| Explain cumulative storage charges | rate_engine.py + calculation.py | Hand-calculated days and fee test |
| Keep exception evidence with zero inventory | run_engine schema | Only-invalid-outbound regression test |

Entities: warehouse, material, opening lot, transaction, rate rule, reporting period, run history. A transaction can allocate across multiple lots. A rate is resolved by scope and effective date. SQLite history is operational traceability, not a tamper-proof enterprise audit log.

Known boundary: opening-lot dates must describe actual opening stock available to the replay. A later dated opening is not a substitute for an inbound transaction. The application is not a replenishment optimizer.
