"""
Image to PDF Converter
=====================
Ứng dụng GUI đơn giản (tkinter) gộp nhiều ảnh thành 1 file PDF.

- Pillow (PIL) : đọc, xoay EXIF, xử lý ảnh có nền trong suốt.
- ReportLab    : dựng file PDF (mỗi ảnh = 1 trang, căn giữa, không méo).

Toàn bộ quá trình chạy offline trên máy người dùng.
"""

import hashlib
import heapq
import io
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import zipfile
from decimal import Decimal, InvalidOperation
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageOps
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as pdf_canvas

# License system
try:
    from license_system import check_license, activate_license, get_license_info
    LICENSE_SYSTEM_AVAILABLE = True
except ImportError:
    LICENSE_SYSTEM_AVAILABLE = False
    def check_license(): return True, "License system not available"
    def activate_license(key): return False, "License system not available"
    def get_license_info(): return None

# ---------------------------------------------------------------------------
# Hằng số
# ---------------------------------------------------------------------------

APP_TITLE = "Toolbox"
SUPPORTED_EXTENSIONS = (
    ("Ảnh được hỗ trợ", "*.jpg *.jpeg *.png *.webp *.bmp"),
    ("JPEG", "*.jpg *.jpeg"),
    ("PNG", "*.png"),
    ("WebP", "*.webp"),
    ("Bitmap", "*.bmp"),
    ("Tất cả tệp", "*.*"),
)

# Kéo thả là tính năng phụ: nếu cài tkinterdnd2 thì dùng được,
# nếu không có thì chương trình vẫn chạy bình thường.
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    DND_AVAILABLE = True
except ImportError:
    DND_AVAILABLE = False

try:
    import yt_dlp

    YTDLP_AVAILABLE = True
except ImportError:
    YTDLP_AVAILABLE = False


# ---------------------------------------------------------------------------
# Xử lý ảnh & tạo PDF
# ---------------------------------------------------------------------------


def load_image_as_rgb(path):
    """Mở ảnh, xoay theo EXIF, nền trong suốt -> nền trắng. Trả về ảnh RGB."""
    with Image.open(path) as raw:
        # Xoay đúng chiều theo thông tin EXIF của ảnh chụp từ điện thoại.
        img = ImageOps.exif_transpose(raw)
        # Chuyển sang RGBA để lấy được kênh alpha (nếu có).
        if img.mode != "RGBA":
            img = img.convert("RGBA")

        # Trải alpha lên nền trắng -> tránh lỗi khi nhúng vào PDF.
        flat = Image.new("RGB", img.size, (255, 255, 255))
        flat.paste(img, mask=img.getchannel("A"))
        return flat


def build_pdf(image_paths, output_path):
    """
    Tạo PDF từ danh sách ảnh. Mỗi ảnh = 1 trang, giữ đúng thứ tự.

    Trả về (số_trang, danh_sách_ảnh_lỗi).
    """
    if not image_paths:
        raise ValueError("Không có ảnh nào để chuyển đổi.")

    # ReportLab có thể ghi file trực tiếp, nhưng dựng trong bộ nhớ trước rồi
    # mới ghi ra đĩa giúp tên file/thư mục có tiếng Việt không bị lỗi.
    buffer = io.BytesIO()
    pdf = pdf_canvas.Canvas(buffer, pagesize=A4)

    failed = []
    page_count = 0
    margin = 15 * mm

    for path in image_paths:
        try:
            image = load_image_as_rgb(path)
        except Exception as exc:  # ảnh hỏng / không đọc được -> bỏ qua, báo lỗi
            failed.append((os.path.basename(path), str(exc)))
            continue

        img_w, img_h = image.size
        # Trang dọc cho ảnh dọc, trang ngang cho ảnh ngang.
        page_size = A4 if img_h >= img_w else landscape(A4)
        page_w, page_h = page_size

        # Scale đúng tỉ lệ gốc để ảnh luôn vừa trang, không bị kéo méo.
        max_w = page_w - 2 * margin
        max_h = page_h - 2 * margin
        scale = min(max_w / img_w, max_h / img_h)
        draw_w = img_w * scale
        draw_h = img_h * scale

        # Căn giữa ảnh trên trang.
        x = (page_w - draw_w) / 2
        y = (page_h - draw_h) / 2

        # Nén JPEG chất lượng cao để giữ chất lượng mà file không quá nặng.
        image_bytes = io.BytesIO()
        image.save(image_bytes, "JPEG", quality=95, optimize=True)
        image_bytes.seek(0)

        pdf.setPageSize(page_size)
        pdf.drawImage(ImageReader(image_bytes), x, y, width=draw_w, height=draw_h)
        pdf.showPage()
        page_count += 1

    if page_count == 0:
        pdf.save()
        raise ValueError("Không tạo được trang nào. Tất cả ảnh đều bị lỗi.")

    pdf.save()

    with open(output_path, "wb") as fh:
        fh.write(buffer.getvalue())

    return page_count, failed


# ---------------------------------------------------------------------------
# Giao diện
# ---------------------------------------------------------------------------


class App:
    def __init__(self, root, parent):
        self.root = root
        self.parent = parent
        self.files = []  # danh sách đường dẫn ảnh theo đúng thứ tự hiển thị

        self._build_ui()
        self._update_status()
        if DND_AVAILABLE:
            self.parent.drop_target_register(DND_FILES)
            self.parent.dnd_bind("<<Drop>>", self._on_drop)

    # ----- dựng giao diện ---------------------------------------------------

    def _build_ui(self):
        # Tiêu đề
        header = ttk.Frame(self.parent, padding=(0, 0, 0, 6))
        header.pack(fill="x")
        ttk.Label(header, text=APP_TITLE, font=("Segoe UI", 16, "bold")).pack(side="left")

        # Khu vực danh sách ảnh
        list_frame = ttk.Frame(self.parent, padding=0)
        list_frame.pack(fill="both", expand=True)

        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)

        self.listbox = tk.Listbox(
            list_frame,
            selectmode=tk.EXTENDED,
            activestyle="none",
            font=("Segoe UI", 10),
            borderwidth=1,
            relief="solid",
        )
        self.listbox.grid(row=0, column=0, sticky="nsew")

        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.listbox.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.listbox.config(yscrollcommand=scrollbar.set)

        # Các nút
        button_frame = ttk.Frame(self.parent, padding=(0, 8, 0, 0))
        button_frame.pack(fill="x")

        self.btn_choose = ttk.Button(button_frame, text="Chọn ảnh", command=self.choose_images)
        self.btn_add = ttk.Button(button_frame, text="Thêm ảnh", command=self.add_images)
        self.btn_remove = ttk.Button(button_frame, text="Xóa ảnh", command=self.remove_selected)
        self.btn_up = ttk.Button(button_frame, text="Lên", command=self.move_up)
        self.btn_down = ttk.Button(button_frame, text="Xuống", command=self.move_down)
        self.btn_clear = ttk.Button(button_frame, text="Xóa tất cả", command=self.clear_all)
        self.btn_convert = ttk.Button(button_frame, text="Chuyển thành PDF", command=self.convert_to_pdf)

        for btn in (
            self.btn_choose,
            self.btn_add,
            self.btn_remove,
            self.btn_up,
            self.btn_down,
            self.btn_clear,
        ):
            btn.pack(side="left", padx=3)

        self.btn_convert.pack(side="right", padx=3)

        # Thanh trạng thái
        status_frame = ttk.Frame(self.parent, padding=(12, 4, 12, 8))
        status_frame.pack(fill="x")
        self.status_label = ttk.Label(status_frame, text="", anchor="w")
        self.status_label.pack(side="left")

        # Gợi ý kéo thả (nếu hỗ trợ)
        if DND_AVAILABLE:
            ttk.Label(
                status_frame, text="(có thể kéo thả ảnh vào cửa sổ)", anchor="e"
            ).pack(side="right")

    def _bind_events(self):
        # Chỉ phản ứng khi tab Image -> PDF đang hiển thị.
        self.root.bind("<Delete>", lambda _e: self.parent.winfo_ismapped() and self.remove_selected())
        self.root.bind("<Control-o>", lambda _e: self.parent.winfo_ismapped() and self.choose_images())

        if DND_AVAILABLE:
            self.listbox.drop_target_register(DND_FILES)
            self.listbox.dnd_bind("<<Drop>>", self._on_drop)

    # ----- tiện ích ---------------------------------------------------------

    def _update_status(self):
        count = len(self.files)
        self.status_label.config(text=f"Đã chọn {count} ảnh")

    def _refresh_list(self):
        self.listbox.delete(0, tk.END)
        for index, path in enumerate(self.files, start=1):
            self.listbox.insert(tk.END, f"{index}. {os.path.basename(path)}")
        self._update_status()

    def _add_paths(self, paths):
        """Thêm các đường dẫn hợp lệ vào danh sách (bỏ qua trùng lặp)."""
        added = 0
        for path in paths:
            path = os.path.abspath(path)
            ext = os.path.splitext(path)[1].lower()
            if ext not in (".jpg", ".jpeg", ".png", ".webp", ".bmp"):
                continue
            if path in self.files:
                continue
            self.files.append(path)
            added += 1
        if added:
            self._refresh_list()

    # ----- các hành động ----------------------------------------------------

    def choose_images(self):
        """Chọn ảnh: danh sách bị thay thế hoàn toàn."""
        paths = filedialog.askopenfilenames(
            title="Chọn ảnh",
            filetypes=SUPPORTED_EXTENSIONS,
        )
        if not paths:
            return
        self.files = []
        self._add_paths(paths)

    def add_images(self):
        """Thêm ảnh: giữ nguyên danh sách cũ và nối thêm vào cuối."""
        paths = filedialog.askopenfilenames(
            title="Thêm ảnh",
            filetypes=SUPPORTED_EXTENSIONS,
        )
        if paths:
            self._add_paths(paths)

    def remove_selected(self):
        """Xóa các dòng đang được chọn."""
        selected = list(self.listbox.curselection())
        if not selected:
            messagebox.showinfo("Thông báo", "Hãy chọn ảnh cần xóa trước.")
            return
        # Xóa từ cuối về đầu để chỉ số không bị lệch.
        for index in reversed(selected):
            del self.files[index]
        self._refresh_list()

    def clear_all(self):
        """Xóa toàn bộ danh sách."""
        if not self.files:
            return
        if messagebox.askyesno("Xác nhận", "Xóa toàn bộ danh sách ảnh?"):
            self.files = []
            self._refresh_list()

    def _move(self, offset):
        """Di chuyển ảnh đang chọn lên (offset=-1) hoặc xuống (offset=+1)."""
        selected = list(self.listbox.curselection())
        if not selected:
            messagebox.showinfo("Thông báo", "Hãy chọn ảnh cần di chuyển trước.")
            return

        # Tính theo thứ tự để giữ nguyên thứ tự tương đối khi di chuyển nhiều mục.
        if offset < 0:
            selected = list(reversed(selected))
        new_selection = []
        for index in selected:
            target = index + offset
            if 0 <= target < len(self.files):
                self.files[index], self.files[target] = self.files[target], self.files[index]
                new_selection.append(target)
        self._refresh_list()
        for index in new_selection:
            self.listbox.selection_set(index)
        self.listbox.activate(new_selection[0] if new_selection else 0)

    def move_up(self):
        self._move(-1)

    def move_down(self):
        self._move(+1)

    def _on_drop(self, event):
        """Xử lý kéo thả file (chỉ hoạt động khi có tkinterdnd2)."""
        try:
            paths = self.root.tk.splitlist(event.data)
        except Exception:
            paths = []
        if paths:
            self._add_paths(paths)

    def convert_to_pdf(self):
        """Hộp thoại chọn nơi lưu, rồi tạo PDF."""
        if not self.files:
            messagebox.showwarning("Chưa có ảnh", "Hãy chọn ít nhất một ảnh trước khi chuyển đổi.")
            return

        default_name = "output.pdf"
        if len(self.files) == 1:
            default_name = os.path.splitext(os.path.basename(self.files[0]))[0] + ".pdf"

        output_path = filedialog.asksaveasfilename(
            title="Lưu file PDF",
            defaultextension=".pdf",
            initialfile=default_name,
            filetypes=[("PDF", "*.pdf"), ("Tất cả tệp", "*.*")],
        )
        if not output_path:
            return

        self.root.config(cursor="watch")
        self.root.update_idletasks()
        try:
            page_count, failed = build_pdf(self.files, output_path)
        except Exception as exc:
            messagebox.showerror("Lỗi", f"Không tạo được file PDF:\n{exc}")
            return
        finally:
            self.root.config(cursor="")
            self.root.update_idletasks()

        # Ảnh nào lỗi thì báo tên, không làm chương trình crash.
        if failed:
            names = "\n".join(f"• {name}" for name, _ in failed)
            messagebox.showwarning(
                "Một số ảnh bị lỗi",
                f"Chuyển đổi thành công với {page_count} trang.\n\n"
                f"Các ảnh sau bị lỗi nên đã bỏ qua:\n{names}",
            )

        messagebox.showinfo(
            "Thành công",
            f"Chuyển đổi thành công!\n\n"
            f"Số trang: {page_count}\n"
            f"Đường dẫn: {output_path}",
        )


# ---------------------------------------------------------------------------
# Media Converter: logic (FFmpeg qua subprocess)
# ---------------------------------------------------------------------------

MEDIA_TARGETS = {  # đuôi file nguồn -> các định dạng output cho phép
    ".mp4": ("MP3", "WAV"),
    ".wav": ("MP3", "MP4"),
    ".mp3": ("WAV",),
}
MEDIA_FILETYPES = [("Media (mp4, wav, mp3)", "*.mp4 *.wav *.mp3"), ("Tất cả tệp", "*.*")]


