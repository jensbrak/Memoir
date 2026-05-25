#!/usr/bin/env python3
"""
memoir.py - Fullscreen image slideshow with fade transitions and session persistence.

Usage:
    python memoir.py <directory>

Dependencies:
    pip install pygame
    pip install pillow   # optional - enables EXIF auto-rotation

Controls (start menu):
    S          - Start slideshow in sequential order
    R          - Start slideshow in random order
    L          - Load previous session  (if available)

Controls (slideshow):
    Space      - Pause / Resume
    T          - Cycle transition mode
    L          - Toggle filename / count label
    Z          - Jump to previously shown image (no transition; does nothing on first)
    X          - Jump to next image (no transition; does nothing on last)
    Escape     - Exit (state is saved)
"""

import os
import sys
import json
import io
import random
import threading
import time
import traceback
from pathlib import Path

import pygame
try:
    from PIL import Image, ImageOps
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False
    print("Warning: Pillow not installed - EXIF rotation will not be applied.")
    print("         Install with: pip install pillow")


# -----------------------------------------------------------------------------
# Tuneable constants
# -----------------------------------------------------------------------------

SUPPORTED_EXTENSIONS  = {'.jpg', '.jpeg', '.png', '.bmp', '.gif', '.tiff', '.tif', '.webp'}
PERSIST_FILENAME      = '.memoir_session.json'
SETTINGS_FILENAME     = '.memoir_settings.json'
MAX_CONSECUTIVE_FAILS = 5     # Abort after this many back-to-back load failures
DISPLAY_FPS           = 60    # Frame-rate during animated transitions
WAIT_FPS              = 30    # Frame-rate while an image is simply being displayed

TRANSITION_NAMES = {0: 'Direct', 1: 'Fade-over', 2: 'Fade-out / Fade-in'}

# Overlay font sizes
LABEL_FONT_SIZE = 36
PAUSE_FONT_SIZE = 48

# Outline thickness in pixels for outlined text
TEXT_OUTLINE_PX = 2


# -----------------------------------------------------------------------------
# Default persisted state  (session — reset on S/R; loaded on L)
# -----------------------------------------------------------------------------

DEFAULTS: dict = {
    'random_order':        False,
    'transition_mode':     1,       # 0=Direct 1=Fade-over 2=Fade-out/in
    'current_image_index': 0,
    'random_seed':         None,
    'paused':              False,
    'show_label':          False,
}


# -----------------------------------------------------------------------------
# Default settings  (persistent config — created once, edited manually)
# -----------------------------------------------------------------------------

SETTINGS_DEFAULTS: dict = {
    'image_delay':                      5,       # seconds each image is shown
    'transition_time_fade_over':        2,       # seconds for fade-over transition
    'transition_time_fade_out_fade_in': 2,       # seconds for fade-out/in transition
    'background_color':                 '#000000',
    'label_text_color':                 '#ffffff',   # overlay label text colour
    'label_outline_color':              '#000000',   # overlay label outline colour
    'start_menu_title':                 'MEMOIR',
    'start_menu_image':                 None,    # filename; filled at first run
}


# -----------------------------------------------------------------------------
# State persistence helpers
# -----------------------------------------------------------------------------

def load_state(target_dir: Path) -> dict | None:
    """Return persisted state dict, or None if no valid file exists."""
    path = target_dir / PERSIST_FILENAME
    if not path.exists():
        return None
    try:
        with open(path, encoding='utf-8') as fh:
            saved = json.load(fh)
        state = DEFAULTS.copy()
        state.update(saved)          # Saved values win; missing keys get defaults
        return state
    except Exception as exc:
        print(f"Warning: could not read session file ({exc}) - ignoring.")
        return None


def save_state(state: dict, target_dir: Path) -> None:
    """Serialize state to the target directory's persist file."""
    path = target_dir / PERSIST_FILENAME
    try:
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(state, fh, indent=2)
        print(f"Session saved -> {path}")
    except Exception as exc:
        print(f"Warning: could not save session ({exc})")


# -----------------------------------------------------------------------------
# Settings persistence
# -----------------------------------------------------------------------------

