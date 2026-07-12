"""
Parser for "Своя игра" (Russian Jeopardy!) review pages (wiki-style HTML export).

Usage:
    from parse_svoya_igra import parse_svoya_igra
    entries = parse_svoya_igra(html_content, year=2004, index="01-03")

This writes datasets_ru/{index}/question.json (the full list of entries) and
downloads any referenced audio/images into that same folder, in addition to
returning the entries list.

Design notes / assumptions (the source HTML has no formal schema, so some
editorial conventions were reverse-engineered from the sample page):

* The page is split into an "Участники" (players) section -- ignored -- and a
  "Ход игры" (game play) section that contains one <h3> per round
  ("Первый раунд", "Второй раунд", "Третий раунд", "Финальный раунд").
* Inside a round, each question normally starts with an <h4> heading of the
  form "Topic (price)". Everything up to the next heading/figure belongs to
  that question: first a descriptive paragraph (the question text, optionally
  prefixed with a bold "Кот в мешке." / "Вопрос-аукцион." marker), then a
  paragraph with the game log ("Отвечает X.", "Ответ игрока: ...",
  "Правильный ответ: ...", etc).
* "Кот в мешке" (Cat in the bag) questions are laid out differently: the grid
  slot the player picked is an <h4> that frequently carries no visible
  question text of its own (sometimes just an audio clip), because in the
  actual show the real topic/price/question for a "cat in the bag" pick is
  only revealed *after* selection. That reveal shows up as a <figure> whose
  caption gives the real "Topic (price)", followed by the real question
  text. Both the grid slot's own media (if any) and the reveal image belong
  to that single revealed question, so a heading with no question/answer
  text of its own never produces a standalone entry -- any media it carries
  is folded into the next entry that actually has question/answer content.
* The final round has no <h4> headings; topic comes from a "Тема: X" line,
  price is not fixed (players wager individually) so it is stored as null.
* Every entry's "content" field is a list of filenames (empty list if the
  question has no attached media -- it can contain more than one file, e.g.
  an audio clip plus a reveal image for a "cat in the bag" question).
"""

import requests
import argparse
import json
import os
import re
import urllib.parse
import urllib.request

from bs4 import BeautifulSoup, NavigableString, Tag

WIKI_BASE = "https://gameshows.ru"

ROUND_NAME_TO_NUMBER = {
    "Первый раунд": 1,
    "Синий раунд": 1,
    "Красный раунд": 2,
    "Второй раунд": 2,
    "Третий раунд": 3,
    "Финальный раунд": "final",
}

CAT_MARKER = "Кот в мешке"
AUCTION_MARKER = "Вопрос-аукцион"