def find_ffmpeg():
    """Tìm ffmpeg trong PATH, rồi tới cạnh file .exe/main.py. Trả về None nếu không có."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    here = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
    local = os.path.join(here, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    return local if os.path.isfile(local) else None


def build_ffmpeg_cmd(ffmpeg, src, dst, target, overwrite):
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y" if overwrite else "-n"]
    if target == "MP4":  # WAV -> MP4: nền đen 640x360, H.264 + AAC
        cmd += ["-f", "lavfi", "-i", "color=c=black:s=640x360:r=10", "-i", src,
                "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-shortest"]
    elif target == "MP3":
        cmd += ["-i", src, "-vn", "-codec:a", "libmp3lame", "-q:a", "2"]
    else:  # WAV
        cmd += ["-i", src, "-vn", "-acodec", "pcm_s16le"]
    return cmd + ["-progress", "pipe:1", "-nostats", dst]


def _hms(text):
    h, m, s = text.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def run_ffmpeg(cmd, on_progress):
    """Chạy FFmpeg, gọi on_progress(0..1 hoặc None). Ném RuntimeError kèm vài dòng cuối nếu lỗi."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # không bật cửa sổ console khi chạy từ .exe
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", creationflags=flags)
    duration, tail = None, []
    for line in proc.stdout:
        line = line.strip()
        tail = (tail + [line])[-8:]
        if duration is None and "Duration:" in line:
            m = re.search(r"Duration:\s*(\d+:\d+:\d+(?:\.\d+)?)", line)
            if m:
                duration = _hms(m.group(1)) or None
        elif line.startswith("out_time=") and duration:
            try:
                on_progress(min(_hms(line[9:]) / duration, 1.0))
            except ValueError:
                pass  # "N/A" đầu quá trình
    if proc.wait() != 0:
        raise RuntimeError("FFmpeg lỗi (mã %d):\n%s" % (proc.returncode, "\n".join(tail)))


# ---------------------------------------------------------------------------
# Code Tools: logic thuần (không đụng tới giao diện)
# ---------------------------------------------------------------------------

BIT_WIDTHS = ("8 bit", "16 bit", "32 bit")


def parse_number(text):
    """Nhận 0x.. / 0b.. / 0o.. / thập phân. Trả về int >= 0, sai thì ValueError."""
    s = text.strip().replace("_", "")
    if not s:
        raise ValueError("Chưa nhập số.")
    low = s.lower()
    try:
        if low.startswith("0x"):
            return int(low[2:], 16) if low[2:] else _bad(text)
        if low.startswith("0b"):
            return int(low[2:], 2) if low[2:] else _bad(text)
        if low.startswith("0o"):
            return int(low[2:], 8) if low[2:] else _bad(text)
        if not low.isdigit():
            _bad(text)
        return int(low, 10)
    except ValueError:
        _bad(text)


def _bad(text):
    raise ValueError(f"Số không hợp lệ: '{text.strip()}' (dùng 0x.., 0b.., 0o.. hoặc số thập phân không âm).")


def natural_width(value):
    for w in (8, 16, 32, 64):
        if value < (1 << w):
            return w
    return (value.bit_length() + 7) // 8 * 8


def fmt_hex(v, w):
    return "0x" + format(v, "X").zfill(w // 4)


def fmt_bin(v, w):
    return "0b" + format(v, "b").zfill(w)


def group_bits(v, w):
    s = format(v, "b").zfill(w)
    return " ".join(s[i:i + 8] for i in range(0, w, 8))


def bit_ruler(v, w):
    """Mỗi byte hai dòng: số thứ tự bit và giá trị bit."""
    s = format(v, "b").zfill(w)
    lines = []
    for b in range(w // 8):
        hi = w - 1 - b * 8
        pos = " ".join(f"{hi - i:2d}" for i in range(8))
        val = " ".join(f" {c}" for c in s[b * 8:b * 8 + 8])
        lines.append(f"bit {pos}\n    {val}")
    return "\n\n".join(lines)


def check_fits(value, w, name="Giá trị"):
    if value >= (1 << w):
        raise ValueError(f"{name} vượt quá {w} bit (tối đa 0x{(1 << w) - 1:X}).")


def bit_operation(value, index, action, w):
    check_fits(value, w)
    if not 0 <= index < w:
        raise ValueError(f"Bit index phải từ 0 đến {w - 1} (đang chọn {w} bit).")
    if action == "SET":
        return value | (1 << index)
    if action == "CLEAR":
        return value & ~(1 << index)
    if action == "TOGGLE":
        return value ^ (1 << index)
    raise ValueError("Thao tác không hợp lệ.")


def mask_ops(reg, mask, w):
    check_fits(reg, w, "Register")
    check_fits(mask, w, "Mask")
    full = (1 << w) - 1
    return [
        ("Register & Mask   (AND)", reg & mask),
        ("Register | Mask   (OR)", reg | mask),
        ("Register ^ Mask   (XOR)", reg ^ mask),
        ("~Register         (NOT)", ~reg & full),
        ("~Mask             (NOT)", ~mask & full),
    ]


def text_to_hex(text):
    return " ".join(f"{b:02X}" for b in text.encode("utf-8"))


def hex_to_text(s):
    cleaned = re.sub(r"0x|[\s,]", "", s, flags=re.IGNORECASE)
    if not cleaned:
        raise ValueError("Chưa nhập HEX.")
    if not re.fullmatch(r"[0-9A-Fa-f]+", cleaned):
        raise ValueError("HEX chứa ký tự không hợp lệ (chỉ 0-9, A-F).")
    if len(cleaned) % 2:
        raise ValueError("HEX phải có số chữ số chẵn (mỗi byte 2 chữ số).")
    try:
        return bytes.fromhex(cleaned).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Dãy byte không phải UTF-8 hợp lệ: {exc}")


SIZE_UNITS = ("Byte", "KB", "MB", "GB")


def convert_size(value, unit, binary):
    """Trả về dict {đơn vị: Decimal}."""
    base = 1024 if binary else 1000
    in_bytes = value * Decimal(base) ** SIZE_UNITS.index(unit)
    return {u: in_bytes / Decimal(base) ** i for i, u in enumerate(SIZE_UNITS)}


def fmt_decimal(d):
    s = f"{d:.6f}".rstrip("0").rstrip(".")
    return s or "0"


# ---------------------------------------------------------------------------
# File Tools: logic thuần
# ---------------------------------------------------------------------------

CLEANUP_NAMES = ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache")
CLEANUP_OPTIONAL = ("build",)  # chỉ liệt kê khi người dùng bật, không bao giờ tick sẵn
INVALID_NAME_CHARS = '<>:"/\\|?*'


def human_size(n):
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} B" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024


def parse_size_text(text):
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB)?\s*", text, re.IGNORECASE)
    if not m:
        raise ValueError("Kích thước không hợp lệ. Ví dụ: 100 MB, 500 KB.")
    unit = (m.group(2) or "MB").upper()
    return int(float(m.group(1)) * 1024 ** ("B KB MB GB TB".split().index(unit)))


def plan_rename(files, prefix, start, digits):
    """Trả về [(src, dst, trạng_thái)]; trạng thái 'OK' hoặc mô tả lỗi."""
    if any(c in prefix for c in INVALID_NAME_CHARS):
        raise ValueError(f"Prefix không được chứa các ký tự: {INVALID_NAME_CHARS}")
    plan = []
    seen = set()
    for i, src in enumerate(files):
        folder, name = os.path.split(src)
        ext = os.path.splitext(name)[1]
        dst = os.path.join(folder, f"{prefix}{str(start + i).zfill(digits)}{ext}")
        key = os.path.normcase(dst)
        if not os.path.exists(src):
            status = "Lỗi: file nguồn không tồn tại"
        elif key == os.path.normcase(src):
            status = "Lỗi: tên mới trùng tên cũ"
        elif os.path.exists(dst):
            status = "Lỗi: tên mới đã tồn tại"
        elif key in seen:
            status = "Lỗi: trùng tên mới khác trong danh sách"
        else:
            status = "OK"
        seen.add(key)
        plan.append((src, dst, status))
    return plan


def hash_file(path, chunk=1024 * 1024):
    md5, sha1, sha256 = hashlib.md5(), hashlib.sha1(), hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            md5.update(block)
            sha1.update(block)
            sha256.update(block)
    return md5.hexdigest(), sha1.hexdigest(), sha256.hexdigest()


def scan_tree(root, min_size=None, top_n=10):
    """
    Duyệt đúng thư mục `root` (không đi theo symlink/junction).
    Trả về dict: total, files, folders, top (n file lớn nhất), big (file >= min_size), skipped.
    """
    total = nfiles = nfolders = skipped = 0
    heap, big, counter = [], [], 0
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _e: None):
        # Không đi vào symlink/junction để khỏi ra ngoài thư mục đã chọn.
        keep = []
        for d in dirnames:
            if os.path.islink(os.path.join(dirpath, d)) or _is_junction(os.path.join(dirpath, d)):
                continue
            keep.append(d)
        dirnames[:] = keep
        nfolders += len(keep)
        for name in filenames:
            path = os.path.join(dirpath, name)
            try:
                if os.path.islink(path):
                    continue
                size = os.stat(path).st_size
            except OSError:
                skipped += 1
                continue
            total += size
            nfiles += 1
            counter += 1
            if top_n <= 0:
                pass
            elif len(heap) < top_n:
                heapq.heappush(heap, (size, counter, path))
            elif size > heap[0][0]:
                heapq.heapreplace(heap, (size, counter, path))
            if min_size is not None and size >= min_size:
                big.append((size, path))
    top = [(s, p) for s, _c, p in sorted(heap, reverse=True)]
    big.sort(reverse=True)
    return {"total": total, "files": nfiles, "folders": nfolders, "top": top, "big": big, "skipped": skipped}


def _is_junction(path):
    try:
        return os.path.isjunction(path)  # Python 3.12+
    except AttributeError:
        return False


def dir_size(path):
    return scan_tree(path, top_n=0)["total"]


def scan_cleanup(root, include_build):
    """Liệt kê thư mục có thể dọn. Không xóa gì. Trả về [(path, size, optional)]."""
    names = set(CLEANUP_NAMES) | (set(CLEANUP_OPTIONAL) if include_build else set())
    found = []
    for dirpath, dirnames, _files in os.walk(root, onerror=lambda _e: None):
        if ".git" in dirnames:
            dirnames.remove(".git")
        for d in list(dirnames):
            full = os.path.join(dirpath, d)
            if os.path.islink(full) or _is_junction(full):
                dirnames.remove(d)
            elif d in names:
                dirnames.remove(d)  # không đi sâu vào trong mục sẽ bị liệt kê
                found.append((full, dir_size(full), d in CLEANUP_OPTIONAL))
    return found


def delete_cleanup_items(paths, root):
    """Xóa các thư mục đã được người dùng chọn. Trả về (số_đã_xóa, [(path, lỗi)])."""
    allowed = set(CLEANUP_NAMES) | set(CLEANUP_OPTIONAL)
    root_abs = os.path.normcase(os.path.abspath(root))
    done, errors = 0, []
    for p in paths:
        try:
            ap = os.path.abspath(p)
            if os.path.basename(ap) not in allowed:
                raise ValueError("tên thư mục không nằm trong danh sách được phép xóa")
            if not os.path.normcase(ap).startswith(root_abs + os.sep):
                raise ValueError("nằm ngoài thư mục đã quét")
            if not os.path.exists(ap):
                raise FileNotFoundError("không còn tồn tại")
            if os.path.islink(ap) or _is_junction(ap) or not os.path.isdir(ap):
                raise ValueError("không phải thư mục thường")
            shutil.rmtree(ap)
            done += 1
        except Exception as exc:
            errors.append((p, str(exc)))
    return done, errors


def make_zip(sources, kind, out_path, progress=None):
    """kind = 'files' | 'folder'. Ghi ra file tạm rồi thay thế, nên lỗi giữa chừng không làm hỏng ZIP cũ."""
    tmp = out_path + ".tmp"
    out_abs = os.path.normcase(os.path.abspath(out_path))
    tmp_abs = os.path.normcase(os.path.abspath(tmp))
    entries, errors = [], []
    if kind == "folder":
        base = os.path.dirname(os.path.abspath(sources[0]))
        for dirpath, dirnames, filenames in os.walk(sources[0]):
            dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
            if not filenames and not dirnames:
                entries.append((dirpath, os.path.relpath(dirpath, base) + "/"))
            for n in filenames:
                full = os.path.join(dirpath, n)
                entries.append((full, os.path.relpath(full, base)))
    else:
        names = set()
        for p in sources:
            n = os.path.basename(p)
            if n.lower() in names:
                errors.append((p, "trùng tên với file khác trong ZIP, đã bỏ qua"))
                continue
            names.add(n.lower())
            entries.append((p, n))
    count = 0
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            for i, (full, arc) in enumerate(entries, 1):
                if os.path.normcase(os.path.abspath(full)) in (out_abs, tmp_abs):
                    continue  # không nén chính file ZIP đang tạo
                try:
                    if arc.endswith("/"):
                        zf.writestr(arc, "")
                    else:
                        zf.write(full, arc)
                    count += 1
                except Exception as exc:
                    errors.append((full, str(exc)))
                if progress:
                    progress(i, len(entries))
        os.replace(tmp, out_path)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise
    return count, errors


def open_location(path):
    if not os.path.exists(path):
        raise FileNotFoundError(f"Không tìm thấy: {path}")
    if os.name == "nt":
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    else:
        raise OSError("Chức năng này chỉ hỗ trợ Windows Explorer.")


# ---------------------------------------------------------------------------
# Giao diện: tiện ích dùng chung
# ---------------------------------------------------------------------------

