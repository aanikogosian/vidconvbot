import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from telegram import Message, Update
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
MAX_OUTPUT_MB = float(os.getenv("MAX_OUTPUT_MB", "10"))
MAX_OUTPUT_BYTES = int(MAX_OUTPUT_MB * 1024 * 1024)
WORKDIR = Path(os.getenv("WORKDIR", "./tmp")).resolve()
STALE_TMP_MAX_AGE_HOURS = int(os.getenv("STALE_TMP_MAX_AGE_HOURS", "24"))
TELEGRAM_BASE_URL = os.getenv("TELEGRAM_BASE_URL", "").strip()
TELEGRAM_BASE_FILE_URL = os.getenv("TELEGRAM_BASE_FILE_URL", "").strip()

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
PERCENT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)%")

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("vidconvbot")


def _cleanup_stale_tempdirs(workdir: Path, max_age_hours: int) -> None:
    if not workdir.exists():
        return

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    for child in workdir.iterdir():
        if not child.is_dir():
            continue
        try:
            modified = datetime.fromtimestamp(child.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if modified < cutoff:
            shutil.rmtree(child, ignore_errors=True)


def _cleanup_files_in_dir(dir_path: Path) -> None:
    for child in dir_path.iterdir():
        if child.is_file():
            try:
                child.unlink()
            except OSError:
                logger.warning("Cannot remove temporary file: %s", child)


class ProgressReporter:
    def __init__(self, message: Message, min_interval_sec: float = 1.2):
        self.message = message
        self.min_interval_sec = min_interval_sec
        self._last_update_ts = 0.0
        self._last_text = ""
        self._fallback_reply_used = False

    async def update(self, text: str, force: bool = False) -> None:
        now = time.monotonic()
        if not force:
            if text == self._last_text:
                return
            if (now - self._last_update_ts) < self.min_interval_sec:
                return

        try:
            await self.message.edit_text(text)
            self._last_update_ts = now
            self._last_text = text
        except Exception as exc:  # noqa: BLE001
            logger.debug("Cannot update progress message via edit_text: %s", exc)
            # Fallback: in some chats/clients editing may fail or be invisible,
            # then send progress as normal bot messages.
            try:
                await self.message.reply_text(text)
                self._last_update_ts = now
                self._last_text = text
                self._fallback_reply_used = True
            except Exception as nested_exc:  # noqa: BLE001
                logger.debug("Cannot send fallback progress message: %s", nested_exc)


def _probe_duration_seconds(video_path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return max(float(result.stdout.strip()), 1.0)


def _extract_first_url(text: str | None) -> str | None:
    if not text:
        return None
    match = URL_RE.search(text)
    return match.group(0) if match else None


def _looks_like_url(text: str) -> bool:
    parsed = urlparse(text)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


async def _download_from_url_with_progress(url: str, dst_dir: Path, progress: ProgressReporter) -> Path:
    output_tpl = str(dst_dir / "source.%(ext)s")
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--newline",
        "--no-playlist",
        "-o",
        output_tpl,
        url,
    ]

    await progress.update("📥 Скачивание: 0%", force=True)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    last_percent = -1
    assert proc.stdout is not None
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        text = line.decode("utf-8", errors="ignore").strip()
        match = PERCENT_RE.search(text)
        if match:
            percent = int(min(float(match.group(1)), 100.0))
            if percent != last_percent:
                last_percent = percent
                await progress.update(f"📥 Скачивание: {percent}%")

    rc = await proc.wait()
    if rc != 0:
        raise RuntimeError("Не удалось скачать видео по ссылке (yt-dlp завершился с ошибкой)")

    downloaded = sorted(dst_dir.glob("source.*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not downloaded:
        raise RuntimeError("Не удалось скачать видео по ссылке")

    await progress.update("📥 Скачивание: 100%", force=True)
    return downloaded[0]


async def _run_ffmpeg_attempt_with_progress(
    input_path: Path,
    output_path: Path,
    video_bitrate: int,
    audio_bitrate: int,
    scale: str | None,
    duration_sec: float,
    progress: ProgressReporter,
    attempt_index: int,
    attempts_total: int,
) -> None:
    vf = []
    if scale:
        vf = ["-vf", f"scale={scale}"]

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        *vf,
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-b:v",
        str(video_bitrate),
        "-maxrate",
        str(int(video_bitrate * 1.2)),
        "-bufsize",
        str(video_bitrate),
        "-c:a",
        "aac",
        "-b:a",
        str(audio_bitrate),
        "-movflags",
        "+faststart",
        "-progress",
        "pipe:1",
        "-nostats",
        str(output_path),
    ]

    await progress.update(f"🗜️ Сжатие: 0% (попытка {attempt_index}/{attempts_total})", force=True)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )

    last_percent = -1
    assert proc.stdout is not None
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        text = line.decode("utf-8", errors="ignore").strip()
        if text.startswith("out_time_ms="):
            ms = text.split("=", 1)[1]
            if ms.isdigit():
                cur_sec = int(ms) / 1_000_000
                percent = int(min((cur_sec / duration_sec) * 100, 100))
                if percent != last_percent:
                    last_percent = percent
                    await progress.update(f"🗜️ Сжатие: {percent}% (попытка {attempt_index}/{attempts_total})")

    rc = await proc.wait()
    if rc != 0:
        raise subprocess.CalledProcessError(rc, cmd)


async def _compress_to_target_with_progress(
    input_path: Path,
    output_path: Path,
    target_bytes: int,
    progress: ProgressReporter,
) -> bool:
    duration = await asyncio.to_thread(_probe_duration_seconds, input_path)

    attempts = [
        {"audio": 96_000, "safety": 0.95, "scale": None},
        {"audio": 64_000, "safety": 0.90, "scale": None},
        {"audio": 48_000, "safety": 0.85, "scale": "1280:-2"},
        {"audio": 32_000, "safety": 0.80, "scale": "854:-2"},
    ]

    for idx, attempt in enumerate(attempts, start=1):
        total_bitrate = int((target_bytes * 8 / duration) * attempt["safety"])
        video_bitrate = max(total_bitrate - attempt["audio"], 150_000)

        try:
            await _run_ffmpeg_attempt_with_progress(
                input_path=input_path,
                output_path=output_path,
                video_bitrate=video_bitrate,
                audio_bitrate=attempt["audio"],
                scale=attempt["scale"],
                duration_sec=duration,
                progress=progress,
                attempt_index=idx,
                attempts_total=len(attempts),
            )
        except subprocess.CalledProcessError as exc:
            logger.warning("ffmpeg failed on attempt %s: %s", attempt, exc)
            continue

        if output_path.exists() and output_path.stat().st_size <= target_bytes:
            await progress.update("🗜️ Сжатие: 100%", force=True)
            return True

    return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    await update.message.reply_text(
        "Привет! Пришлите видео файлом/видео в Telegram или ссылку на видео. "
        "Я сожму его до размера < 10 МБ и верну как файл."
    )


async def _download_telegram_media(message: Message, dst_dir: Path, progress: ProgressReporter) -> Path | None:
    if message.video:
        await progress.update("📥 Скачивание файла из Telegram: 0%", force=True)
        ext = ".mp4"
        target = dst_dir / f"telegram_video{ext}"
        try:
            tg_file = await message.video.get_file()
        except BadRequest as exc:
            if "File is too big" in str(exc):
                raise RuntimeError(
                    "Telegram cloud Bot API не дает скачать такой большой файл. "
                    "Чтобы принимать большие видео из Telegram, нужно подключить локальный Telegram Bot API server "
                    "через TELEGRAM_BASE_URL и TELEGRAM_BASE_FILE_URL."
                ) from exc
            raise
        await tg_file.download_to_drive(custom_path=str(target))
        await progress.update("📥 Скачивание файла из Telegram: 100%", force=True)
        return target

    if message.document and message.document.mime_type and message.document.mime_type.startswith("video/"):
        await progress.update("📥 Скачивание файла из Telegram: 0%", force=True)
        suffix = Path(message.document.file_name or "video.mp4").suffix or ".mp4"
        target = dst_dir / f"telegram_document{suffix}"
        try:
            tg_file = await message.document.get_file()
        except BadRequest as exc:
            if "File is too big" in str(exc):
                raise RuntimeError(
                    "Telegram cloud Bot API не дает скачать такой большой файл. "
                    "Чтобы принимать большие видео из Telegram, нужно подключить локальный Telegram Bot API server "
                    "через TELEGRAM_BASE_URL и TELEGRAM_BASE_FILE_URL."
                ) from exc
            raise
        await tg_file.download_to_drive(custom_path=str(target))
        await progress.update("📥 Скачивание файла из Telegram: 100%", force=True)
        return target

    return None


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message:
        return

    status_message = await message.reply_text("⏳ Запрос принят. Готовлюсь к обработке...")
    progress = ProgressReporter(status_message)

    WORKDIR.mkdir(parents=True, exist_ok=True)
    _cleanup_stale_tempdirs(WORKDIR, STALE_TMP_MAX_AGE_HOURS)
    with tempfile.TemporaryDirectory(dir=WORKDIR) as tmp:
        tmp_dir = Path(tmp)

        try:
            source_path = await _download_telegram_media(message, tmp_dir, progress)

            if source_path is None:
                text = message.text or message.caption or ""
                url = _extract_first_url(text)
                if not url or not _looks_like_url(url):
                    await progress.update(
                        "❌ Не вижу видео или валидной ссылки. Пришлите ссылку (http/https) либо загрузите видео в чат.",
                        force=True,
                    )
                    return
                source_path = await _download_from_url_with_progress(url, tmp_dir, progress)

            await progress.update("🗜️ Начинаю сжатие...", force=True)
            compressed_path = tmp_dir / "compressed.mp4"
            ok = await _compress_to_target_with_progress(
                source_path,
                compressed_path,
                MAX_OUTPUT_BYTES,
                progress,
            )

            if not ok:
                await progress.update(
                    "❌ Не удалось сжать видео до требуемого размера (<10 МБ). Попробуйте более короткое видео.",
                    force=True,
                )
                return

            final_size_mb = compressed_path.stat().st_size / (1024 * 1024)
            with compressed_path.open("rb") as f:
                await message.reply_document(
                    document=f,
                    filename="compressed.mp4",
                    caption=f"Готово ✅ Размер: {final_size_mb:.2f} МБ",
                )

            # Extra cleanup right after successful send.
            _cleanup_files_in_dir(tmp_dir)
            await progress.update("✅ Готово! Видео отправлено файлом.", force=True)

        except RuntimeError as exc:
            logger.exception("Runtime error: %s", exc)
            await progress.update(f"❌ {exc}", force=True)
        except subprocess.CalledProcessError as exc:
            logger.exception("Subprocess error: %s", exc)
            await progress.update(
                "❌ Ошибка при скачивании или конвертации видео. Проверьте ссылку и попробуйте еще раз.",
                force=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unhandled error: %s", exc)
            await progress.update("❌ Произошла непредвиденная ошибка. Попробуйте позже.", force=True)
        finally:
            _cleanup_files_in_dir(tmp_dir)


def _ensure_deps() -> None:
    for dep in ["ffmpeg", "ffprobe"]:
        if shutil.which(dep) is None:
            raise RuntimeError(f"Требуется установленная утилита: {dep}")


def main() -> None:
    if not TOKEN:
        raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN в переменных окружения")
    _ensure_deps()

    builder = Application.builder().token(TOKEN)
    if TELEGRAM_BASE_URL:
        builder = builder.base_url(TELEGRAM_BASE_URL)
    if TELEGRAM_BASE_FILE_URL:
        builder = builder.base_file_url(TELEGRAM_BASE_FILE_URL)

    app = builder.build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT | filters.VIDEO | filters.Document.ALL, handle_message))

    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
