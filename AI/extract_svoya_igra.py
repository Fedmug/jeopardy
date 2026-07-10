#!/usr/bin/env python3
"""
Скрипт для извлечения вопросов и ответов из видео-файлов передачи "Своя Игра" (Jeopardy!).
Использует современный Google GenAI SDK и мультимодальную модель Gemini для точного распознавания.
"""

import os
import sys
import json
import argparse
import time
from typing import List, Optional

# Проверка наличия необходимых библиотек
try:
    from pydantic import BaseModel, Field
except ImportError:
    print("Ошибка: библиотека 'pydantic' не установлена.", file=sys.stderr)
    print("Установите её командой: pip install pydantic", file=sys.stderr)
    sys.exit(1)

try:
    from google import genai
    from google.genai import types
except ImportError:
    print("Ошибка: библиотека 'google-genai' не установлена.", file=sys.stderr)
    print("Установите её командой: pip install google-genai", file=sys.stderr)
    sys.exit(1)


# Определение структуры выходных данных для строгого контроля формата со стороны Gemini
class QuestionEntry(BaseModel):
    year: Optional[int] = Field(
        None, 
        description="Год проведения игры (если его можно определить из видео/заставки, например 1994)"
    )
    price: Optional[int] = Field(
        None, 
        description="Номинал (стоимость) вопроса в очках/рублях (например: 20, 40, 100, 500)"
    )
    topic: str = Field(
        ..., 
        description="Тема (категория) вопроса на русском языке (например: 'Музыка', 'Литература')"
    )
    question: str = Field(
        ..., 
        description="Полный текст вопроса, который зачитывает ведущий или который отображается на экране"
    )
    answer: str = Field(
        ..., 
        description="Правильный ответ, озвученный ведущим или принятый у выигравшего игрока"
    )
    players_answers: List[str] = Field(
        default_factory=list, 
        description="Неверные или непринятые версии ответов, которые давали другие игроки до озвучивания правильного"
    )


class ExtractionResult(BaseModel):
    questions: List[QuestionEntry] = Field(
        ..., 
        description="Список всех успешно распознанных вопросов из видеозаписи"
    )


