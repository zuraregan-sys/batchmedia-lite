"""BatchMedia Lite — a fast, private, batch image optimizer for Windows."""

from __future__ import annotations

import ctypes
import os
import queue
import sys
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageOps
from tkinter import filedialog, messagebox
from tkinterdnd2 import COPY, DND_FILES, TkinterDnD


APP_NAME = "BatchMedia Lite"
OUTPUT_DIR_NAME = "optimized"
SKIPPED_DIRS = {"optimized", "optimizadas"}
SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}

# Palette
BG = "#0C0F15"
PANEL = "#141922"
CARD = "#171D27"
CARD_ALT = "#1C2330"
ROW = "#1B2230"
ROW_HOVER = "#222B3B"
BORDER = "#293140"
TEXT = "#F3F6FA"
MUTED = "#8D97A9"
FAINT = "#5F6A7D"
ACCENT = "#6EE7B7"
ACCENT_HOVER = "#34D399"
ACCENT_DARK = "#071B14"
SUCCESS = "#4ADE80"
SUCCESS_BG = "#11251D"
DANGER = "#F87171"
DANGER_BG = "#2A171B"
CLEAR_BG = "#3B1619"
CLEAR_BORDER = "#7F1D1D"
CLEAR_TEXT = "#FCA5A5"
CLEAR_HOVER = "#DC2626"

FORMAT_SPECS = {
    "JPG": ("JPEG", ".jpg"),
    "PNG": ("PNG", ".png"),
    "WEBP": ("WEBP", ".webp"),
    "Keep original": (None, None),
}

ORIGINAL_FORMATS = {
    ".jpg": ("JPEG", ".jpg"),
    ".jpeg": ("JPEG", ".jpeg"),
    ".png": ("PNG", ".png"),
    ".webp": ("WEBP", ".webp"),
    ".bmp": ("BMP", ".bmp"),
}

BADGE_COLORS = {
    ".jpg": ("#3A2D1A", "#FBBF24"),
    ".jpeg": ("#3A2D1A", "#FBBF24"),
    ".png": ("#1B2A42", "#93C5FD"),
    ".webp": ("#2A2340", "#C4B5FD"),
    ".bmp": ("#252E3B", "#A8B3C5"),
}


@dataclass(frozen=True)
class ProcessOptions:
    output_format: str
    quality: int
    resize_mode: str
    custom_width: Optional[int]
    remove_exif: bool


@dataclass
class ProcessResult:
    total_input: int
    total_output: int
    successes: int
    failures: list[tuple[str, str]]


def format_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024**2:
        return f"{num_bytes / 1024:.1f} KB"
    if num_bytes < 1024**3:
        return f"{num_bytes / 1024**2:.1f} MB"
    return f"{num_bytes / 1024**3:.1f} GB"


def shorten_middle(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    left = (max_chars - 1) // 2
    right = max_chars - 1 - left
    return f"{text[:left]}…{text[-right:]}"


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


def is_inside_output_folder(path: Path) -> bool:
    """True when any path segment is one of our output folder names."""
    return any(part.casefold() in SKIPPED_DIRS for part in path.parts)


def iter_image_files(folder: Path) -> list[Path]:
    if is_inside_output_folder(folder):
        return []

    found: list[Path] = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d.casefold() not in SKIPPED_DIRS)
        for filename in sorted(files):
            path = Path(root) / filename
            if path.suffix.lower() in SUPPORTED_EXTENSIONS:
                found.append(path)
    return found


def _exif_bytes(image: Image.Image) -> Optional[bytes]:
    """Read EXIF while removing the orientation tag (pixels are already rotated)."""
    try:
        exif = image.getexif()
        if not exif:
            return None
        if 0x0112 in exif:
            del exif[0x0112]
        data = exif.tobytes()
        return data or None
    except Exception:
        return None


def _prepare_mode(image: Image.Image, save_format: str) -> Image.Image:
    has_alpha = image.mode in ("RGBA", "LA") or (
        image.mode == "P" and "transparency" in image.info
    )

    if save_format in ("JPEG", "BMP"):
        if has_alpha:
            rgba = image.convert("RGBA")
            background = Image.new("RGB", rgba.size, (255, 255, 255))
            background.paste(rgba, mask=rgba.getchannel("A"))
            return background
        return image.convert("RGB")

    if has_alpha:
        return image.convert("RGBA") if image.mode != "RGBA" else image
    return image.convert("RGB") if image.mode not in ("RGB", "L") else image


def _resized(image: Image.Image, options: ProcessOptions) -> Image.Image:
    width, height = image.size
    ratio = 1.0

    if options.resize_mode == "75%":
        ratio = 0.75
    elif options.resize_mode == "50%":
        ratio = 0.5
    elif options.resize_mode == "Custom" and options.custom_width:
        if width > options.custom_width:
            ratio = options.custom_width / width

    if ratio >= 1.0:
        return image

    new_size = (max(1, round(width * ratio)), max(1, round(height * ratio)))
    return image.resize(new_size, Image.Resampling.LANCZOS)