MONO = ("Consolas", 10)


def copy_text(widget, text):
    widget.clipboard_clear()
    widget.clipboard_append(text)


def result_row(parent, widget, label, row, width=48):
    """Một dòng: nhãn + ô kết quả (chỉ đọc) + nút Copy. Trả về StringVar."""
    var = tk.StringVar()
    ttk.Label(parent, text=label, width=10).grid(row=row, column=0, sticky="w", pady=2)
    ttk.Entry(parent, textvariable=var, state="readonly", font=MONO, width=width).grid(
        row=row, column=1, sticky="ew", padx=4, pady=2)
    ttk.Button(parent, text="Copy", width=7, command=lambda: copy_text(widget, var.get())).grid(
        row=row, column=2, pady=2)
    parent.columnconfigure(1, weight=1)
    return var


def make_tree(parent, columns, widths, height=8):
    frame = ttk.Frame(parent)
    tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings", height=height,
                        selectmode="extended")
    for (cid, title), w in zip(columns, widths):
        tree.heading(cid, text=title)
        tree.column(cid, width=w, anchor="w", stretch=(cid == "path" or cid == "new"))
    sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    return frame, tree


# ---------------------------------------------------------------------------
# Tab Code Tools
# ---------------------------------------------------------------------------


class CodeTools(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=6, pady=6)
        for title, builder in (
            ("Number Converter", self._build_number),
            ("Bit Tools", self._build_bits),
            ("ASCII / HEX", self._build_ascii),
            ("Data Size", self._build_size),
        ):
            tab = ttk.Frame(nb, padding=10)
            nb.add(tab, text=title)
            builder(tab)

    # ----- Number Converter -----

    def _build_number(self, tab):
        ttk.Label(tab, text="Nhập số (0x.. HEX, 0b.. BIN, 0o.. OCT, còn lại là DEC):").pack(anchor="w")
        top = ttk.Frame(tab)
        top.pack(fill="x", pady=6)
        self.num_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.num_var, font=MONO).pack(side="left", fill="x", expand=True)
        ttk.Button(top, text="Clear", command=lambda: self.num_var.set("")).pack(side="left", padx=4)
        self.num_err = ttk.Label(tab, text="", foreground="#c00000")
        self.num_err.pack(anchor="w")
        grid = ttk.Frame(tab)
        grid.pack(fill="x", pady=6)
        self.num_out = {k: result_row(grid, self, k, i) for i, k in enumerate(("HEX", "DEC", "BIN", "OCT"))}
        self.num_var.trace_add("write", lambda *_: self._num_update())

    def _num_update(self):
        text = self.num_var.get()
        if not text.strip():
            self.num_err.config(text="")
            for v in self.num_out.values():
                v.set("")
            return
        try:
            n = parse_number(text)
        except ValueError as exc:
            self.num_err.config(text=str(exc))
            for v in self.num_out.values():
                v.set("")
            return
        self.num_err.config(text="")
        w = natural_width(n)
        self.num_out["HEX"].set(fmt_hex(n, w))
        self.num_out["DEC"].set(str(n))
        self.num_out["BIN"].set(fmt_bin(n, w))
        self.num_out["OCT"].set("0o" + format(n, "o"))

    # ----- Bit Tools (Bit Viewer + Bitmask) -----

    def _build_bits(self, tab):
        # Bit viewer
        box = ttk.LabelFrame(tab, text="Bit Viewer", padding=8)
        box.pack(fill="x")
        row = ttk.Frame(box)
        row.pack(fill="x")
        ttk.Label(row, text="Giá trị:").pack(side="left")
        self.bv_var = tk.StringVar()
        e = ttk.Entry(row, textvariable=self.bv_var, font=MONO, width=20)
        e.pack(side="left", padx=4)
        e.bind("<Return>", lambda _e: self._bv_show())
        self.bv_width = ttk.Combobox(row, values=BIT_WIDTHS, state="readonly", width=8)
        self.bv_width.current(2)
        self.bv_width.pack(side="left", padx=4)
        self.bv_width.bind("<<ComboboxSelected>>", lambda _e: self._bv_show())
        ttk.Button(row, text="Hiển thị", command=self._bv_show).pack(side="left")
        ttk.Label(row, text="   Bit index:").pack(side="left")
        self.bv_idx = tk.StringVar()
        ttk.Entry(row, textvariable=self.bv_idx, width=5).pack(side="left", padx=4)
        for act in ("SET", "CLEAR", "TOGGLE"):
            ttk.Button(row, text=act, width=8, command=lambda a=act: self._bv_apply(a)).pack(side="left", padx=2)
        self.bv_err = ttk.Label(box, text="", foreground="#c00000")
        self.bv_err.pack(anchor="w")
        self.bv_group = ttk.Label(box, text="", font=("Consolas", 11, "bold"))
        self.bv_group.pack(anchor="w")
        self.bv_ruler = ttk.Label(box, text="", font=MONO, justify="left")
        self.bv_ruler.pack(anchor="w", pady=4)
        grid = ttk.Frame(box)
        grid.pack(fill="x")
        self.bv_out = {k: result_row(grid, self, k, i) for i, k in enumerate(("HEX", "DEC", "BIN"))}

        # Bitmask
        mk = ttk.LabelFrame(tab, text="Bitmask Calculator", padding=8)
        mk.pack(fill="x", pady=8)
        row = ttk.Frame(mk)
        row.pack(fill="x")
        self.mk_reg, self.mk_mask = tk.StringVar(), tk.StringVar()
        ttk.Label(row, text="Register:").pack(side="left")
        ttk.Entry(row, textvariable=self.mk_reg, font=MONO, width=16).pack(side="left", padx=4)
        ttk.Label(row, text="Mask:").pack(side="left")
        ttk.Entry(row, textvariable=self.mk_mask, font=MONO, width=16).pack(side="left", padx=4)
        self.mk_width = ttk.Combobox(row, values=BIT_WIDTHS, state="readonly", width=8)
        self.mk_width.current(2)
        self.mk_width.pack(side="left", padx=4)
        ttk.Button(row, text="Tính", command=self._mk_calc).pack(side="left")
        self.mk_err = ttk.Label(mk, text="", foreground="#c00000")
        self.mk_err.pack(anchor="w")
        grid = ttk.Frame(mk)
        grid.pack(fill="x")
        self.mk_out = []
        for i in range(5):
            var = tk.StringVar()
            lab = ttk.Label(grid, text="", width=26, font=MONO)
            lab.grid(row=i, column=0, sticky="w")
            ttk.Entry(grid, textvariable=var, state="readonly", font=MONO, width=50).grid(
                row=i, column=1, sticky="ew", padx=4, pady=1)
            hexvar = tk.StringVar()
            ttk.Button(grid, text="Copy HEX", command=lambda h=hexvar: copy_text(self, h.get())).grid(row=i, column=2)
            self.mk_out.append((lab, var, hexvar))
        grid.columnconfigure(1, weight=1)

    def _bv_width_val(self):
        return int(self.bv_width.get().split()[0])

    def _bv_show(self, value=None):
        self.bv_err.config(text="")
        try:
            w = self._bv_width_val()
            v = parse_number(self.bv_var.get()) if value is None else value
            check_fits(v, w)
        except ValueError as exc:
            self.bv_err.config(text=str(exc))
            return None
        self.bv_group.config(text=group_bits(v, w))
        self.bv_ruler.config(text=bit_ruler(v, w))
        self.bv_out["HEX"].set(fmt_hex(v, w))
        self.bv_out["DEC"].set(str(v))
        self.bv_out["BIN"].set(fmt_bin(v, w))
        return v

    def _bv_apply(self, action):
        v = self._bv_show()
        if v is None:
            return
        try:
            idx_text = self.bv_idx.get().strip()
            if not idx_text.isdigit():
                raise ValueError("Bit index phải là số nguyên không âm.")
            new = bit_operation(v, int(idx_text), action, self._bv_width_val())
        except ValueError as exc:
            self.bv_err.config(text=str(exc))
            return
        w = self._bv_width_val()
        self.bv_var.set(fmt_hex(new, w))  # để thao tác tiếp theo dùng giá trị mới
        self._bv_show(new)

    def _mk_calc(self):
        self.mk_err.config(text="")
        try:
            w = int(self.mk_width.get().split()[0])
            res = mask_ops(parse_number(self.mk_reg.get()), parse_number(self.mk_mask.get()), w)
        except ValueError as exc:
            self.mk_err.config(text=str(exc))
            for lab, var, hv in self.mk_out:
                lab.config(text="")
                var.set("")
                hv.set("")
            return
        for (lab, var, hv), (name, val) in zip(self.mk_out, res):
            lab.config(text=name)
            hv.set(fmt_hex(val, w))
            var.set(f"{fmt_hex(val, w)}  DEC {val}  {group_bits(val, w)}")

    # ----- ASCII / HEX -----

    def _build_ascii(self, tab):
        ttk.Label(tab, text="Input (Text hoặc HEX):").pack(anchor="w")
        self.as_in = tk.Text(tab, height=6, font=MONO, wrap="word")
        self.as_in.pack(fill="x", pady=4)
        row = ttk.Frame(tab)
        row.pack(fill="x", pady=4)
        ttk.Button(row, text="Text → HEX", command=self._as_t2h).pack(side="left", padx=2)
        ttk.Button(row, text="HEX → Text", command=self._as_h2t).pack(side="left", padx=2)
        ttk.Button(row, text="Clear", command=self._as_clear).pack(side="left", padx=2)
        ttk.Button(row, text="Copy kết quả", command=lambda: copy_text(self, self.as_out.get("1.0", "end-1c"))
                   ).pack(side="left", padx=2)
        self.as_err = ttk.Label(tab, text="", foreground="#c00000")
        self.as_err.pack(anchor="w")
        ttk.Label(tab, text="Kết quả:").pack(anchor="w")
        self.as_out = tk.Text(tab, height=6, font=MONO, wrap="word")
        self.as_out.pack(fill="x", pady=4)

    def _as_set(self, text):
        self.as_out.delete("1.0", "end")
        self.as_out.insert("1.0", text)

    def _as_t2h(self):
        self.as_err.config(text="")
        self._as_set(text_to_hex(self.as_in.get("1.0", "end-1c")))

    def _as_h2t(self):
        try:
            self.as_err.config(text="")
            self._as_set(hex_to_text(self.as_in.get("1.0", "end-1c")))
        except ValueError as exc:
            self.as_err.config(text=str(exc))
            self._as_set("")

    def _as_clear(self):
        self.as_in.delete("1.0", "end")
        self._as_set("")
        self.as_err.config(text="")

    # ----- Data Size -----

    def _build_size(self, tab):
        row = ttk.Frame(tab)
        row.pack(fill="x")
        self.ds_val = tk.StringVar()
        ttk.Entry(row, textvariable=self.ds_val, font=MONO, width=18).pack(side="left")
        self.ds_unit = ttk.Combobox(row, values=SIZE_UNITS, state="readonly", width=7)
        self.ds_unit.current(0)
        self.ds_unit.pack(side="left", padx=4)
        self.ds_binary = tk.BooleanVar(value=True)
        ttk.Radiobutton(row, text="Binary (1 KB = 1024 B)", variable=self.ds_binary, value=True,
                        command=self._ds_update).pack(side="left", padx=6)
        ttk.Radiobutton(row, text="Decimal (1 KB = 1000 B)", variable=self.ds_binary, value=False,
                        command=self._ds_update).pack(side="left")
        self.ds_err = ttk.Label(tab, text="", foreground="#c00000")
        self.ds_err.pack(anchor="w", pady=4)
        grid = ttk.Frame(tab)
        grid.pack(fill="x")
        self.ds_out = {u: result_row(grid, self, u, i) for i, u in enumerate(SIZE_UNITS)}
        self.ds_val.trace_add("write", lambda *_: self._ds_update())
        self.ds_unit.bind("<<ComboboxSelected>>", lambda _e: self._ds_update())

    def _ds_update(self):
        text = self.ds_val.get().strip()
        for v in self.ds_out.values():
            v.set("")
        self.ds_err.config(text="")
        if not text:
            return
        try:
            val = Decimal(text)
            if not val.is_finite() or val < 0:
                raise InvalidOperation
        except InvalidOperation:
            self.ds_err.config(text="Giá trị không hợp lệ (cần số không âm).")
            return
        for u, d in convert_size(val, self.ds_unit.get(), self.ds_binary.get()).items():
            self.ds_out[u].set(fmt_decimal(d))


# ---------------------------------------------------------------------------
# Tab File Tools
# ---------------------------------------------------------------------------


