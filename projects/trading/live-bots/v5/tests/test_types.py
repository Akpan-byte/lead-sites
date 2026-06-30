# CHANGE_SUMMARY
# 2026-06-30  kilo
#   - Smoke tests for v5.types dataclasses and enums.
# WHY: Catches trivial regressions in shared interfaces before they propagate.

"""Smoke tests for the v5 shared types."""

from datetime import datetime, timezone

from v5.types import (
    AccountSnapshot,
    Bar,
    Direction,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSnapshot,
    Quote,
    RiskCheck,
    Signal,
    SignalAction,
    StoredState,
    TFPosition,
    TFState,
    TimeFrameParam,
    WorkingOrder,
)


def test_bar_immutability_and_fields() -> None:
    ts = datetime(2026, 6, 30, 9, 30, tzinfo=timezone.utc)
    bar = Bar(timestamp=ts, open=41000.0, high=41050.0, low=40990.0, close=41020.0, volume=100)
    assert bar.time_min is None
    assert bar.high == 41050.0


def test_quote_normalisation() -> None:
    q = Quote(contract_id="CON.F.US.YM.U26", last_price=41020.0, bid=41019.0, ask=41021.0)
    assert q.contract_id == "CON.F.US.YM.U26"
    assert q.last_price == 41020.0


def test_order_enums_map_to_sdk_values() -> None:
    assert OrderSide.BUY.sdk_value == 0
    assert OrderSide.SELL.sdk_value == 1
    assert OrderSide.from_sdk(0) is OrderSide.BUY
    assert OrderStatus.FILLED.is_terminal is True
    assert OrderStatus.OPEN.is_working is True
    assert OrderType.STOP.value == 4


def test_direction_enum() -> None:
    assert Direction.LONG.value == "Long"
    assert Direction.SHORT.value == "Short"


def test_account_snapshot_from_sdk_dict() -> None:
    raw = {
        "id": 123,
        "name": "50KTC",
        "balance": 50000.0,
        "canTrade": True,
        "isVisible": True,
        "simulated": False,
    }
    snap = AccountSnapshot.from_sdk(raw)
    assert snap.account_id == 123
    assert snap.can_trade is True
    assert snap.raw == raw


def test_position_snapshot_signed_size() -> None:
    raw = {
        "id": 1,
        "accountId": 123,
        "contractId": "CON.F.US.YM.U26",
        "creationTimestamp": "2026-06-30T09:30:00Z",
        "type": 2,
        "size": 3,
        "averagePrice": 41000.0,
    }
    pos = PositionSnapshot.from_sdk(raw)
    assert pos.direction is Direction.SHORT
    assert pos.signed_size == -3


def test_working_order_remaining_size() -> None:
    order = WorkingOrder(
        order_id=42,
        account_id=123,
        contract_id="CON.F.US.YM.U26",
        creation_timestamp="2026-06-30T09:30:00Z",
        status=OrderStatus.OPEN,
        order_type=OrderType.STOP,
        side=OrderSide.BUY,
        size=2,
        fill_volume=1,
        stop_price=41050.0,
        tf_name="5m",
    )
    assert order.remaining_size == 1
    assert order.is_buy is True
    assert order.tf_name == "5m"


def test_signal_and_risk_check() -> None:
    ts = datetime(2026, 6, 30, 9, 30, tzinfo=timezone.utc)
    signal = Signal(
        timestamp=ts,
        tf_name="5m",
        action=SignalAction.ENTER,
        direction=Direction.LONG,
        price=41050.0,
        qty=2,
        reason="ORB breakout",
    )
    assert signal.direction is Direction.LONG

    check = RiskCheck(allow=True, max_qty=2)
    assert check.allow is True

    deny = RiskCheck(allow=False, reason="MLL breach", limit_hit="mll")
    assert deny.limit_hit == "mll"


def test_tf_state_and_stored_state_defaults() -> None:
    tf = TFState(tf_name="5m", or_high=41000.0, or_low=40950.0)
    assert tf.entries_taken == 0
    assert tf.position is None

    pos = TFPosition(
        direction=Direction.LONG,
        entry_price=41000.0,
        entry_time=None,
        qty=2,
        virtual_sl=40980.0,
    )
    tf.position = pos
    assert tf.position.virtual_sl == 40980.0

    state = StoredState(date=None, equity=50000.0, peak_equity=50000.0)
    assert state.tf_states == {}
    assert state.pending_orders == []


def test_timeframe_param_namedtuple() -> None:
    p = TimeFrameParam(or_min=575, trig=0.05, sint=2.00, lock=0.90)
    assert p.or_min == 575
    assert p.lock == 0.90
