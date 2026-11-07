"""Загрузить и проверить закреплённую модель до включения голосовых. Без токенов бота."""
from __future__ import annotations

import argparse
from pathlib import Path

from app.speech_models import prepare


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, help='Каталог модели; по умолчанию SPEECH_MODEL_DIR или пользовательский кэш')
    args = parser.parse_args()
    print(f'Модель проверена: {prepare(args.directory)}')


if __name__ == '__main__':
    main()
