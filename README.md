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

## Как это работает

- Если пользователь присылает видео в Telegram:
  - бот скачивает файл через Telegram API.
- Если пользователь присылает ссылку:
  - бот скачивает исходник через `yt-dlp`.
- Далее бот подбирает bitrate (при необходимости снижает аудио/разрешение), чтобы уложиться в лимит `<10 МБ`.
- Ответ отправляется как `document` (`compressed.mp4`).

## Важные ограничения

- Поддержка конкретных файлообменников зависит от возможностей `yt-dlp` и доступности ссылки без дополнительной авторизации.
- Очень длинные/тяжёлые ролики могут не ужаться до 10 МБ с приемлемым качеством — в таком случае бот возвращает сообщение об ошибке.

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
