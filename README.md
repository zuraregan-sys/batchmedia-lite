# BatchMedia Lite

A portable Windows desktop app for batch optimizing images — everything runs locally, with no uploads.

## Features

- Drag & drop or browse individual images and folders
- Output as the original format, JPG, PNG, or WEBP
- Adjustable quality (10–100%)
- Resize to 100%, 75%, 50%, or a custom maximum width
- Remove EXIF metadata, including camera and GPS information
- See original and optimized sizes, with total space saved
- Clear the whole queue at once, or auto-clear it after processing while keeping the savings summary
- Previous output folders named `optimized` are automatically skipped
- Saves every result to an `optimized` subfolder beside the original
- Dark, modern interface

Supported inputs: PNG, JPG, JPEG, WEBP, BMP.

## Run from source

```bat
python -m pip install -r requirements.txt
python app.py
```

## Build the portable .exe

Double-click `build.bat`, or run:

```bat
build.bat
```

The single-file, console-free executable is created at `dist\BatchMediaLite.exe`.

Requirements: Windows and Python 3.10+.
