"""Первый запуск тома: кладём исходную KB один раз, существующие данные не меняем."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def initialize(data: Path, seed: Path) -> None:
    data.mkdir(parents=True, exist_ok=True)
    marker = data / ".initialized"
    if marker.exists():
        return
    kb = data / "kb"
    # Пустая папка KB допустима: бизнес может начать с наблюдения.
    if not kb.exists():
        if any(data.iterdir()):
            raise RuntimeError("В томе есть данные, но нет kb/: восстановите полный бэкап вместо повторной инициализации")
        shutil.copytree(seed, kb)
    marker.write_text("1\n", encoding="utf-8")


def main() -> None:
    args = sys.argv[1:] or ["python", "-m", "app"]
    if args[:3] == ["python", "-m", "app"]:
        initialize(Path("/app/data"), Path("/app/kb-seed"))
    os.execvp(args[0], args)


if __name__ == "__main__":
    main()