def extract_questions_from_video(video_path: str, output_path: str, model_name: str = "gemini-2.5-flash"):
    # Проверка API-ключа
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Ошибка: Переменная окружения GEMINI_API_KEY не задана.", file=sys.stderr)
        print("Пожалуйста, установите её: export GEMINI_API_KEY='ваш_ключ'", file=sys.stderr)
        sys.exit(1)

    # Инициализация официального клиента Google GenAI
    client = genai.Client(api_key=api_key)

    if not os.path.exists(video_path):
        print(f"Ошибка: Файл '{video_path}' не найден.", file=sys.stderr)
        sys.exit(1)

    print(f"|— Шаг 1: Загрузка файла '{video_path}' в Google File API...")
    try:
        # Загрузка видео-файла (Gemini может обрабатывать большие видео/аудио через Files API)
        video_file = client.files.upload(file=video_path)
        print(f"|   Видео успешно загружено. Имя в системе: {video_file.name}")
    except Exception as e:
        print(f"Ошибка при загрузке файла в Google Cloud: {e}", file=sys.stderr)
        sys.exit(1)

    print("|— Шаг 2: Ожидание обработки видео моделью Gemini (это может занять некоторое время)...")
    # Видеофайлам требуется время на транскодирование на стороне Google
    try:
        while True:
            file_state = client.files.get(name=video_file.name)
            state_name = file_state.state.name if hasattr(file_state.state, "name") else str(file_state.state)
            
            if state_name == "ACTIVE":
                print("\n|   Обработка завершена! Видео готово к анализу.")
                break
            elif state_name == "FAILED":
                print("\nОшибка: Обработка видеофайла на сервере завершилась неудачей.", file=sys.stderr)
                # Попробуем удалить файл перед выходом
                try:
                    client.files.delete(name=video_file.name)
                except:
                    pass
                sys.exit(1)
            else:
                print(".", end="", flush=True)
                time.sleep(10)
    except KeyboardInterrupt:
        print("\nПроцесс прерван пользователем. Удаление временного файла на сервере...")
        try:
            client.files.delete(name=video_file.name)
        except:
            pass
        sys.exit(0)
    except Exception as e:
        print(f"\nОшибка при проверке статуса файла: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"|— Шаг 3: Запуск мультимодального анализа с моделью '{model_name}'...")
    
    # Промпт, подробно описывающий правила извлечения вопросов "Своей игры"
    prompt = """
    Ты — профессиональный архивариус телепередачи "Своя Игра" (российского аналога Jeopardy!).
    Твоя задача — внимательно изучить загруженную видеозапись и извлечь все прозвучавшие вопросы в хронологическом порядке.
    
    Методология извлечения:
    1. Категория/Тема (topic): Определи тему раунда. Обычно она пишется на синем табло или озвучивается ведущим перед зачитыванием вопросов.
    2. Стоимость (price): Определи номинал вопроса (например, 20, 40 ... в старых выпусках или 100, 200 ... в новых).
    3. Текст вопроса (question): Восстанови полный и точный текст вопроса, комбинируя визуальный текст на экране (если он есть) и речь ведущего. Текст должен быть связным и грамотным на русском языке.
    4. Правильный ответ (answer): Внимательно слушай аудио-дорожку. Тебе нужен финальный вердикт:
       - Если игрок дал верный ответ и ведущий его принял (например, сказал "Верно", "Да", "+100"), запиши этот ответ.
       - Если игроки ошиблись и никто не ответил верно, дождись, пока ведущий сам озвучит правильный ответ в конце раунда. Запиши его.
    5. Неверные ответы игроков (players_answers): Запиши ответы игроков, которые пытались ответить, но получили штраф (ведущий сказал "Нет", "Неверно"). Если попыток не было, оставь список пустым.
    6. Год игры (year): Попробуй определить год выпуска (из заставки, титров или контекста). Если не удаётся определить, оставь `null` или попробуй сделать обоснованное дедуктивное предположение.

    Отправляй результат СТРОГО в соответствии с предложенной JSON-схемой.
    """.strip()

    try:
        response = client.models.generate_content(
            model=model_name,
            contents=[video_file, prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ExtractionResult,
                temperature=0.1,  # Низкая температура для максимальной фактологической точности
            ),
        )
        
        # Разбор результата
        result_data = json.loads(response.text)
        
        # Сохранение в файл
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(result_data, f, ensure_ascii=False, indent=4)
            
        print(f"|— Успех! Извлечено вопросов: {len(result_data.get('questions', []))}")
        print(f"|— Результаты сохранены в файл: '{output_path}'")
        
    except Exception as e:
        print(f"Ошибка при генерации или сохранении данных: {e}", file=sys.stderr)
        if 'response' in locals() and response.text:
            print("Сырой текст ответа от модели:", file=sys.stderr)
            print(response.text, file=sys.stderr)
    finally:
        # Всегда удаляем файл из облака во избежание захламления дискового пространства
        print("|— Очистка: Удаление временного файла из Google File API...")
        try:
            client.files.delete(name=video_file.name)
            print("|   Файл успешно удален из хранилища.")
        except Exception as e:
            print(f"Предупреждение: Не удалось удалить файл из облака: {e}", file=sys.stderr)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Извлечение вопросов 'Своей игры' из видео с помощью Google Gemini Multimodal API."
    )
    parser.add_argument("video_path", help="Путь к видео-файлу (mp4, mkv, avi, webm)")
    parser.add_argument("-o", "--output", default="questions.json", help="Путь для сохранения итогового JSON (по умолчанию: questions.json)")
    parser.add_argument("-m", "--model", default="gemini-2.5-flash", help="Модель Gemini для использования (по умолчанию: gemini-2.5-flash)")
    
    args = parser.parse_args()
    
    # Быстрая проверка переменных окружения
    if "GEMINI_API_KEY" not in os.environ:
        print("ВНИМАНИЕ: Переменная GEMINI_API_KEY не установлена. Скрипт завершится ошибкой.", file=sys.stderr)
        print("Пожалуйста, выполните: export GEMINI_API_KEY='ваш_ключ_api'", file=sys.stderr)
        print("-" * 50)
        
    extract_questions_from_video(args.video_path, args.output, args.model)
