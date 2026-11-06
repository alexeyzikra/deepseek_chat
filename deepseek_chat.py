import os
import json
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY_CHAT"),
    base_url="https://api.deepseek.com"
)

MODEL = "deepseek-flash"

# Файл с системным промптом для команды /system_file — ищется в той же папке, где лежит этот скрипт
SYSTEM_PROMPT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "system_prompt.txt")

# Системный промпт по умолчанию (с ним программа стартует и к нему возвращается после /clear)
system_prompt = "Ты полезный ассистент. Отвечай кратко и емко."

# История живет только в этом списке в оперативной памяти
messages = [
    {"role": "system", "content": system_prompt}
]

HELP = """Команды:
  /system <текст>          — изменить системный промпт (для коротких промптов)
  /system_file             — взять системный промпт из файла system_prompt.txt рядом со скриптом
  /user <текст>            — записать ваше сообщение в историю БЕЗ отправки боту
                             (если последнее в истории уже ваше неотправленное — заменяет его)
  /fake <текст>            — заменить последний ответ бота на ваш текст
                             (если последнее в истории ваше сообщение без ответа — дописывает ответ)
  /user_num <N> <текст>    — заменить N-е с конца ваше сообщение (1 — последнее)
  /fake_num <N> <текст>    — заменить N-й с конца ответ бота (1 — последний)
  /send                    — отправить боту ваше неотправленное сообщение и получить ответ
  /clear                   — сбросить историю диалога
  /show                    — посмотреть сырой JSON всей истории
  /help                    — показать этот список
  exit                     — выйти (память очистится)"""


def last_role():
    return messages[-1]["role"]


def count_role(role):
    return sum(1 for m in messages if m["role"] == role)


def find_from_end(role, n):
    """Индекс n-го с конца сообщения роли role (n=1 — последнее). None, если такого нет."""
    count = 0
    for i in range(len(messages) - 1, 0, -1):
        if messages[i]["role"] == role:
            count += 1
            if count == n:
                return i
    return None


def ask_bot():
    """Отправляет всю историю, печатает ответ по мере генерации и сохраняет его в историю."""
    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            temperature=0.7,
            stream=True
        )
        print("\nDeepSeek: ", end="", flush=True)
        full_reply = ""
        for chunk in response:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content or ""
            print(delta, end="", flush=True)
            full_reply += delta
        print()
    except Exception as e:
        print(f"\n[Ошибка запроса]: {e}")
        return False

    messages.append({"role": "assistant", "content": full_reply})
    return True


def replace_numbered(role, arg, cmd):
    """Общая логика для /user_num и /fake_num."""
    parts = arg.split(maxsplit=1)
    if len(parts) < 2 or not parts[0].isdigit() or int(parts[0]) < 1:
        print(f"[Формат: {cmd} <номер с конца> <текст>, например: {cmd} 2 новый текст]")
        return
    n, text = int(parts[0]), parts[1]
    idx = find_from_end(role, n)
    who = "ваших сообщений" if role == "user" else "ответов бота"
    if idx is None:
        print(f"[Нет такого сообщения. Всего {who} в истории: {count_role(role)}]")
        return
    messages[idx]["content"] = text
    label = "Ваше сообщение №{} с конца заменено" if role == "user" else "Ответ бота №{} с конца заменён"
    print(f"[{label.format(n)}]")


def main():
    global messages

    print("Песочница запущена!")
    print(HELP + "\n")

    while True:
        try:
            user_input = input("\nВы: ").strip()
        except (KeyboardInterrupt, EOFError):
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            print("Сессия завершена, история стёрта из памяти.")
            break

        cmd, _, arg = user_input.partition(" ")
        arg = arg.strip()

        # Смена системного промпта
        if cmd == "/system":
            if not arg:
                print("[Формат: /system <текст>]")
                continue
            messages[0]["content"] = arg
            print(f"[Системный промпт обновлен]: {arg}")
            continue

        # Системный промпт из файла
        if cmd == "/system_file":
            try:
                # utf-8-sig — чтобы корректно читать файлы, сохранённые Блокнотом Windows
                with open(SYSTEM_PROMPT_FILE, encoding="utf-8-sig") as f:
                    file_prompt = f.read().strip()
            except FileNotFoundError:
                print(f"[Файл не найден: {SYSTEM_PROMPT_FILE}]")
                continue
            except UnicodeDecodeError:
                print("[Не удалось прочитать файл: сохраните его в кодировке UTF-8]")
                continue
            if not file_prompt:
                print("[Файл system_prompt.txt пустой, промпт не изменён]")
                continue
            messages[0]["content"] = file_prompt
            preview = file_prompt[:80].replace("\n", " ")
            more = "…" if len(file_prompt) > 80 else ""
            print(f"[Системный промпт загружен из файла, {len(file_prompt)} символов]: {preview}{more}")
            continue

        # Ваше сообщение без отправки
        if cmd == "/user":
            if not arg:
                print("[Формат: /user <текст>]")
                continue
            if last_role() == "user":
                messages[-1]["content"] = arg
                print("[Ваше неотправленное сообщение заменено]")
            else:
                messages.append({"role": "user", "content": arg})
                print("[Сообщение записано без отправки. Добавьте ответ бота через /fake "
                      "или получите настоящий через /send]")
            continue

        # Подмена ответа бота
        if cmd == "/fake":
            if not arg:
                print("[Формат: /fake <текст>]")
                continue
            if last_role() == "assistant":
                messages[-1]["content"] = arg
                print("[Последний ответ бота заменён]")
            else:
                messages.append({"role": "assistant", "content": arg})
                print("[Ответ бота добавлен в историю]")
            continue

        # Замена любого сообщения по номеру с конца
        if cmd == "/user_num":
            replace_numbered("user", arg, cmd)
            continue
        if cmd == "/fake_num":
            replace_numbered("assistant", arg, cmd)
            continue

        # Отправка неотправленного сообщения
        if cmd == "/send":
            if last_role() != "user":
                print("[Нечего отправлять: в истории нет вашего сообщения без ответа]")
                continue
            ask_bot()
            continue

        # Очистка контекста
        if cmd == "/clear":
            messages = [{"role": "system", "content": system_prompt}]
            print("[Контекст очищен]")
            continue

        # Просмотр массива запроса
        if cmd == "/show":
            print(json.dumps(messages, ensure_ascii=False, indent=2))
            continue

        if cmd == "/help":
            print(HELP)
            continue

        # Обычный запрос
        if last_role() == "user":
            print("[В истории висит ваше сообщение без ответа. Добавьте ответ через /fake, "
                  "отправьте его через /send или замените через /user]")
            continue

        messages.append({"role": "user", "content": user_input})
        if not ask_bot():
            # Запрос не удался — убираем сообщение, чтобы не сломать порядок
            messages.pop()


if __name__ == "__main__":
    main()
