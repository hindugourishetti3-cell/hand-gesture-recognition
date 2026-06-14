"""
╔══════════════════════════════════════════════════════════════════╗
║           REAL-TIME HAND GESTURE CONTROL SYSTEM                  ║
║  Controls: Volume | Brightness | Slides via hand gestures        ║
╚══════════════════════════════════════════════════════════════════╝

INSTALLATION:
    pip install opencv-python mediapipe pyautogui screen-brightness-control

    For volume control (Windows only):
        pip install pycaw comtypes

    For volume control (Linux):
        pip install pulsectl

    For volume control (macOS):
        pip install osascript  (or use subprocess with osascript)

USAGE:
    python hand_gesture_control.py

GESTURE GUIDE:
    ┌─────────────────────────────────────────────────────┐
    │  MODE SELECTION (hold gesture for ~0.5s to activate)│
    │  • 1 finger  (index only)      → VOLUME MODE        │
    │  • 2 fingers (index + middle)  → BRIGHTNESS MODE    │
    │  • 3 fingers (i + m + ring)    → SLIDE MODE         │
    │                                                      │
    │  ACTIONS (inside any mode)                           │
    │  • Thumb UP   → Increase / Next                     │
    │  • Thumb DOWN → Decrease / Previous                 │
    │                                                      │
    │  LOCK / UNLOCK                                       │
    │  • Open palm (all 5 fingers) for 2 seconds           │
    └─────────────────────────────────────────────────────┘
"""

import cv2
import mediapipe as mp
import time
import sys
import platform
import pyautogui

# ─── Optional imports with graceful fallbacks ───────────────────────────────

try:
    import screen_brightness_control as sbc
    HAS_BRIGHTNESS = True
except ImportError:
    HAS_BRIGHTNESS = False
    print("[WARN] screen_brightness_control not found. Brightness mode disabled.")

# Volume control — platform-specific
PLATFORM = platform.system()
HAS_VOLUME = False

if PLATFORM == "Windows":
    try:
        from ctypes import cast, POINTER
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        # FIX 1: removed `import math` — it was imported but never used anywhere

        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        volume_interface = cast(interface, POINTER(IAudioEndpointVolume))
        VOL_RANGE = volume_interface.GetVolumeRange()  # (min_dB, max_dB, step)
        HAS_VOLUME = True
    except Exception as e:
        print(f"[WARN] pycaw unavailable: {e}. Volume mode disabled.")

elif PLATFORM == "Linux":
    try:
        import pulsectl
        pulse = pulsectl.Pulse("gesture-control")
        HAS_VOLUME = True
    except ImportError:
        print("[WARN] pulsectl not found. Volume mode disabled.")

elif PLATFORM == "Darwin":  # macOS
    import subprocess
    HAS_VOLUME = True  # Will use osascript via subprocess


# ─── Constants ───────────────────────────────────────────────────────────────

# Finger landmark tip indices (MediaPipe order)
FINGER_TIPS   = [4, 8, 12, 16, 20]  # thumb, index, middle, ring, pinky
FINGER_BASES  = [3, 6, 10, 14, 18]  # knuckle below each tip

# Gesture modes
MODE_NONE       = "NONE"
MODE_VOLUME     = "VOLUME"
MODE_BRIGHTNESS = "BRIGHTNESS"
MODE_SLIDE      = "SLIDE"

# Timing constants (seconds)
ACTION_COOLDOWN   = 0.8   # Minimum time between repeated actions
LOCK_HOLD_TIME    = 2.0   # Seconds to hold open palm to toggle lock
MODE_CONFIRM_TIME = 0.5   # Seconds to hold finger count before mode switches

# Volume / brightness step sizes
VOL_STEP        = 5       # % per gesture
BRIGHTNESS_STEP = 10      # % per gesture