def load_settings(target_dir: Path) -> dict | None:
    """Return settings dict from file, or None if the file does not exist."""
    path = target_dir / SETTINGS_FILENAME
    if not path.exists():
        return None
    try:
        with open(path, encoding='utf-8') as fh:
            saved = json.load(fh)
        settings = SETTINGS_DEFAULTS.copy()
        settings.update(saved)      # File wins; unknown keys kept; missing keys default
        return settings
    except Exception as exc:
        print(f"Warning: could not read settings file ({exc}) - using defaults.")
        return SETTINGS_DEFAULTS.copy()


def create_settings(target_dir: Path, image_files: list[Path]) -> dict:
    """
    Create a settings file with defaults and return the settings dict.
    Uses the first image (alphabetically) as the start menu background.
    Called only when no settings file exists in the target directory.
    """
    settings = SETTINGS_DEFAULTS.copy()
    if image_files:
        settings['start_menu_image'] = image_files[0].name
    path = target_dir / SETTINGS_FILENAME
    try:
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(settings, fh, indent=2)
        print(f"Settings created -> {path}  (edit to customise)")
    except Exception as exc:
        print(f"Warning: could not create settings file ({exc})")
    return settings


# -----------------------------------------------------------------------------
# Image discovery and playlist construction
# -----------------------------------------------------------------------------

def discover_images(target_dir: Path) -> list[Path]:
    """Return sorted list of supported image paths in target_dir."""
    return sorted(
        p for p in target_dir.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
    )


def build_playlist(image_files: list[Path], state: dict) -> list[Path]:
    """Return image_files in sequential or reproducibly-random order."""
    if not state['random_order']:
        return list(image_files)

    seed = state.get('random_seed')
    if seed is None:
        seed = random.randint(0, 2 ** 31 - 1)
        state['random_seed'] = seed          # Persist so the same order is resumed

    rng = random.Random(seed)
    playlist = list(image_files)
    rng.shuffle(playlist)
    return playlist


# -----------------------------------------------------------------------------
# Image loading, scaling, and framing
# -----------------------------------------------------------------------------

def hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip('#')
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def scale_surface(surf: pygame.Surface, screen_w: int, screen_h: int) -> pygame.Surface:
    """Scale surf down proportionally if it exceeds the screen; never scale up."""
    iw, ih = surf.get_size()
    if iw <= screen_w and ih <= screen_h:
        return surf
    factor = min(screen_w / iw, screen_h / ih)
    return pygame.transform.smoothscale(surf, (int(iw * factor), int(ih * factor)))


def scale_surface_cover(surf: pygame.Surface, screen_w: int, screen_h: int) -> pygame.Surface:
    """Scale surf to cover the full screen proportionally, cropping any overflow.
    Used for the start menu background image."""
    iw, ih = surf.get_size()
    factor = max(screen_w / iw, screen_h / ih)
    new_w, new_h = int(iw * factor), int(ih * factor)
    scaled = pygame.transform.smoothscale(surf, (new_w, new_h))
    x = (new_w - screen_w) // 2
    y = (new_h - screen_h) // 2
    return scaled.subsurface((x, y, screen_w, screen_h))


