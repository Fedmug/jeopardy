"""
Tests for parse_svoya_igra.py.

Two kinds of tests:

* Parsing unit tests that feed small synthetic HTML snippets through
  `parse_svoya_igra` and check the resulting entries -- in particular the
  "Вопросы от…" special-topic handling (announcer sentence -> real topic
  name, and the duplicate-figure bogus-entry bug).
* Data-quality tests that load every already-generated
  `dataset_ru/*/questions.json` file and check basic structural invariants:
  no duplicate (topic, price) pairs, and no more than 5 questions per topic
  (the final round is excluded since it has a single, price-less question).
"""

import glob
import json
import os
from collections import Counter

import pytest

from parse_svoya_igra import parse_svoya_igra

DATASET_RU_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "dataset_ru")


def _parse(html_body, air_date="2000-01-01", index="_test", tmp_path=None):
    html = f"""
    <html><body>
    <h2>Ход игры</h2>
    {html_body}
    <h2>Итог игры</h2>
    </body></html>
    """
    cwd = os.getcwd()
    try:
        if tmp_path is not None:
            os.chdir(tmp_path)
        return parse_svoya_igra(html, air_date, index, download_content=False)
    finally:
        os.chdir(cwd)


# --------------------------------------------------------------------------
# "Вопросы от…" parsing
# --------------------------------------------------------------------------