class FileTools(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.status = tk.StringVar(value="Sẵn sàng.")
        ttk.Label(self, textvariable=self.status, anchor="w").pack(side="bottom", fill="x", padx=8, pady=4)
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=6, pady=6)
        for title, builder in (
            ("Batch Rename", self._build_rename),
            ("File Hash", self._build_hash),
            ("Folder Size", self._build_folder),
            ("Large Files", self._build_large),
            ("Cleanup", self._build_cleanup),
            ("Create ZIP", self._build_zip),
        ):
            tab = ttk.Frame(nb, padding=10)
            nb.add(tab, text=title)
            builder(tab)

    # ----- thread đơn giản: worker chỉ tính toán, widget chỉ cập nhật ở main thread qua after() -----

    def run_bg(self, work, done, buttons, message):
        for b in buttons:
            b.config(state="disabled")
        self.status.set(message)
        q = queue.Queue()

        def target():
            try:
                q.put((work(), None))
            except Exception as exc:
                q.put((None, exc))

        threading.Thread(target=target, daemon=True).start()

        def poll():
            try:
                result, err = q.get_nowait()
            except queue.Empty:
                self.after(100, poll)
                return
            for b in buttons:
                b.config(state="normal")
            if err is not None:
                self.status.set(f"Lỗi: {err}")
                messagebox.showerror("Lỗi", str(err))
            else:
                done(result)

        self.after(100, poll)

    def _open_selected(self, tree, path_col):
        sel = tree.selection()
        if not sel:
            messagebox.showinfo("Thông báo", "Hãy chọn một dòng trước.")
            return
        try:
            open_location(tree.set(sel[0], path_col))
        except Exception as exc:
            messagebox.showerror("Lỗi", str(exc))

    def _copy_selected_path(self, tree):
        sel = tree.selection()
        if sel:
            copy_text(self, tree.set(sel[0], "path"))
            self.status.set("Đã copy đường dẫn.")

    # ----- Batch Rename -----

    def _build_rename(self, tab):
        self.rn_files = []
        self.rn_plan = None
        row = ttk.Frame(tab)
        row.pack(fill="x")
        ttk.Button(row, text="Chọn file...", command=self._rn_choose).pack(side="left", padx=2)
        ttk.Button(row, text="Xóa khỏi danh sách", command=self._rn_remove).pack(side="left", padx=2)
        ttk.Button(row, text="Xóa tất cả", command=self._rn_clear).pack(side="left", padx=2)
        opt = ttk.Frame(tab)
        opt.pack(fill="x", pady=6)
        self.rn_prefix, self.rn_start, self.rn_digits = tk.StringVar(value="Lab_"), tk.StringVar(value="1"), tk.StringVar(value="1")
        for text, var, w in (("Prefix:", self.rn_prefix, 14), ("Start:", self.rn_start, 6), ("Số chữ số:", self.rn_digits, 4)):
            ttk.Label(opt, text=text).pack(side="left", padx=(6, 2))
            ttk.Entry(opt, textvariable=var, width=w).pack(side="left")
            var.trace_add("write", lambda *_: self._rn_invalidate())
        self.rn_btn_prev = ttk.Button(opt, text="Preview", command=self._rn_preview)
        self.rn_btn_prev.pack(side="left", padx=10)
        self.rn_btn_do = ttk.Button(opt, text="Rename", command=self._rn_do, state="disabled")
        self.rn_btn_do.pack(side="left")
        frame, self.rn_tree = make_tree(
            tab, (("old", "Tên cũ"), ("new", "Tên mới"), ("status", "Trạng thái")), (240, 240, 220), height=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(tab, text="Bắt buộc Preview trước khi Rename. Không ghi đè file đã tồn tại; "
                            "nếu có bất kỳ xung đột nào thì không đổi tên gì cả.").pack(anchor="w", pady=4)

    def _rn_invalidate(self):
        self.rn_plan = None
        self.rn_btn_do.config(state="disabled")

    def _rn_show_files(self):
        self.rn_tree.delete(*self.rn_tree.get_children())
        for p in self.rn_files:
            self.rn_tree.insert("", "end", values=(os.path.basename(p), "", ""))
        self._rn_invalidate()
        self.status.set(f"Đã chọn {len(self.rn_files)} file.")

    def _rn_choose(self):
        paths = filedialog.askopenfilenames(title="Chọn các file cần đổi tên")
        if paths:
            for p in paths:
                p = os.path.abspath(p)
                if p not in self.rn_files:
                    self.rn_files.append(p)
            self._rn_show_files()

    def _rn_remove(self):
        idx = sorted((self.rn_tree.index(i) for i in self.rn_tree.selection()), reverse=True)
        for i in idx:
            del self.rn_files[i]
        self._rn_show_files()

    def _rn_clear(self):
        self.rn_files = []
        self._rn_show_files()

    def _rn_preview(self):
        if not self.rn_files:
            messagebox.showwarning("Chưa có file", "Hãy chọn file trước.")
            return
        try:
            start = int(self.rn_start.get())
            digits = int(self.rn_digits.get())
            if start < 0 or not 1 <= digits <= 12:
                raise ValueError
        except ValueError:
            messagebox.showerror("Lỗi", "Start phải là số nguyên >= 0 và số chữ số từ 1 đến 12.")
            return
        try:
            plan = plan_rename(self.rn_files, self.rn_prefix.get(), start, digits)
        except ValueError as exc:
            messagebox.showerror("Lỗi", str(exc))
            return
        self.rn_tree.delete(*self.rn_tree.get_children())
        for src, dst, status in plan:
            self.rn_tree.insert("", "end", values=(os.path.basename(src), os.path.basename(dst), status))
        bad = sum(1 for p in plan if p[2] != "OK")
        self.rn_plan = plan
        if bad:
            self.rn_btn_do.config(state="disabled")
            self.status.set(f"Preview: {bad} file có lỗi/xung đột. Hãy sửa rồi Preview lại.")
        else:
            self.rn_btn_do.config(state="normal")
            self.status.set("Preview OK. Bấm Rename để thực hiện.")

    def _rn_do(self):
        plan = self.rn_plan
        if not plan or any(p[2] != "OK" for p in plan):
            return
        if not messagebox.askyesno("Xác nhận", f"Đổi tên {len(plan)} file theo bản Preview?"):
            return
        errors, new_files = [], []
        for src, dst, _ in plan:
            try:
                # Kiểm tra lại ngay trước khi đổi; os.rename trên Windows cũng không ghi đè.
                if not os.path.exists(src):
                    raise FileNotFoundError("file nguồn không còn tồn tại")
                if os.path.exists(dst):
                    raise FileExistsError("tên mới đã tồn tại")
                os.rename(src, dst)
                new_files.append(dst)
            except Exception as exc:
                errors.append((src, str(exc)))
                new_files.append(src)
        self.rn_files = new_files
        self._rn_show_files()
        ok = len(plan) - len(errors)
        self.status.set(f"Đã đổi tên {ok}/{len(plan)} file.")
        if errors:
            messagebox.showwarning("Một số file lỗi", "\n".join(f"• {p}: {e}" for p, e in errors))
        else:
            messagebox.showinfo("Thành công", f"Đã đổi tên {ok} file.")

    # ----- File Hash -----

    def _build_hash(self, tab):
        row = ttk.Frame(tab)
        row.pack(fill="x")
        self.h_btn = ttk.Button(row, text="Chọn file và tính hash...", command=self._h_choose)
        self.h_btn.pack(side="left")
        grid = ttk.Frame(tab)
        grid.pack(fill="x", pady=10)
        self.h_out = {k: result_row(grid, self, k, i, width=70)
                      for i, k in enumerate(("File", "Size", "MD5", "SHA1", "SHA256"))}

    def _h_choose(self):
        path = filedialog.askopenfilename(title="Chọn file")
        if not path:
            return
        for v in self.h_out.values():
            v.set("")

        def work():
            size = os.path.getsize(path)
            return (size,) + hash_file(path)

        def done(res):
            self.h_out["File"].set(os.path.basename(path))
            self.h_out["Size"].set(f"{human_size(res[0])} ({res[0]} bytes)")
            self.h_out["MD5"].set(res[1])
            self.h_out["SHA1"].set(res[2])
            self.h_out["SHA256"].set(res[3])
            self.status.set("Đã tính xong hash.")

        self.run_bg(work, done, [self.h_btn], f"Đang tính hash: {os.path.basename(path)} ...")

    # ----- Folder Size -----

    def _build_folder(self, tab):
        row = ttk.Frame(tab)
        row.pack(fill="x")
        self.fs_btn = ttk.Button(row, text="Chọn thư mục và quét...", command=self._fs_choose)
        self.fs_btn.pack(side="left")
        self.fs_info = ttk.Label(tab, text="", font=("Segoe UI", 10, "bold"))
        self.fs_info.pack(anchor="w", pady=6)
        ttk.Label(tab, text="Top 10 largest files").pack(anchor="w")
        frame, self.fs_tree = make_tree(
            tab, (("name", "Tên file"), ("size", "Size"), ("path", "Path")), (200, 90, 400))
        frame.pack(fill="both", expand=True)
        self.fs_tree.bind("<Double-1>", lambda _e: self._open_selected(self.fs_tree, "path"))
        ttk.Button(tab, text="Mở vị trí file", command=lambda: self._open_selected(self.fs_tree, "path")
                   ).pack(anchor="w", pady=4)

    def _fs_choose(self):
        folder = filedialog.askdirectory(title="Chọn thư mục")
        if not folder:
            return
        self.fs_tree.delete(*self.fs_tree.get_children())
        self.fs_info.config(text="")

        def done(r):
            self.fs_info.config(text=f"Tổng: {human_size(r['total'])}   |   {r['files']} file   |   "
                                     f"{r['folders']} thư mục" + (f"   |   bỏ qua {r['skipped']} file không đọc được"
                                                                  if r["skipped"] else ""))
            for size, path in r["top"]:
                self.fs_tree.insert("", "end", values=(os.path.basename(path), human_size(size), path))
            self.status.set("Quét xong.")

        self.run_bg(lambda: scan_tree(os.path.abspath(folder)), done, [self.fs_btn], "Đang quét thư mục ...")

    # ----- Find Large Files -----

    def _build_large(self, tab):
        row = ttk.Frame(tab)
        row.pack(fill="x")
        ttk.Label(row, text="Minimum file size:").pack(side="left")
        self.lg_size = tk.StringVar(value="100 MB")
        ttk.Entry(row, textvariable=self.lg_size, width=12).pack(side="left", padx=4)
        ttk.Label(row, text="(B/KB/MB/GB/TB, mặc định MB)").pack(side="left")
        self.lg_btn = ttk.Button(row, text="Chọn thư mục và tìm...", command=self._lg_choose)
        self.lg_btn.pack(side="left", padx=10)
        frame, self.lg_tree = make_tree(
            tab, (("name", "Tên file"), ("size", "Size"), ("path", "Path")), (200, 90, 400), height=12)
        frame.pack(fill="both", expand=True, pady=6)
        self.lg_tree.bind("<Double-1>", lambda _e: self._open_selected(self.lg_tree, "path"))
        row = ttk.Frame(tab)
        row.pack(fill="x")
        ttk.Button(row, text="Open file location", command=lambda: self._open_selected(self.lg_tree, "path")
                   ).pack(side="left", padx=2)
        ttk.Button(row, text="Copy path", command=lambda: self._copy_selected_path(self.lg_tree)
                   ).pack(side="left", padx=2)
        ttk.Label(row, text="Chỉ liệt kê, không xóa file nào.").pack(side="left", padx=10)

    def _lg_choose(self):
        try:
            min_size = parse_size_text(self.lg_size.get())
        except ValueError as exc:
            messagebox.showerror("Lỗi", str(exc))
            return
        folder = filedialog.askdirectory(title="Chọn thư mục")
        if not folder:
            return
        self.lg_tree.delete(*self.lg_tree.get_children())

        def done(r):
            for size, path in r["big"]:
                self.lg_tree.insert("", "end", values=(os.path.basename(path), human_size(size), path))
            self.status.set(f"Tìm thấy {len(r['big'])} file >= {human_size(min_size)}.")

        self.run_bg(lambda: scan_tree(os.path.abspath(folder), min_size=min_size, top_n=0), done,
                    [self.lg_btn], "Đang tìm file lớn ...")

    # ----- Cleanup -----

    def _build_cleanup(self, tab):
        self.cl_root = None
        row = ttk.Frame(tab)
        row.pack(fill="x")
        self.cl_build = tk.BooleanVar(value=False)
        self.cl_btn = ttk.Button(row, text="Chọn thư mục project và quét...", command=self._cl_choose)
        self.cl_btn.pack(side="left")
        ttk.Checkbutton(row, text="Liệt kê cả 'build' (không tick sẵn)", variable=self.cl_build
                        ).pack(side="left", padx=10)
        frame, self.cl_tree = make_tree(
            tab, (("sel", "Xóa"), ("type", "Loại"), ("size", "Size"), ("path", "Path")), (50, 110, 90, 450), height=12)
        frame.pack(fill="both", expand=True, pady=6)
        self.cl_tree.bind("<Button-1>", self._cl_click)
        row = ttk.Frame(tab)
        row.pack(fill="x")
        ttk.Button(row, text="Bỏ chọn tất cả", command=lambda: self._cl_set_all(False)).pack(side="left", padx=2)
        self.cl_del = ttk.Button(row, text="Delete Selected", command=self._cl_delete, state="disabled")
        self.cl_del.pack(side="left", padx=10)
        ttk.Label(row, text="Click cột 'Xóa' để tick. Không có gì bị xóa nếu bạn không tick và xác nhận."
                  ).pack(side="left")

    def _cl_choose(self):
        folder = filedialog.askdirectory(title="Chọn thư mục project")
        if not folder:
            return
        self.cl_root = os.path.abspath(folder)
        self.cl_tree.delete(*self.cl_tree.get_children())
        self.cl_del.config(state="disabled")
        include_build = self.cl_build.get()
        root = self.cl_root

        def done(found):
            for path, size, optional in found:
                self.cl_tree.insert("", "end", values=(
                    "☐", "build (tùy chọn)" if optional else "cache", human_size(size), path))
            self.cl_del.config(state="normal" if found else "disabled")
            self.status.set(f"Tìm thấy {len(found)} mục. Chưa có mục nào được tick.")

        self.run_bg(lambda: scan_cleanup(root, include_build), done, [self.cl_btn], "Đang quét ...")

    def _cl_click(self, event):
        if self.cl_tree.identify_region(event.x, event.y) != "cell":
            return
        if self.cl_tree.identify_column(event.x) != "#1":
            return
        item = self.cl_tree.identify_row(event.y)
        if item:
            cur = self.cl_tree.set(item, "sel")
            self.cl_tree.set(item, "sel", "☐" if cur == "☑" else "☑")
            return "break"

    def _cl_set_all(self, value):
        for item in self.cl_tree.get_children():
            self.cl_tree.set(item, "sel", "☑" if value else "☐")

    def _cl_delete(self):
        items = [i for i in self.cl_tree.get_children() if self.cl_tree.set(i, "sel") == "☑"]
        if not items:
            messagebox.showinfo("Thông báo", "Chưa tick mục nào.")
            return
        paths = [self.cl_tree.set(i, "path") for i in items]
        listing = "\n".join(paths[:15]) + (f"\n... và {len(paths) - 15} mục khác" if len(paths) > 15 else "")
        if not messagebox.askyesno(
                "Xác nhận xóa", f"Xóa vĩnh viễn {len(paths)} thư mục sau (không vào Recycle Bin)?\n\n{listing}",
                icon="warning", default="no"):
            return
        root = self.cl_root

        def done(res):
            n, errors = res
            self.status.set(f"Đã xóa {n}/{len(paths)} mục.")
            for i in items:
                if not os.path.exists(self.cl_tree.set(i, "path")):
                    self.cl_tree.delete(i)
            if errors:
                messagebox.showwarning("Một số mục không xóa được",
                                       "\n".join(f"• {p}: {e}" for p, e in errors))

        self.run_bg(lambda: delete_cleanup_items(paths, root), done, [self.cl_btn, self.cl_del], "Đang xóa ...")

    # ----- Create ZIP -----

    def _build_zip(self, tab):
        self.z_items, self.z_kind = [], "files"
        row = ttk.Frame(tab)
        row.pack(fill="x")
        ttk.Button(row, text="Thêm file...", command=self._z_add_files).pack(side="left", padx=2)
        ttk.Button(row, text="Chọn thư mục...", command=self._z_pick_folder).pack(side="left", padx=2)
        ttk.Button(row, text="Xóa tất cả", command=self._z_clear).pack(side="left", padx=2)
        self.z_list = tk.Listbox(tab, height=8, font=("Segoe UI", 10), selectmode="extended")
        self.z_list.pack(fill="both", expand=True, pady=6)
        grid = ttk.Frame(tab)
        grid.pack(fill="x")
        self.z_name, self.z_dir = tk.StringVar(value="archive.zip"), tk.StringVar()
        ttk.Label(grid, text="Tên ZIP:").grid(row=0, column=0, sticky="w")
        ttk.Entry(grid, textvariable=self.z_name).grid(row=0, column=1, sticky="ew", padx=4, pady=2)
        ttk.Label(grid, text="Lưu tại:").grid(row=1, column=0, sticky="w")
        ttk.Entry(grid, textvariable=self.z_dir).grid(row=1, column=1, sticky="ew", padx=4, pady=2)
        ttk.Button(grid, text="Browse...", command=self._z_browse).grid(row=1, column=2)
        grid.columnconfigure(1, weight=1)
        self.z_btn = ttk.Button(tab, text="Create ZIP", command=self._z_create)
        self.z_btn.pack(anchor="e", pady=6)

    def _z_refresh(self):
        self.z_list.delete(0, "end")
        for p in self.z_items:
            self.z_list.insert("end", p)
        self.status.set(f"{len(self.z_items)} mục ({'thư mục' if self.z_kind == 'folder' else 'file'}).")

    def _z_add_files(self):
        paths = filedialog.askopenfilenames(title="Chọn file")
        if not paths:
            return
        if self.z_kind == "folder":
            self.z_items = []
        self.z_kind = "files"
        for p in paths:
            p = os.path.abspath(p)
            if p not in self.z_items:
                self.z_items.append(p)
        self._z_refresh()

    def _z_pick_folder(self):
        folder = filedialog.askdirectory(title="Chọn thư mục")
        if folder:
            self.z_items, self.z_kind = [os.path.abspath(folder)], "folder"
            self.z_name.set(os.path.basename(self.z_items[0]) + ".zip")
            self._z_refresh()

    def _z_clear(self):
        self.z_items = []
        self._z_refresh()

    def _z_browse(self):
        d = filedialog.askdirectory(title="Chọn nơi lưu ZIP")
        if d:
            self.z_dir.set(os.path.abspath(d))

    def _z_create(self):
        if not self.z_items:
            messagebox.showwarning("Chưa chọn", "Hãy chọn file hoặc một thư mục.")
            return
        name = self.z_name.get().strip()
        if not name or any(c in name for c in INVALID_NAME_CHARS):
            messagebox.showerror("Lỗi", "Tên ZIP không hợp lệ.")
            return
        if not name.lower().endswith(".zip"):
            name += ".zip"
        out_dir = self.z_dir.get().strip()
        if not out_dir or not os.path.isdir(out_dir):
            messagebox.showerror("Lỗi", "Hãy chọn thư mục lưu hợp lệ.")
            return
        out = os.path.join(out_dir, name)
        if os.path.exists(out) and not messagebox.askyesno(
                "File đã tồn tại", f"{out}\nđã tồn tại. Ghi đè?", icon="warning", default="no"):
            return
        missing = [p for p in self.z_items if not os.path.exists(p)]
        if missing:
            messagebox.showerror("Lỗi", "Không tìm thấy:\n" + "\n".join(missing))
            return
        items, kind = list(self.z_items), self.z_kind

        def done(res):
            count, errors = res
            self.status.set(f"Đã tạo ZIP: {count} mục.")
            if errors:
                messagebox.showwarning("Một số file lỗi", "\n".join(f"• {p}: {e}" for p, e in errors))
            messagebox.showinfo("Thành công", f"Đã tạo {out}\n({count} mục)")

        self.run_bg(lambda: make_zip(items, kind, out), done, [self.z_btn], "Đang tạo ZIP ...")


# ---------------------------------------------------------------------------
# PDF Tools: logic (pypdf để sửa PDF, pypdfium2 để render ra ảnh)
# ---------------------------------------------------------------------------

try:
    from pypdf import PdfReader, PdfWriter
    import pypdfium2

    PDF_LIBS_OK = True
except ImportError:
    PDF_LIBS_OK = False

COMPRESS_LEVELS = {  # tên -> (DPI tối đa của ảnh, chất lượng JPEG)
    "Low Compression": (200, 85),
    "Balanced": (150, 70),
    "High Compression": (100, 50),
}


def parse_page_range(text, total):
    """'1', '1,3,5', '1-5', '1-3,5,8-10' -> danh sách chỉ số trang 0-based, không trùng, giữ thứ tự nhập."""
    text = text.replace(" ", "")
    if not text:
        raise ValueError("Chưa nhập số trang.")
    pages, seen = [], set()
    for part in text.split(","):
        m = re.fullmatch(r"([0-9]+)(?:-([0-9]+))?", part)
        if not m:
            raise ValueError(f"'{part}' không hợp lệ. Dùng dạng 1, 1-3 hoặc 1-3,5,8-10.")
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        if a < 1 or b < 1:
            raise ValueError("Số trang bắt đầu từ 1 (không có trang 0).")
        if a > b:
            raise ValueError(f"Khoảng '{part}' bị ngược (số đầu lớn hơn số cuối).")
        if b > total:
            raise ValueError(f"Trang {b} vượt quá tổng số trang ({total}).")
        for p in range(a - 1, b):
            if p not in seen:
                seen.add(p)
                pages.append(p)
    return pages


def pdf_open(path):
    """Mở PDF để đọc. Lỗi (hỏng, mã hóa, không đọc được) -> ValueError có tên file."""
    name = os.path.basename(path)
    try:
        reader = PdfReader(path)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("PDF được bảo vệ bằng mật khẩu")
        len(reader.pages)  # ép đọc cấu trúc trang để phát hiện file hỏng ngay
        return reader
    except ValueError as exc:
        raise ValueError(f"{name}: {exc}")
    except Exception as exc:
        raise ValueError(f"{name}: không đọc được PDF ({exc})")


def save_pdf_safely(writer, out):
    """Ghi ra file tạm rồi mới thay vào đích, nên lỗi giữa chừng không để lại output dở hay làm hỏng file cũ."""
    tmp = out + ".part"
    try:
        with open(tmp, "wb") as fh:
            writer.write(fh)
        os.replace(tmp, out)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise


def pdf_merge(paths, out):
    writer = PdfWriter()
    for p in paths:
        writer.append(pdf_open(p))
    save_pdf_safely(writer, out)
    return len(writer.pages)


def pdf_write_pages(src, indices, out):
    reader = pdf_open(src)
    writer = PdfWriter()
    for i in indices:
        writer.add_page(reader.pages[i])
    save_pdf_safely(writer, out)


def pdf_split_every_page(src, out_dir):
    reader = pdf_open(src)
    stem = os.path.splitext(os.path.basename(src))[0]
    created = []
    try:
        for i in range(len(reader.pages)):
            out = os.path.join(out_dir, f"{stem}_page_{i + 1:03d}.pdf")
            writer = PdfWriter()
            writer.add_page(reader.pages[i])
            save_pdf_safely(writer, out)
            created.append(out)
    except Exception:
        for p in created:  # dọn các file đã tạo trong lần chạy này
            try:
                os.remove(p)
            except OSError:
                pass
        raise
    return created


def pdf_to_images(src, indices, fmt, dpi, out_dir):
    name = os.path.basename(src)
    stem = os.path.splitext(name)[0]
    created = []
    try:
        doc = pypdfium2.PdfDocument(src)
    except Exception as exc:
        raise ValueError(f"{name}: không mở được PDF ({exc})")
    try:
        for i in indices:
            out = os.path.join(out_dir, f"{stem}_page_{i + 1:03d}.{fmt.lower()}")
            img = doc[i].render(scale=dpi / 72).to_pil()
            if fmt == "JPG":
                if img.mode != "RGB":  # nền trắng, bỏ alpha để JPG không lỗi
                    flat = Image.new("RGB", img.size, (255, 255, 255))
                    flat.paste(img, mask=img.getchannel("A") if "A" in img.getbands() else None)
                    img = flat
                img.save(out, "JPEG", quality=92)
            else:
                img.save(out, "PNG")
            created.append(out)
    except Exception as exc:
        for p in created:
            try:
                os.remove(p)
            except OSError:
                pass
        raise ValueError(f"{name}: lỗi khi render ({exc})")
    finally:
        doc.close()
    return created


def pdf_compress(src, out, level):
    """
    Nén thật: hạ độ phân giải và nén lại ảnh JPEG (chỉ thay khi nhỏ hơn), nén content stream,
    gộp object trùng. Ảnh có kênh trong suốt/mask được giữ nguyên.
    Trả về số ảnh đã được nén lại.
    """
    max_dpi, quality = COMPRESS_LEVELS[level]
    reader = pdf_open(src)
    writer = PdfWriter(clone_from=reader)
    done, replaced = set(), 0
    for page in writer.pages:
        page_w_in = float(page.mediabox.width) / 72
        try:
            images = list(page.images)
        except Exception:
            continue
        for img in images:
            try:
                ref = img.indirect_reference
                if ref is None or ref.idnum in done:
                    continue
                done.add(ref.idnum)
                obj = ref.get_object()
                if "/SMask" in obj or "/Mask" in obj or obj.get("/ImageMask"):
                    continue
                pil = img.image
                if pil is None or pil.mode not in ("RGB", "L"):
                    continue
                orig_len = len(getattr(obj, "_data", b"")) or len(img.data)
                target_w = int(page_w_in * max_dpi)
                if 0 < target_w < pil.width:
                    pil = pil.resize((target_w, max(1, round(pil.height * target_w / pil.width))), Image.LANCZOS)
                probe = io.BytesIO()
                pil.save(probe, "JPEG", quality=quality, optimize=True)
                if probe.tell() >= orig_len:
                    continue
                img.replace(pil, quality=quality)
                replaced += 1
            except Exception:
                continue  # một ảnh khó xử lý thì giữ nguyên ảnh đó, không dừng cả file
    for page in writer.pages:
        try:
            page.compress_content_streams()
        except Exception:
            pass
    writer.compress_identical_objects(remove_identicals=True, remove_orphans=True)
    save_pdf_safely(writer, out)
    return replaced


# ---------------------------------------------------------------------------
# Tab PDF Tools
# ---------------------------------------------------------------------------


class PdfTools(ttk.Frame):
    run_bg = FileTools.run_bg  # dùng lại helper thread của File Tools (cần self.status và self.after)

    def __init__(self, parent):
        super().__init__(parent)
        self.status = tk.StringVar(value="Sẵn sàng.")
        if not PDF_LIBS_OK:
            ttk.Label(self, padding=20, foreground="#c00000",
                      text="Thiếu thư viện PDF. Cài bằng lệnh:\n\npip install pypdf pypdfium2").pack(anchor="w")
            return
        ttk.Label(self, textvariable=self.status, anchor="w").pack(side="bottom", fill="x", padx=8, pady=4)
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=6, pady=6)
        self.sources = {}  # tab -> (path, tổng_số_trang)
        for title, builder in (
            ("Merge PDF", self._build_merge),
            ("Split PDF", self._build_split),
            ("Extract / Delete Pages", self._build_extract),
            ("Reorder Pages", self._build_reorder),
            ("PDF → Images", self._build_images),
            ("Compress PDF", self._build_compress),
        ):
            tab = ttk.Frame(nb, padding=10)
            nb.add(tab, text=title)
            builder(tab)

    # ----- tiện ích dùng chung -----

    def _pick_pdf(self, key, label_var):
        """Chọn 1 PDF cho tab `key`, hiển thị tên + số trang. Trả về True nếu chọn được."""
        path = filedialog.askopenfilename(title="Chọn file PDF", filetypes=[("PDF", "*.pdf"), ("Tất cả tệp", "*.*")])
        if not path:
            return False
        path = os.path.abspath(path)
        try:
            total = len(pdf_open(path).pages)
        except ValueError as exc:
            messagebox.showerror("Lỗi", str(exc))
            return False
        self.sources[key] = (path, total)
        label_var.set(f"{os.path.basename(path)}  -  {total} trang")
        return True

    def _need_source(self, key):
        src = self.sources.get(key)
        if not src:
            messagebox.showwarning("Chưa chọn", "Hãy chọn một file PDF trước.")
            return None
        if not os.path.isfile(src[0]):
            messagebox.showerror("Lỗi", f"File không còn tồn tại:\n{src[0]}")
            return None
        return src

    def _ask_save(self, default_name, sources):
        """Hộp thoại lưu PDF. Chặn ghi đè file nguồn; hỏi trước khi ghi đè file khác."""
        out = filedialog.asksaveasfilename(title="Lưu PDF", defaultextension=".pdf", initialfile=default_name,
                                           filetypes=[("PDF", "*.pdf")], confirmoverwrite=False)
        if not out:
            return None
        out = os.path.abspath(out)
        if os.path.normcase(out) in {os.path.normcase(os.path.abspath(s)) for s in sources}:
            messagebox.showerror("Lỗi", "Không được ghi đè file PDF nguồn. Hãy chọn tên khác.")
            return None
        if os.path.exists(out) and not messagebox.askyesno(
                "File đã tồn tại", f"{out}\nđã tồn tại. Ghi đè?", icon="warning", default="no"):
            return None
        return out

    def _ask_folder_for(self, names_fn, src):
        """Chọn thư mục output cho nhiều file; hỏi một lần nếu có file sẽ bị ghi đè."""
        folder = filedialog.askdirectory(title="Chọn thư mục lưu")
        if not folder:
            return None
        folder = os.path.abspath(folder)
        existing = [n for n in names_fn() if os.path.exists(os.path.join(folder, n))]
        if existing and not messagebox.askyesno(
                "File đã tồn tại", f"{len(existing)} file trong thư mục này đã tồn tại, ví dụ:\n{existing[0]}\n\n"
                "Ghi đè tất cả?", icon="warning", default="no"):
            return None
        return folder

    def _source_label(self, tab, key, text="Chọn PDF..."):
        row = ttk.Frame(tab)
        row.pack(fill="x", pady=(0, 8))
        var = tk.StringVar(value="(chưa chọn file)")
        btn = ttk.Button(row, text=text, command=lambda: self._pick_pdf(key, var) and self._on_picked(key))
        btn.pack(side="left")
        ttk.Label(row, textvariable=var).pack(side="left", padx=8)
        return var

    def _on_picked(self, key):
        if key == "reorder":
            self.ro_order = list(range(self.sources["reorder"][1]))
            self._ro_refresh()
        elif key == "compress":
            self.cp_result.set("Original: %s" % human_size(os.path.getsize(self.sources["compress"][0])))

    def _done_msg(self, text):
        self.status.set(text)
        messagebox.showinfo("Thành công", text)

    # ----- Merge -----

    def _build_merge(self, tab):
        self.mg_files = []
        row = ttk.Frame(tab)
        row.pack(fill="x")
        for text, cmd in (("Add PDF", self._mg_add), ("Remove", self._mg_remove), ("Move Up", lambda: self._mg_move(-1)),
                          ("Move Down", lambda: self._mg_move(1)), ("Clear", self._mg_clear)):
            ttk.Button(row, text=text, command=cmd).pack(side="left", padx=2)
        self.mg_list = tk.Listbox(tab, height=10, font=("Segoe UI", 10), selectmode="extended", activestyle="none")
        self.mg_list.pack(fill="both", expand=True, pady=6)
        self.mg_btn = ttk.Button(tab, text="Merge", command=self._mg_run)
        self.mg_btn.pack(anchor="e")

    def _mg_refresh(self, select=()):
        self.mg_list.delete(0, "end")
        for i, (p, n) in enumerate(self.mg_files, 1):
            self.mg_list.insert("end", f"{i}. {os.path.basename(p)}  ({n} trang)")
        for i in select:
            self.mg_list.selection_set(i)
        self.status.set(f"{len(self.mg_files)} file, tổng {sum(n for _, n in self.mg_files)} trang.")

    def _mg_add(self):
        paths = filedialog.askopenfilenames(title="Chọn PDF", filetypes=[("PDF", "*.pdf")])
        errors = []
        for p in paths:
            p = os.path.abspath(p)
            try:
                self.mg_files.append((p, len(pdf_open(p).pages)))
            except ValueError as exc:
                errors.append(str(exc))
        self._mg_refresh()
        if errors:
            messagebox.showerror("Một số PDF không dùng được", "\n".join(errors))

    def _mg_remove(self):
        for i in reversed(self.mg_list.curselection()):
            del self.mg_files[i]
        self._mg_refresh()

    def _mg_move(self, off):
        sel = list(self.mg_list.curselection())
        if off > 0:
            sel.reverse()
        new = []
        for i in sel:
            j = i + off
            if 0 <= j < len(self.mg_files) and j not in sel:
                self.mg_files[i], self.mg_files[j] = self.mg_files[j], self.mg_files[i]
                new.append(j)
            else:
                new.append(i)
        self._mg_refresh(new)

    def _mg_clear(self):
        self.mg_files = []
        self._mg_refresh()

    def _mg_run(self):
        if len(self.mg_files) < 2:
            messagebox.showwarning("Chưa đủ file", "Hãy thêm ít nhất 2 file PDF.")
            return
        paths = [p for p, _ in self.mg_files]
        missing = [p for p in paths if not os.path.isfile(p)]
        if missing:
            messagebox.showerror("Lỗi", "Không tìm thấy:\n" + "\n".join(missing))
            return
        out = self._ask_save("merged.pdf", paths)
        if out:
            self.run_bg(lambda: pdf_merge(paths, out),
                        lambda n: self._done_msg(f"Đã ghép {len(paths)} file thành {n} trang:\n{out}"),
                        [self.mg_btn], "Đang ghép PDF ...")

    # ----- Split -----

    def _build_split(self, tab):
        self.sp_label = self._source_label(tab, "split")
        self.sp_mode = tk.StringVar(value="every")
        ttk.Radiobutton(tab, text="Split every page (mỗi trang một file)", variable=self.sp_mode,
                        value="every").pack(anchor="w")
        row = ttk.Frame(tab)
        row.pack(fill="x", pady=4)
        ttk.Radiobutton(row, text="Split by range:", variable=self.sp_mode, value="range").pack(side="left")
        self.sp_range = tk.StringVar()
        ttk.Entry(row, textvariable=self.sp_range, width=24).pack(side="left", padx=6)
        ttk.Label(row, text="ví dụ 1-3,5,8-10 (trang bắt đầu từ 1)").pack(side="left")
        self.sp_btn = ttk.Button(tab, text="Split", command=self._sp_run)
        self.sp_btn.pack(anchor="w", pady=10)

    def _sp_run(self):
        src = self._need_source("split")
        if not src:
            return
        path, total = src
        stem = os.path.splitext(os.path.basename(path))[0]
        if self.sp_mode.get() == "every":
            folder = self._ask_folder_for(lambda: [f"{stem}_page_{i:03d}.pdf" for i in range(1, total + 1)], path)
            if folder:
                self.run_bg(lambda: pdf_split_every_page(path, folder),
                            lambda files: self._done_msg(f"Đã tạo {len(files)} file trong:\n{folder}"),
                            [self.sp_btn], "Đang tách PDF ...")
            return
        try:
            pages = parse_page_range(self.sp_range.get(), total)
        except ValueError as exc:
            messagebox.showerror("Lỗi", str(exc))
            return
        out = self._ask_save(f"{stem}_split.pdf", [path])
        if out:
            self.run_bg(lambda: pdf_write_pages(path, pages, out),
                        lambda _r: self._done_msg(f"Đã tạo PDF {len(pages)} trang:\n{out}"),
                        [self.sp_btn], "Đang tách PDF ...")

    # ----- Extract / Delete -----

    def _build_extract(self, tab):
        self.ex_label = self._source_label(tab, "extract")
        row = ttk.Frame(tab)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text="Trang:").pack(side="left")
        self.ex_range = tk.StringVar()
        ttk.Entry(row, textvariable=self.ex_range, width=24).pack(side="left", padx=6)
        ttk.Label(row, text="ví dụ 1,3,5-8").pack(side="left")
        row = ttk.Frame(tab)
        row.pack(fill="x", pady=10)
        self.ex_btns = [ttk.Button(row, text="Extract Pages", command=lambda: self._ex_run(False)),
                        ttk.Button(row, text="Delete Pages", command=lambda: self._ex_run(True))]
        for b in self.ex_btns:
            b.pack(side="left", padx=2)

    def _ex_run(self, delete):
        src = self._need_source("extract")
        if not src:
            return
        path, total = src
        try:
            pages = parse_page_range(self.ex_range.get(), total)
            if delete:
                drop = set(pages)
                pages = [i for i in range(total) if i not in drop]
                if not pages:
                    raise ValueError("Không thể xóa toàn bộ các trang (sẽ tạo PDF rỗng).")
        except ValueError as exc:
            messagebox.showerror("Lỗi", str(exc))
            return
        stem = os.path.splitext(os.path.basename(path))[0]
        out = self._ask_save(f"{stem}_{'deleted' if delete else 'extracted'}.pdf", [path])
        if out:
            self.run_bg(lambda: pdf_write_pages(path, pages, out),
                        lambda _r: self._done_msg(f"Đã tạo PDF {len(pages)} trang:\n{out}"),
                        self.ex_btns, "Đang xử lý ...")

    # ----- Reorder -----

    def _build_reorder(self, tab):
        self.ro_order = []
        self.ro_label = self._source_label(tab, "reorder")
        row = ttk.Frame(tab)
        row.pack(fill="x")
        ttk.Button(row, text="Move Up", command=lambda: self._ro_move(-1)).pack(side="left", padx=2)
        ttk.Button(row, text="Move Down", command=lambda: self._ro_move(1)).pack(side="left", padx=2)
        self.ro_list = tk.Listbox(tab, height=10, font=("Segoe UI", 10), selectmode="browse", activestyle="none",
                                  exportselection=False)
        self.ro_list.pack(fill="both", expand=True, pady=6)
        self.ro_btn = ttk.Button(tab, text="Save Reordered PDF", command=self._ro_run)
        self.ro_btn.pack(anchor="e")

    def _ro_refresh(self, select=None):
        self.ro_list.delete(0, "end")
        for p in self.ro_order:
            self.ro_list.insert("end", f"Page {p + 1}")
        if select is not None:
            self.ro_list.selection_set(select)
            self.ro_list.see(select)

    def _ro_move(self, off):
        sel = self.ro_list.curselection()
        if not sel:
            return
        i, j = sel[0], sel[0] + off
        if 0 <= j < len(self.ro_order):
            self.ro_order[i], self.ro_order[j] = self.ro_order[j], self.ro_order[i]
            self._ro_refresh(j)

    def _ro_run(self):
        src = self._need_source("reorder")
        if not src:
            return
        path = src[0]
        order = list(self.ro_order)
        stem = os.path.splitext(os.path.basename(path))[0]
        out = self._ask_save(f"{stem}_reordered.pdf", [path])
        if out:
            self.run_bg(lambda: pdf_write_pages(path, order, out),
                        lambda _r: self._done_msg(f"Đã lưu PDF mới:\n{out}"), [self.ro_btn], "Đang lưu ...")

    # ----- PDF -> Images -----

    def _build_images(self, tab):
        self.im_label = self._source_label(tab, "images")
        row = ttk.Frame(tab)
        row.pack(fill="x", pady=4)
        self.im_fmt = ttk.Combobox(row, values=("PNG", "JPG"), state="readonly", width=6)
        self.im_fmt.current(0)
        self.im_dpi = ttk.Combobox(row, values=("100", "150", "200", "300"), state="readonly", width=6)
        self.im_dpi.set("150")
        ttk.Label(row, text="Định dạng:").pack(side="left")
        self.im_fmt.pack(side="left", padx=(4, 12))
        ttk.Label(row, text="DPI:").pack(side="left")
        self.im_dpi.pack(side="left", padx=4)
        row = ttk.Frame(tab)
        row.pack(fill="x", pady=4)
        self.im_all = tk.BooleanVar(value=True)
        ttk.Radiobutton(row, text="All pages", variable=self.im_all, value=True).pack(side="left")
        ttk.Radiobutton(row, text="Page range:", variable=self.im_all, value=False).pack(side="left", padx=(12, 0))
        self.im_range = tk.StringVar()
        ttk.Entry(row, textvariable=self.im_range, width=20).pack(side="left", padx=6)
        ttk.Label(row, text="ví dụ 1-3,5").pack(side="left")
        self.im_btn = ttk.Button(tab, text="Convert to Images", command=self._im_run)
        self.im_btn.pack(anchor="w", pady=10)

    def _im_run(self):
        src = self._need_source("images")
        if not src:
            return
        path, total = src
        try:
            pages = list(range(total)) if self.im_all.get() else parse_page_range(self.im_range.get(), total)
        except ValueError as exc:
            messagebox.showerror("Lỗi", str(exc))
            return
        fmt, dpi = self.im_fmt.get(), int(self.im_dpi.get())
        stem = os.path.splitext(os.path.basename(path))[0]
        folder = self._ask_folder_for(lambda: [f"{stem}_page_{i + 1:03d}.{fmt.lower()}" for i in pages], path)
        if folder:
            self.run_bg(lambda: pdf_to_images(path, pages, fmt, dpi, folder),
                        lambda files: self._done_msg(f"Đã tạo {len(files)} ảnh {fmt} ({dpi} DPI) trong:\n{folder}"),
                        [self.im_btn], "Đang render trang ...")

    # ----- Compress -----

    def _build_compress(self, tab):
        self.cp_label = self._source_label(tab, "compress")
        self.cp_level = tk.StringVar(value="Balanced")
        for name in COMPRESS_LEVELS:
            ttk.Radiobutton(tab, text=name, variable=self.cp_level, value=name).pack(anchor="w")
        self.cp_btn = ttk.Button(tab, text="Compress", command=self._cp_run)
        self.cp_btn.pack(anchor="w", pady=10)
        self.cp_result = tk.StringVar()
        ttk.Label(tab, textvariable=self.cp_result, font=("Consolas", 11), justify="left").pack(anchor="w")
        ttk.Label(tab, text="Nén bằng cách hạ độ phân giải/nén lại ảnh và gộp dữ liệu trùng. PDF chủ yếu là "
                            "chữ/vector thì mức giảm sẽ nhỏ. Không cần Ghostscript.",
                  wraplength=700).pack(anchor="w", pady=10)

    def _cp_run(self):
        src = self._need_source("compress")
        if not src:
            return
        path = src[0]
        level = self.cp_level.get()
        stem = os.path.splitext(os.path.basename(path))[0]
        out = self._ask_save(f"{stem}_compressed.pdf", [path])
        if not out:
            return
        orig = os.path.getsize(path)
        tmp_out = out + ".new"  # nén ra file tạm, chỉ giữ nếu thật sự nhỏ hơn bản gốc

        def work():
            try:
                n = pdf_compress(path, tmp_out, level)
                return n, os.path.getsize(tmp_out)
            except Exception:
                if os.path.exists(tmp_out):
                    os.remove(tmp_out)
                raise

        def done(res):
            n, new = res
            if new >= orig:
                os.remove(tmp_out)
                self.cp_result.set(f"Original: {human_size(orig)}\nKhông giảm được dung lượng, không tạo file.")
                self.status.set("PDF này không nén thêm được.")
                return
            os.replace(tmp_out, out)
            saved = (1 - new / orig) * 100
            self.cp_result.set(f"Original:   {human_size(orig)}\nCompressed: {human_size(new)}\nSaved:      {saved:.1f}%")
            self._done_msg(f"Đã nén ({n} ảnh được nén lại):\n{out}")

        self.run_bg(work, done, [self.cp_btn], "Đang nén PDF ...")


