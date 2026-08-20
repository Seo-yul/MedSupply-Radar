"""입고 상태를 **as_of 시점 기준으로 재구성**하는 술어 모음(순수 함수).

백테스트에서 `incoming_shipments.actual_date`(도착 스탬프)는 **as_of 시점에는 아직 존재하지
않는 미래 정보**다. 그래서 "미도착"을 `actual_date IS NULL`로 판정하면, 나중에 도착했다는
사실로 과거 시점의 상태를 소급 왜곡하게 된다(Task S-17b에서 실측으로 드러난 결함).

이 모듈은 그 판정을 한 곳에 모은다. 같은 규칙을 depletion(소진 추정)과 anomaly(입고 지연
탐지)가 각자 구현하면 한쪽만 고쳐지는 사고가 나기 때문이다 — 실제로 S-17c가 depletion만
고쳤고 anomaly는 구 의미론으로 남아 S-17d에서 다시 손대야 했다.

as_of 시점 상태 분류(세 술어는 상호배타적이며, 도착분을 제외하면 전체를 덮는다):
    도착   arrived_by      : actual_date <= as_of
    연체   is_overdue_at   : expected_date <= as_of AND 미도착
    예정   is_pending_at   : expected_date >  as_of AND 미도착
"""

from __future__ import annotations

from datetime import date

import pandas as pd


def _is_missing(value) -> bool:
    """결측(None·NaT·NaN) 여부.

    NaT를 date와 직접 비교하면 pandas 버전에 따라 조용히 False가 되거나 예외가 날 수 있어,
    결측 판정을 항상 비교보다 먼저 한다(판정의 결정성을 위해 명시적으로 처리한다).
    """
    return value is None or pd.isna(value)


def is_on_or_before(value, as_of: date) -> bool:
    """value가 실제 날짜이고 as_of 이하인가. 결측이면 False."""
    if _is_missing(value):
        return False
    return value <= as_of


def is_strictly_after(value, as_of: date) -> bool:
    """value가 실제 날짜이고 as_of보다 뒤인가. 결측이면 False."""
    if _is_missing(value):
        return False
    return value > as_of


def arrived_by(actual_date, as_of: date) -> bool:
    """as_of 시점에 이미 도착했는가(actual_date <= as_of).

    actual_date가 as_of보다 뒤면 as_of 시점에는 **아직 도착하지 않은 것**으로 본다 —
    도착 스탬프를 미래 정보로 취급하지 않기 위한 핵심 규칙이다.
    """
    return is_on_or_before(actual_date, as_of)


def is_overdue_at(expected_date, actual_date, as_of: date) -> bool:
    """as_of 시점 연체인가: 예정일이 지났는데(<= as_of) 아직 도착하지 않았다."""
    return is_on_or_before(expected_date, as_of) and not arrived_by(actual_date, as_of)


def is_pending_at(expected_date, actual_date, as_of: date) -> bool:
    """as_of 시점 미래 예정(pending)인가: 예정일이 아직 오지 않았고(> as_of) 미도착이다."""
    return is_strictly_after(expected_date, as_of) and not arrived_by(actual_date, as_of)
