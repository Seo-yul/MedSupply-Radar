"""CLI 진입점 — 결정적 데이터셋 생성기.

사용법:
    python scripts/generate_dataset.py --out data/medsupply.db --seed 20260801 \
        --base-date 2026-08-01 --baseline-only

현재는 --baseline-only 경로만 구현되어 있다(정상 운영 패턴 12개월 시계열). 시나리오
주입(--baseline-only 없이 실행)은 후속 태스크(S-12) 소관이며, 이 스크립트는 그 경우
"시나리오 주입은 미구현(S-12)" 에러로 종료한다. --config 인자는 받되 --baseline-only
에서는 무시한다.

실제 생성 로직은 scripts/datagen/baseline.py에 있다 — 이 파일은 얇은 CLI 진입점이며,
scripts/datagen/과 마찬가지로 medsupply 패키지를 import하지 않는다.
"""

from __future__ import annotations

import sys
from pathlib import Path

# 리포 루트를 sys.path에 올려 `scripts.datagen.baseline`을 절대 경로 실행에서도
# import할 수 있게 한다("python scripts/generate_dataset.py"로 직접 실행하면
# sys.path[0]이 scripts/가 되어 리포 루트가 기본으로는 잡히지 않는다).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.datagen.baseline import main  # noqa: E402

if __name__ == "__main__":
    main()