# ---------------------------------------------------------------------------
# YouTube Downloader
# ---------------------------------------------------------------------------


class YouTubeDownloader(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, padding=12)
        self.status_text = tk.StringVar(value="Sẵn sàng.")
        self.progress_var = tk.DoubleVar(value=0)
        self.output_folder = tk.StringVar(value=os.path.expanduser("~/Downloads"))
        self.url_var = tk.StringVar()
        self.format_var = tk.StringVar(value="MP4")
        self.quality_var = tk.StringVar(value="1080p")
        self.bitrate_var = tk.StringVar(value="192")
        self.playlist_var = tk.BooleanVar(value=False)
        self.info = None
        self.download_thread = None
        self.cancel_flag = False

        if not YTDLP_AVAILABLE:
            ttk.Label(self, padding=20, foreground="#c00000",
                      text="Thiếu thư viện yt-dlp. Cài bằng lệnh:\n\npip install yt-dlp").pack(anchor="w")
            return

        self._build_ui()

    def _build_ui(self):
        # URL
        url_frame = ttk.LabelFrame(self, text="YouTube URL", padding=10)
        url_frame.pack(fill="x", pady=(0, 10))
        row = ttk.Frame(url_frame)
        row.pack(fill="x")
        ttk.Entry(row, textvariable=self.url_var, font=("Segoe UI", 10)).pack(side="left", fill="x", expand=True, padx=(0, 4))
        ttk.Button(row, text="Paste", command=self._paste, width=8).pack(side="left", padx=2)
        ttk.Button(row, text="Get Info", command=self._get_info, width=10).pack(side="left", padx=2)

        # Info display
        info_frame = ttk.LabelFrame(self, text="Video Info", padding=10)
        info_frame.pack(fill="x", pady=(0, 10))
        self.info_label = ttk.Label(info_frame, text="(chưa có thông tin)", anchor="w", justify="left")
        self.info_label.pack(fill="x")

        # Options
        opt_frame = ttk.LabelFrame(self, text="Options", padding=10)
        opt_frame.pack(fill="x", pady=(0, 10))

        row = ttk.Frame(opt_frame)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text="Download as:").pack(side="left")
        for fmt in ("MP4", "MP3"):
            ttk.Radiobutton(row, text=fmt, variable=self.format_var, value=fmt, command=self._format_changed).pack(side="left", padx=(8, 0))

        row = ttk.Frame(opt_frame)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text="Quality:").pack(side="left")
        self.quality_combo = ttk.Combobox(row, textvariable=self.quality_var, state="readonly", width=12,
                                          values=("Best", "2160p", "1440p", "1080p", "720p", "480p", "360p"))
        self.quality_combo.pack(side="left", padx=(8, 0))

        row = ttk.Frame(opt_frame)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text="MP3 Bitrate:").pack(side="left")
        self.bitrate_combo = ttk.Combobox(row, textvariable=self.bitrate_var, state="readonly", width=12,
                                          values=("128", "192", "256", "320"))
        self.bitrate_combo.pack(side="left", padx=(8, 0))

        ttk.Checkbutton(opt_frame, text="Download playlist", variable=self.playlist_var).pack(anchor="w", pady=(4, 0))

        # Output folder
        folder_frame = ttk.Frame(opt_frame)
        folder_frame.pack(fill="x", pady=(8, 0))
        ttk.Label(folder_frame, text="Output:").pack(side="left")
        ttk.Entry(folder_frame, textvariable=self.output_folder, state="readonly").pack(side="left", fill="x", expand=True, padx=(8, 4))
        ttk.Button(folder_frame, text="Choose Folder", command=self._choose_folder, width=14).pack(side="left")

        self._format_changed()

        # Progress
        prog_frame = ttk.LabelFrame(self, text="Progress", padding=10)
        prog_frame.pack(fill="x", pady=(0, 10))
        self.progress_bar = ttk.Progressbar(prog_frame, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill="x", pady=(0, 4))
        ttk.Label(prog_frame, textvariable=self.status_text, anchor="w").pack(fill="x")

        # Buttons
        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x")
        self.download_btn = ttk.Button(btn_frame, text="Download", command=self._download)
        self.download_btn.pack(side="left", padx=2)
        self.cancel_btn = ttk.Button(btn_frame, text="Cancel", command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=2)

    def _format_changed(self):
        is_mp4 = self.format_var.get() == "MP4"
        self.quality_combo.config(state="readonly" if is_mp4 else "disabled")
        self.bitrate_combo.config(state="disabled" if is_mp4 else "readonly")

    def _paste(self):
        try:
            self.url_var.set(self.clipboard_get())
        except tk.TclError:
            pass

    def _choose_folder(self):
        folder = filedialog.askdirectory(title="Chọn thư mục lưu", initialdir=self.output_folder.get())
        if folder:
            self.output_folder.set(os.path.abspath(folder))

    def _get_info(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("Chưa nhập URL", "Hãy nhập URL YouTube.")
            return
        self.info_label.config(text="Đang lấy thông tin...")
        self.download_btn.config(state="disabled")
        playlist_mode = self.playlist_var.get()  # đọc từ main thread
        threading.Thread(target=self._get_info_worker, args=(url, playlist_mode), daemon=True).start()

    def _get_info_worker(self, url, playlist_mode):
        try:
            ydl_opts = {'quiet': True, 'no_warnings': True, 'extract_flat': False if not playlist_mode else 'in_playlist'}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if 'entries' in info and not playlist_mode:
                    # URL playlist nhưng không bật Download playlist
                    self.after(0, lambda: self._get_info_done(None, "URL này là playlist. Bật 'Download playlist' nếu muốn tải."))
                    return
                self.after(0, lambda: self._get_info_done(info, None))
        except Exception as exc:
            self.after(0, lambda: self._get_info_done(None, str(exc)))

    def _get_info_done(self, info, error):
        self.download_btn.config(state="normal")
        if error:
            self.info_label.config(text=f"Lỗi: {error[:200]}")
            self.info = None
            return
        self.info = info
        if 'entries' in info:
            count = len(list(info['entries']))
            self.info_label.config(text=f"Playlist: {info.get('title', 'Unknown')}\n{count} video")
        else:
            title = info.get('title', 'Unknown')
            channel = info.get('channel', info.get('uploader', 'Unknown'))
            dur = info.get('duration', 0)
            dur_str = f"{dur // 60}:{dur % 60:02d}" if dur else "Unknown"
            formats = info.get('formats', [])
            heights = sorted({f.get('height') for f in formats if f.get('height')}, reverse=True)
            qual_str = ', '.join(f"{h}p" for h in heights[:5]) if heights else "Unknown"
            self.info_label.config(text=f"Title: {title[:60]}\nChannel: {channel[:40]}\nDuration: {dur_str}\nQualities: {qual_str}")
        self.status_text.set("Sẵn sàng.")

    def _download(self):
        # Kiểm tra license trước khi download
        valid, message = check_license()
        if not valid:
            messagebox.showerror("License không hợp lệ", f"{message}\n\nVui lòng kích hoạt license để sử dụng chức năng này.")
            return

        if not self.info:
            messagebox.showwarning("Chưa Get Info", "Hãy Get Info trước khi Download.")
            return
        folder = self.output_folder.get()
        if not os.path.isdir(folder):
            messagebox.showerror("Lỗi", f"Thư mục không tồn tại:\n{folder}")
            return
        if not os.access(folder, os.W_OK):
            messagebox.showerror("Lỗi", f"Không có quyền ghi vào:\n{folder}")
            return
        if not find_ffmpeg():
            messagebox.showerror("Thiếu FFmpeg", "Không tìm thấy FFmpeg.\n\n"
                                 "Hãy cài FFmpeg và đặt trong PATH, hoặc đặt ffmpeg.exe cạnh chương trình.")
            return

        if 'entries' in self.info:
            count = len(list(self.info['entries']))
            if not messagebox.askyesno("Download Playlist", f"Playlist có {count} video. Tải tất cả?", icon="question"):
                return

        self.cancel_flag = False
        self.download_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.progress_var.set(0)
        self.status_text.set("Đang chuẩn bị...")
        url = self.url_var.get().strip()
        fmt = self.format_var.get()
        qual = self.quality_var.get()
        bitrate = self.bitrate_var.get()
        self.download_thread = threading.Thread(target=self._download_worker, args=(url, folder, fmt, qual, bitrate), daemon=True)
        self.download_thread.start()

    def _download_worker(self, url, folder, fmt, qual, bitrate):
        try:
            ffmpeg = find_ffmpeg()
            ydl_opts = {
                'outtmpl': os.path.join(folder, '%(title).200s.%(ext)s'),
                'ffmpeg_location': os.path.dirname(ffmpeg) if ffmpeg else None,
                'progress_hooks': [self._progress_hook],
                'quiet': True,
                'no_warnings': True,
            }

            if fmt == "MP4":
                if qual == "Best":
                    ydl_opts['format'] = 'bestvideo+bestaudio/best'
                else:
                    height = int(qual.rstrip('p'))
                    ydl_opts['format'] = f'bestvideo[height<={height}]+bestaudio/best[height<={height}]'
                ydl_opts['merge_output_format'] = 'mp4'
            else:  # MP3
                ydl_opts['format'] = 'bestaudio/best'
                ydl_opts['postprocessors'] = [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': bitrate,
                }]

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            if self.cancel_flag:
                self.after(0, lambda: self._download_done(False, "Đã hủy."))
            else:
                self.after(0, lambda: self._download_done(True, None))
        except Exception as exc:
            self.after(0, lambda: self._download_done(False, str(exc)))

    def _progress_hook(self, d):
        if self.cancel_flag:
            raise yt_dlp.utils.DownloadCancelled()
        status = d.get('status')
        if status == 'downloading':
            percent = d.get('_percent_str', '0%').strip()
            downloaded = d.get('_downloaded_bytes_str', '?')
            total = d.get('_total_bytes_str', '?')
            speed = d.get('_speed_str', '?')
            eta = d.get('_eta_str', '?')
            try:
                pct = float(percent.rstrip('%'))
                self.after(0, lambda p=pct: self.progress_var.set(p))
            except ValueError:
                pass
            msg = f"Downloading: {percent} | {downloaded} / {total} | {speed} | ETA {eta}"
            self.after(0, lambda m=msg: self.status_text.set(m))
        elif status == 'finished':
            self.after(0, lambda: self.progress_var.set(100))
            self.after(0, lambda: self.status_text.set("Merging/Converting..."))

    def _download_done(self, success, error):
        self.download_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")
        if success:
            self.progress_var.set(100)
            self.status_text.set("Completed!")
            messagebox.showinfo("Thành công", f"Đã tải xong vào:\n{self.output_folder.get()}")
        else:
            self.status_text.set(f"Lỗi: {error[:100] if error else 'Unknown'}")
            messagebox.showerror("Lỗi", error[:300] if error else "Download thất bại.")

    def _cancel(self):
        self.cancel_flag = True
        self.cancel_btn.config(state="disabled")
        self.status_text.set("Đang hủy...")


