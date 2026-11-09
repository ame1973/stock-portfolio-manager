"""Compare eligibility against the existing replay across deterministic ledger histories."""

from datetime import date, timedelta
from decimal import Decimal
from random import Random

from spm_api.dividend_eligibility import recorded_eligibility
from spm_api.replay import Event, Split, Trade, Transfer, replay


def test_recorded_quantities_match_authoritative_replay_across_histories() -> None:
    accounts = ("A", "B", "C", "D")
    first = date(2020, 1, 1)
    for seed in range(1000):
        randomizer = Random(seed)
        quantities = dict.fromkeys(accounts, 0)
        events: list[Event] = []
        for index in range(1, 21):
            account = randomizer.choice(accounts)
            kind = randomizer.choice(("BUY", "SELL", "TRANSFER", "SPLIT"))
            operation: Trade | Transfer | Split
            if kind == "SELL" and quantities[account] > 0:
                amount = randomizer.randint(1, quantities[account])
                quantities[account] -= amount
                operation = Trade(account, "S", "SELL", Decimal(amount), Decimal(0))
            elif kind == "TRANSFER" and quantities[account] > 0:
                target = randomizer.choice([item for item in accounts if item != account])
                amount = randomizer.randint(1, quantities[account])
                quantities[account] -= amount
                quantities[target] += amount
                operation = Transfer(account, target, "S", Decimal(amount))
            elif kind == "SPLIT":
                operation = Split("S", 2, 1)
                quantities = {key: value * 2 for key, value in quantities.items()}
            else:
                amount = randomizer.randint(1, 20)
                quantities[account] += amount
                operation = Trade(account, "S", "BUY", Decimal(amount), Decimal(0))
            events.append(Event(f"fact-{index}", first + timedelta(days=index), index, operation))

        ex_date = first + timedelta(days=randomizer.randint(1, 21))
        as_of = first + timedelta(days=randomizer.randint(1, 21))
        reached = ex_date <= as_of
        expected = replay(events, as_of=ex_date if reached else as_of, exclusive=reached)
        result = recorded_eligibility(
            events,
            (),
            accounts,
            security_id="S",
            ex_date=ex_date,
            as_of=as_of,
            rule="ORDINARY_PRE_EX_DATE",
        )
        for item in result:
            position = expected.get((item.account_id, "S"))
            quantity = position.quantity if position is not None else Decimal(0)
            assert item.recorded_quantity == f"{quantity:.8f}", (seed, item.account_id)
            assert item.state == (
                "RECORDED_PRE_EX_DATE" if reached else "UPCOMING_RECORDED_HOLDINGS"
            )
