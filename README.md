# Memoir

A single-file Python slideshow — simple enough for a party, reliable enough to leave unattended. No install, no account, no nag screen.

- Pause, step back, and skip forward at any time — controls built for group viewing as much as set-and-forget
- Exits cleanly and resumes exactly where it left off — random order included, with the same seeded shuffle restored
- Multi-display support with an interactive picker; `--display N` selects a display directly for unattended startup
- Three transition modes switchable at runtime (direct cut, fade-over, fade-out/in)
- Config written on first run and never overwritten — safe to deploy and customize

The folder is the playlist — every image in it, in filename order or shuffled. Image files are never modified; Memoir only writes two small metadata files to the directory for session state and settings.

## Requirements

- Python 3.10+
- [pygame](https://www.pygame.org/) — `pip install pygame`
- [Pillow](https://python-pillow.org/) *(optional, enables EXIF correction)* — `pip install pillow`

## Usage

```
python memoir.py <imagedir> [options]
```

Supports JPG, PNG, BMP, GIF, TIFF, and WebP. Memoir opens fullscreen and loops until you quit.

Key options:

- `--order sequential|random` — start in the given order, skipping the start menu
- `--continue` — continue a previously saved session, skipping the start menu
- `--display N` — use display N (1-based), skipping the display picker
- `--delay SECONDS` — override image duration
- `--transition direct|fade-over|fade-out-in` — override transition style
- `--windowed` — run as a borderless window instead of exclusive fullscreen

Run `python memoir.py --help` for the full flag reference. In-slideshow controls are listed in the script's opening docstring.

## Configuration

A `.memoir_settings.json` file is written to the image directory on first run. Edit it to adjust timing, transitions, colours, and the start menu appearance. The file is never overwritten by the program.

## Non-goals

Things intentionally left out of scope:

- Recursive directory scanning *(sub-directories introduce prioritization choices the flat model intentionally avoids)*
- File management, renaming, or sorting
- Video or audio playback
- Network or remote image sources
- A GUI settings editor
- Multiple folders or playlists