# ---------------------------------------------------------------------------
# About Tab
# ---------------------------------------------------------------------------


class AboutTab(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, padding=20)

        # Container căn giữa
        container = ttk.Frame(self)
        container.place(relx=0.5, rely=0.5, anchor="center")

        # Title
        ttk.Label(container, text="Student Toolbox", font=("Segoe UI", 24, "bold")).pack(pady=(0, 8))
        ttk.Label(container, text="A personal all-in-one utility toolbox", font=("Segoe UI", 11), foreground="#666666").pack(pady=(0, 20))

        # License status
        license_info = get_license_info()
        if license_info:
            from datetime import datetime
            license_text = f"License: {license_info['license_type'].upper()}"
            if license_info['license_type'] == 'inf':
                license_text += " (Vĩnh viễn)"
            else:
                expiry = license_info.get('expiry_date', 'N/A')
                expiry_date = datetime.strptime(expiry, '%Y-%m-%d').date()
                current_date = datetime.now().date()
                days_left = (expiry_date - current_date).days

                if days_left > 0:
                    license_text += f" - Còn {days_left} ngày"
                elif days_left == 0:
                    license_text += " - Hết hạn hôm nay"
                else:
                    license_text += " - Đã hết hạn"

            ttk.Label(container, text=license_text, font=("Segoe UI", 10, "bold"), foreground="#00AA00").pack(pady=(0, 10))
        else:
            ttk.Label(container, text="⚠ Chưa kích hoạt License", font=("Segoe UI", 11, "bold"), foreground="#CC6600").pack(pady=(0, 8))
            ttk.Button(container, text="Kích hoạt License", command=lambda: self._show_activation_dialog()).pack(pady=(0, 10))

        # Separator
        ttk.Separator(container, orient="horizontal").pack(fill="x", pady=10)

        # Created by
        ttk.Label(container, text="Created by", font=("Segoe UI", 10), foreground="#888888").pack(pady=(10, 4))
        ttk.Label(container, text="Cao Tín Dương", font=("Segoe UI", 18, "bold"), foreground="#0066CC").pack(pady=(0, 20))

        # Separator
        ttk.Separator(container, orient="horizontal").pack(fill="x", pady=10)

        # Built with
        ttk.Label(container, text="Built with", font=("Segoe UI", 10), foreground="#888888").pack(pady=(10, 8))
        tech_frame = ttk.Frame(container)
        tech_frame.pack()
        for tech in ["Python"]:
            ttk.Label(tech_frame, text=f"• {tech}", font=("Segoe UI", 10)).pack(anchor="w", pady=2)

        # Separator
        ttk.Separator(container, orient="horizontal").pack(fill="x", pady=(20, 10))

        # Development
        ttk.Label(container, text="Development", font=("Segoe UI", 10), foreground="#888888").pack(pady=(10, 4))
        ttk.Label(container, text="Cao Tín Dương", font=("Segoe UI", 11, "bold")).pack(pady=(0, 20))

        # Separator
        ttk.Separator(container, orient="horizontal").pack(fill="x", pady=10)

        # Footer
        ttk.Label(container, text="Made for learning, experimenting & everyday use.",
                 font=("Segoe UI", 10, "italic"), foreground="#999999").pack(pady=(10, 0))


