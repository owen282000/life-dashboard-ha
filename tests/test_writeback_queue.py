"""Test the queue of measurements waiting for a phone.

No Home Assistant here: the id rule, the debounce, the corrections, the acks and the
limits are the part worth pinning down on their own. The bug these tests exist for is
a scale that advertises one weighing for twenty seconds ending up as twenty records in
Health Connect, and a corrected reading that never reaches the phone.
"""

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.life_dashboard.writeback_queue import (
    DEBOUNCE,
    DELIVERED_MAX,
    MAX_AGE,
    MAX_PER_ENTITY,
    PAGE_BYTES,
    PAGE_SIZE,
    Reading,
    WritebackQueue,
    configured_types,
    epoch_ms,
    reading_id,
)

T0 = datetime(2026, 9, 27, 6, 30, tzinfo=UTC)
NOW = datetime(2026, 9, 27, 7, 0, tzinfo=UTC)


def _weight(kilograms: float, at: datetime = T0, entity_id: str = "sensor.owen_weight") -> Reading:
    return Reading(
        id=reading_id(entity_id, at),
        version=1,
        type="weight",
        entity_id=entity_id,
        time=at,
        values={"kilograms": kilograms},
        zone_offset="+02:00",
        device={"type": "scale", "manufacturer": "Xiaomi", "model": "Mi Scale 2"},
    )


def _pressure(systolic: float, diastolic: float, at: datetime = T0) -> Reading:
    return Reading(
        id=reading_id("sensor.omron_systolic", at),
        version=1,
        type="blood_pressure",
        entity_id="sensor.omron_systolic",
        time=at,
        values={"systolic": systolic, "diastolic": diastolic},
        recording_method="active",
        extra={"body_position": "sitting_down", "measurement_location": "left_upper_arm"},
    )


# --- The id rule -----------------------------------------------------------


def test_the_id_is_the_entity_and_the_millisecond() -> None:
    assert reading_id("sensor.zejulio_weight", T0) == "sensor.zejulio_weight@1790490600000"
    # Integer arithmetic: a microsecond timestamp does not round into the next ms.
    assert epoch_ms(T0.replace(microsecond=999_999)) == 1790490600999
    # Any zone gives the same id for the same instant.
    from zoneinfo import ZoneInfo

    local = T0.astimezone(ZoneInfo("Europe/Amsterdam"))
    assert reading_id("sensor.x", local) == reading_id("sensor.x", T0)


def test_a_reading_on_the_wire() -> None:
    wire = _weight(81.35).to_wire()
    assert wire == {
        "id": "sensor.owen_weight@1790490600000",
        "version": 1,
        "type": "weight",
        "kilograms": 81.35,
        "time": "2026-09-27T06:30:00Z",
        "zone_offset": "+02:00",
        "recording_method": "auto",
        "device": {"type": "scale", "manufacturer": "Xiaomi", "model": "Mi Scale 2"},
    }
    pressure = _pressure(128.0, 82.0).to_wire()
    assert pressure["systolic"] == 128.0
    assert pressure["diastolic"] == 82.0
    assert pressure["recording_method"] == "active"
    assert pressure["body_position"] == "sitting_down"
    assert pressure["measurement_location"] == "left_upper_arm"
    assert "device" not in pressure


# --- Offering ----------------------------------------------------------------


def test_a_new_measurement_is_version_one() -> None:
    queue = WritebackQueue()
    stored = queue.offer(_weight(81.35))
    assert stored is not None
    assert stored.version == 1
    assert list(queue.pending) == ["sensor.owen_weight@1790490600000"]


def test_the_same_value_within_the_debounce_is_the_same_measurement() -> None:
    """A BLE scale advertises one weighing for seconds; that is one reading."""
    queue = WritebackQueue()
    assert queue.offer(_weight(81.35)) is not None
    for seconds in (3, 20, 599):
        assert queue.offer(_weight(81.35, T0 + timedelta(seconds=seconds))) is None
    assert len(queue.pending) == 1