NBSP = "\xa0"


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _clean_text(text):
    """Collapse whitespace, normalize non-breaking spaces, strip."""
    if text is None:
        return ""
    text = text.replace(NBSP, " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    text = text.strip()
    return text


def _parse_price(raw):
    """'1 200' / '1\xa0200' -> 1200 (int). Returns None if not parseable."""
    if raw is None:
        return None
    raw = raw.replace(NBSP, "").replace(" ", "").strip()
    raw = raw.strip("()")
    try:
        return int(raw)
    except ValueError:
        return None


TOPIC_PRICE_RE = re.compile(r"^(.*?)\s*\(([\d\s\xa0]+)\)\s*$")


def _split_topic_price(heading_text):
    """'Чудеса XXI века (400)' -> ('Чудеса XXI века', 400)"""
    heading_text = _clean_text(heading_text)
    m = TOPIC_PRICE_RE.match(heading_text)
    if not m:
        return heading_text, None
    topic = m.group(1).strip()
    price = _parse_price(m.group(2))
    return topic, price


def _filename_from_title(title):
    """'Файл:RU-SI-2004-01-03-2.mp3' -> 'RU-SI-2004-01-03-2.mp3'"""
    if not title:
        return None
    if ":" in title:
        return title.split(":", 1)[1].strip()
    return title.strip()


def _filename_from_href(href):
    """/wiki/%D0%A4%D0%B0%D0%B9%D0%BB:RU-SI-2004-01-03-2.mp3 -> filename"""
    if not href:
        return None
    href = urllib.parse.unquote(href)
    if ":" in href:
        return href.rsplit(":", 1)[1].strip()
    return None


def _original_image_url_from_thumb_src(src):
    """
    Thumbnail src looks like:
      /w/images/thumb/3/39/RU-SI-2004-01-03-6.jpg/200px-RU-SI-2004-01-03-6.jpg
    The full-resolution original lives at:
      /w/images/3/39/RU-SI-2004-01-03-6.jpg
    """
    if not src or "/thumb/" not in src:
        return None
    prefix, _, _rest = src.partition("/thumb/")
    parts = _rest.split("/")
    if len(parts) < 3:
        return None
    dir1, dir2, filename = parts[0], parts[1], parts[2]
    return f"{prefix}/{dir1}/{dir2}/{filename}", filename


def _download_file(url, out_path):
    """Best-effort download; never raises."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        with open(out_path, "wb") as f:
            f.write(data)
        return True
    except Exception as exc:  # noqa: BLE001 - best effort, log & continue
        print(f"  [warn] could not download {url}: {exc}")
        return False


def _file_path_url(filename):
    """
    MediaWiki's Special:FilePath/<filename> redirects straight to the actual
    file bytes (whether the file lives locally or on Wikimedia Commons), so
    it works uniformly for both images and audio without having to guess
    thumbnail hash-directories or scrape the file description page.
    """
    return f"{WIKI_BASE}/wiki/{urllib.parse.quote('Special:FilePath/' + filename)}"


def _find_file_reference(block_tags):
    """
    Look through a list of tags (the elements between two headings) for an
    inline file reference. Two markup styles are seen in the wild:

    1. A modern embedded player: <audio data-mwtitle="X.mp3"><source src="/w/images/.../X.mp3"></audio>
       The <source src> is a ready-made relative URL for the actual file, so
       we use it directly rather than going through Special:FilePath.
    2. An older plain link: <a title="Файл:X.mp3">Файл:X.mp3</a> (or, for a
       missing/broken file, the same wrapped in a "new"-class link).

    Returns (filename, download_url, matched_tag_for_exclusion) or
    (None, None, None). `matched_tag_for_exclusion` lets the caller drop
    that tag's own label text (e.g. "Файл:X.mp3") from the question body;
    <audio> embeds have no visible text so this is mostly relevant to case 2.
    """
    for tag in block_tags:
        if not isinstance(tag, Tag):
            continue

        audio = tag.find("audio")
        if audio is not None:
            filename = audio.get("data-mwtitle")
            source = audio.find("source")
            src = source.get("src") if source is not None else None
            if not filename and src:
                filename = src.rsplit("/", 1)[-1]
            if filename:
                url = f"{WIKI_BASE}{src}" if src else _file_path_url(filename)
                return filename, url, audio

        for a in tag.find_all("a"):
            title = a.get("title", "")
            if title.startswith("Файл:"):
                filename = _filename_from_title(title)
                # broken/missing file -> no real download target
                if "new" in (a.get("class") or []):
                    return filename, None, a
                return filename, _file_path_url(filename), a
    return None, None, None


def _find_figure_image(figure_tag):
    """Given a <figure> tag, return (filename, download_url)."""
    filename = None
    a = figure_tag.find("a", class_="mw-file-description")
    if a is not None:
        filename = _filename_from_href(a.get("href", ""))
    if not filename:
        img = figure_tag.find("img")
        if img is not None:
            result = _original_image_url_from_thumb_src(img.get("src", ""))
            if result is not None:
                _, filename = result
    if not filename:
        return None, None
    return filename, _file_path_url(filename)


# --------------------------------------------------------------------------
# answer-block parsing (the "Отвечает X. / Ответ игрока: ... / Правильный
# ответ: ..." log that follows every question)
# --------------------------------------------------------------------------

ANSWERS_LINE_RE = re.compile(r"^Ответ игрока:\s*(.+)$")
CORRECT_LINE_RE = re.compile(r"^Правильный ответ:\s*(.+)$")
PLAYER_TURN_RE = re.compile(r"^Отвечает\s+([^\.]+)\.?\s*$")
AUCTION_TURN_RE = re.compile(r"^Играет\s+([^\.]+)\.\s*Ставка.*$")
NO_ANSWER_RE = re.compile(r"^Игрок\s+н[ае]\s+даёт\s+ответа\.?\s*$", re.IGNORECASE)
FINAL_ANSWER_RE = re.compile(r"^Ответ\s+(\S+):\s*(.+)$")
BET_LINE_RE = re.compile(r"^Ставка.*$")

ACCEPTED_NOTE_RE = re.compile(r"\(\s*ответ\s+был\s+принят\s*\)", re.IGNORECASE)


def _normalize_answer(text):
    text = text.strip()
    text = text.strip("«»\"'")
    text = re.sub(r"\s+", " ", text)
    return text.lower()


def _parse_answer_block(lines):
    """
    lines: list of plain-text lines (already split on <br/> and cleaned)
    making up the portion of the block that starts at the first recognized
    marker (Отвечает / Играет / Ответ.../ Правильный ответ / Игрок не даёт...).

    Returns (correct_answer_text, players_answers list of {"answer","correct"})
    """
    raw_entries = []  # list of [given_answer_or_None]
    open_slot = False  # a player is "up" awaiting resolution

    correct_answer_raw = None

    for line in lines:
        line = line.strip()
        if not line:
            continue

        m = FINAL_ANSWER_RE.match(line)
        if m:
            # Final-round style: "Ответ Веры: —"
            given = m.group(2).strip()
            raw_entries.append(given)
            open_slot = False
            continue

        if BET_LINE_RE.match(line) and not AUCTION_TURN_RE.match(line):
            # standalone "Ставка — N." line (final round) -> ignore, no new info
            continue

        m = AUCTION_TURN_RE.match(line)
        if m:
            if open_slot:
                raw_entries.append(None)  # previous player resolved silently
            open_slot = True
            continue

        m = PLAYER_TURN_RE.match(line)
        if m:
            if open_slot:
                raw_entries.append(None)
            open_slot = True
            continue

        m = ANSWERS_LINE_RE.match(line)
        if m:
            raw_entries.append(m.group(1).strip())
            open_slot = False
            continue

        if NO_ANSWER_RE.match(line):
            raw_entries.append("")
            open_slot = False
            continue

        m = CORRECT_LINE_RE.match(line)
        if m:
            correct_answer_raw = m.group(1).strip()
            if open_slot:
                raw_entries.append(None)
                open_slot = False
            continue

        # Unrecognized line (e.g. a "Прим.:" note) -> ignore for structured
        # parsing purposes.

    if open_slot:
        raw_entries.append(None)

    accepted_note = False
    correct_answer_text = correct_answer_raw
    if correct_answer_text is not None and ACCEPTED_NOTE_RE.search(correct_answer_text):
        accepted_note = True
        correct_answer_text = ACCEPTED_NOTE_RE.sub("", correct_answer_text).strip()
        correct_answer_text = re.sub(r"\s+", " ", correct_answer_text)

    players_answers = []
    n = len(raw_entries)
    for i, given in enumerate(raw_entries):
        is_last = i == n - 1
        if given is None:
            # player's turn resolved without an explicit "Ответ игрока" line
            # right before the correct answer was revealed -> they got it.
            final_answer = correct_answer_text or ""
            correct = True
        elif given in ("", "—", "-"):
            final_answer = ""
            correct = False
        else:
            final_answer = given
            correct = False
            if correct_answer_text is not None:
                correct = _normalize_answer(given) == _normalize_answer(correct_answer_text)
            if not correct and accepted_note and is_last:
                correct = True
        players_answers.append({"answer": final_answer, "correct": correct})

    return correct_answer_text, players_answers


# --------------------------------------------------------------------------
# question-text / tag extraction
# --------------------------------------------------------------------------

MARKER_LINE_RES = [
    re.compile(r"^Отвечает\s"),
    re.compile(r"^Играет\s"),
    re.compile(r"^Ответ игрока:"),
    re.compile(r"^Правильный ответ:"),
    re.compile(r"^Игрок\s+н[ае]\s+даёт\s+ответа", re.IGNORECASE),
    re.compile(r"^Ответ\s+\S+:"),
    re.compile(r"^Ставка"),
]


def _is_marker_line(line):
    line = line.strip()
    return any(r.match(line) for r in MARKER_LINE_RES)


TEMA_PREFIX_RE = re.compile(r"^Тема:\s*.+?\.\s*")


def _extract_tag_and_strip(question_text):
    tag = None
    if CAT_MARKER in question_text:
        tag = "cat"
        question_text = question_text.replace(CAT_MARKER + ".", "")
        question_text = question_text.replace(CAT_MARKER, "")
    elif AUCTION_MARKER in question_text:
        tag = "auction"
        question_text = question_text.replace(AUCTION_MARKER + ".", "")
        question_text = question_text.replace(AUCTION_MARKER, "")
    question_text = re.sub(r"\s+", " ", question_text).strip()
    if tag == "cat":
        # "Кот в мешке" questions restate their (already-captured-in-`topic`)
        # theme inline, e.g. "Тема: Я вам спою. Назовите группу...". Drop
        # that lead-in since it's redundant with the entry's topic field.
        question_text = TEMA_PREFIX_RE.sub("", question_text, count=1).strip()
    return tag, question_text


def _block_to_lines(block_tags, skip_tag=None):
    """
    Turn a list of tags (paragraphs etc, with any <figure> already filtered
    out by the caller) into a flat list of plain-text lines, splitting on
    <br/> so the "Отвечает X. / Ответ игрока: ..." log lines are separated.

    If `skip_tag` is given, any text that is a descendant of that specific
    tag instance is omitted (used to drop a "Файл:..." link's own label text
    from the question, since that's just a media reference, not question
    content).
    """
    lines = []
    for tag in block_tags:
        if not isinstance(tag, Tag):
            continue
        # Split on <br> manually to keep line structure.
        current = []
        for child in tag.descendants:
            if isinstance(child, Tag) and child.name == "br":
                lines.append(_clean_text("".join(current)))
                current = []
            elif isinstance(child, NavigableString):
                if skip_tag is not None and skip_tag in child.parents:
                    continue
                # skip text that is itself inside a nested <a>/<i> already
                # captured by parent traversal -- descendants gives us every
                # NavigableString exactly once, so just append.
                current.append(str(child))
        if current:
            lines.append(_clean_text("".join(current)))
    # drop empties created by stray <br><br>
    return [l for l in lines if l]


def _build_question_and_answer(block_tags):
    """
    block_tags: tags between a heading/figcaption and the next heading/figure
    (paragraphs only, figures handled separately by the caller).

    Returns dict with question, answer, players_answers, tag, content(from
    inline file refs only -- figure-derived content is handled by caller).
    """
    inline_filename, inline_url, file_anchor_tag = _find_file_reference(block_tags)

    lines = _block_to_lines(block_tags, skip_tag=file_anchor_tag)

    extracted_topic = None
    tema_re = re.compile(r"^Тема:\s*(.+)$")
    if lines:
        m = tema_re.match(lines[0])
        if m:
            extracted_topic = m.group(1).strip().rstrip(".")
            lines = lines[1:]

    split_idx = len(lines)
    for i, line in enumerate(lines):
        if _is_marker_line(line):
            split_idx = i
            break

    question_lines = lines[:split_idx]
    answer_lines = lines[split_idx:]

    question_text = " ".join(question_lines).strip()
    tag, question_text = _extract_tag_and_strip(question_text)

    correct_answer, players_answers = _parse_answer_block(answer_lines)

    media = [{"filename": inline_filename, "url": inline_url}] if inline_filename else []

    return {
        "question": question_text,
        "answer": correct_answer or "",
        "players_answers": players_answers,
        "tag": tag,
        "media": media,
        "extracted_topic": extracted_topic,
    }


# --------------------------------------------------------------------------
# top level parse
# --------------------------------------------------------------------------

def parse_svoya_igra(html_content, year, index):
    """
    Parse a single game's review page HTML, return the list of question
    dicts, AND persist them as JSON to disk.

    Output layout (created at the start of the call):
        datasets_ru/{index}/question.json   <- the full list of entries
        datasets_ru/{index}/<filename>      <- any downloaded audio/images

    `year` tags each entry with the game year. `index` identifies the game
    (e.g. "01-03" for RU-SI-2004-01-03) and is used as the output subfolder
    name.

    Each entry's "content" field is always a list of filenames (empty list
    if the question had no attached media). A single question can have more
    than one file - most commonly a "Кот в мешке" (cat-in-the-bag) question,
    which is laid out across the source HTML as: the grid heading the player
    picked (frequently carrying just an audio clip and no visible question
    text of its own, since the *real* question is revealed after selection),
    followed by a <figure> whose caption gives the actually-revealed topic
    and price, followed by the real question text. Both the grid heading's
    audio and the reveal image belong to that single revealed question, so
    they end up together in one entry's "content" list. Headings with no
    question/answer text of their own never produce a standalone entry --
    any media they carry is folded into the next entry that actually has
    question/answer content.
    """
    soup = BeautifulSoup(html_content, "html.parser")

    output_dir = os.path.join(os.getcwd(), "dataset_ru", str(index))
    os.makedirs(output_dir, exist_ok=True)

    all_tags = soup.find_all(["h2", "h3", "h4", "p", "ul", "figure"])

    entries = []

    current_round = None
    in_game_section = False

    current_topic = None
    current_price = None
    current_block_tags = []
    have_pending_heading = False
    carried_media = []  # media (audio/images) waiting to attach to the next real entry

    def download_media(media_list):
        filenames = []
        for m in media_list:
            fn = m.get("filename")
            if not fn:
                continue
            filenames.append(fn)
            url = m.get("url")
            if url:
                _download_file(url, os.path.join(output_dir, fn))
        return filenames

    def flush_current():
        nonlocal carried_media, current_block_tags, have_pending_heading
        if not have_pending_heading:
            return
        if not current_block_tags:
            parsed = {
                "question": "",
                "answer": "",
                "players_answers": [],
                "tag": None,
                "media": [],
                "extracted_topic": None,
            }
        else:
            parsed = _build_question_and_answer(current_block_tags)

        media_list = carried_media + parsed["media"]

        topic = current_topic
        price = current_price
        if topic is None and parsed.get("extracted_topic"):
            topic = parsed["extracted_topic"]

        if not parsed["question"] and not parsed["answer"]:
            # Nothing substantive for this heading (e.g. a grid slot whose
            # only trace is an audio clip for the cat-in-the-bag question
            # that follows). Don't emit an entry - just carry the media
            # forward so it attaches to the next real question instead.
            carried_media = media_list
            current_block_tags = []
            have_pending_heading = False
            return

        content_filenames = download_media(media_list)

        entry = {
            "year": year,
            "topic": topic,
            "price": price,
            "round": current_round,
            "tag": parsed["tag"],
            "content": content_filenames,
            "question": parsed["question"],
            "answer": parsed["answer"],
            "players_answers": parsed["players_answers"],
        }
        entries.append(entry)

        carried_media = []
        current_block_tags = []
        have_pending_heading = False

    for tag in all_tags:
        if tag.name == "h2":
            heading_text = _clean_text(tag.get_text())
            if heading_text == "Ход игры":
                in_game_section = True
            elif heading_text == "Итог игры":
                flush_current()
                in_game_section = False
                break
            continue

        if not in_game_section:
            continue

        if tag.name == "h3":
            flush_current()
            heading_text = _clean_text(tag.get_text())
            current_round = ROUND_NAME_TO_NUMBER.get(heading_text, current_round)
            if heading_text == "Финальный раунд":
                current_topic = None
                current_price = None
                # no <h4> precedes the final round's single question, so
                # start collecting paragraphs for it right away
                have_pending_heading = True
                current_block_tags = []
            continue

        if tag.name == "h4":
            heading_text = _clean_text(tag.get_text())
            if heading_text.startswith("Итог раунда"):
                flush_current()  # scoreboard heading, not a question
                continue
            flush_current()
            current_topic, current_price = _split_topic_price(heading_text)
            have_pending_heading = True
            continue

        if tag.name == "ul":
            # Either the round's theme list ("Темы:") or a scoreboard list;
            # neither carries question data.
            continue

        if tag.name == "figure":
            figcaption = tag.find("figcaption")
            fc_text = _clean_text(figcaption.get_text()) if figcaption else ""
            fc_topic, fc_price = _split_topic_price(fc_text) if fc_text else (None, None)

            if fc_price is not None:
                # This figure marks the real start of a "Кот в мешке"
                # question: flush (and likely just carry-forward, since it
                # rarely has real text) whatever the current heading had
                # accumulated, then start a new block using the figcaption's
                # topic/price, remembering the reveal image as media that
                # will attach to this new (still-to-come) entry.
                flush_current()
                filename, url = _find_figure_image(tag)
                if filename:
                    carried_media = carried_media + [{"filename": filename, "url": url}]
                current_topic, current_price = fc_topic, fc_price
                current_block_tags = []
                have_pending_heading = True
            # else: an unrelated illustrative figure (participant photo,
            # studio picture, final scoreboard picture) -> ignore.
            continue

        if tag.name == "p":
            if have_pending_heading:
                current_block_tags.append(tag)
            continue

    # final safety flush (in case "Итог игры" heading was missing)
    flush_current()

    # If media was carried but never attached to any entry (e.g. the source
    # page ended on a bare media reference), attach it to the last entry we
    # produced rather than silently losing it.
    if carried_media and entries:
        extra_filenames = download_media(carried_media)
        entries[-1]["content"].extend(
            fn for fn in extra_filenames if fn not in entries[-1]["content"]
        )
    elif carried_media:
        print(f"  [warn] {len(carried_media)} media file(s) could not be attached to any question")

    out_path = os.path.join(output_dir, "questions.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)

    return entries


headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Parse Svoya Igra games from games.json for a numeric key range."
    )
    parser.add_argument("--start", type=int, required=True, help="Start key (inclusive)")
    parser.add_argument("--end", type=int, required=True, help="End key (inclusive)")
    args = parser.parse_args(argv)

    if args.start > args.end:
        parser.error("--start must be less than or equal to --end")

    script_dir = os.path.dirname(os.path.abspath(__file__))
    games_path = os.path.join(script_dir, "games.json")
    dataset_root = os.path.join(script_dir, "dataset_ru")

    with open(games_path, "r", encoding="utf-8") as f:
        games = json.load(f)

    selected = []
    for key, game_data in games.items():
        try:
            numeric_key = int(key)
        except (TypeError, ValueError):
            continue

        if args.start <= numeric_key <= args.end:
            if os.path.exists(os.path.join(dataset_root, key)):
                print(f"[{key}] skipping (already exists)")
                continue
            review_html = requests.get(game_data['link'], headers=headers, timeout=10).text
            questions = parse_svoya_igra(review_html, int(game_data['date'][-4:]), key)
            print(f"in game {key} parsed {len(questions)} questions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