# UI colours (BGR)
COLOR_BG        = (15,  15,  25)
COLOR_ACCENT    = (0,   220, 180)   # cyan-green
COLOR_WARNING   = (0,   100, 255)   # orange
COLOR_SUCCESS   = (50,  220,  80)   # green
COLOR_LOCK      = (40,   40, 200)   # red-ish
COLOR_WHITE     = (240, 240, 240)
COLOR_GRAY      = (120, 120, 120)

MODE_COLORS = {
    MODE_NONE:       COLOR_GRAY,
    MODE_VOLUME:     (255, 180,  50),   # amber
    MODE_BRIGHTNESS: (100, 220, 255),   # sky blue
    MODE_SLIDE:      (180, 120, 255),   # purple
}

pyautogui.FAILSAFE = False


# ─── Volume helpers ───────────────────────────────────────────────────────────

def get_volume() -> int:
    """Return current volume as 0-100 integer."""
    if PLATFORM == "Windows" and HAS_VOLUME:
        scalar = volume_interface.GetMasterVolumeLevelScalar()
        return int(scalar * 100)
    elif PLATFORM == "Linux" and HAS_VOLUME:
        sink = pulse.sink_list()[0]
        return int(pulse.volume_get_all_chans(sink) * 100)
    elif PLATFORM == "Darwin" and HAS_VOLUME:
        result = subprocess.run(
            ["osascript", "-e", "output volume of (get volume settings)"],
            capture_output=True, text=True
        )
        return int(result.stdout.strip())
    return 50


def set_volume(level: int) -> None:
    """Set system volume. level: 0-100."""
    level = max(0, min(100, level))
    if PLATFORM == "Windows" and HAS_VOLUME:
        volume_interface.SetMasterVolumeLevelScalar(level / 100.0, None)
    elif PLATFORM == "Linux" and HAS_VOLUME:
        sink = pulse.sink_list()[0]
        pulse.volume_set_all_chans(sink, level / 100.0)
    elif PLATFORM == "Darwin" and HAS_VOLUME:
        subprocess.run(["osascript", "-e", f"set volume output volume {level}"])


def get_brightness() -> int:
    """Return current brightness as 0-100."""
    if HAS_BRIGHTNESS:
        try:
            val = sbc.get_brightness()
            return val[0] if isinstance(val, list) else int(val)
        except Exception:
            pass
    return 50


def set_brightness(level: int) -> None:
    """Set screen brightness. level: 0-100."""
    if HAS_BRIGHTNESS:
        try:
            sbc.set_brightness(max(0, min(100, level)))
        except Exception as e:
            print(f"[WARN] Brightness set failed: {e}")


# ─── Gesture / Finger Logic ───────────────────────────────────────────────────

def is_finger_up(landmarks, tip_idx: int, base_idx: int, is_thumb: bool = False) -> bool:
    """
    Returns True if the given finger is extended.

    For the thumb we use horizontal distance from base to tip
    (adjusted for handedness) rather than vertical, since the thumb
    bends differently from other fingers.
    """
    tip = landmarks[tip_idx]
    # FIX 2: `base` was assigned unconditionally but only ever read in the
    # non-thumb branch, causing a "assigned but never used" warning for thumb
    # calls.  Moved the assignment inside the else branch where it is needed.

    if is_thumb:
        mcp   = landmarks[2]
        wrist = landmarks[0]
        # If wrist is to the left of MCP → right hand: thumb up means tip.x < mcp.x
        if wrist.x < mcp.x:
            return tip.x < mcp.x - 0.04
        else:
            return tip.x > mcp.x + 0.04
    else:
        base = landmarks[base_idx]
        return tip.y < base.y  # tip higher on screen = finger up


def count_fingers(landmarks) -> list:
    """
    Returns a list of booleans [thumb, index, middle, ring, pinky]
    True = finger is up.
    """
    fingers = []

    # Thumb (special case)
    fingers.append(is_finger_up(landmarks, FINGER_TIPS[0], FINGER_BASES[0], is_thumb=True))

    # Other four fingers
    for i in range(1, 5):
        fingers.append(is_finger_up(landmarks, FINGER_TIPS[i], FINGER_BASES[i]))

    return fingers  # [thumb, index, middle, ring, pinky]