def optimize_image(
    source: Path,
    options: ProcessOptions,
    reserved_targets: Optional[set[Path]] = None,
) -> tuple[int, int]:
    """Optimize one image and return (original bytes, output bytes)."""
    source = Path(source)
    input_size = source.stat().st_size
    suffix = source.suffix.lower()

    if options.output_format == "Keep original":
        spec = ORIGINAL_FORMATS.get(suffix)
        if spec is None:
            raise ValueError(f"Unsupported format: {suffix or 'unknown'}")
        save_format, output_suffix = spec
    else:
        save_format, output_suffix = FORMAT_SPECS[options.output_format]

    target_dir = source.parent / OUTPUT_DIR_NAME
    target = target_dir / f"{source.stem}{output_suffix}"

    # Two source names can map to the same output name (photo.png + photo.webp
    # -> photo.jpg). Never overwrite a file produced earlier in this batch.
    if reserved_targets is not None:
        candidate = target
        counter = 1
        while candidate in reserved_targets:
            candidate = target.with_name(f"{target.stem} ({counter}){target.suffix}")
            counter += 1
        target = candidate
        reserved_targets.add(target)

    target_dir.mkdir(parents=True, exist_ok=True)

    temp_target = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.tmp")

    try:
        with Image.open(source) as opened:
            opened.load()
            icc_profile = opened.info.get("icc_profile")

            # Rotate first: reading/clearing EXIF before this would hide the
            # orientation tag from exif_transpose.
            image = ImageOps.exif_transpose(opened)
            preserved_exif = None if options.remove_exif else _exif_bytes(opened)
            image = _prepare_mode(image, save_format)
            image = _resized(image, options)

            save_kwargs: dict = {}
            if save_format == "JPEG":
                save_kwargs = {
                    "quality": options.quality,
                    "optimize": True,
                    "progressive": True,
                }
            elif save_format == "PNG":
                save_kwargs = {"optimize": True, "compress_level": 9}
            elif save_format == "WEBP":
                save_kwargs = {
                    "quality": options.quality,
                    "method": 6,
                    "optimize": True,
                }

            if preserved_exif and save_format in ("JPEG", "PNG", "WEBP"):
                save_kwargs["exif"] = preserved_exif
            if icc_profile and save_format in ("JPEG", "PNG", "WEBP"):
                save_kwargs["icc_profile"] = icc_profile

            image.save(temp_target, format=save_format, **save_kwargs)

        os.replace(temp_target, target)
        return input_size, target.stat().st_size
    except Exception:
        try:
            if temp_target.exists():
                temp_target.unlink()
        except OSError:
            pass
        raise


