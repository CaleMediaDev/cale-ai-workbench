"""Render a bundled, animated Cale portrait when the assistant speaks."""

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

SIZE = (320, 132)
CAMERA_SIZE = (200, 132)


def camera_frame(jpeg: bytes) -> Image.Image:
    """Style a live, never-saved webcam frame to sit beside Cale's portrait."""
    with Image.open(BytesIO(jpeg)) as source:
        fitted = ImageOps.fit(
            ImageOps.mirror(source.convert("RGB")),
            (CAMERA_SIZE[0] - 4, CAMERA_SIZE[1] - 4),
            method=Image.Resampling.LANCZOS,
        )
    border = Image.new("RGB", CAMERA_SIZE, "#151d31")
    border.paste(fitted, (2, 2))
    mask = Image.new("L", CAMERA_SIZE, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, CAMERA_SIZE[0] - 1, CAMERA_SIZE[1] - 1), radius=15, fill=255,
    )
    rounded = Image.new("RGB", CAMERA_SIZE, "#0b1020")
    rounded.paste(border, (0, 0), mask)
    ImageDraw.Draw(rounded).rounded_rectangle(
        (0, 0, CAMERA_SIZE[0] - 1, CAMERA_SIZE[1] - 1), radius=15,
        outline="#51e5d0", width=2,
    )
    return rounded


def load_frames(path: str | Path) -> tuple[list[Image.Image], list[int]]:
    """Decode a short animated WebP/GIF into bounded, polished UI frames."""
    frames, durations = [], []
    with Image.open(path) as source:
        count = min(getattr(source, "n_frames", 1), 72)
        for index in range(count):
            source.seek(index)
            image = ImageOps.fit(
                source.convert("RGB"), (SIZE[0] - 4, SIZE[1] - 4),
                method=Image.Resampling.LANCZOS, centering=(0.5, 0.4),
            )
            border = Image.new("RGB", SIZE, "#151d31")
            border.paste(image, (2, 2))
            mask = Image.new("L", SIZE, 0)
            drawing = ImageDraw.Draw(mask)
            drawing.rounded_rectangle((0, 0, SIZE[0] - 1, SIZE[1] - 1), radius=15, fill=255)
            rounded = Image.new("RGB", SIZE, "#0b1020")
            rounded.paste(border, (0, 0), mask)
            ImageDraw.Draw(rounded).rounded_rectangle(
                (0, 0, SIZE[0] - 1, SIZE[1] - 1), radius=15,
                outline="#6e82ff", width=2,
            )
            frames.append(rounded)
            durations.append(max(45, min(int(source.info.get("duration", 90)), 160)))
    return frames, durations


def attach(parent, path: str | Path, speaking_event) -> bool:
    """Replace the heading with video only when a real portrait asset exists."""
    if not Path(path).is_file():
        return False
    import tkinter as tk

    from PIL import ImageTk

    pictures, durations = load_frames(path)
    if not pictures:
        return False
    frames = [ImageTk.PhotoImage(picture) for picture in pictures]
    label = tk.Label(parent, image=frames[0], bg="#0b1020", borderwidth=0)
    label.pack(anchor="w")
    position = [0]

    def update():
        if not label.winfo_exists():
            return
        position[0] = (position[0] + 1) % len(frames) if speaking_event.is_set() else 0
        label.configure(image=frames[position[0]])
        label.after(durations[position[0]], update)

    label.after(durations[0], update)
    label.cale_frames = frames  # Keep Tk image references alive for the window.
    return True