def fingers_to_count(fingers: list) -> int:
    """Count total raised fingers."""
    return sum(fingers)


def classify_mode_gesture(fingers: list) -> str:
    """
    Map a finger pattern to a mode selection gesture.
    Returns MODE_NONE if pattern doesn't match any mode selector.

    Rules:
        1 finger  = index only (no thumb, no middle/ring/pinky)
        2 fingers = index + middle
        3 fingers = index + middle + ring
    """
    thumb, index, middle, ring, pinky = fingers

    non_thumb       = [index, middle, ring, pinky]
    non_thumb_count = sum(non_thumb)

    # Strict matching — pinky must not be raised for modes
    if non_thumb_count == 1 and index and not thumb and not pinky:
        return MODE_VOLUME
    if non_thumb_count == 2 and index and middle and not thumb and not pinky:
        return MODE_BRIGHTNESS
    if non_thumb_count == 3 and index and middle and ring and not thumb and not pinky:
        return MODE_SLIDE

    return MODE_NONE


def detect_thumb_direction(landmarks) -> str:
    """
    Detect if thumb is pointing UP or DOWN, or NEUTRAL.
    Uses the y-coordinate of the thumb tip relative to the wrist.
    """
    tip   = landmarks[4]
    wrist = landmarks[0]

    if tip.y < wrist.y - 0.15:
        return "UP"
    if tip.y > wrist.y + 0.05:
        return "DOWN"
    return "NEUTRAL"


def is_open_palm(fingers: list) -> bool:
    """All 5 fingers extended."""
    return all(fingers)


# ─── UI Drawing Helpers ───────────────────────────────────────────────────────

def draw_rounded_rect(img, x, y, w, h, r, color, thickness=-1, alpha=1.0):
    """Draw a filled rounded rectangle."""
    overlay = img.copy()
    cv2.rectangle(overlay, (x + r, y),     (x + w - r, y + h),     color, thickness)
    cv2.rectangle(overlay, (x,     y + r), (x + w,     y + h - r), color, thickness)
    for cx, cy in [(x + r, y + r), (x + w - r, y + r),
                   (x + r, y + h - r), (x + w - r, y + h - r)]:
        cv2.circle(overlay, (cx, cy), r, color, thickness)
    cv2.addWeighted(overlay, alpha, img, 1 - alpha, 0, img)


def draw_progress_bar(img, x, y, w, h, value, color, bg_color=(50, 50, 60)):
    """Draw a horizontal progress bar (0-100)."""
    cv2.rectangle(img, (x, y), (x + w, y + h), bg_color, -1)
    filled = int(w * value / 100)
    if filled > 0:
        cv2.rectangle(img, (x, y), (x + filled, y + h), color, -1)
    cv2.rectangle(img, (x, y), (x + w, y + h), (80, 80, 90), 1)