def test_the_debounce_slides_with_every_report() -> None:
    """A polled integration writes the same value every five minutes all day long."""
    queue = WritebackQueue()
    assert queue.offer(_weight(81.35)) is not None
    for minutes in range(5, 24 * 60, 5):
        assert queue.offer(_weight(81.35, T0 + timedelta(minutes=minutes))) is None
    assert len(queue.pending) == 1
    # Silence for a night, then the same value: that is the next morning's weighing.
    assert queue.offer(_weight(81.35, T0 + timedelta(days=2))) is not None


def test_the_same_value_after_the_debounce_is_a_new_measurement() -> None:
    """Two identical morning weights on two days are two readings."""
    queue = WritebackQueue()
    assert queue.offer(_weight(81.35)) is not None
    assert queue.offer(_weight(81.35, T0 + DEBOUNCE + timedelta(seconds=1))) is not None
    assert queue.offer(_weight(81.35, T0 + timedelta(days=1))) is not None
    assert len(queue.pending) == 3


def test_another_value_within_the_debounce_is_another_measurement() -> None:
    """Weight-only pass, then the reading with impedance: different value, own id."""
    queue = WritebackQueue()
    assert queue.offer(_weight(81.35)) is not None
    assert queue.offer(_weight(81.4, T0 + timedelta(seconds=5))) is not None
    assert len(queue.pending) == 2


def test_a_different_value_on_the_same_moment_is_a_correction() -> None:
    """Same id, version + 1: Health Connect keeps the higher version."""
    queue = WritebackQueue()
    queue.offer(_weight(81.35))
    corrected = queue.offer(_weight(81.5))
    assert corrected is not None
    assert corrected.id == "sensor.owen_weight@1790490600000"
    assert corrected.version == 2
    assert len(queue.pending) == 1
    assert queue.pending[corrected.id].values == {"kilograms": 81.5}


def test_a_correction_of_a_delivered_reading_comes_back() -> None:
    queue = WritebackQueue()
    first = queue.offer(_weight(81.35))
    queue.ack([first.id], NOW)
    assert not queue.pending

    # The same value again is nothing: the phone has it.
    assert queue.offer(_weight(81.35)) is None
    # A corrected value is version 2 of that id, pending again.
    again = queue.offer(_weight(81.5))
    assert again.version == 2
    assert again.id == first.id
    assert queue.pending == {first.id: again}


def test_the_debounce_is_per_entity() -> None:
    queue = WritebackQueue()
    assert queue.offer(_weight(81.35)) is not None
    assert queue.offer(_weight(81.35, entity_id="sensor.partner_weight")) is not None
    assert len(queue.pending) == 2


def test_blood_pressure_is_one_reading_with_two_values() -> None:
    queue = WritebackQueue()
    assert queue.offer(_pressure(128.0, 82.0)) is not None
    # The retry of the same cuff reading.
    assert queue.offer(_pressure(128.0, 82.0, T0 + timedelta(seconds=30))) is None
    # A different diastolic is a different measurement.
    assert queue.offer(_pressure(128.0, 84.0, T0 + timedelta(seconds=40))) is not None


def test_a_backfill_does_not_move_the_debounce_back() -> None:
    """Old readings offered after new ones keep their own ids and leave the live
    debounce where it was."""
    queue = WritebackQueue()
    queue.offer(_weight(81.35, NOW))
    old = queue.offer(_weight(81.35, T0 - timedelta(days=10)))
    assert old is not None
    # Live still debounces against NOW, not against the backfilled reading.
    assert queue.offer(_weight(81.35, NOW + timedelta(minutes=5))) is None


