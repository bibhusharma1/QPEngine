"""Reconciliation: automatic processing recovery and question-bank safety.

Public entry points:

- ``worker.reconcile_all()`` — one synchronous pass (also the manual trigger)
- ``worker.start_scheduler()``/``worker.status()`` — hosted worker control
- ``validation.validate_records()`` — deterministic Python-only review pass
"""
