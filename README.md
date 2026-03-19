# Telegram Video Compressor Bot

Бот делает ровно 2 действия:
1. Принимает **ссылку на видео** (Google Drive, Яндекс.Диск, файлообменник и т.д., если `yt-dlp` умеет скачать) **или видео, отправленное прямо в Telegram**.
2. Сжимает видео до размера **меньше `10 МБ`** и отправляет обратно **как файл (document)**.

## Документация Telegram, на которую опирается реализация

- Telegram Bot API (официально): https://core.telegram.org/bots/api
- Python Telegram Bot (официальная библиотека): https://docs.python-telegram-bot.org/

В коде используется `sendDocument` через `python-telegram-bot` (`reply_document`), чтобы бот отправлял результат именно **файлом**, а не видео.

## Требования

- Python 3.11+
- `ffmpeg` и `ffprobe`
- Python-пакет `yt-dlp` (ставится из `requirements.txt`)

## Быстрый старт

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# заполните TELEGRAM_BOT_TOKEN
python bot.py
```

## Настройки

Изменяются через переменные окружения (можно в `.env`):

- `TELEGRAM_BOT_TOKEN` — токен бота от BotFather (**легко сменить в любой момент**).
- `MAX_OUTPUT_MB` — максимальный размер итогового файла (по умолчанию `10`).
- `WORKDIR` — временная директория для загрузок и конвертации.
- `STALE_TMP_MAX_AGE_HOURS` — удаление старых временных папок в `WORKDIR` старше N часов (по умолчанию `24`).
- `TELEGRAM_BASE_URL` — необязательный кастомный endpoint Telegram Bot API, например для локального Bot API server.
- `TELEGRAM_BASE_FILE_URL` — необязательный file endpoint для локального Bot API server.

## Как это работает

- Если пользователь присылает видео в Telegram:
  - бот скачивает файл через Telegram API.
- Если пользователь присылает ссылку:
  - бот скачивает исходник через `yt-dlp`.
- Далее бот подбирает bitrate (при необходимости снижает аудио/разрешение), чтобы уложиться в лимит `<10 МБ`.
- Во время обработки бот обновляет статус с прогрессом: скачивание (в процентах для ссылок) и сжатие (в процентах по времени кодирования).
- Если редактирование статус-сообщения недоступно/не отображается в клиенте, бот автоматически отправляет прогресс отдельными сообщениями.
- Ответ отправляется как `document` (`compressed.mp4`).
- Временные файлы запроса удаляются сразу после отправки результата (и дополнительно в `finally`), чтобы не хламить сервер.

## Важные ограничения

- Поддержка конкретных файлообменников зависит от возможностей `yt-dlp` и доступности ссылки без дополнительной авторизации.
- Очень длинные/тяжёлые ролики могут не ужаться до 10 МБ с приемлемым качеством — в таком случае бот возвращает сообщение об ошибке.

## Ограничение Telegram на входящие файлы от пользователя

Если бот работает через **обычный облачный Telegram Bot API**, у метода `getFile` есть лимит на размер файла.  
Из-за этого ошибка вида `telegram.error.BadRequest: File is too big` может возникать **ещё до сжатия**, прямо на этапе скачивания файла из Telegram.

Чтобы бот принимал большие файлы, нужно запускать **локальный Telegram Bot API server** и направить бота на него через:

```env
TELEGRAM_BASE_URL=http://127.0.0.1:8081/bot
TELEGRAM_BASE_FILE_URL=http://127.0.0.1:8081/file/bot
```

В коде это уже поддержано: если переменные заданы, бот использует их при создании `Application`.

### Что это даёт

- снимается лимит `getFile` локального Bot API server для скачивания файлов ботом;
- отправка файлов ботом через локальный сервер доступна до **2000 МБ**;
- входящий файл в 33 МБ перестаёт быть проблемой именно на стороне Telegram Bot API.

### Что сделать на сервере

1. Поднять локальный Telegram Bot API server.
2. Прописать в `.env`:

```env
TELEGRAM_BASE_URL=http://127.0.0.1:8081/bot
TELEGRAM_BASE_FILE_URL=http://127.0.0.1:8081/file/bot
```

3. Перезапустить бота:

```bash
systemctl restart vidconvbot
```

## Если сервис падает с ошибкой про `yt-dlp` в systemd

Если при ручном запуске всё работает, а в `systemd` падает с `RuntimeError: Требуется установленная утилита: yt-dlp`,
значит проблема в окружении `PATH` для сервиса.

В этой версии бот запускает `yt-dlp` как Python-модуль (`python -m yt_dlp`), поэтому достаточно, чтобы пакет был установлен
в то же виртуальное окружение, что и бот:

```bash
cd /workspace/vidconvbot
source .venv/bin/activate
pip install -r requirements.txt
systemctl restart vidconvbot
```


## Диагностика: если в логах всё ещё старая ошибка про `yt-dlp`

Если в `journalctl` продолжает появляться старый текст ошибки `Требуется установленная утилита: yt-dlp`,
обычно это означает, что сервис запущен не из актуального кода или не тем Python.

Проверьте пошагово:

```bash
cd /workspace/vidconvbot
git log --oneline -n 3
python -m py_compile bot.py
systemctl cat vidconvbot
```

В `ExecStart` должно быть именно виртуальное окружение проекта, например:

```ini
ExecStart=/workspace/vidconvbot/.venv/bin/python /workspace/vidconvbot/bot.py
```

После изменения unit-файла обязательно:

```bash
systemctl daemon-reload
systemctl restart vidconvbot
```

Проверка, каким Python реально запущен процесс сервиса:

```bash
PID=$(systemctl show -p MainPID --value vidconvbot)
readlink -f /proc/$PID/exe
```
