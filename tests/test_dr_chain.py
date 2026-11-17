from datetime import UTC, datetime, timedelta

from minihci.dr.chain import (
    RPO_SLACK_S,
    ReplicaInfo,
    next_replica,
    prune_ids,
    rpo_status,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def rep(id, chain, seq, t, fin=5, cp=None):
    return ReplicaInfo(
        id=id,
        chain_id=chain,
        seq=seq,
        dr_path=f"/dr/{id}.qcow2",
        checkpoint=cp or f"cp{id}",
        snapshot_time=T0 + timedelta(seconds=t),
        finished_at=T0 + timedelta(seconds=t + fin),
    )


def chain(chain_id, seqs, start_id, start_t):
    return [rep(start_id + i, chain_id, s, start_t + i) for i, s in enumerate(seqs)]


def test_first_is_full():
    n = next_replica([], 3, [], False)
    assert (n.mode, n.reason, n.seq, n.chain_id, n.since, n.backing) == ("full", "first", 0, None, None, None)


def test_incremental_follows_last():
    r = [rep(1, 1, 0, 0), rep(2, 1, 1, 60)]
    n = next_replica(r, 4, ["cp1", "cp2"], True)
    assert n.mode == "incremental"
    assert (n.since, n.backing, n.chain_id, n.seq, n.reason) == ("cp2", "/dr/2.qcow2", 1, 2, None)


def test_chain_full_starts_new_chain():
    r = chain(1, [0, 1, 2], 1, 0)
    n = next_replica(r, 3, ["cp1", "cp2", "cp3"], True)
    assert (n.mode, n.reason, n.seq, n.chain_id) == ("full", "chain_full", 0, None)


def test_checkpoint_missing_is_full():
    r = chain(1, [0, 1], 1, 0)
    n = next_replica(r, 5, ["cp1"], True)
    assert (n.mode, n.reason, n.seq) == ("full", "checkpoint_missing", 0)


def test_backing_missing_is_full():
    r = chain(1, [0, 1], 1, 0)
    n = next_replica(r, 5, ["cp1", "cp2"], False)
    assert (n.mode, n.reason) == ("full", "backing_missing")


def test_retention_one():
    r = [rep(1, 1, 0, 0)]
    n = next_replica(r, 1, ["cp1"], True)
    assert (n.mode, n.reason) == ("full", "chain_full")
    fulls = [rep(1, 1, 0, 0), rep(2, 2, 0, 60), rep(3, 3, 0, 120)]
    assert prune_ids(fulls, 1) == [1, 2]


def test_newest_by_snapshot_time_not_input_order():
    r = [rep(2, 1, 1, 60), rep(1, 1, 0, 0)]
    n = next_replica(r, 5, ["cp1", "cp2"], True)
    assert n.since == "cp2" and n.seq == 2


def test_prune_keeps_ancestors_of_kept():
    a = chain(1, [0, 1, 2], 1, 0)  # ids 1..3
    b = chain(2, [0, 1], 4, 10)  # ids 4,5
    assert prune_ids(a + b, 3) == []
    b2 = b + [rep(6, 2, 2, 12), rep(7, 2, 3, 13)]
    # newest 3 = B1,B2,B3; B0 is their ancestor; A is gone, children first
    assert prune_ids(a + b2, 3) == [3, 2, 1]


def test_prune_orders_by_chain_then_seq_desc():
    a = chain(1, [0, 1], 1, 0)
    b = chain(2, [0, 1], 3, 10)
    c = chain(3, [0], 5, 20)
    assert prune_ids(a + b + c, 1) == [2, 1, 4, 3]


def test_prune_edge_cases():
    assert prune_ids([], 3) == []
    assert prune_ids(chain(1, [0, 1], 1, 0), 5) == []


def test_identical_snapshot_time_orders_by_id():
    r = [rep(2, 2, 0, 0), rep(1, 1, 0, 0)]
    assert prune_ids(r, 1) == [1]
    r2 = [rep(5, 1, 1, 0), rep(4, 1, 0, 0)]
    n2 = next_replica(r2, 9, ["cp4", "cp5"], True)
    assert n2.since == "cp5"


def test_rpo_status_values():
    h = [rep(1, 1, 0, 0), rep(2, 1, 1, 60), rep(3, 1, 2, 125)]
    s = rpo_status(h, 60, T0 + timedelta(seconds=140))
    assert s.last_snapshot_at == T0 + timedelta(seconds=125)
    assert s.lag_s == 15
    assert s.achieved_rpo_s == 70
    assert s.target_s == 60 + RPO_SLACK_S == 90
    assert s.within_target is True


def test_status_out_of_target_when_lag_large():
    h = [rep(1, 1, 0, 0), rep(2, 1, 1, 60)]
    s = rpo_status(h, 60, T0 + timedelta(seconds=300))
    assert s.lag_s == 240
    assert s.within_target is False


def test_rpo_single_replica():
    s = rpo_status([rep(1, 1, 0, 0)], 60, T0 + timedelta(seconds=10))
    assert s.achieved_rpo_s is None
    assert s.lag_s == 10
    assert s.within_target is True


def test_rpo_no_replicas():
    s = rpo_status([], 60, T0)
    assert (s.last_snapshot_at, s.lag_s, s.achieved_rpo_s) == (None, None, None)
    assert s.target_s == 90
    assert s.within_target is False


def test_failed_incremental_is_full():
    """F1: a checkpoint listed by libvirt whose qcow2 bitmap was lost makes every incremental from it fail."""
    r = [rep(1, 1, 0, 0), rep(2, 1, 1, 60)]
    n = next_replica(r, 4, ["cp1", "cp2"], True, last_attempt_failed_incremental=True)
    assert (n.mode, n.reason, n.seq, n.chain_id, n.since, n.backing) == (
        "full",
        "incremental_failed",
        0,
        None,
        None,
        None,
    )
    assert next_replica(r, 4, ["cp1", "cp2"], True, last_attempt_failed_incremental=False).mode == "incremental"