def make_frame(surf: pygame.Surface,
               screen_size: tuple[int, int],
               bg_color: tuple[int, int, int]) -> pygame.Surface:
    """Return a full-screen surface with surf centred on a solid background."""
    sw, sh = screen_size
    frame = pygame.Surface(screen_size)
    frame.fill(bg_color)
    iw, ih = surf.get_size()
    frame.blit(surf, ((sw - iw) // 2, (sh - ih) // 2))
    return frame


def load_raw(path: Path) -> pygame.Surface:
    """
    Load an image from disk without display-format conversion (thread-safe).
    If Pillow is available, EXIF orientation is applied before handing the
    surface to pygame so rotated photos appear correctly.
    Falls back to a plain pygame load if EXIF correction fails for any reason,
    printing a warning to the console.
    """
    if _PIL_AVAILABLE:
        try:
            pil_img = Image.open(path)
            pil_img = ImageOps.exif_transpose(pil_img)   # no-op if no/normal orientation
            pil_img = pil_img.convert('RGB')
            buf = io.BytesIO()
            pil_img.save(buf, format='PNG')
            buf.seek(0)
            return pygame.image.load(buf, 'img.png')
        except Exception as exc:
            print(f"Warning: EXIF rotation failed for '{path.name}' ({exc})"
                  f" - showing unrotated.")
    return pygame.image.load(str(path))


def prepare_frame(raw: pygame.Surface,
                  screen_size: tuple[int, int],
                  bg_color: tuple[int, int, int]) -> pygame.Surface:
    """Convert raw surface to display format, scale, and centre — call on main thread."""
    converted = raw.convert()
    scaled = scale_surface(converted, *screen_size)
    return make_frame(scaled, screen_size, bg_color)


# -----------------------------------------------------------------------------
# Background image pre-loader
# -----------------------------------------------------------------------------

class BackgroundLoader:
    """Pre-loads the next image on a worker thread before the main thread needs it.
    Holds the raw (unconverted) Surface; display-format conversion happens on the
    main thread in prepare_frame()."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._raw:    pygame.Surface | None   = None
        self._error:  str | None              = None
        self._path:   Path | None             = None
        self._lock = threading.Lock()

    def start(self, path: Path) -> None:
        """Begin loading path in the background (cancels any previous request)."""
        with self._lock:
            self._path  = path
            self._raw   = None
            self._error = None
        t = threading.Thread(target=self._work, args=(path,), daemon=True)
        t.start()
        self._thread = t

    def _work(self, path: Path) -> None:
        try:
            raw = load_raw(path)
            with self._lock:
                if self._path == path:   # Guard against superseded requests
                    self._raw = raw
        except Exception as exc:
            with self._lock:
                if self._path == path:
                    self._error = str(exc)

    def get(self) -> pygame.Surface:
        """Block until loading finishes; return raw surface or raise IOError."""
        if self._thread:
            self._thread.join()
        with self._lock:
            if self._error:
                raise IOError(self._error)
            if self._raw is None:
                raise IOError("Loader produced no result.")
            return self._raw


# -----------------------------------------------------------------------------
# Outlined text rendering
# -----------------------------------------------------------------------------

def draw_outlined_text(surface: pygame.Surface,
                       text: str,
                       font: pygame.font.Font,
                       color: tuple[int, int, int],
                       cx: int,
                       y: int,
                       outline_color: tuple[int, int, int] = (0, 0, 0),
                       outline_px: int = TEXT_OUTLINE_PX,
                       align: str = 'center') -> None:
    """
    Render text with a solid outline so it reads on any background.
    align='center' : cx is the horizontal centre of the text.
    align='left'   : cx is the left edge of the text.
    align='right'  : cx is the right edge of the text.
    """
    outline_surf = font.render(text, True, outline_color)
    text_surf    = font.render(text, True, color)
    w = text_surf.get_width()
    if align == 'center':
        x = cx - w // 2
    elif align == 'right':
        x = cx - w
    else:                   # 'left'
        x = cx
    for dx in range(-outline_px, outline_px + 1):
        for dy in range(-outline_px, outline_px + 1):
            if dx == 0 and dy == 0:
                continue
            surface.blit(outline_surf, (x + dx, y + dy))
    surface.blit(text_surf, (x, y))


# -----------------------------------------------------------------------------
# Overlay drawing
# -----------------------------------------------------------------------------

def draw_overlays(screen: pygame.Surface,
                  state: dict,
                  idx: int,
                  playlist: list[Path],
                  label_font: pygame.font.Font,
                  pause_font: pygame.font.Font,
                  settings: dict) -> None:
    """
    Blit overlays directly onto screen after the frame has been blitted.
    When show_label is on, three labels appear on the top row:
      top-left   : ' << prev_name'   (absent on first image)
      top-center : 'name   [N/total]'
      top-right  : 'next_name >>'    (absent on last image)
    When paused, a resume prompt appears at bottom-center.
    Label colours come from settings.
    """
    W, H = screen.get_size()
    PAD  = 18

    text_col    = hex_to_rgb(settings.get('label_text_color',    '#ffffff'))
    outline_col = hex_to_rgb(settings.get('label_outline_color', '#000000'))

    if state.get('show_label'):
        n = len(playlist)

        centre_text = f"{playlist[idx].name}   [{idx + 1}/{n}]"
        draw_outlined_text(screen, centre_text, label_font,
                           color=text_col, outline_color=outline_col,
                           cx=W // 2, y=PAD, align='center')

        if idx > 0:
            prev_text = f" << {playlist[idx - 1].name}"
            draw_outlined_text(screen, prev_text, label_font,
                               color=text_col, outline_color=outline_col,
                               cx=PAD, y=PAD, align='left')

        if idx < n - 1:
            next_text = f"{playlist[idx + 1].name} >>"
            draw_outlined_text(screen, next_text, label_font,
                               color=text_col, outline_color=outline_col,
                               cx=W - PAD, y=PAD, align='right')

    if state.get('paused'):
        draw_outlined_text(screen, "Press Space to resume", pause_font,
                           color=(255, 255, 255), outline_color=(0, 0, 0),
                           cx=W // 2, y=H - pause_font.get_height() - PAD,
                           align='center')


# -----------------------------------------------------------------------------
# Event polling helpers
# -----------------------------------------------------------------------------

def poll_events(state: dict) -> str | None:
    """
    Lightweight poller used inside transition renderers.
    Handles: T (cycle transition), Escape / window-close.
    Returns 'QUIT' or None.
    """
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            return 'QUIT'
        if event.type == pygame.KEYDOWN:
            if event.key == pygame.K_ESCAPE:
                return 'QUIT'
            if event.key == pygame.K_t:
                state['transition_mode'] = (state['transition_mode'] + 1) % 3
                print(f"Transition mode -> {TRANSITION_NAMES[state['transition_mode']]}")
    return None


def poll_slideshow_events(state: dict) -> str | None:
    """
    Full event poller used in the slideshow wait / pause phase.
    Returns one of: 'QUIT' | 'PREV' | 'NEXT' | 'TOGGLE_PAUSE' | 'TOGGLE_LABEL' | None.
    T is handled here as a side-effect (no separate return value needed).
    """
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            return 'QUIT'
        if event.type == pygame.KEYDOWN:
            if event.key == pygame.K_ESCAPE:
                return 'QUIT'
            elif event.key == pygame.K_t:
                state['transition_mode'] = (state['transition_mode'] + 1) % 3
                print(f"Transition mode -> {TRANSITION_NAMES[state['transition_mode']]}")
            elif event.key == pygame.K_SPACE:
                return 'TOGGLE_PAUSE'
            elif event.key == pygame.K_l:
                return 'TOGGLE_LABEL'
            elif event.key == pygame.K_z:
                return 'PREV'
            elif event.key == pygame.K_x:
                return 'NEXT'
    return None


# -----------------------------------------------------------------------------
# Transition renderers
# -----------------------------------------------------------------------------

def transition_direct(screen: pygame.Surface,
                      _curr: pygame.Surface,
                      nxt: pygame.Surface,
                      _state: dict,
                      _settings: dict,
                      _bg: tuple,
                      _clock: pygame.time.Clock) -> str | None:
    screen.blit(nxt, (0, 0))
    pygame.display.flip()
    return None


def transition_fade_over(screen: pygame.Surface,
                         curr: pygame.Surface,
                         nxt: pygame.Surface,
                         state: dict,
                         settings: dict,
                         _bg: tuple,
                         clock: pygame.time.Clock) -> str | None:
    """Blend current image out while blending next image in simultaneously."""
    duration = max(float(settings['transition_time_fade_over']), 0.01)
    t0 = time.monotonic()
    while True:
        t = min((time.monotonic() - t0) / duration, 1.0)
        overlay = nxt.copy()
        overlay.set_alpha(int(255 * t))
        screen.blit(curr, (0, 0))
        screen.blit(overlay, (0, 0))
        pygame.display.flip()
        clock.tick(DISPLAY_FPS)
        if poll_events(state) == 'QUIT':
            return 'QUIT'
        if t >= 1.0:
            return None


def transition_fade_out_fade_in(screen: pygame.Surface,
                                curr: pygame.Surface,
                                nxt: pygame.Surface,
                                state: dict,
                                settings: dict,
                                bg_color: tuple,
                                clock: pygame.time.Clock) -> str | None:
    """Fade current image to background, then fade next image in."""
    duration = max(float(settings['transition_time_fade_out_fade_in']), 0.01)
    half = duration / 2.0

    # -- Fade out current
    t0 = time.monotonic()
    while True:
        t = min((time.monotonic() - t0) / half, 1.0)
        fading = curr.copy()
        fading.set_alpha(int(255 * (1.0 - t)))
        screen.fill(bg_color)
        screen.blit(fading, (0, 0))
        pygame.display.flip()
        clock.tick(DISPLAY_FPS)
        if poll_events(state) == 'QUIT':
            return 'QUIT'
        if t >= 1.0:
            break

    # -- Fade in next
    t0 = time.monotonic()
    while True:
        t = min((time.monotonic() - t0) / half, 1.0)
        fading = nxt.copy()
        fading.set_alpha(int(255 * t))
        screen.fill(bg_color)
        screen.blit(fading, (0, 0))
        pygame.display.flip()
        clock.tick(DISPLAY_FPS)
        if poll_events(state) == 'QUIT':
            return 'QUIT'
        if t >= 1.0:
            return None


TRANSITIONS = {
    0: transition_direct,
    1: transition_fade_over,
    2: transition_fade_out_fade_in,
}


# -----------------------------------------------------------------------------
# Display picker
# -----------------------------------------------------------------------------

def pick_display() -> int | None:
    """
    If only one display is present, return 0 immediately (no window shown).
    Otherwise open a small picker window on the primary display listing every
    available screen with its resolution, wait for the user to press a number
    key (1-based), and return the chosen 0-based display index.
    Returns None if the user cancels with Escape or closes the window.
    """
    num = pygame.display.get_num_displays()
    if num <= 1:
        return 0

    sizes   = pygame.display.get_desktop_sizes()   # list of (w, h), one per display
    n_valid = min(num, 9)                           # keys 1-9 at most

    # -- Build picker window on the primary display
    WIN_W, WIN_H = 660, 80 + n_valid * 52 + 40
    font_title = pygame.font.SysFont('monospace', 28, bold=True)
    font_item  = pygame.font.SysFont('monospace', 26)
    font_hint  = pygame.font.SysFont('monospace', 20)

    win = pygame.display.set_mode((WIN_W, WIN_H), 0, display=0)
    pygame.display.set_caption("Memoir - select display")

    # Colours
    BG      = (18,  18,  18)
    ACCENT  = (255, 220, 80)
    WHITE   = (200, 200, 200)
    MUTED   = (110, 110, 110)

    def render() -> None:
        win.fill(BG)

        title_surf = font_title.render("Select display for slideshow", True, WHITE)
        win.blit(title_surf, (WIN_W // 2 - title_surf.get_width() // 2, 18))

        # Horizontal rule
        pygame.draw.line(win, MUTED, (30, 56), (WIN_W - 30, 56), 1)

        for i in range(n_valid):
            w, h    = sizes[i]
            label   = f"[{i + 1}]"
            primary = "  (primary)" if i == 0 else ""
            desc    = f"Display {i + 1}  -  {w} x {h}{primary}"
            y       = 68 + i * 52

            key_surf  = font_item.render(label, True, ACCENT)
            desc_surf = font_item.render(desc,  True, WHITE)
            win.blit(key_surf,  (40, y))
            win.blit(desc_surf, (40 + key_surf.get_width() + 14, y))

        hint = font_hint.render(
            f"Press 1-{n_valid} to choose  -  Escape to cancel", True, MUTED
        )
        win.blit(hint, (WIN_W // 2 - hint.get_width() // 2, WIN_H - 30))
        pygame.display.flip()

    render()

    # -- Event loop
    while True:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return None
            if event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    return None
                # K_1 … K_9 are contiguous in pygame
                if pygame.K_1 <= event.key <= pygame.K_1 + n_valid - 1:
                    return event.key - pygame.K_1    # 0-based index
        pygame.time.wait(40)


# -----------------------------------------------------------------------------
# Start menu
# -----------------------------------------------------------------------------

def show_start_menu(screen: pygame.Surface,
                    has_persisted: bool,
                    settings: dict,
                    target_dir: Path) -> str:
    """
    Display the start menu and block until the user presses a valid key.
    Returns one of: 'S', 'R', 'L', 'QUIT'.

    Background: the image named in settings['start_menu_image'], cover-scaled.
    Falls back to solid black if the image is missing or unloadable.
    A semi-transparent panel frames all menu text.
    Title and hint are centred; key-shortcut lines are left-aligned as a block.
    """
    W, H = screen.get_size()

    # -- Fonts
    font_title = pygame.font.SysFont('monospace', 64, bold=True)
    font_item  = pygame.font.SysFont('monospace', 36)
    font_note  = pygame.font.SysFont('monospace', 24)

    # -- Background image
    bg_surf: pygame.Surface | None = None
    img_name = settings.get('start_menu_image')
    if img_name:
        img_path = target_dir / img_name
        try:
            raw     = load_raw(img_path)
            bg_surf = scale_surface_cover(raw.convert(), W, H)
        except Exception as exc:
            print(f"Warning: could not load start menu image '{img_name}' ({exc})"
                  " - using black background.")

    # -- Build key-line content
    key_lines = [
        ("[S]", "  Sequential order"),
        ("[R]", "  Random order"),
    ]
    if has_persisted:
        key_lines.append(("[L]", "  Load previous session"))

    # Pre-render to measure widths for block-centering
    ACCENT = (255, 220, 80)
    WHITE  = (220, 220, 220)
    MUTED  = (150, 150, 150)

    key_surfs  = [font_item.render(k, True, ACCENT) for k, _ in key_lines]
    desc_surfs = [font_item.render(d, True, WHITE)  for _, d in key_lines]
    line_widths = [k.get_width() + d.get_width()
                   for k, d in zip(key_surfs, desc_surfs)]
    max_line_w  = max(line_widths)
    line_h      = font_item.get_height()

    title_surf = font_title.render(settings.get('start_menu_title', 'MEMOIR'),
                                   True, (255, 255, 255))
    note_text  = ("Pressing S or R will reset the persisted session."
                  if has_persisted else "")
    note_surf  = font_note.render(note_text, True, MUTED) if note_text else None

    # -- Panel geometry
    BOX_PAD   = 40
    SEP_GAP   = 16   # gap above/below the separator line
    ROW_GAP   = 14   # extra gap between key lines
    NOTE_GAP  = 20   # gap above the note line

    inner_w = max(title_surf.get_width(), max_line_w,
                  note_surf.get_width() if note_surf else 0)
    inner_h = (title_surf.get_height() + SEP_GAP * 2 + 1 +   # title + separator
               len(key_lines) * (line_h + ROW_GAP) - ROW_GAP +
               ((NOTE_GAP + font_note.get_height()) if note_surf else 0))

    panel_w = inner_w + BOX_PAD * 2
    panel_h = inner_h + BOX_PAD * 2
    panel_x = (W - panel_w) // 2
    panel_y = (H - panel_h) // 2 - H // 14   # slightly above true centre

    # -- Render
    def render() -> None:
        # Background
        if bg_surf:
            screen.blit(bg_surf, (0, 0))
        else:
            screen.fill((0, 0, 0))

        # Semi-transparent panel
        panel = pygame.Surface((panel_w, panel_h), pygame.SRCALPHA)
        panel.fill((0, 0, 0, 195))   # ~76 % opaque black
        screen.blit(panel, (panel_x, panel_y))

        # Title — centred in panel
        tx = panel_x + (panel_w - title_surf.get_width()) // 2
        ty = panel_y + BOX_PAD
        screen.blit(title_surf, (tx, ty))

        # Separator line
        sep_y = ty + title_surf.get_height() + SEP_GAP
        pygame.draw.line(screen, (100, 100, 100),
                         (panel_x + BOX_PAD, sep_y),
                         (panel_x + panel_w - BOX_PAD, sep_y), 1)

        # Key lines — left-aligned as a block, block centred in panel
        block_x = panel_x + (panel_w - max_line_w) // 2
        ky = sep_y + SEP_GAP

        for ks, ds in zip(key_surfs, desc_surfs):
            screen.blit(ks, (block_x, ky))
            screen.blit(ds, (block_x + ks.get_width(), ky))
            ky += line_h + ROW_GAP

        # Note — centred in panel
        if note_surf:
            nx = panel_x + (panel_w - note_surf.get_width()) // 2
            screen.blit(note_surf, (nx, ky + NOTE_GAP - ROW_GAP))

        pygame.display.flip()

    render()

    # -- Event loop
    valid = {pygame.K_r: 'R', pygame.K_s: 'S'}
    if has_persisted:
        valid[pygame.K_l] = 'L'

    while True:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return 'QUIT'
            if event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    return 'QUIT'
                if event.key in valid:
                    return valid[event.key]
        pygame.time.wait(40)


# -----------------------------------------------------------------------------
# Synchronous single-image load helper for Z / X navigation
# -----------------------------------------------------------------------------

def load_frame_sync(path: Path,
                    screen_sz: tuple[int, int],
                    bg_color: tuple[int, int, int]) -> pygame.Surface | None:
    """Synchronously load and prepare one frame; return None and log on failure."""
    try:
        return prepare_frame(load_raw(path), screen_sz, bg_color)
    except Exception as exc:
        print(f"Could not load '{path.name}': {exc}")
        return None


# -----------------------------------------------------------------------------
# Main slideshow loop
# -----------------------------------------------------------------------------

def run_slideshow(screen: pygame.Surface,
                  playlist: list[Path],
                  state: dict,
                  settings: dict) -> str:
    """
    Drive the slideshow until the user exits or an unrecoverable error occurs.
    Mutates state['current_image_index'] to reflect the last displayed image.
    Returns 'QUIT' (clean exit) or 'ERROR'.
    """
    bg_color  = hex_to_rgb(settings['background_color'])
    screen_sz = screen.get_size()
    clock     = pygame.time.Clock()
    n         = len(playlist)
    idx       = state['current_image_index'] % n
    consec    = 0   # Consecutive load failures

    # Overlay fonts — monospace keeps all characters on a consistent baseline
    label_font = pygame.font.SysFont('monospace', LABEL_FONT_SIZE)
    pause_font = pygame.font.SysFont('monospace', PAUSE_FONT_SIZE)

    loader = BackgroundLoader()

    # -- Helper: blit frame + overlays and flip
    def redraw(frame: pygame.Surface) -> None:
        screen.blit(frame, (0, 0))
        draw_overlays(screen, state, idx, playlist, label_font, pause_font, settings)
        pygame.display.flip()

    # -- Load the first image
    current_frame: pygame.Surface | None = None
    start_idx = idx
    while current_frame is None:
        try:
            raw = load_raw(playlist[idx])
            current_frame = prepare_frame(raw, screen_sz, bg_color)
            consec = 0
        except Exception as exc:
            print(f"Skipping '{playlist[idx].name}': {exc}")
            consec += 1
            if consec >= MAX_CONSECUTIVE_FAILS:
                print(f"Fatal: {MAX_CONSECUTIVE_FAILS} consecutive load failures at startup.")
                return 'ERROR'
            idx = (idx + 1) % n
            if idx == start_idx:
                print("Fatal: No images could be loaded from the playlist.")
                return 'ERROR'

    state['current_image_index'] = idx
    redraw(current_frame)

    # Kick off background pre-load for the next image
    next_idx = (idx + 1) % n
    loader.start(playlist[next_idx])
    display_deadline = time.monotonic() + settings['image_delay']

    # -- Main loop
    while True:

        # -- Display-wait / pause phase
        # Runs until the deadline is reached (skipped entirely while paused).
        pause_start: float | None = None

        while True:
            if not state.get('paused') and time.monotonic() >= display_deadline:
                break   # Time to advance to the next image

            signal = poll_slideshow_events(state)

            if signal == 'QUIT':
                state['current_image_index'] = idx
                return 'QUIT'

            elif signal == 'TOGGLE_PAUSE':
                state['paused'] = not state.get('paused', False)
                if state['paused']:
                    pause_start = time.monotonic()
                    print("Paused.")
                else:
                    # Shift the deadline forward by however long we were paused
                    if pause_start is not None:
                        display_deadline += time.monotonic() - pause_start
                        pause_start = None
                    print("Resumed.")
                redraw(current_frame)

            elif signal == 'TOGGLE_LABEL':
                state['show_label'] = not state.get('show_label', False)
                redraw(current_frame)

            elif signal == 'PREV':
                # -- Z: step back one image in the playlist (floor: index 0)
                if idx > 0:
                    target = idx - 1
                    frame  = load_frame_sync(playlist[target], screen_sz, bg_color)
                    if frame is not None:
                        idx           = target
                        current_frame = frame
                        state['current_image_index'] = idx
                        next_idx = (idx + 1) % n
                        loader.start(playlist[next_idx])
                        display_deadline = time.monotonic() + settings['image_delay']
                        redraw(current_frame)
                # else: already at first image — do nothing

            elif signal == 'NEXT':
                # -- X: step forward one image in the playlist (ceil: last index)
                if idx < n - 1:
                    target = idx + 1
                    frame  = load_frame_sync(playlist[target], screen_sz, bg_color)
                    if frame is not None:
                        idx           = target
                        current_frame = frame
                        state['current_image_index'] = idx
                        next_idx = (idx + 1) % n
                        loader.start(playlist[next_idx])
                        display_deadline = time.monotonic() + settings['image_delay']
                        redraw(current_frame)
                # else: already at last image — do nothing

            clock.tick(WAIT_FPS)

        # -- Retrieve next frame
        # Try the background-loaded image first; fall back to synchronous loads,
        # skipping bad files and tracking consecutive failures.
        next_frame: pygame.Surface | None = None
        candidate     = next_idx
        first_attempt = True

        while next_frame is None:
            try:
                if first_attempt:
                    raw           = loader.get()     # Usually already done
                    first_attempt = False
                else:
                    raw = load_raw(playlist[candidate])

                next_frame = prepare_frame(raw, screen_sz, bg_color)
                consec = 0

            except Exception as exc:
                print(f"Skipping '{playlist[candidate].name}': {exc}")
                consec += 1
                first_attempt = False
                if consec >= MAX_CONSECUTIVE_FAILS:
                    print(f"Fatal: {MAX_CONSECUTIVE_FAILS} consecutive load failures.")
                    state['current_image_index'] = idx
                    return 'ERROR'
                candidate = (candidate + 1) % n
                if candidate == idx:
                    print("Fatal: Unable to load any image other than the one on screen.")
                    state['current_image_index'] = idx
                    return 'ERROR'

        next_idx_actual = candidate

        # -- Transition
        trans_fn = TRANSITIONS[state['transition_mode']]
        result   = trans_fn(screen, current_frame, next_frame, state, settings, bg_color, clock)
        if result == 'QUIT':
            state['current_image_index'] = idx
            return 'QUIT'

        # -- Advance state
        idx           = next_idx_actual
        current_frame = next_frame
        state['current_image_index'] = idx

        # Re-draw overlays on the freshly displayed frame
        redraw(current_frame)

        next_idx = (idx + 1) % n
        loader.start(playlist[next_idx])
        display_deadline = time.monotonic() + settings['image_delay']


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------

def main() -> None:
    # -- Argument and directory validation
    if len(sys.argv) < 2:
        print("Error: No target directory specified.")
        print("Usage: python memoir.py <directory>")
        sys.exit(1)

    target_dir = Path(sys.argv[1])

    if not target_dir.exists():
        print(f"Error: Path does not exist: '{target_dir}'")
        sys.exit(1)
    if not target_dir.is_dir():
        print(f"Error: Not a directory: '{target_dir}'")
        sys.exit(1)
    if not os.access(target_dir, os.R_OK):
        print(f"Error: Directory is not readable: '{target_dir}'")
        sys.exit(1)

    image_files = discover_images(target_dir)
    if not image_files:
        exts = ', '.join(sorted(SUPPORTED_EXTENSIONS))
        print(f"Error: No supported images found in '{target_dir}'.")
        print(f"       Supported formats: {exts}")
        sys.exit(1)

    print(f"Found {len(image_files)} image(s) in '{target_dir}'.")

    persisted = load_state(target_dir)

    # Load settings, creating the file with defaults on first run.
    settings = load_settings(target_dir)
    if settings is None:
        settings = create_settings(target_dir, image_files)

    # -- Pygame initialisation
    pygame.init()

    display_idx = pick_display()
    if display_idx is None:
        pygame.quit()
        return   # User cancelled the display picker

    screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN, display=display_idx)
    pygame.display.set_caption("Memoir")
    pygame.mouse.set_visible(False)

    exit_code = 0
    state: dict | None = None   # guards the finally-block save; None means no session was started

    try:
        choice = show_start_menu(screen,
                                 has_persisted=persisted is not None,
                                 settings=settings,
                                 target_dir=target_dir)

        if choice == 'QUIT':
            return

        # Build session state
        if choice == 'L' and persisted is not None:
            state = persisted
            print(f"Resumed session - image index {state['current_image_index']}, "
                  f"mode {'random' if state['random_order'] else 'sequential'}.")
        else:
            state = DEFAULTS.copy()
            state['random_order'] = (choice == 'R')

        playlist = build_playlist(image_files, state)

        run_slideshow(screen, playlist, state, settings)

    except Exception as exc:
        print(f"Unrecoverable error: {exc}")
        traceback.print_exc()
        exit_code = 1

    finally:
        # Save session whenever the slideshow was actually started,
        # regardless of whether it exited cleanly, hit an error, or crashed.
        # This ensures the L option always appears on the next run.
        if state is not None:
            save_state(state, target_dir)
        pygame.mouse.set_visible(True)
        pygame.quit()

    sys.exit(exit_code)


if __name__ == '__main__':
    main()