# ---------------------------------------------------------------------------
# License Activation Dialog
# ---------------------------------------------------------------------------


class LicenseDialog(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title("Kích hoạt License - Student Toolbox")
        self.geometry("700x400")
        self.resizable(False, False)
        self.result = None

        # Center window
        self.transient(parent)
        self.grab_set()

        main_frame = ttk.Frame(self, padding=30)
        main_frame.pack(fill="both", expand=True)

        # Header
        ttk.Label(main_frame, text="Kích hoạt License", font=("Segoe UI", 18, "bold")).pack(pady=(0, 10))
        ttk.Label(main_frame, text="Nhập license key để sử dụng đầy đủ tính năng",
                  font=("Segoe UI", 11), foreground="#666666").pack(pady=(0, 30))

        # License key entry
        ttk.Label(main_frame, text="License Key:", font=("Segoe UI", 12)).pack(anchor="w", pady=(0, 8))
        self.key_entry = ttk.Entry(main_frame, font=("Consolas", 14), width=50)
        self.key_entry.pack(fill="x", pady=(0, 20), ipady=8)

        # Status label
        self.status_label = ttk.Label(main_frame, text="", font=("Segoe UI", 11),
                                      foreground="#666666", wraplength=640)
        self.status_label.pack(pady=(0, 20))

        # Buttons
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill="x", pady=(20, 0))
        activate_btn = ttk.Button(btn_frame, text="Kích hoạt", command=self._activate)
        activate_btn.pack(side="left", padx=5, ipadx=20, ipady=8)
        cancel_btn = ttk.Button(btn_frame, text="Hủy", command=self.destroy)
        cancel_btn.pack(side="left", padx=5, ipadx=20, ipady=8)

        # Hint
        ttk.Label(main_frame, text="💡",
                  font=("Segoe UI", 9, "italic"), foreground="#888888").pack(side="bottom", pady=(20, 0))

        self.key_entry.focus()
        self.bind('<Return>', lambda e: self._activate())
        self.bind('<Escape>', lambda e: self.destroy())

    def _activate(self):
        key = self.key_entry.get().strip()
        if not key:
            self.status_label.config(text="⚠ Vui lòng nhập license key.", foreground="#CC0000", font=("Segoe UI", 11, "bold"))
            return

        self.status_label.config(text="⏳ Đang kích hoạt...", foreground="#0066CC", font=("Segoe UI", 11))
        self.update()

        success, message = activate_license(key)

        if success:
            self.status_label.config(text=f"✓ {message}", foreground="#00AA00", font=("Segoe UI", 12, "bold"))
            self.result = True
            self.after(2000, self.destroy)
        else:
            self.status_label.config(text=f"✗ {message}", foreground="#CC0000", font=("Segoe UI", 11, "bold"))


