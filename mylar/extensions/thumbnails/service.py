"""
Issue Thumbnail Generation & Caching Service.

Extracts cover images from CBZ/CBR archives, safely resizes them,
and atomically writes WebP thumbnails to the cache directory.
"""

from io import BytesIO
import os
import time
import zipfile

from PIL import Image

try:
    from unrar.cffi import rarfile
except ImportError:
    try:
        from lib.rarfile import rarfile
    except ImportError:
        from rarfile import rarfile

import mylar
from mylar import logger


def _ensure_rar_tool():
    """Ensure rarfile has a working unrar binary on Windows/Linux."""
    if hasattr(rarfile, "UNRAR_TOOL"):
        current_tool = getattr(rarfile, "UNRAR_TOOL", None)
        if not current_tool or not os.path.isfile(str(current_tool)):
            unrar_cmd = getattr(getattr(mylar, "CONFIG", None), "UNRAR_CMD", None)
            if unrar_cmd and os.path.isfile(unrar_cmd):
                rarfile.UNRAR_TOOL = unrar_cmd
            elif os.name == "nt":
                for path in (
                    r"C:\Program Files\WinRAR\UnRAR.exe",
                    r"C:\Program Files\WinRAR\Rar.exe",
                    r"C:\Program Files (x86)\WinRAR\UnRAR.exe",
                    r"C:\Program Files (x86)\WinRAR\Rar.exe",
                ):
                    if os.path.isfile(path):
                        rarfile.UNRAR_TOOL = path
                        break


def is_image_file(filename):
    """Check if the filename has an image file extension."""
    return os.path.splitext(filename)[1][1:].lower() in {"jpg", "jpeg", "png", "webp"}


def get_or_create_issue_thumbnail(comicid, issueid, file_path, target_width=300):
    """
    Generates and caches a bounded ~300px WebP thumbnail for an issue from its local archive.
    - CBZ: extracts first image entry via zipfile.
    - CBR: attempts rarfile decompression if unrar is available; falls back cleanly if not.
    - Writes atomically to cache/thumbnails/<comicid>/<issueid>.webp.
    - Never modifies source archives or network shares.
    - Returns cached thumbnail filepath on success, None on fallback.
    """
    if not comicid or not issueid or not file_path:
        return None

    cache_dir = getattr(mylar.CONFIG, "CACHE_DIR", None)
    if not cache_dir:
        return None

    thumb_dir = os.path.join(cache_dir, "thumbnails", str(comicid))
    thumb_path = os.path.join(thumb_dir, f"{issueid}.webp")

    # Fast return if cached thumbnail exists
    if os.path.isfile(thumb_path) and os.path.getsize(thumb_path) > 0:
        return thumb_path

    if not os.path.isfile(file_path):
        return None

    raw_bytes = None
    lower_path = file_path.lower()

    if lower_path.endswith(".cbz"):
        try:
            with zipfile.ZipFile(file_path, "r") as z:
                img_names = [n for n in z.namelist() if is_image_file(n)]
                if not img_names:
                    logger.fdebug(f"[THUMBNAIL] No image entries found in CBZ: {file_path}")
                    return None
                img_names.sort()
                raw_bytes = z.read(img_names[0])
        except Exception as e:
            logger.warn(f"[THUMBNAIL] Error reading CBZ archive '{os.path.basename(file_path)}': {e}")
            return None
    elif lower_path.endswith(".cbr"):
        try:
            _ensure_rar_tool()
            if not rarfile.is_rarfile(file_path):
                return None
            with rarfile.RarFile(file_path) as rf:
                img_names = [n for n in rf.namelist() if is_image_file(n)]
                if not img_names:
                    logger.fdebug(f"[THUMBNAIL] No image entries found in CBR: {file_path}")
                    return None
                img_names.sort()
                raw_bytes = rf.read(img_names[0])
        except Exception as e:
            logger.fdebug(f"[THUMBNAIL] CBR extraction unsupported/failed for '{os.path.basename(file_path)}': {e}")
            return None
    else:
        return None

    if not raw_bytes:
        return None

    temp_file = None
    try:
        img = Image.open(BytesIO(raw_bytes))
        # Decompression bomb safety limit (100 megapixels)
        if img.size[0] * img.size[1] > 100_000_000:
            logger.warn(f"[THUMBNAIL] Image in '{os.path.basename(file_path)}' exceeds safe pixel limit ({img.size})")
            return None

        scale = target_width / float(img.size[0])
        target_height = max(1, int(scale * img.size[1]))

        if img.mode in ("RGBA", "P", "LA", "1"):
            img = img.convert("RGB")

        resized = img.resize((target_width, target_height), Image.LANCZOS)

        os.makedirs(thumb_dir, exist_ok=True)
        temp_file = os.path.join(thumb_dir, f".tmp_{issueid}_{os.getpid()}_{time.time_ns()}.webp")
        with open(temp_file, "wb") as out_f:
            resized.save(out_f, format="WEBP", quality=80)

        # Atomic write replace
        os.replace(temp_file, thumb_path)
        return thumb_path
    except Exception as e:
        logger.warn(f"[THUMBNAIL] Failed to scale/cache thumbnail for issue {issueid}: {e}")
        if temp_file and os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except Exception:
                pass
        return None
