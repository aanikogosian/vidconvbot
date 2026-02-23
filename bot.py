import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from telegram import Message, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
MAX_OUTPUT_MB = float(os.getenv("MAX_OUTPUT_MB", "10"))
MAX_OUTPUT_BYTES = int(MAX_OUTPUT_MB * 1024 * 1024)
WORKDIR = Path(os.getenv("WORKDIR", "./tmp")).resolve()

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("vidconvbot")


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


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


def _compress_to_target(input_path: Path, output_path: Path, target_bytes: int) -> bool:
    duration = _probe_duration_seconds(input_path)

    attempts = [
        {"audio": 96_000, "safety": 0.95, "scale": None},
        {"audio": 64_000, "safety": 0.90, "scale": None},
        {"audio": 48_000, "safety": 0.85, "scale": "1280:-2"},
        {"audio": 32_000, "safety": 0.80, "scale": "854:-2"},
    ]

    for attempt in attempts:
        total_bitrate = int((target_bytes * 8 / duration) * attempt["safety"])
        video_bitrate = max(total_bitrate - attempt["audio"], 150_000)

        vf = []
        if attempt["scale"]:
            vf = ["-vf", f"scale={attempt['scale']}"]

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
            str(attempt["audio"]),
            "-movflags",
            "+faststart",
            str(output_path),
        ]

        try:
            _run(cmd)
        except subprocess.CalledProcessError as exc:
            logger.warning("ffmpeg failed on attempt %s: %s", attempt, exc)
            continue

        if output_path.exists() and output_path.stat().st_size <= target_bytes:
            return True

    return False


def _extract_first_url(text: str | None) -> str | None:
    if not text:
        return None
    match = URL_RE.search(text)
    return match.group(0) if match else None


def _looks_like_url(text: str) -> bool:
    parsed = urlparse(text)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _download_from_url(url: str, dst_dir: Path) -> Path:
    output_tpl = str(dst_dir / "source.%(ext)s")
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "-o",
        output_tpl,
        url,
    ]
    _run(cmd)

    downloaded = sorted(dst_dir.glob("source.*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not downloaded:
        raise RuntimeError("Не удалось скачать видео по ссылке")
    return downloaded[0]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Пришлите видео файлом/видео в Telegram или ссылку на видео. "
        "Я сожму его до размера < 10 МБ и верну как файл."
    )


async def _download_telegram_media(message: Message, dst_dir: Path) -> Path | None:
    if message.video:
        ext = ".mp4"
        target = dst_dir / f"telegram_video{ext}"
        tg_file = await message.video.get_file()
        await tg_file.download_to_drive(custom_path=str(target))
        return target

    if message.document and message.document.mime_type and message.document.mime_type.startswith("video/"):
        suffix = Path(message.document.file_name or "video.mp4").suffix or ".mp4"
        target = dst_dir / f"telegram_document{suffix}"
        tg_file = await message.document.get_file()
        await tg_file.download_to_drive(custom_path=str(target))
        return target

    return None


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message:
        return

    await message.reply_text("Принял. Обрабатываю видео, это может занять немного времени...")

    WORKDIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=WORKDIR) as tmp:
        tmp_dir = Path(tmp)
        source_path: Path | None = None

        try:
            source_path = await _download_telegram_media(message, tmp_dir)

            if source_path is None:
                text = message.text or message.caption or ""
                url = _extract_first_url(text)
                if not url or not _looks_like_url(url):
                    await message.reply_text(
                        "Не вижу видео или валидной ссылки. "
                        "Пришлите ссылку (http/https) либо загрузите видео в чат."
                    )
                    return
                source_path = await asyncio.to_thread(_download_from_url, url, tmp_dir)

            compressed_path = tmp_dir / "compressed.mp4"
            ok = await asyncio.to_thread(
                _compress_to_target,
                source_path,
                compressed_path,
                MAX_OUTPUT_BYTES,
            )

            if not ok:
                await message.reply_text(
                    "Не удалось сжать видео до требуемого размера. "
                    "Попробуйте более короткое видео или файл меньшего исходного размера."
                )
                return

            final_size_mb = compressed_path.stat().st_size / (1024 * 1024)
            with compressed_path.open("rb") as f:
                await message.reply_document(
                    document=f,
                    filename="compressed.mp4",
                    caption=f"Готово ✅ Размер: {final_size_mb:.2f} МБ",
                )

        except subprocess.CalledProcessError as exc:
            logger.exception("Subprocess error: %s", exc)
            await message.reply_text(
                "Ошибка при скачивании или конвертации видео. "
                "Проверьте ссылку и попробуйте еще раз."
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unhandled error: %s", exc)
            await message.reply_text("Произошла непредвиденная ошибка. Попробуйте позже.")
        finally:
            for path in tmp_dir.iterdir():
                if path.exists() and path.is_file():
                    try:
                        path.unlink()
                    except OSError:
                        logger.warning("Cannot remove temporary file: %s", path)


def _ensure_deps() -> None:
    for dep in ["ffmpeg", "ffprobe", "yt-dlp"]:
        if shutil.which(dep) is None:
            raise RuntimeError(f"Требуется установленная утилита: {dep}")


def main() -> None:
    if not TOKEN:
        raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN в переменных окружения")
    _ensure_deps()

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT | filters.VIDEO | filters.Document.ALL, handle_message))

    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