def draw_ui(frame, mode, locked, finger_count, fingers,
            volume_level, brightness_level, lock_progress,
            last_action, action_feedback_timer, fps, thumb_active=False):
    """
    Render all HUD elements on the frame.
    Uses a semi-transparent panel on the left side.
    """
    h, w = frame.shape[:2]

    # ── Semi-transparent left panel ──────────────────────────────────────────
    panel_w = 260
    overlay = frame.copy()
    cv2.rectangle(overlay, (0, 0), (panel_w, h), (10, 12, 20), -1)
    cv2.addWeighted(overlay, 0.65, frame, 0.35, 0, frame)

    # ── Title ─────────────────────────────────────────────────────────────────
    cv2.putText(frame, "GESTURE", (12, 35),
                cv2.FONT_HERSHEY_DUPLEX, 0.9, COLOR_ACCENT, 1, cv2.LINE_AA)
    cv2.putText(frame, "CONTROL", (12, 58),
                cv2.FONT_HERSHEY_DUPLEX, 0.9, COLOR_ACCENT, 1, cv2.LINE_AA)
    cv2.line(frame, (12, 65), (panel_w - 12, 65), COLOR_ACCENT, 1)

    # ── Lock status ───────────────────────────────────────────────────────────
    lock_color = (60, 60, 200) if locked else COLOR_SUCCESS
    lock_label = "** LOCKED **" if locked else "RUNNING"
    lock_hint  = "gestures paused" if locked else "gestures active"
    draw_rounded_rect(frame, 12, 74, panel_w - 24, 28, 5, lock_color, alpha=0.6)
    cv2.putText(frame, lock_label, (20, 91),
                cv2.FONT_HERSHEY_SIMPLEX, 0.52, COLOR_WHITE, 1, cv2.LINE_AA)
    cv2.putText(frame, lock_hint, (20, 108),
                cv2.FONT_HERSHEY_SIMPLEX, 0.34, (200, 200, 200), 1, cv2.LINE_AA)

    # ── Mode display ──────────────────────────────────────────────────────────
    mode_color = MODE_COLORS.get(mode, COLOR_GRAY)
    cv2.putText(frame, "MODE", (12, 132),
                cv2.FONT_HERSHEY_SIMPLEX, 0.42, COLOR_GRAY, 1, cv2.LINE_AA)
    cv2.putText(frame, mode, (12, 155),
                cv2.FONT_HERSHEY_DUPLEX, 0.82, mode_color, 1, cv2.LINE_AA)
    cv2.line(frame, (12, 163), (panel_w - 12, 163), (40, 40, 55), 1)

    # ── Finger count & indicator dots ────────────────────────────────────────
    cv2.putText(frame, f"FINGERS: {finger_count}", (12, 184),
                cv2.FONT_HERSHEY_SIMPLEX, 0.48, COLOR_GRAY, 1, cv2.LINE_AA)

    dot_labels      = ["T", "I", "M", "R", "P"]
    display_fingers = list(fingers)
    if thumb_active:
        display_fingers[0] = True
    for i, (up, lbl) in enumerate(zip(display_fingers, dot_labels)):
        cx    = 18 + i * 44
        color = COLOR_SUCCESS if up else (60, 60, 70)
        cv2.circle(frame, (cx, 206), 11, color, -1)
        cv2.putText(frame, lbl, (cx - 5, 211),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.35, COLOR_WHITE, 1, cv2.LINE_AA)

    cv2.line(frame, (12, 226), (panel_w - 12, 226), (40, 40, 55), 1)

    # ── Volume bar ────────────────────────────────────────────────────────────
    cv2.putText(frame, f"VOL  {volume_level:3d}%", (12, 246),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, MODE_COLORS[MODE_VOLUME], 1, cv2.LINE_AA)
    draw_progress_bar(frame, 12, 252, panel_w - 24, 8, volume_level,
                      MODE_COLORS[MODE_VOLUME])

    # ── Brightness bar ────────────────────────────────────────────────────────
    cv2.putText(frame, f"BRI  {brightness_level:3d}%", (12, 278),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, MODE_COLORS[MODE_BRIGHTNESS], 1, cv2.LINE_AA)
    draw_progress_bar(frame, 12, 284, panel_w - 24, 8, brightness_level,
                      MODE_COLORS[MODE_BRIGHTNESS])

    # ── Lock progress bar ─────────────────────────────────────────────────────
    if lock_progress > 0:
        pct = int(lock_progress / LOCK_HOLD_TIME * 100)
        cv2.putText(frame, f"PALM HOLD {pct}%", (12, 313),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.42, COLOR_WARNING, 1, cv2.LINE_AA)
        draw_progress_bar(frame, 12, 318, panel_w - 24, 6, pct,
                          COLOR_WARNING, bg_color=(40, 20, 0))

    # ── Action feedback (centre of frame) ─────────────────────────────────────
    if last_action and time.time() - action_feedback_timer < 1.2:
        fade       = 1.0 - (time.time() - action_feedback_timer) / 1.2
        text_color = tuple(int(c * fade) for c in COLOR_SUCCESS)
        cy         = h // 2
        cv2.putText(frame, last_action, (panel_w + 20, cy),
                    cv2.FONT_HERSHEY_DUPLEX, 1.1, text_color, 2, cv2.LINE_AA)

    # ── FPS ───────────────────────────────────────────────────────────────────
    cv2.putText(frame, f"FPS {fps:4.1f}", (12, h - 12),
                cv2.FONT_HERSHEY_SIMPLEX, 0.38, (70, 70, 80), 1, cv2.LINE_AA)

    # ── Help hint (bottom right) ──────────────────────────────────────────────
    cv2.putText(frame, "5 fingers x2s = LOCK/UNLOCK", (panel_w + 10, h - 12),
                cv2.FONT_HERSHEY_SIMPLEX, 0.38, (70, 70, 80), 1, cv2.LINE_AA)


