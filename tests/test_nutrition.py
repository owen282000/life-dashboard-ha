"""What was eaten today: the meals' day sums in the ledger, and the four sensors."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from http import HTTPStatus
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant

from custom_components.life_dashboard.history import Ledger, apply_payload
from custom_components.life_dashboard.nutrition import NUTRITION_SENSORS, nutrition_updates
from custom_components.life_dashboard.statistics import _DAY_UNITS, _UNIT_CLASSES

from .test_sensor import _post, entry, loaded  # noqa: F401  fixtures, used by name

AMSTERDAM = ZoneInfo("Europe/Amsterdam")


def _meal(uuid: str, end: str, **fields) -> dict:
    return {"uuid": uuid, "start_time": end, "end_time": end, **fields}


def _payload(*meals: dict, **extra) -> dict:
    return {
        "timestamp": "2026-11-18T12:00:00Z",
        "app_version": "1.30.1",
        "source": "health_connect",
        "nutrition": list(meals),
        **extra,
    }


def test_meals_sum_per_day_and_each_nutrient_on_its_own() -> None:
    ledger = Ledger()
    change = apply_payload(
        ledger,
        _payload(
            _meal(
                "a",
                "2026-11-18T07:30:00Z",
                calories=450.0,
                protein_grams=25.0,
                carbs_grams=60.0,
                fat_grams=12.0,
            ),
            # Only calories: the other three sums leave it out.
            _meal("b", "2026-11-18T11:00:00Z", calories=120.5),
            # 23:30 UTC is already the next day in Amsterdam.
            _meal("c", "2026-11-18T23:30:00Z", calories=200.0, protein_grams=10.0),
        ),
        tz=AMSTERDAM,
    )
    assert ledger.day_totals("nutrition_calories") == {"2026-11-18": 570.5, "2026-11-19": 200.0}
    assert ledger.day_totals("nutrition_protein") == {"2026-11-18": 25.0, "2026-11-19": 10.0}
    assert ledger.day_totals("nutrition_carbs") == {"2026-11-18": 60.0}
    assert ledger.day_totals("nutrition_fat") == {"2026-11-18": 12.0}
    assert set(change.day_keys) == {
        "nutrition_calories",
        "nutrition_protein",
        "nutrition_carbs",
        "nutrition_fat",
    }


def test_a_meal_sent_again_or_edited_replaces_its_own_share() -> None:
    ledger = Ledger()
    meal = _meal("a", "2026-11-18T07:30:00Z", calories=450.0, protein_grams=25.0)
    apply_payload(ledger, _payload(meal), tz=AMSTERDAM)
    apply_payload(ledger, _payload(meal), tz=AMSTERDAM)
    assert ledger.day_totals("nutrition_calories") == {"2026-11-18": 450.0}
    edited = {**meal, "calories": 500.0}
    apply_payload(ledger, _payload(edited), tz=AMSTERDAM)
    assert ledger.day_totals("nutrition_calories") == {"2026-11-18": 500.0}
    assert ledger.day_totals("nutrition_protein") == {"2026-11-18": 25.0}


def test_a_deleted_meal_leaves_all_four_sums() -> None:
    ledger = Ledger()
    apply_payload(
        ledger,
        _payload(
            _meal(
                "a",
                "2026-11-18T07:30:00Z",
                calories=450.0,
                protein_grams=25.0,
                carbs_grams=60.0,
                fat_grams=12.0,
            ),
            _meal("b", "2026-11-18T12:00:00Z", calories=300.0, fat_grams=8.0),
        ),
        tz=AMSTERDAM,
    )
    change = apply_payload(
        ledger,
        _payload(deleted_records=[{"type": "nutrition", "uuid": "a"}]),
        tz=AMSTERDAM,
    )
    assert ledger.day_totals("nutrition_calories") == {"2026-11-18": 300.0}
    assert ledger.day_totals("nutrition_fat") == {"2026-11-18": 8.0}
    # A day the meal left with nothing stays at 0, so its rows don't jump.
    assert ledger.day_totals("nutrition_protein") == {"2026-11-18": 0.0}
    assert ledger.day_totals("nutrition_carbs") == {"2026-11-18": 0.0}
    assert set(change.day_keys) == {
        "nutrition_calories",
        "nutrition_protein",
        "nutrition_carbs",
        "nutrition_fat",
    }


def test_unreadable_meals_are_skipped() -> None:
    ledger = Ledger()
    apply_payload(
        ledger,
        _payload(
            _meal("a", "not a time", calories=450.0),
            _meal("b", "2026-11-18T07:30:00Z", calories="lots"),
            {"calories": 100.0},
            "a meal",
        ),
        tz=AMSTERDAM,
    )
    assert ledger.day_totals("nutrition_calories") == {}


def test_updates_only_for_a_phone_that_sent_meals_and_zero_on_a_day_without() -> None:
    today = date(2026, 11, 18)
    sums: dict[str, dict[str, float]] = {}
    assert nutrition_updates(lambda key: sums.get(key, {}), today=today, tz=AMSTERDAM) == []

    sums = {"nutrition_calories": {"2026-11-17": 1800.0, "2026-11-18": 650.25}}
    updates = nutrition_updates(lambda key: sums.get(key, {}), today=today, tz=AMSTERDAM)
    assert [(u.key, u.value) for u in updates] == [("calories_eaten_today", 650.2)]
    assert updates[0].attributes == {"date": "2026-11-18"}
    assert updates[0].last_reset == datetime(2026, 11, 18, tzinfo=AMSTERDAM)

    # The next day: the sensors that exist read 0 instead of keeping yesterday.
    updates = nutrition_updates(
        lambda key: sums.get(key, {}),
        today=today + timedelta(days=1),
        tz=AMSTERDAM,
        known=frozenset({"calories_eaten_today", "protein_today"}),
    )
    assert [(u.key, u.value) for u in updates] == [
        ("calories_eaten_today", 0.0),
        ("protein_today", 0.0),
    ]


def test_every_sum_has_a_statistic_unit() -> None:
    for ledger_key in NUTRITION_SENSORS.values():
        assert ledger_key in _DAY_UNITS
        assert _UNIT_CLASSES[_DAY_UNITS[ledger_key]] in ("energy", "mass")


async def test_the_sensors_from_a_sync(
    hass: HomeAssistant,
    hass_client_no_auth,
    loaded,  # noqa: F811
    freezer,
) -> None:
    freezer.move_to(datetime(2026, 11, 18, 12, 0, tzinfo=UTC))
    client = await hass_client_no_auth()
    payload = _payload(
        _meal(
            "a",
            "2026-11-18T07:30:00Z",
            calories=450.0,
            protein_grams=25.0,
            carbs_grams=60.0,
            fat_grams=12.0,
        ),
        _meal(
            "b",
            "2026-11-18T11:00:00Z",
            calories=320.0,
            protein_grams=30.0,
            carbs_grams=5.0,
            fat_grams=18.0,
        ),
        # Yesterday's dinner counts for yesterday, not for today.
        _meal("c", "2026-11-17T18:00:00Z", calories=700.0, protein_grams=40.0),
    )
    assert await _post(client, payload) == HTTPStatus.OK
    await hass.async_block_till_done()

    eaten = hass.states.get("sensor.owen_s_pixel_calories_eaten_today")
    assert eaten.state == "770.0"
    assert eaten.attributes["unit_of_measurement"] == "kcal"
    assert eaten.attributes["date"] == "2026-11-18"
    assert "state_class" not in eaten.attributes
    assert hass.states.get("sensor.owen_s_pixel_protein_today").state == "55.0"
    assert hass.states.get("sensor.owen_s_pixel_carbs_today").state == "65.0"
    assert hass.states.get("sensor.owen_s_pixel_fat_today").state == "30.0"

    # A second sync with only a snack adds to the day; the meals before stay counted.
    assert (
        await _post(client, _payload(_meal("d", "2026-11-18T11:45:00Z", calories=80.0)))
        == HTTPStatus.OK
    )
    await hass.async_block_till_done()
    assert hass.states.get("sensor.owen_s_pixel_calories_eaten_today").state == "850.0"
    assert hass.states.get("sensor.owen_s_pixel_protein_today").state == "55.0"
