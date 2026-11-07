"""Проверка GigaAM на записях команды, последовательно и без облачной модели.

.venv/bin/python -m eval.speech_eval manifest.csv --python .venv/speech/bin/python --output /tmp/voice-eval.csv
manifest.csv: audio,reference (путь относительно manifest и ручная расшифровка).
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import re
from pathlib import Path

from app.config import ROOT
from app.core.knowledge import KnowledgeBase
from app.core.real_flow import BusinessAssistant
from app.llm.mock import MockProvider
from app.speech import SpeechError, SpeechRecognizer


def words(text):
    return re.findall(r'\w+', text.lower().replace('ё', 'е'))


def distance(reference, hypothesis):
    row = list(range(len(hypothesis) + 1))
    for i, expected in enumerate(reference, 1):
        following = [i]
        for j, actual in enumerate(hypothesis, 1):
            following.append(min(following[-1]+1, row[j]+1, row[j-1]+(expected != actual)))
        row = following
    return row[-1]


async def evaluate(manifest, python, output):
    recognizer = SpeechRecognizer(True, python)
    assistant = BusinessAssistant(KnowledgeBase.load(ROOT / 'data/kb'), MockProvider())
    with manifest.open(encoding='utf-8-sig') as source:
        questions = list(csv.DictReader(source))
    results = []
    for question in questions:
        reference = question['reference']
        expected = await assistant.answer(reference)
        error = ''
        try:
            audio = (manifest.parent / question['audio']).read_bytes()
            transcript = await recognizer.transcribe(audio)
            actual = await assistant.answer(transcript)
            agrees = actual.decision == expected.decision
        except (OSError, SpeechError) as exc:
            transcript, agrees, error = '', False, type(exc).__name__
        count = len(words(reference))
        results.append({'audio':question['audio'], 'words':count,
                        'errors':distance(words(reference), words(transcript)),
                        'decision_agrees':agrees, 'error':error})
    if not results or sum(r['words'] for r in results) == 0:
        raise ValueError('Нужны записи с непустой эталонной расшифровкой')
    with output.open('w', encoding='utf-8', newline='') as target:
        writer = csv.DictWriter(target, fieldnames=results[0])
        writer.writeheader()
        writer.writerows(results)
    print(f"Записей {len(results)}, WER {sum(r['errors'] for r in results)/sum(r['words'] for r in results):.1%}, "
          f"совпадение решений {sum(r['decision_agrees'] for r in results)/len(results):.1%}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--python', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    asyncio.run(evaluate(args.manifest, args.python, args.output))