def test_announcer_sentence_in_same_block_is_stripped(tmp_path):
    html_body = """
    <h3>Второй раунд</h3>
    <h4>Вопросы от… (600)</h4>
    <figure typeof="mw:File/Thumb">
      <a href="/wiki/%D0%A4%D0%B0%D0%B9%D0%BB:RU-SI-2017-09-02-18.jpg" class="mw-file-description">
        <img src="/w/images/thumb/2/26/RU-SI-2017-09-02-18.jpg/200px-RU-SI-2017-09-02-18.jpg"/>
      </a>
      <figcaption>Сегодня в теме «Вопросы от…» психолог-гипнотерапевт</figcaption>
    </figure>
    <p><i><b>Вопросы задаёт психолог-гипнотерапевт Олеся Фоминых.</b></i><br/>
    В книгу «Мой голос останется с вами» вошли рассказы американского психотерапевта
    Милтона Эриксона. Но изначально Эриксон создал рассказы с этой целью.
    </p>
    <p>Отвечает Геннадий.<br/>
    Правильный ответ: <i>Аутотренинг, чтобы помогать пациентам</i>
    </p>
    """
    entries = _parse(html_body, tmp_path=tmp_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["topic"] == "Вопросы от психолога-гипнотерапевта"
    assert entry["price"] == 600
    assert "Вопросы задаёт" not in entry["question"]
    assert entry["question"].startswith("В книгу «Мой голос останется с вами»")
    assert entry["answer"] == "Аутотренинг, чтобы помогать пациентам"


def test_announcer_before_duplicate_figure_is_not_a_bogus_entry(tmp_path):
    html_body = """
    <h3>Второй раунд</h3>
    <h4>Вопросы от… (600)</h4>
    <figure typeof="mw:File/Thumb">
      <a href="/wiki/%D0%A4%D0%B0%D0%B9%D0%BB:RU-SI-2016-12-24-19.jpg" class="mw-file-description">
        <img src="/w/images/thumb/4/4e/RU-SI-2016-12-24-19.jpg/200px-RU-SI-2016-12-24-19.jpg"/>
      </a>
      <figcaption>В теме «Вопросы от…» Анастасия Чернобровина</figcaption>
    </figure>
    <p><i><b>Вопросы по теме «Европа» задаёт советник президента Русского географического
    общества по информационной политике Анастасия Чернобровина.</b></i><br/>
    </p>
    <figure typeof="mw:File/Thumb">
      <a href="/wiki/%D0%A4%D0%B0%D0%B9%D0%BB:RU-SI-2016-12-24-20.jpg" class="mw-file-description">
        <img src="/w/images/thumb/2/20/RU-SI-2016-12-24-20.jpg/200px-RU-SI-2016-12-24-20.jpg"/>
      </a>
      <figcaption>Вопросы от… (600)</figcaption>
    </figure>
    <p>Музей алхимиков и магов в Старом городе этой столицы напоминает о временах
    императора Рудольфа II, покровителя магов и колдунов.
    </p>
    <p>Отвечает Егор.<br/>
    Правильный ответ: <i>Прага</i>
    </p>
    """
    entries = _parse(html_body, tmp_path=tmp_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["topic"] == "Европа"
    assert entry["price"] == 600
    assert entry["answer"] == "Прага"
    assert entry["content"] == ["RU-SI-2016-12-24-20.jpg"]


def test_rgo_guest_without_explicit_theme_falls_back_to_rgo(tmp_path):
    html_body = """
    <h3>Второй раунд</h3>
    <h4>Вопросы от… (600)</h4>
    <figure><figcaption>В теме «Вопросы от…» Анастасия Чернобровина</figcaption></figure>
    <p><i><b>Вопросы задаёт советник президента Русского географического общества по
    информационной политике Анастасия Чернобровина.</b></i><br/>
    </p>
    <figure><figcaption>Вопросы от… (600)</figcaption></figure>
    <p>Некий вопрос без явно указанной темы.
    </p>
    <p>Отвечает Егор.<br/>
    Правильный ответ: <i>Ответ</i>
    </p>
    """
    entries = _parse(html_body, tmp_path=tmp_path)
    assert len(entries) == 1
    assert entries[0]["topic"] == "Вопросы от РГО"


def test_subsequent_price_tiers_reuse_resolved_topic_name(tmp_path):
    html_body = """
    <h3>Второй раунд</h3>
    <h4>Вопросы от… (600)</h4>
    <p><i><b>Вопросы задаёт психолог-гипнотерапевт Олеся Фоминых.</b></i><br/>
    Первый вопрос от психолога.
    </p>
    <p>Отвечает Геннадий.<br/>
    Правильный ответ: <i>Ответ 1</i>
    </p>
    <h4>Вопросы от… (800)</h4>
    <p>Второй вопрос, без вступительной фразы.
    </p>
    <p>Отвечает Егор.<br/>
    Правильный ответ: <i>Ответ 2</i>
    </p>
    """
    entries = _parse(html_body, tmp_path=tmp_path)
    assert len(entries) == 2
    assert entries[0]["topic"] == "Вопросы от психолога-гипнотерапевта"
    assert entries[1]["topic"] == "Вопросы от психолога-гипнотерапевта"


def test_new_round_resets_resolved_topic_name(tmp_path):
    html_body = """
    <h3>Второй раунд</h3>
    <h4>Вопросы от… (600)</h4>
    <p><i><b>Вопросы задаёт психолог-гипнотерапевт Олеся Фоминых.</b></i><br/>
    Вопрос второго раунда.
    </p>
    <p>Отвечает Геннадий.<br/>
    Правильный ответ: <i>Ответ 1</i>
    </p>
    <h3>Третий раунд</h3>
    <h4>Вопросы от… (900)</h4>
    <p>Вопрос третьего раунда, из другой темы (без вступления в этом фрагменте).
    </p>
    <p>Отвечает Егор.<br/>
    Правильный ответ: <i>Ответ 2</i>
    </p>
    """
    entries = _parse(html_body, tmp_path=tmp_path)
    assert len(entries) == 2
    assert entries[0]["topic"] == "Вопросы от психолога-гипнотерапевта"
    # No announcer sentence in round 3 and no earlier resolution in that
    # round -> falls back to the raw, unresolved heading text.
    assert entries[1]["topic"] == "Вопросы от…"


# --------------------------------------------------------------------------
# Data-quality checks over the already-generated dataset
# --------------------------------------------------------------------------

_DATASET_FILES = sorted(glob.glob(os.path.join(DATASET_RU_DIR, "*", "questions.json")))


@pytest.mark.parametrize("path", _DATASET_FILES, ids=lambda p: os.path.basename(os.path.dirname(p)))
def test_no_duplicate_topic_price_pairs(path):
    with open(path, encoding="utf-8") as f:
        entries = json.load(f)

    seen = {}
    duplicates = []
    for entry in entries:
        if entry.get("round") == "final" or entry.get("tag") == "cat":
            continue
        key = (entry.get("topic"), entry.get("price"))
        if key in seen:
            duplicates.append(key)
        seen[key] = True

    assert not duplicates, f"{path}: duplicate (topic, price) pairs: {duplicates}"


@pytest.mark.parametrize("path", _DATASET_FILES, ids=lambda p: os.path.basename(os.path.dirname(p)))
def test_at_most_five_questions_per_topic(path):
    with open(path, encoding="utf-8") as f:
        entries = json.load(f)

    counts = {}
    for entry in entries:
        if entry.get("round") == "final" or entry.get("tag") == "cat":
            continue
        topic = entry.get("topic")
        counts[topic] = counts.get(topic, 0) + 1

    offenders = {topic: n for topic, n in counts.items() if n > 5}
    assert not offenders, f"{path}: topics with more than 5 questions: {offenders}"


_DATASET_DIRS = sorted(os.path.dirname(p) for p in _DATASET_FILES)


@pytest.mark.parametrize("dir_path", _DATASET_DIRS, ids=lambda p: os.path.basename(p))
def test_each_media_file_referenced_exactly_once(dir_path):
    with open(os.path.join(dir_path, "questions.json"), encoding="utf-8") as f:
        entries = json.load(f)

    referenced = Counter()
    for entry in entries:
        referenced.update(entry.get("content") or [])

    media_files = {
        name
        for name in os.listdir(dir_path)
        if name != "questions.json" and os.path.isfile(os.path.join(dir_path, name))
    }

    missing = sorted(name for name in media_files if referenced[name] == 0)
    duplicated = sorted(name for name in media_files if referenced[name] > 1)

    assert not missing, f"{dir_path}: media file(s) on disk not referenced in questions.json: {missing}"
    assert not duplicated, (
        f"{dir_path}: media file(s) referenced more than once in questions.json: {duplicated}"
    )