def test_rekey_moves_a_pending_reading_to_the_better_moment() -> None:
    """The timestamp entity updates a moment after the value it belongs to."""
    queue = WritebackQueue()
    late = T0 + timedelta(seconds=4)
    queued = queue.offer(
        Reading(
            id=reading_id("sensor.owen_weight", late),
            version=1,
            type="weight",
            entity_id="sensor.owen_weight",
            time=late,
            values={"kilograms": 81.35},
            time_source="state",
        )
    )
    moved = queue.rekey(queued.id, T0)
    assert moved.id == reading_id("sensor.owen_weight", T0)
    assert moved.time == T0
    assert moved.time_source is None
    assert list(queue.pending) == [moved.id]
    # The debounce follows: the same value seconds later is still the same reading.
    assert queue.offer(_weight(81.35, T0 + timedelta(seconds=9))) is None
    # A reading the phone has seen does not move: it may be written under that id.
    queue.page(["weight"])
    assert queue.rekey(moved.id, late) is None
    assert list(queue.pending) == [moved.id]
    queue.ack([moved.id], NOW)
    assert queue.rekey(moved.id, late) is None


def test_rekey_follows_the_version_rule_on_the_new_id() -> None:
    """The target id may already exist, pending or delivered; it is never overwritten."""
    queue = WritebackQueue()
    # Delivered with the same value: the moved reading is what the phone has.
    done = queue.offer(_weight(81.35))
    queue.page(["weight"])
    queue.ack([done.id], NOW)
    provisional = queue.offer(_weight(81.35, T0 + timedelta(hours=1)))
    assert queue.rekey(provisional.id, T0) is None
    assert queue.pending == {}
    # Delivered with another value: a correction, version 2.
    provisional = queue.offer(_weight(81.5, T0 + timedelta(hours=2)))
    moved = queue.rekey(provisional.id, T0)
    assert moved.id == done.id
    assert moved.version == 2
    # Pending with another value: also a correction of that pending reading.
    provisional = queue.offer(_weight(81.7, T0 + timedelta(hours=3)))
    moved = queue.rekey(provisional.id, T0)
    assert moved.version == 3
    assert list(queue.pending) == [done.id]


# --- What the phone said --------------------------------------------------------


def test_ack_moves_a_reading_to_delivered() -> None:
    queue = WritebackQueue()
    reading = queue.offer(_weight(81.35))
    acked = queue.ack([reading.id, "sensor.nobody@1", 42], NOW)
    assert [r.id for r in acked] == [reading.id]
    assert not queue.pending
    assert queue.delivered[reading.id]["v"] == 1
    assert queue.delivered[reading.id]["t"] == "weight"
    assert queue.acked_total == 1
    assert queue.last_ack_at == NOW
    # No value survives in the delivered record.
    assert "81.35" not in str(queue.delivered)


def test_an_ack_is_about_the_offered_version() -> None:
    """A correction between the offer and the ack must still reach the phone."""
    queue = WritebackQueue()
    first = queue.offer(_weight(81.0))
    page, _ = queue.page(["weight"])
    assert [r.version for r in page] == [1]
    corrected = queue.offer(_weight(81.2))
    assert corrected.version == 2

    acked = queue.ack([first.id], NOW)
    assert [r.id for r in acked] == [first.id]
    # Version 1 is delivered; version 2 still waits.
    assert queue.delivered[first.id]["v"] == 1
    assert "h" not in queue.delivered[first.id]
    assert queue.pending[first.id].version == 2
    assert queue.offered == {}

    page, _ = queue.page(["weight"])
    assert [r.version for r in page] == [2]
    queue.ack([first.id], NOW)
    assert queue.pending == {}
    assert queue.delivered[first.id]["v"] == 2
    # And the same value again is recognised as delivered.
    assert queue.offer(_weight(81.2)) is None


def test_a_permanent_failure_is_about_the_offered_version_too() -> None:
    queue = WritebackQueue()
    first = queue.offer(_weight(81.0))
    queue.page(["weight"])
    queue.offer(_weight(81.2))
    failures = queue.fail([{"id": first.id, "code": "out_of_range"}], NOW)
    assert failures[0].permanent
    assert queue.pending[first.id].version == 2
    assert first.id not in queue.delivered