class BatchMediaApp(ctk.CTk, TkinterDnD.DnDWrapper):
    def __init__(self) -> None:
        super().__init__(fg_color=BG)
        self.TkdndVersion = TkinterDnD._require(self)

        self._files: dict[str, int] = {}
        self._rows: dict[str, ctk.CTkFrame] = {}
        self._processing = False
        self._closing = False
        self._events: queue.Queue = queue.Queue()
        self._drop_icon_image: Optional[ctk.CTkImage] = None

        self._setup_window()
        self._create_fonts()
        self._build_ui()
        self._register_drop_targets(self.drop_zone)
        self._set_status("Add images to get started.")
        self._update_file_summary()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ window

    def _setup_window(self) -> None:
        self.title(APP_NAME)
        width, height = 1120, 720
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 2 - 18)
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.minsize(1000, 700)

        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "BatchMedia.Lite.App"
            )
        except Exception:
            pass

        icon_path = resource_path("assets/icon.ico")
        if icon_path.exists():
            try:
                self.iconbitmap(str(icon_path))
            except Exception:
                pass

    def _create_fonts(self) -> None:
        family = "Segoe UI"
        self.font_title = ctk.CTkFont(family, 24, "bold")
        self.font_subtitle = ctk.CTkFont(family, 12)
        self.font_heading = ctk.CTkFont(family, 15, "bold")
        self.font_body = ctk.CTkFont(family, 12)
        self.font_body_strong = ctk.CTkFont(family, 12, "bold")
        self.font_small = ctk.CTkFont(family, 11)
        self.font_tiny = ctk.CTkFont(family, 10)
        self.font_button = ctk.CTkFont(family, 12, "bold")
        self.font_process = ctk.CTkFont(family, 14, "bold")
        self.font_logo = ctk.CTkFont(family, 20, "bold")
        self.font_badge = ctk.CTkFont(family, 9, "bold")
        self.font_section = ctk.CTkFont(family, 11, "bold")

    def _make_drop_icon(self) -> ctk.CTkImage:
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle(
            (5, 5, 59, 59),
            radius=16,
            fill=(110, 231, 183, 24),
            outline=(110, 231, 183, 105),
            width=2,
        )
        draw.rounded_rectangle(
            (20, 16, 44, 40), radius=5, outline=(110, 231, 183, 255), width=2
        )
        draw.line((32, 22, 32, 33), fill=(110, 231, 183, 255), width=2)
        draw.polygon([(27, 29), (32, 35), (37, 29)], fill=(110, 231, 183, 255))
        draw.line((24, 44, 40, 44), fill=(110, 231, 183, 180), width=2)
        self._drop_icon_image = ctk.CTkImage(
            light_image=img, dark_image=img, size=(38, 38)
        )
        return self._drop_icon_image

    # ---------------------------------------------------------------------- ui

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._build_header()

        main = ctk.CTkFrame(self, fg_color="transparent")
        main.grid(row=1, column=0, sticky="nsew", padx=28, pady=(0, 24))
        main.grid_columnconfigure(0, weight=1)
        main.grid_columnconfigure(1, weight=0, minsize=344)
        main.grid_rowconfigure(0, weight=0)
        main.grid_rowconfigure(1, weight=1)

        left = ctk.CTkFrame(main, fg_color="transparent")
        left.grid(row=0, column=0, rowspan=2, sticky="nsew", padx=(0, 18))
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(1, weight=1)

        self._build_drop_zone(left)
        self._build_file_list(left)
        self._build_settings(main)
        self._build_action_panel(main)

    def _build_header(self) -> None:
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=28, pady=(22, 16))
        header.grid_columnconfigure(1, weight=1)

        logo = ctk.CTkLabel(
            header,
            text="B",
            width=40,
            height=40,
            corner_radius=12,
            fg_color=ACCENT,
            text_color=ACCENT_DARK,
            font=self.font_logo,
        )
        logo.grid(row=0, column=0, rowspan=2, sticky="w")

        title = ctk.CTkLabel(
            header,
            text=APP_NAME,
            font=self.font_title,
            text_color=TEXT,
            anchor="w",
        )
        title.grid(row=0, column=1, sticky="sw", padx=(12, 0))

        subtitle = ctk.CTkLabel(
            header,
            text="Fast, private batch image optimization.",
            font=self.font_subtitle,
            text_color=MUTED,
            anchor="w",
        )
        subtitle.grid(row=1, column=1, sticky="nw", padx=(12, 0))

        privacy = ctk.CTkLabel(
            header,
            text="●  100% local  ·  no uploads",
            height=28,
            corner_radius=14,
            fg_color="#12251F",
            text_color="#7EE7C4",
            font=self.font_tiny,
            padx=12,
        )
        privacy.grid(row=0, column=2, rowspan=2, sticky="e")

    def _build_drop_zone(self, parent: ctk.CTkFrame) -> None:
        self.drop_zone = ctk.CTkFrame(
            parent,
            height=164,
            fg_color=PANEL,
            border_width=1,
            border_color=BORDER,
            corner_radius=16,
        )
        self.drop_zone.grid(row=0, column=0, sticky="ew")
        self.drop_zone.grid_propagate(False)
        self.drop_zone.grid_columnconfigure(0, weight=1)
        self.drop_zone.grid_rowconfigure(0, weight=1)
        try:
            self.drop_zone.configure(cursor="hand2")
        except Exception:
            pass

        content = ctk.CTkFrame(self.drop_zone, fg_color="transparent")
        content.grid(row=0, column=0, pady=(14, 12))
        content.grid_columnconfigure(0, weight=1)

        icon = ctk.CTkLabel(
            content,
            text="",
            image=self._make_drop_icon(),
            width=38,
            height=38,
        )
        icon.grid(row=0, column=0)

        heading = ctk.CTkLabel(
            content,
            text="Drag & drop your images here",
            height=24,
            font=self.font_heading,
            text_color=TEXT,
        )
        heading.grid(row=1, column=0, pady=(5, 0))

        hint = ctk.CTkLabel(
            content,
            text="PNG  ·  JPG  ·  JPEG  ·  WEBP  ·  BMP",
            height=14,
            font=self.font_tiny,
            text_color=FAINT,
        )
        hint.grid(row=2, column=0, pady=(1, 8))

        buttons = ctk.CTkFrame(content, fg_color="transparent")
        buttons.grid(row=3, column=0)

        self.add_files_button = ctk.CTkButton(
            buttons,
            text="Select Files",
            width=116,
            height=30,
            corner_radius=8,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            text_color=ACCENT_DARK,
            font=self.font_button,
            command=self._choose_files,
        )
        self.add_files_button.grid(row=0, column=0, padx=(0, 8))

        self.add_folder_button = ctk.CTkButton(
            buttons,
            text="Select Folder",
            width=116,
            height=30,
            corner_radius=8,
            fg_color=ROW,
            hover_color=ROW_HOVER,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
            font=self.font_button,
            command=self._choose_folder,
        )
        self.add_folder_button.grid(row=0, column=1)

        # make the whole drop area clickable
        for widget in (self.drop_zone, content, icon, heading, hint):
            widget.bind("<Button-1>", lambda _event: self._choose_files(), add="+")

    def _build_file_list(self, parent: ctk.CTkFrame) -> None:
        card = ctk.CTkFrame(
            parent,
            fg_color=PANEL,
            border_width=1,
            border_color=BORDER,
            corner_radius=16,
        )
        card.grid(row=1, column=0, sticky="nsew", pady=(14, 0))
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(2, weight=1)

        header = ctk.CTkFrame(card, fg_color="transparent", height=44)
        header.grid(row=0, column=0, sticky="ew", padx=16, pady=(8, 0))
        header.grid_columnconfigure(1, weight=1)

        title = ctk.CTkLabel(
            header,
            text="Images",
            font=self.font_heading,
            text_color=TEXT,
        )
        title.grid(row=0, column=0, sticky="w")

        self.file_count_label = ctk.CTkLabel(
            header,
            text="0 images  ·  0 KB",
            font=self.font_small,
            text_color=MUTED,
        )
        self.file_count_label.grid(row=0, column=1, sticky="e", padx=(8, 12))

        self.clear_button = ctk.CTkButton(
            header,
            text="Clear all",
            width=82,
            height=28,
            corner_radius=14,
            border_width=1,
            border_color=CLEAR_BORDER,
            fg_color=CLEAR_BG,
            hover_color=CLEAR_HOVER,
            text_color=CLEAR_TEXT,
            text_color_disabled="#8A5A5D",
            font=self.font_button,
            command=self._clear_files,
        )
        self.clear_button.grid(row=0, column=2, sticky="e")
        for hover_target in (
            self.clear_button,
            getattr(self.clear_button, "_canvas", None),
        ):
            if hover_target is not None:
                hover_target.bind("<Enter>", self._on_clear_hover, add="+")
                hover_target.bind("<Leave>", self._on_clear_unhover, add="+")

        options_row = ctk.CTkFrame(card, fg_color="transparent")
        options_row.grid(row=1, column=0, sticky="ew", padx=16, pady=(2, 0))

        self.auto_clear_var = ctk.BooleanVar(value=True)
        self.auto_clear_checkbox = ctk.CTkCheckBox(
            options_row,
            text="Auto-clear list after processing",
            variable=self.auto_clear_var,
            onvalue=True,
            offvalue=False,
            width=18,
            height=22,
            checkbox_width=18,
            checkbox_height=18,
            corner_radius=4,
            border_width=2,
            border_color="#3B4556",
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            checkmark_color=ACCENT_DARK,
            text_color=MUTED,
            text_color_disabled=FAINT,
            font=self.font_small,
        )
        self.auto_clear_checkbox.pack(side="left")

        self.file_scroll = ctk.CTkScrollableFrame(
            card,
            fg_color="transparent",
            corner_radius=0,
            scrollbar_fg_color="transparent",
            scrollbar_button_color=BORDER,
            scrollbar_button_hover_color="#3A4559",
        )
        self.file_scroll.grid(
            row=2, column=0, sticky="nsew", padx=(10, 6), pady=(8, 10)
        )

        self.empty_label = ctk.CTkLabel(
            self.file_scroll,
            text=(
                "No images loaded yet\n\n"
                "Drop files above or browse your computer.\n\n"
                "Optimized copies go to an 'optimized' folder."
            ),
            font=self.font_body,
            text_color=FAINT,
            justify="center",
        )
        self.empty_label.pack(pady=72)

    def _build_settings(self, parent: ctk.CTkFrame) -> None:
        card = ctk.CTkFrame(
            parent,
            fg_color=PANEL,
            border_width=1,
            border_color=BORDER,
            corner_radius=16,
        )
        card.grid(row=0, column=1, sticky="new")
        card.grid_columnconfigure(0, weight=1)

        pad = {"padx": 18}

        heading = ctk.CTkLabel(
            card, text="Output settings", font=self.font_heading, text_color=TEXT
        )
        heading.grid(row=0, column=0, sticky="w", pady=(16, 0), **pad)

        format_label = ctk.CTkLabel(
            card, text="FORMAT", height=16, font=self.font_section, text_color=FAINT
        )
        format_label.grid(row=2, column=0, sticky="w", pady=(12, 0), **pad)

        self.format_var = ctk.StringVar(value="Keep original")
        self.format_menu = ctk.CTkOptionMenu(
            card,
            values=["Keep original", "JPG", "PNG", "WEBP"],
            variable=self.format_var,
            command=self._on_format_change,
            height=32,
            corner_radius=8,
            fg_color=ROW,
            button_color=ROW,
            button_hover_color=ROW_HOVER,
            dropdown_fg_color=CARD_ALT,
            dropdown_hover_color=ROW_HOVER,
            text_color=TEXT,
            font=self.font_body,
            dropdown_font=self.font_body,
            anchor="w",
            dynamic_resizing=False,
        )
        self.format_menu.grid(row=3, column=0, sticky="ew", pady=(5, 12), **pad)

        quality_row = ctk.CTkFrame(card, fg_color="transparent")
        quality_row.grid(row=4, column=0, sticky="ew", **pad)
        quality_row.grid_columnconfigure(0, weight=1)

        quality_label = ctk.CTkLabel(
            quality_row,
            text="QUALITY",
            height=16,
            font=self.font_section,
            text_color=FAINT,
        )
        quality_label.grid(row=0, column=0, sticky="w")

        self.quality_value = ctk.CTkLabel(
            quality_row,
            text="80%",
            height=16,
            font=self.font_body_strong,
            text_color=ACCENT,
        )
        self.quality_value.grid(row=0, column=1, sticky="e")

        self.quality_slider = ctk.CTkSlider(
            card,
            from_=10,
            to=100,
            number_of_steps=90,
            command=self._on_quality_change,
            height=16,
            fg_color="#2B3441",
            progress_color=ACCENT,
            button_color=ACCENT,
            button_hover_color=ACCENT_HOVER,
        )
        self.quality_slider.set(80)
        self.quality_slider.grid(row=5, column=0, sticky="ew", pady=(7, 0), **pad)

        resize_label = ctk.CTkLabel(
            card, text="RESIZE", height=16, font=self.font_section, text_color=FAINT
        )
        resize_label.grid(row=7, column=0, sticky="w", pady=(12, 0), **pad)

        self.resize_var = ctk.StringVar(value="100%")
        self.resize_segment = ctk.CTkSegmentedButton(
            card,
            values=["100%", "75%", "50%", "Custom"],
            variable=self.resize_var,
            command=self._on_resize_change,
            height=30,
            corner_radius=8,
            border_width=2,
            fg_color=CARD_ALT,
            selected_color=ACCENT,
            selected_hover_color=ACCENT_HOVER,
            unselected_color=CARD_ALT,
            unselected_hover_color=ROW_HOVER,
            text_color=TEXT,
            text_color_disabled=MUTED,
            font=self.font_small,
            dynamic_resizing=False,
        )
        self.resize_segment.grid(row=8, column=0, sticky="ew", pady=(5, 0), **pad)
        self._update_resize_segment_colors()

        self.custom_width_frame = ctk.CTkFrame(card, fg_color="transparent")
        self.custom_width_frame.grid(row=9, column=0, sticky="ew", pady=(8, 0), **pad)
        self.custom_width_frame.grid_columnconfigure(0, weight=1)

        custom_label = ctk.CTkLabel(
            self.custom_width_frame,
            text="Max width",
            font=self.font_small,
            text_color=MUTED,
        )
        custom_label.grid(row=0, column=0, sticky="w")

        entry_wrap = ctk.CTkFrame(
            self.custom_width_frame, fg_color=ROW, corner_radius=7, height=30
        )
        entry_wrap.grid(row=0, column=1, sticky="e")
        entry_wrap.grid_columnconfigure(0, weight=1)

        self.custom_width_entry = ctk.CTkEntry(
            entry_wrap,
            width=68,
            height=28,
            border_width=0,
            fg_color="transparent",
            text_color=TEXT,
            placeholder_text="1920",
            placeholder_text_color=FAINT,
            font=self.font_small,
            justify="right",
        )
        self.custom_width_entry.grid(row=0, column=0, padx=(5, 0))

        px_label = ctk.CTkLabel(
            entry_wrap, text="px", font=self.font_tiny, text_color=MUTED, width=20
        )
        px_label.grid(row=0, column=1, padx=(0, 5))

        self.custom_width_frame.grid_remove()

        metadata_label = ctk.CTkLabel(
            card, text="METADATA", height=16, font=self.font_section, text_color=FAINT
        )
        metadata_label.grid(row=10, column=0, sticky="w", pady=(14, 0), **pad)

        self.remove_exif_var = ctk.BooleanVar(value=True)
        self.exif_switch = ctk.CTkSwitch(
            card,
            text="Remove EXIF metadata",
            variable=self.remove_exif_var,
            onvalue=True,
            offvalue=False,
            font=self.font_body,
            text_color=TEXT,
            fg_color="#333D4D",
            progress_color=ACCENT,
            button_color="#F8FAFC",
            button_hover_color="#E2E8F0",
            switch_width=36,
            switch_height=18,
        )
        self.exif_switch.grid(row=11, column=0, sticky="w", pady=(8, 16), **pad)

    def _build_action_panel(self, parent: ctk.CTkFrame) -> None:
        card = ctk.CTkFrame(
            parent,
            fg_color=PANEL,
            border_width=1,
            border_color=BORDER,
            corner_radius=16,
        )
        card.grid(row=1, column=1, sticky="nsew", pady=(14, 0))
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(4, weight=1)

        heading = ctk.CTkLabel(
            card, text="Optimize", height=24, font=self.font_heading, text_color=TEXT
        )
        heading.grid(row=0, column=0, sticky="w", padx=18, pady=(14, 0))

        self.progress = ctk.CTkProgressBar(
            card,
            height=7,
            corner_radius=4,
            fg_color="#293241",
            progress_color=ACCENT,
        )
        self.progress.set(0)
        self.progress.grid(row=2, column=0, sticky="ew", padx=18, pady=(8, 0))

        self.status_label = ctk.CTkLabel(
            card,
            text="Ready.",
            height=18,
            font=self.font_small,
            text_color=MUTED,
            wraplength=300,
            justify="left",
            anchor="w",
        )
        self.status_label.grid(row=3, column=0, sticky="ew", padx=18, pady=(7, 6))

        self.summary_frame = ctk.CTkFrame(
            card, fg_color=SUCCESS_BG, corner_radius=10
        )
        self.summary_frame.grid_columnconfigure(0, weight=1)

        self.summary_title = ctk.CTkLabel(
            self.summary_frame,
            text="",
            height=18,
            font=self.font_body_strong,
            text_color=TEXT,
            wraplength=290,
            justify="left",
            anchor="w",
        )
        self.summary_title.grid(
            row=0, column=0, sticky="ew", padx=12, pady=(7, 0)
        )

        self.summary_detail = ctk.CTkLabel(
            self.summary_frame,
            text="",
            height=16,
            font=self.font_small,
            text_color=SUCCESS,
            wraplength=290,
            justify="left",
            anchor="w",
        )
        self.summary_detail.grid(
            row=1, column=0, sticky="ew", padx=12, pady=(0, 7)
        )
        self.summary_frame.grid(row=4, column=0, sticky="new", padx=18, pady=(2, 6))
        self.summary_frame.grid_remove()

        self.process_button = ctk.CTkButton(
            card,
            text="Process Files",
            height=44,
            corner_radius=10,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            text_color=ACCENT_DARK,
            font=self.font_process,
            command=self._start_processing,
        )
        self.process_button.grid(
            row=5, column=0, sticky="ew", padx=18, pady=(6, 16)
        )

    # --------------------------------------------------------------- file list

    def _make_file_row(self, path: str, size: int) -> ctk.CTkFrame:
        row = ctk.CTkFrame(
            self.file_scroll,
            fg_color=ROW,
            corner_radius=10,
            height=52,
        )
        row.pack(fill="x", pady=(0, 7), padx=(2, 4))
        row.grid_columnconfigure(1, weight=1)

        ext = Path(path).suffix.lower()
        badge_bg, badge_fg = BADGE_COLORS.get(ext, (ROW_HOVER, MUTED))

        badge = ctk.CTkLabel(
            row,
            text=ext.lstrip(".").upper()[:4],
            width=44,
            height=24,
            corner_radius=6,
            fg_color=badge_bg,
            text_color=badge_fg,
            font=self.font_badge,
        )
        badge.grid(row=0, column=0, rowspan=2, padx=(11, 9), pady=13)

        name = ctk.CTkLabel(
            row,
            text=shorten_middle(Path(path).name, 54),
            font=self.font_body_strong,
            text_color=TEXT,
            anchor="w",
        )
        name.grid(row=0, column=1, sticky="ew", pady=(8, 0))

        parent_text = shorten_middle(str(Path(path).parent), 58)
        parent = ctk.CTkLabel(
            row,
            text=parent_text,
            font=self.font_tiny,
            text_color=FAINT,
            anchor="w",
        )
        parent.grid(row=1, column=1, sticky="ew", pady=(0, 8))

        size_label = ctk.CTkLabel(
            row,
            text=format_size(size),
            width=62,
            font=self.font_small,
            text_color=MUTED,
            anchor="e",
        )
        size_label.grid(row=0, column=2, rowspan=2, padx=(6, 9))

        remove = ctk.CTkButton(
            row,
            text="×",
            width=26,
            height=26,
            corner_radius=6,
            fg_color="transparent",
            hover_color=DANGER_BG,
            text_color=FAINT,
            font=self.font_heading,
            command=lambda p=path: self._remove_file(p),
        )
        remove.grid(row=0, column=3, rowspan=2, padx=(0, 9))

        # Hover feedback
        def enter(_event):
            row.configure(fg_color=ROW_HOVER)

        def leave(_event):
            row.configure(fg_color=ROW)

        for widget in (row, badge, name, parent, size_label):
            widget.bind("<Enter>", enter, add="+")
            widget.bind("<Leave>", leave, add="+")

        return row

    def _add_paths(self, raw_paths: list[str]) -> None:
        if self._processing:
            return

        candidates: list[Path] = []
        unsupported = 0
        for raw in raw_paths:
            if not raw:
                continue
            path = Path(raw)
            try:
                if is_inside_output_folder(path):
                    unsupported += 1
                elif path.is_dir():
                    candidates.extend(iter_image_files(path))
                elif path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
                    candidates.append(path)
                else:
                    unsupported += 1
            except OSError:
                unsupported += 1

        added = 0
        for path in candidates:
            try:
                resolved = path.resolve()
                key = str(resolved)
                if key in self._files:
                    continue
                size = resolved.stat().st_size
            except OSError:
                unsupported += 1
                continue

            self._files[key] = size
            row = self._make_file_row(key, size)
            self._rows[key] = row
            added += 1

        if added:
            self.empty_label.pack_forget()
            self._reset_result()
            self._set_status(f"{added} image{'s' if added != 1 else ''} added.")
        elif candidates and not unsupported:
            self._set_status("Those images are already in the list.")
        else:
            self._set_status("No supported images were found.")

        self._update_file_summary()

    def _update_file_summary(self) -> None:
        count = len(self._files)
        total = sum(self._files.values())
        if count == 0:
            label = "0 images  ·  0 KB"
        else:
            label = f"{count} image{'s' if count != 1 else ''}  ·  {format_size(total)}"
        self.file_count_label.configure(text=label)
        self.clear_button.configure(
            state="normal" if count and not self._processing else "disabled"
        )

    def _on_clear_hover(self, _event=None) -> None:
        if str(self.clear_button.cget("state")) != "disabled":
            self.clear_button.configure(text_color="#FFFFFF")

    def _on_clear_unhover(self, _event=None) -> None:
        self.clear_button.configure(text_color=CLEAR_TEXT)

    def _clear_file_entries(self) -> None:
        for row in self._rows.values():
            row.destroy()
        self._rows.clear()
        self._files.clear()
        self.empty_label.pack(pady=72)
        self._update_file_summary()

    def _remove_file(self, path: str) -> None:
        if self._processing:
            return
        self._files.pop(path, None)
        row = self._rows.pop(path, None)
        if row is not None:
            row.destroy()
        if not self._files:
            self.empty_label.pack(pady=72)
        self._reset_result()
        self._set_status("Ready." if not self._files else "Image removed.")
        self._update_file_summary()

    def _clear_files(self) -> None:
        if self._processing:
            return
        self._clear_file_entries()
        self._reset_result()
        self._set_status("Add images to get started.")

    # ------------------------------------------------------------------- pickers

    def _choose_files(self) -> None:
        if self._processing:
            return
        paths = filedialog.askopenfilenames(
            parent=self,
            title="Select images",
            filetypes=[
                ("Image files", "*.png *.jpg *.jpeg *.webp *.bmp"),
                ("PNG", "*.png"),
                ("JPEG", "*.jpg *.jpeg"),
                ("WEBP", "*.webp"),
                ("BMP", "*.bmp"),
                ("All files", "*.*"),
            ],
        )
        if paths:
            self._add_paths(list(paths))

    def _choose_folder(self) -> None:
        if self._processing:
            return
        folder = filedialog.askdirectory(parent=self, title="Select a folder")
        if folder:
            self._add_paths([folder])

    # --------------------------------------------------------------- drag & drop

    def _register_drop_targets(self, widget) -> None:
        try:
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self._on_drop)
            widget.dnd_bind("<<DropEnter>>", self._on_drag_enter)
            widget.dnd_bind("<<DropLeave>>", self._on_drag_leave)
        except Exception:
            pass

        try:
            children = widget.winfo_children()
        except Exception:
            return
        for child in children:
            self._register_drop_targets(child)

    def _on_drag_enter(self, _event=None):
        self._set_drop_highlight(True)
        return COPY

    def _on_drag_leave(self, _event=None):
        self._set_drop_highlight(False)
        return COPY

    def _on_drop(self, event):
        self._set_drop_highlight(False)
        if self._processing:
            self._set_status("Please wait for the current batch to finish.")
            return COPY
        try:
            paths = list(self.tk.splitlist(event.data))
        except Exception:
            paths = [str(event.data).strip("{}")]
        self._add_paths(paths)
        return COPY

    def _set_drop_highlight(self, active: bool) -> None:
        self.drop_zone.configure(border_color=ACCENT if active else BORDER)

    # --------------------------------------------------------------- settings

    def _on_format_change(self, value: str) -> None:
        lossless = value == "PNG"
        self.quality_slider.configure(state="disabled" if lossless else "normal")
        if lossless:
            self.quality_value.configure(text="N/A", text_color=FAINT)
        else:
            self._update_quality_label()

    def _update_quality_label(self) -> None:
        self.quality_value.configure(
            text=f"{round(self.quality_slider.get())}%",
            text_color=ACCENT,
        )

    def _on_quality_change(self, value: float) -> None:
        self._update_quality_label()

    def _on_resize_change(self, value: str) -> None:
        if value == "Custom":
            self.custom_width_frame.grid()
        else:
            self.custom_width_frame.grid_remove()
        self._update_resize_segment_colors()

    def _update_resize_segment_colors(self) -> None:
        """Use dark labels on the mint selected segment for proper contrast."""
        try:
            selected = self.resize_var.get()
            for value, button in self.resize_segment._buttons_dict.items():
                button.configure(
                    text_color=ACCENT_DARK if value == selected else TEXT
                )
        except Exception:
            pass

    # ---------------------------------------------------------------- processing

    def _read_options(self) -> Optional[ProcessOptions]:
        custom_width = None
        resize_mode = self.resize_var.get()
        if resize_mode == "Custom":
            text = self.custom_width_entry.get().strip()
            if not text.isdigit() or not (16 <= int(text) <= 20000):
                messagebox.showerror(
                    "Invalid width",
                    "Enter a maximum width between 16 and 20,000 pixels.",
                    parent=self,
                )
                self.custom_width_entry.focus_set()
                return None
            custom_width = int(text)

        return ProcessOptions(
            output_format=self.format_var.get(),
            quality=round(self.quality_slider.get()),
            resize_mode=resize_mode,
            custom_width=custom_width,
            remove_exif=bool(self.remove_exif_var.get()),
        )

    def _start_processing(self) -> None:
        if self._processing:
            return
        if not self._files:
            messagebox.showinfo(
                "No images",
                "Add at least one image before processing.",
                parent=self,
            )
            return

        options = self._read_options()
        if options is None:
            return

        paths = list(self._files.keys())
        self._processing = True
        self._set_processing_state(True)
        self._reset_result()
        self.progress.set(0)
        self._set_status(f"Preparing {len(paths)} images…")

        thread = threading.Thread(
            target=self._worker,
            args=(paths, options),
            daemon=True,
        )
        thread.start()
        self.after(60, self._poll_events)

    def _worker(self, paths: list[str], options: ProcessOptions) -> None:
        total_input = 0
        total_output = 0
        successes = 0
        failures: list[tuple[str, str]] = []
        total = len(paths)
        reserved_targets: set[Path] = set()

        for index, path in enumerate(paths, start=1):
            self._events.put(("progress", index, total, Path(path).name))
            try:
                input_size, output_size = optimize_image(
                    Path(path), options, reserved_targets
                )
                total_input += input_size
                total_output += output_size
                successes += 1
            except Exception as exc:
                failures.append((Path(path).name, str(exc)))

        self._events.put(
            (
                "done",
                ProcessResult(
                    total_input=total_input,
                    total_output=total_output,
                    successes=successes,
                    failures=failures,
                ),
            )
        )

    def _poll_events(self) -> None:
        if self._closing:
            return

        completed = False
        try:
            while True:
                event = self._events.get_nowait()
                if event[0] == "progress":
                    _, index, total, name = event
                    self.progress.set(index / total if total else 0)
                    self._set_status(
                        f"Processing {index} of {total}  ·  {shorten_middle(name, 34)}"
                    )
                elif event[0] == "done":
                    self._handle_done(event[1])
                    completed = True
        except queue.Empty:
            pass

        if self._processing and not completed:
            self.after(60, self._poll_events)

    def _handle_done(self, result: ProcessResult) -> None:
        self._processing = False
        self.progress.set(1 if result.successes else 0)
        self._set_processing_state(False)
        self._update_file_summary()

        if result.successes == 0:
            self._set_status("No images could be processed.", error=True)
            if result.failures:
                details = "\n".join(
                    f"• {name}: {reason[:120]}"
                    for name, reason in result.failures[:5]
                )
                messagebox.showerror(
                    "Processing failed",
                    f"None of the images could be processed.\n\n{details}",
                    parent=self,
                )
            return

        input_size = result.total_input
        output_size = result.total_output
        saved = input_size - output_size

        if input_size > 0:
            percent = abs(saved) / input_size * 100
        else:
            percent = 0

        self.summary_title.configure(
            text=f"From {format_size(input_size)} to {format_size(output_size)}"
        )

        if saved >= 0:
            savings_text = f"You saved {format_size(saved)}  ·  {percent:.0f}% smaller"
        else:
            savings_text = f"Output is {format_size(abs(saved))} larger  ·  {percent:.0f}% increase"
            self.summary_detail.configure(text_color="#FCD34D")

        self.summary_detail.configure(text=savings_text)
        self.summary_frame.grid()

        status = f"Complete — {result.successes} of {len(self._files)} files saved."
        if result.failures:
            status = (
                f"Complete — {result.successes} saved, "
                f"{len(result.failures)} failed."
            )
        self._set_status(status)

        if self.auto_clear_var.get():
            self._clear_file_entries()
            self._set_status(f"{status.rstrip('.')}  ·  List cleared.")

        if result.failures:
            details = "\n".join(
                f"• {name}: {reason[:120]}"
                for name, reason in result.failures[:5]
            )
            extra = (
                f"\n…and {len(result.failures) - 5} more."
                if len(result.failures) > 5
                else ""
            )
            messagebox.showwarning(
                "Some images could not be processed",
                f"{details}{extra}",
                parent=self,
            )

    def _set_processing_state(self, processing: bool) -> None:
        state = "disabled" if processing else "normal"
        self.process_button.configure(
            state=state,
            text="Processing…" if processing else "Process Files",
        )
        self.clear_button.configure(state=state)
        self.add_files_button.configure(state=state)
        self.add_folder_button.configure(state=state)
        self.format_menu.configure(state=state)
        self.resize_segment.configure(state=state)
        self.exif_switch.configure(state=state)
        self.auto_clear_checkbox.configure(state=state)
        self.quality_slider.configure(
            state=("disabled" if processing or self.format_var.get() == "PNG" else "normal")
        )
        self.custom_width_entry.configure(state=state)
        self._update_resize_segment_colors()
        self._update_file_summary()

    def _reset_result(self) -> None:
        self.summary_frame.grid_remove()
        self.summary_detail.configure(text_color=SUCCESS)
        if not self._processing:
            self.progress.set(0)

    def _set_status(self, text: str, error: bool = False) -> None:
        self.status_label.configure(
            text=text,
            text_color=DANGER if error else MUTED,
        )

    # -------------------------------------------------------------------- close

    def _on_close(self) -> None:
        if self._processing:
            should_close = messagebox.askyesno(
                "Batch in progress",
                "Images are still being processed. Close anyway?",
                parent=self,
            )
            if not should_close:
                return
        self._closing = True
        self.destroy()


def main() -> None:
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")

    app = BatchMediaApp()
    app.mainloop()


if __name__ == "__main__":
    main()