class MediaConverter(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, padding=12)
        self.src = None
        row = ttk.Frame(self)
        row.pack(fill="x")
        ttk.Button(row, text="Chọn file...", command=self._choose).pack(side="left")
        self.src_var = tk.StringVar(value="(chưa chọn file)")
        ttk.Label(row, textvariable=self.src_var).pack(side="left", padx=8)

        grid = ttk.Frame(self)
        grid.pack(fill="x", pady=10)
        ttk.Label(grid, text="Output:").grid(row=0, column=0, sticky="w")
        self.fmt = ttk.Combobox(grid, state="readonly", width=8, values=())
        self.fmt.grid(row=0, column=1, sticky="w", padx=4, pady=2)
        self.fmt.bind("<<ComboboxSelected>>", lambda _e: self._suggest_out())
        ttk.Label(grid, text="Lưu thành:").grid(row=1, column=0, sticky="w")
        self.out_var = tk.StringVar()
        ttk.Entry(grid, textvariable=self.out_var).grid(row=1, column=1, sticky="ew", padx=4, pady=2)
        ttk.Button(grid, text="Browse...", command=self._browse).grid(row=1, column=2)
        grid.columnconfigure(1, weight=1)

        self.btn = ttk.Button(self, text="Convert", command=self._convert)
        self.btn.pack(anchor="e")
        self.bar = ttk.Progressbar(self, maximum=100)
        self.bar.pack(fill="x", pady=6)
        self.status = tk.StringVar()
        ttk.Label(self, textvariable=self.status, wraplength=800, justify="left").pack(anchor="w")
        ff = find_ffmpeg()
        self.status.set(f"FFmpeg: {ff}" if ff else
                        "Không tìm thấy FFmpeg. Cài FFmpeg và thêm vào PATH (hoặc đặt ffmpeg.exe cạnh chương trình).")

    def _choose(self):
        path = filedialog.askopenfilename(title="Chọn file media", filetypes=MEDIA_FILETYPES)
        if not path:
            return
        ext = os.path.splitext(path)[1].lower()
        if ext not in MEDIA_TARGETS:
            messagebox.showerror("Lỗi", "Chỉ hỗ trợ file .mp4, .wav, .mp3.")
            return
        self.src = os.path.abspath(path)
        self.src_var.set(self.src)
        self.fmt.config(values=MEDIA_TARGETS[ext])
        self.fmt.current(0)
        self._suggest_out()

    def _suggest_out(self):
        if self.src:
            self.out_var.set(os.path.splitext(self.src)[0] + "." + self.fmt.get().lower())

    def _browse(self):
        if not self.src:
            messagebox.showinfo("Thông báo", "Hãy chọn file nguồn trước.")
            return
        ext = "." + self.fmt.get().lower()
        path = filedialog.asksaveasfilename(
            title="Lưu file output", defaultextension=ext, initialfile=os.path.basename(self.out_var.get()),
            filetypes=[(self.fmt.get(), "*" + ext)], confirmoverwrite=False)
        if path:
            self.out_var.set(os.path.abspath(path))

    def _convert(self):
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            messagebox.showerror("Thiếu FFmpeg", "Không tìm thấy FFmpeg.\nHãy cài FFmpeg và thêm vào PATH, "
                                 "hoặc đặt ffmpeg.exe cạnh chương trình.")
            return
        if not self.src or not os.path.isfile(self.src):
            messagebox.showerror("Lỗi", "Chưa chọn file nguồn hoặc file không còn tồn tại.")
            return
        out = self.out_var.get().strip()
        target = self.fmt.get()
        if not out or not os.path.isdir(os.path.dirname(os.path.abspath(out))):
            messagebox.showerror("Lỗi", "Đường dẫn lưu không hợp lệ.")
            return
        if os.path.normcase(os.path.abspath(out)) == os.path.normcase(self.src):
            messagebox.showerror("Lỗi", "File output trùng file nguồn.")
            return
        existed = os.path.exists(out)
        if existed and not messagebox.askyesno("File đã tồn tại", f"{out}\nđã tồn tại. Ghi đè?",
                                               icon="warning", default="no"):
            return
        cmd = build_ffmpeg_cmd(ffmpeg, self.src, out, target, existed)
        self.btn.config(state="disabled")
        self.bar.config(mode="indeterminate")
        self.bar.start(15)
        self.status.set("Đang convert ...")
        q = queue.Queue()

        def target_fn():
            try:
                run_ffmpeg(cmd, lambda f: q.put(("p", f)))
                q.put(("done", None))
            except Exception as exc:
                q.put(("err", exc))

        threading.Thread(target=target_fn, daemon=True).start()

        def poll():
            finished = None
            try:
                while True:
                    kind, val = q.get_nowait()
                    if kind == "p":
                        self.bar.stop()
                        self.bar.config(mode="determinate", value=val * 100)
                        self.status.set(f"Đang convert ... {val * 100:.0f}%")
                    else:
                        finished = (kind, val)
                        break
            except queue.Empty:
                pass
            if not finished:
                self.after(100, poll)
                return
            self.bar.stop()
            self.btn.config(state="normal")
            if finished[0] == "done":
                self.bar.config(mode="determinate", value=100)
                self.status.set(f"Xong: {out}")
                messagebox.showinfo("Thành công", f"Đã tạo {out}")
            else:
                self.bar.config(mode="determinate", value=0)
                if not existed and os.path.exists(out):
                    try:
                        os.remove(out)  # dọn file output dở dang do chính lần chạy này tạo ra
                    except OSError:
                        pass
                self.status.set("Convert thất bại.")
                messagebox.showerror("Lỗi", str(finished[1]))

        self.after(100, poll)


    def _on_tab_change(self, event):
        tab = event.widget.select()
        if tab and self.tab_map.get(tab) == "img":
            self.root.bind("<Delete>", lambda e: self.app.remove_selected())
            self.root.bind("<Control-o>", lambda e: self.app.choose_images())
        else:
            self.root.unbind("<Delete>")
            self.root.unbind("<Control-o>")


def main():
    # Kiểm tra license khi khởi động
    valid, message = check_license()

    root = TkinterDnD.Tk() if DND_AVAILABLE else tk.Tk()
    root.title(APP_TITLE)
    root.geometry("960x700")
    root.minsize(800, 560)

    # Hiển thị license status trên title bar nếu không valid
    if not valid:
        root.title(f"{APP_TITLE} - [License: {message}]")

        # Hiển thị dialog activation
        def show_activation_dialog():
            dialog = LicenseDialog(root)
            root.wait_window(dialog)
            if dialog.result:
                messagebox.showinfo("Thành công", "License đã được kích hoạt! Vui lòng khởi động lại ứng dụng.")
                root.destroy()
                return
            # Nếu không activate, vẫn cho mở app nhưng không dùng được API features

        # Menu bar với option kích hoạt
        menubar = tk.Menu(root)
        root.config(menu=menubar)
        license_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="License", menu=license_menu)
        license_menu.add_command(label="Kích hoạt License", command=show_activation_dialog)
        license_menu.add_separator()
        license_menu.add_command(label="Thoát", command=root.destroy)

    tabs = ttk.Notebook(root)
    tabs.pack(fill="both", expand=True)
    img_tab = ttk.Frame(tabs, padding=12)
    tabs.add(img_tab, text="Image → PDF")
    tabs.add(MediaConverter(tabs), text="Media Converter")
    tabs.add(YouTubeDownloader(tabs), text="YouTube Downloader")
    tabs.add(PdfTools(tabs), text="PDF Tools")
    tabs.add(CodeTools(tabs), text="Code Tools")
    tabs.add(FileTools(tabs), text="File Tools")
    tabs.add(AboutTab(tabs), text="About")
    app = App(root, img_tab)

    def on_tab_change(event):
        if tabs.select() == str(img_tab):
            root.bind("<Delete>", lambda _e: app.remove_selected())
            root.bind("<Control-o>", lambda _e: app.choose_images())
        else:
            root.unbind("<Delete>")
            root.unbind("<Control-o>")

    tabs.bind("<<NotebookTabChanged>>", on_tab_change)
    on_tab_change(None)  # áp bind cho tab đầu tiên
    root.mainloop()


if __name__ == "__main__":
    main()