# ─── Main Application ─────────────────────────────────────────────────────────

def main():
    # ── MediaPipe setup ───────────────────────────────────────────────────────
    mp_hands   = mp.solutions.hands
    mp_drawing = mp.solutions.drawing_utils
    mp_styles  = mp.solutions.drawing_styles

    hands = mp_hands.Hands(
        static_image_mode=False,
        max_num_hands=1,
        min_detection_confidence=0.7,
        min_tracking_confidence=0.6,
    )

    # ── Webcam setup ──────────────────────────────────────────────────────────
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("[ERROR] Cannot open webcam.")
        sys.exit(1)

    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
    cap.set(cv2.CAP_PROP_FPS, 30)

    # ── State variables ───────────────────────────────────────────────────────
    current_mode      = MODE_NONE
    locked            = False

    last_action_time  = 0.0
    last_action_label = ""
    action_feedback_t = 0.0

    candidate_mode    = MODE_NONE
    candidate_mode_t  = 0.0

    palm_hold_start   = 0.0
    palm_hold_active  = False

    thumb_active      = False
    SMOOTH_BUF        = 5
    fingers_history   = []

    fps_times         = []

    vol_level  = get_volume()
    bri_level  = get_brightness()
    sys_poll_t = 0.0

    print("[INFO] Hand Gesture Control running. Press Q to quit.")

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        frame = cv2.flip(frame, 1)               # Mirror for natural interaction
        rgb   = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        now   = time.time()

        # FPS counter
        fps_times.append(now)
        fps_times = [t for t in fps_times if now - t < 1.0]
        fps = len(fps_times)

        # Periodically refresh system volume/brightness values
        if now - sys_poll_t > 1.0:
            vol_level  = get_volume()
            bri_level  = get_brightness()
            sys_poll_t = now

        # ── MediaPipe inference ───────────────────────────────────────────────
        results = hands.process(rgb)

        finger_count     = 0
        lock_progress    = 0.0
        smoothed_fingers = [False] * 5

        if results.multi_hand_landmarks:
            hand_lm = results.multi_hand_landmarks[0]

            # Draw hand skeleton
            mp_drawing.draw_landmarks(
                frame, hand_lm, mp_hands.HAND_CONNECTIONS,
                mp_styles.get_default_hand_landmarks_style(),
                mp_styles.get_default_hand_connections_style(),
            )

            lm          = hand_lm.landmark
            raw_fingers = count_fingers(lm)

            # Rolling majority-vote smoothing
            fingers_history.append(raw_fingers)
            if len(fingers_history) > SMOOTH_BUF:
                fingers_history.pop(0)

            smoothed_fingers = [
                sum(h[i] for h in fingers_history) > len(fingers_history) // 2
                for i in range(5)
            ]

            finger_count = fingers_to_count(smoothed_fingers)

            # ── Open palm → lock/unlock toggle ───────────────────────────────
            if is_open_palm(smoothed_fingers):
                if not palm_hold_active:
                    palm_hold_start  = now
                    palm_hold_active = True
                elif now - palm_hold_start >= LOCK_HOLD_TIME:
                    locked             = not locked
                    palm_hold_active   = False
                    last_action_label  = "UNLOCKED" if not locked else "LOCKED"
                    action_feedback_t  = now
                    palm_hold_start    = now   # reset to avoid immediate re-trigger
            else:
                palm_hold_active = False

            lock_progress = min(
                (now - palm_hold_start) if palm_hold_active else 0.0,
                LOCK_HOLD_TIME
            )

            # ── Skip gesture logic when locked ───────────────────────────────
            if not locked:

                # Mode selection (requires holding the gesture briefly)
                gesture_mode = classify_mode_gesture(smoothed_fingers)

                if gesture_mode != MODE_NONE:
                    if gesture_mode == candidate_mode:
                        if now - candidate_mode_t >= MODE_CONFIRM_TIME:
                            current_mode = gesture_mode
                    else:
                        candidate_mode   = gesture_mode
                        candidate_mode_t = now
                else:
                    candidate_mode = MODE_NONE

                # Action detection — fist (0 fingers) + thumb gesture
                if current_mode != MODE_NONE and finger_count == 0:
                    thumb_dir = detect_thumb_direction(lm)

                    if thumb_dir in ("UP", "DOWN"):
                        thumb_active = True
                        cooldown_ok  = (now - last_action_time) >= ACTION_COOLDOWN

                        if cooldown_ok:
                            direction        = 1 if thumb_dir == "UP" else -1
                            last_action_time = now

                            if current_mode == MODE_VOLUME:
                                new_vol           = vol_level + direction * VOL_STEP
                                set_volume(new_vol)
                                vol_level         = max(0, min(100, new_vol))
                                last_action_label = (
                                    f"VOL {'UP' if direction > 0 else 'DOWN'}  {vol_level}%"
                                )
                                action_feedback_t = now

                            elif current_mode == MODE_BRIGHTNESS:
                                new_bri           = bri_level + direction * BRIGHTNESS_STEP
                                set_brightness(new_bri)
                                bri_level         = max(0, min(100, new_bri))
                                last_action_label = (
                                    f"BRI {'UP' if direction > 0 else 'DOWN'}  {bri_level}%"
                                )
                                action_feedback_t = now

                            elif current_mode == MODE_SLIDE:
                                key               = "right" if direction > 0 else "left"
                                pyautogui.press(key)
                                last_action_label = (
                                    "NEXT SLIDE" if direction > 0 else "PREV SLIDE"
                                )
                                action_feedback_t = now
                    else:
                        thumb_active = False
                else:
                    thumb_active = False

        else:
            # No hand detected — reset all transient state
            fingers_history  = []
            palm_hold_active = False
            lock_progress    = 0.0
            smoothed_fingers = [False] * 5
            finger_count     = 0
            thumb_active     = False

        # ── Draw HUD ─────────────────────────────────────────────────────────
        draw_ui(
            frame,
            mode                  = current_mode,
            locked                = locked,
            finger_count          = finger_count,
            fingers               = smoothed_fingers if fingers_history else [False] * 5,
            volume_level          = vol_level,
            brightness_level      = bri_level,
            lock_progress         = lock_progress if palm_hold_active else 0.0,
            last_action           = last_action_label,
            action_feedback_timer = action_feedback_t,
            fps                   = fps,
            thumb_active          = thumb_active,
        )

        cv2.imshow("Gesture Control", frame)

        key = cv2.waitKey(1) & 0xFF
        if key == ord("q") or key == 27:   # Q or ESC
            break

    cap.release()
    cv2.destroyAllWindows()
    hands.close()
    print("[INFO] Gesture Control stopped.")


if __name__ == "__main__":
    main()