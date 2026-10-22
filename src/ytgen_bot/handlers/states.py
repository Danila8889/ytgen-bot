from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class LinkStates(StatesGroup):
    email = State()
    code = State()


class PromoStates(StatesGroup):
    code = State()


class WithdrawStates(StatesGroup):
    amount = State()