def test_a_refused_reading_is_not_offered_again_with_the_same_value() -> None:
    """The backfill offers the recorder's past again; a refusal must hold."""
    queue = WritebackQueue()
    reading = queue.offer(_weight(83.1))
    queue.page(["weight"])
    queue.fail([{"id": reading.id, "code": "permission_denied"}], NOW)
    assert queue.pending == {}
    assert queue.delivered[reading.id]["code"] == "permission_denied"
    assert "83.1" not in str(queue.delivered)

    assert queue.offer(_weight(83.1)) is None
    assert queue.pending == {}
    # A corrected value is a new version and goes.
    again = queue.offer(_weight(83.3))
    assert again.version == 2
    assert queue.counts()["dropped"] == {"weight": 1}


def test_a_permanent_failure_leaves_the_queue() -> None:
    queue = WritebackQueue()
    reading = queue.offer(_weight(81.35))
    failures = queue.fail([{"id": reading.id, "code": "out_of_range"}], NOW)
    assert len(failures) == 1
    assert failures[0].permanent
    assert failures[0].reading.entity_id == "sensor.owen_weight"
    assert not queue.pending
    assert queue.failed_total == {"out_of_range": 1}
    # Not a code the user can fix, so no repair.
    assert queue.refused == {}


def test_a_transient_failure_stays() -> None:
    queue = WritebackQueue()
    reading = queue.offer(_weight(81.35))
    for code in ("rate_limited", "hc_unavailable"):
        failures = queue.fail([{"id": reading.id, "code": code}], NOW)
        assert not failures[0].permanent
        assert reading.id in queue.pending
    assert queue.failed_total == {"rate_limited": 1, "hc_unavailable": 1}


def test_permission_denied_earns_a_repair_until_the_type_is_acked() -> None:
    queue = WritebackQueue()
    first = queue.offer(_weight(81.35))
    queue.fail([{"id": first.id, "code": "permission_denied"}], NOW)
    assert queue.refused == {"weight": "permission_denied"}
    assert first.id not in queue.pending

    second = queue.offer(_weight(82.0, T0 + timedelta(days=1)))
    queue.fail([{"id": second.id, "code": "unsupported_type"}], NOW)
    assert queue.refused == {"weight": "unsupported_type"}

    third = queue.offer(_weight(82.5, T0 + timedelta(days=2)))
    queue.ack([third.id], NOW)
    assert queue.refused == {}


def test_an_unknown_code_is_treated_as_transient() -> None:
    """A newer app may have a reason we do not know; keeping is bounded, dropping is not."""
    queue = WritebackQueue()
    reading = queue.offer(_weight(81.35))
    failures = queue.fail([{"id": reading.id, "code": "moon_phase"}], NOW)
    assert not failures[0].permanent
    assert reading.id in queue.pending
    # A missing code reads as invalid, which is permanent.
    failures = queue.fail([{"id": reading.id}], NOW)
    assert failures[0].code == "invalid"
    assert reading.id not in queue.pending


def test_failures_for_unknown_ids_are_counted_not_reported() -> None:
    queue = WritebackQueue()
    assert (
        queue.fail([{"id": "sensor.gone@1", "code": "invalid"}, "junk", {"code": "x"}], NOW) == []
    )
    assert queue.failed_total == {"invalid": 1}


# --- Limits ------------------------------------------------------------------------


def test_readings_older_than_the_age_limit_are_not_offered() -> None:
    queue = WritebackQueue()
    queue.offer(_weight(80.0, NOW - MAX_AGE - timedelta(days=1)))
    queue.offer(_weight(81.0, NOW - MAX_AGE + timedelta(days=1)))
    queue.prune(NOW)
    assert [r.values["kilograms"] for r in queue.pending.values()] == [81.0]


