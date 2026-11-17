"""bench_dr.py must summarise only the replicas its own run created, not the VM's whole history."""

from scripts import benchlib


def test_last_id_is_the_newest_replica_id_or_zero_when_none():
    assert benchlib.last_replica_id([]) == 0
    assert benchlib.last_replica_id([{"id": 3}, {"id": 9}, {"id": 5}]) == 9


def test_rows_since_keeps_only_replicas_after_the_start_id():
    rows = [
        {"id": 1, "mode": "full", "status": "succeeded", "size_bytes": 50},
        {"id": 2, "mode": "incremental", "status": "succeeded", "size_bytes": 7},
        {"id": 3, "mode": "incremental", "status": "succeeded", "size_bytes": 9},
    ]
    kept = benchlib.rows_since(rows, 1)
    assert [r["id"] for r in kept] == [2, 3]
    assert benchlib.rows_since(rows, 3) == []