def test_an_entity_keeps_at_most_the_cap_oldest_out_first() -> None:
    queue = WritebackQueue()
    start = NOW - timedelta(days=30)
    for i in range(MAX_PER_ENTITY + 20):
        queue.offer(_weight(70.0 + i * 0.01, start + timedelta(minutes=i * 15)))
    queue.offer(_weight(60.0, start, entity_id="sensor.partner_weight"))
    queue.prune(NOW)

    own = [r for r in queue.pending.values() if r.entity_id == "sensor.owen_weight"]
    assert len(own) == MAX_PER_ENTITY
    assert min(r.time for r in own) == start + timedelta(minutes=20 * 15)
    # The other entity's one reading is untouched.
    assert any(r.entity_id == "sensor.partner_weight" for r in queue.pending.values())


def test_readings_of_a_removed_mapping_are_dropped() -> None:
    queue = WritebackQueue()
    queue.offer(_weight(81.35))
    queue.offer(_weight(82.0, T0 + timedelta(days=1)))
    queue.page(["weight"])
    queue.offer(_pressure(128.0, 82.0))
    assert queue.clear_type("weight") == 2
    assert [r.type for r in queue.pending.values()] == ["blood_pressure"]
    assert queue.offered == {}
    assert queue.clear_type("weight") == 0


def test_a_page_stays_under_the_byte_budget() -> None:
    queue = WritebackQueue()
    long_id = "sensor." + "x" * 900
    for i in range(20):
        queue.offer(_weight(70.0 + i, T0 + timedelta(minutes=15 * i), entity_id=long_id))
    page, more = queue.page(["weight"], byte_budget=5000)
    assert 0 < len(page) < 20
    assert more is True
    assert sum(len(str(r.to_wire())) for r in page) < 5000 + 1200
    # The default budget fits a full page of ordinary readings.
    assert PAGE_BYTES > PAGE_SIZE * 400


def test_a_non_finite_value_never_gets_in() -> None:
    queue = WritebackQueue()
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            _weight(bad)
    # A reading built around the check is still refused by the queue.
    reading = _weight(81.35)
    object.__setattr__(reading, "values", {"kilograms": float("nan")})
    assert queue.offer(reading) is None
    assert queue.pending == {}
    # And one that got into a store is dropped when reading it back, with a debug line.
    restored = WritebackQueue.from_dict(
        {"pending": {reading.id: {**_weight(81.35).to_dict(), "kilograms": float("inf")}}}
    )
    assert restored.pending == {}


def test_delivered_is_a_ring() -> None:
    queue = WritebackQueue()
    start = NOW - timedelta(days=60)
    for i in range(DELIVERED_MAX + 10):
        reading = queue.offer(_weight(70.0, start + timedelta(minutes=i * 15)))
        queue.ack([reading.id], start + timedelta(minutes=i * 15))
    old = queue.offer(_weight(75.0, NOW - MAX_AGE - timedelta(days=2)))
    queue.ack([old.id], NOW - MAX_AGE - timedelta(days=2))
    queue.prune(NOW)
    assert len(queue.delivered) == DELIVERED_MAX
    assert old.id not in queue.delivered


def test_a_page_is_the_oldest_first_and_only_the_types_asked() -> None:
    queue = WritebackQueue()
    for i in range(PAGE_SIZE + 5):
        queue.offer(_weight(70.0 + i, T0 - timedelta(days=1) + timedelta(minutes=i * 15)))
    queue.offer(_pressure(128.0, 82.0, T0))

    page, more = queue.page(["weight"])
    assert len(page) == PAGE_SIZE
    assert more is True
    assert page[0].values["kilograms"] == 70.0
    assert all(r.type == "weight" for r in page)

    page, more = queue.page(["blood_pressure", "height", 7])
    assert [r.type for r in page] == ["blood_pressure"]
    assert more is False

    assert queue.page([]) == ([], False)


# --- Persistence and diagnostics ---------------------------------------------------


def test_the_queue_survives_a_round_trip() -> None:
    queue = WritebackQueue()
    weight = queue.offer(_weight(81.35))
    pressure = queue.offer(_pressure(128.0, 82.0, T0 + timedelta(minutes=2)))
    done = queue.offer(_weight(80.0, T0 - timedelta(days=1)))
    queue.ack([done.id], NOW)
    queue.fail([{"id": weight.id, "code": "rate_limited"}], NOW)

    queue.page(["weight"])

    restored = WritebackQueue.from_dict(queue.to_dict())
    assert restored.pending == {weight.id: weight, pressure.id: pressure}
    assert restored.offered == {weight.id: 1}
    assert restored.delivered == queue.delivered
    assert restored.acked_total == 1
    assert restored.failed_total == {"rate_limited": 1}
    assert restored.last_ack_at == NOW
    # The debounce memory came along: the same value again is still nothing.
    assert restored.offer(_weight(81.35, T0 + timedelta(seconds=30))) is None
    # And a delivered id with the same value is still recognised.
    assert restored.offer(_weight(80.0, T0 - timedelta(days=1))) is None


def test_a_broken_store_is_not_fatal(caplog) -> None:
    assert WritebackQueue.from_dict(None).pending == {}
    restored = WritebackQueue.from_dict(
        {
            "pending": {"x": {"type": "weight"}},
            "offered": {"x": 1, "y": 2},
            "seen": {"sensor.x": {"time": "bad"}},
        }
    )
    assert restored.pending == {}
    assert restored.offered == {}
    assert restored.seen == {}
    assert "Dropped an unreadable pending reading x" in caplog.text
    # A 0 queued by a version without the range check is dropped on the way in.
    zero = _weight(81.35)
    object.__setattr__(zero, "values", {"kilograms": 0.0})
    restored = WritebackQueue.from_dict({"pending": {zero.id: zero.to_dict()}})
    assert restored.pending == {}


def test_a_refusal_is_dated_when_it_arrives() -> None:
    """The delivered ring prunes on that date; an old reading refused today must
    stay remembered for the full ninety days, not be forgotten by its own age."""
    queue = WritebackQueue()
    old = queue.offer(_weight(81.35, NOW - timedelta(days=85)))
    queue.page(["weight"])
    queue.fail([{"id": old.id, "code": "too_old"}], NOW)
    assert queue.delivered[old.id]["at"] == NOW.isoformat()
    queue.prune(NOW + timedelta(days=10))
    assert old.id in queue.delivered


def test_counts_carry_no_value_and_no_moment_of_a_pending_reading() -> None:
    queue = WritebackQueue()
    queue.offer(_weight(81.35))
    done = queue.offer(_pressure(128.0, 82.0))
    queue.ack([done.id], NOW)
    counts = queue.counts()
    assert counts == {
        "pending": {"weight": 1},
        "offered": 0,
        "delivered": {"blood_pressure": 1},
        "dropped": {},
        "acked_total": 1,
        "failed": {},
        "last_ack_at": NOW.isoformat(),
        "refused": {},
    }
    assert "81.35" not in str(counts)
    assert "2026-09-27T06:30" not in str(counts)


# --- The options mapping --------------------------------------------------------------


@pytest.mark.parametrize(
    ("mapping", "expected"),
    [
        (None, []),
        ({}, []),
        ({"weight": {"entity": "sensor.w"}}, ["weight"]),
        ({"weight": {"entity": ""}}, []),
        ({"blood_pressure": {"systolic": "sensor.s"}}, []),
        (
            {
                "blood_pressure": {"systolic": "sensor.s", "diastolic": "sensor.d"},
                "height": {"entity": "sensor.h"},
                "bmi": {"entity": "sensor.no"},
            },
            ["height", "blood_pressure"],
        ),
    ],
)
def test_configured_types(mapping, expected) -> None:
    assert configured_types(mapping) == expected
