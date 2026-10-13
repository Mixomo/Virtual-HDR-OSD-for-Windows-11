from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, fields
from pathlib import PurePath
from typing import Any, Literal

from . import greyscale

DisplayMode = Literal["SDR", "HDR"]


def normalize_primaries(values: Any) -> tuple[float, ...]:
    """Eight CIE xy coordinates, or an empty tuple if they are not usable.

    Primaries reach us from display drivers and from state embedded in profiles
    written by older builds, so neither the length nor the numbers can be taken
    on trust. Anything rejected here falls back to the generic per-mode table,
    which is merely inexact; letting a degenerate set through instead yields a
    profile describing an impossible display.
    """
    try:
        numbers = tuple(float(value) for value in values)
    except (TypeError, ValueError):
        return ()
    if len(numbers) != 8:
        return ()
    for number in numbers:
        # NaN fails every comparison, so this rejects it too.
        if not 0.0 <= number <= 1.0:
            return ()
    # Each y divides in the conversion to XYZ, and a zero would raise there.
    if any(numbers[index] <= 0.0 for index in (1, 3, 5, 7)):
        return ()
    return numbers


def _text(value: Any, default: str = "") -> str:
    """A text field from the hand-editable state file.

    ``str()`` turned a null into the text "None", which then named a profile, a
    path or a display that does not exist. Anything that is not a string is the
    default.
    """
    return value if isinstance(value, str) else default


def _flag(value: Any, default: bool) -> bool:
    """An on/off field from the state file. ``bool("false")`` is True, so only a JSON
    true or false is taken as one; anything else is the default."""
    return value if isinstance(value, bool) else default


@dataclass(slots=True)
class ModeState:
    profile_name: str

    # HDR corrections. The UI keeps generous ranges with fine-grained steps for precise calibration.
    temperature: float = 0.0          # Kelvin offset from D65; + warms, - cools.
    red_channel: float = 0.0          # Percent gain trim around the neutral axis.
    green_channel: float = 0.0
    blue_channel: float = 0.0
    tint: float = 0.0                 # Fine green <-> magenta trim (app units).
    gamma: float = 2.2                # Traditional gamma; 2.20 is neutral.
    saturation: float = 0.0           # Percent chroma trim.
    brightness_trim: float = 0.0      # Subtle midtone brightness trim, percent.
    contrast: float = 0.0             # Subtle contrast trim, percent.

    # Legacy field retained only so older embedded profile states deserialize safely.
    # It is no longer exposed in the GUI and never affects the generated HDR transform.
    brightness: float = 30.0

    # Retained only so older embedded states deserialize harmlessly. These controls
    # are not exposed and are always neutralized on import.
    gamma_conversion: str = "None"
    sdr_gamma_correction: str = "Off"
    exposure: float = 0.0
    low_lights: float = 0.0
    mid_lights: float = 0.0
    high_lights: float = 0.0

    minimum_luminance_nits: float = 0.0
    peak_luminance_nits: float = 1000.0
    full_frame_luminance_nits: float = 400.0
    lut_entries: int = 4096
    imported_profile: str = ""
    base_profile: str = ""
    base_profile_name: str = ""

    # The display's own primaries and white point as CIE xy, ordered
    # rx, ry, gx, gy, bx, by, wx, wy. Empty means "not known", and the generic
    # per-mode table in icc.PRIMARIES stands in. Capturing them matters because
    # a profile built without a base profile to inherit from would otherwise
    # claim BT.2020 on a panel that is nothing of the sort.
    panel_primaries: tuple[float, ...] = ()
    # Which display the panel figures above came from, as DisplayInfo.stable_key.
    # The editor keeps one HDR ModeState for all displays, so without this the
    # luminance and gamut read from display A were written into display B's profile
    # the moment the target changed -- silently, because icc.py regenerates the MHC2
    # header and lumi tag from state and those override any inherited tag.
    # Empty means "provenance unknown", which is how every state file written before
    # this field existed deserialises.
    panel_source_key: str = ""

    # The measured greyscale response, flattened -- see greyscale.RESPONSE_STRIDE. It
    # pairs each channel's delivered luminance with the code that channel was sent, so
    # it describes the panel rather than the correction on top of it and a fresh
    # measurement replaces it instead of composing with it. Empty means the curves come
    # from the controls alone, which is how every state written before this existed
    # deserialises and how a display that has never been measured behaves.
    #
    # Guarded by panel_source_key, exactly as the luminance figures are: there is one
    # HDR ModeState for all displays, and a transfer function measured on one panel is
    # not merely stale on another, it is a correction for a display that is not there.
    panel_response: tuple[float, ...] = ()
    # The reference white's channel luminance split, which is what grey is held to.
    # Stored beside the response and validated with it; neither is usable alone.
    panel_response_weights: tuple[float, ...] = ()

    # The intent curve the response was captured under, sampled at a few codes. The
    # response pairs a delivered luminance with the code that was sent for it, and which
    # code gets sent is decided by the shaping -- so changing a control that shapes the
    # curve leaves the response describing a pipeline that no longer exists.
    #
    # Not a reason to refuse: the next run replaces it, and one pass of reduced accuracy
    # is the whole cost. Measured that cost once, switching SDR-in-HDR from Auto to Off
    # with a correction captured under Auto still in place: the midrange came back at
    # 0.84-0.92 of target, and a single re-measure took it to 0.99-1.01. Worth saying out
    # loud so the dip is expected rather than alarming.
    panel_response_shaping: tuple[float, ...] = ()

    # The monitor's own HDR setting when the response was measured, read over DDC/CI. On
    # the display this was developed against that preset decides whether the panel is
    # additive: switching it moved every primary by about 2.36x while leaving white and
    # every chromaticity alone, so a correction measured under one preset describes a
    # different display under the other.
    #
    # It has been observed changing with nobody touching the OSD, which is the only
    # account ever found for 22 logged runs splitting into a clean cluster and a boosted
    # one with nothing in between.
    #
    # None means "not read", never "no preset". DDC/CI is absent on plenty of machines,
    # roughly a quarter of single reads fail on this one, and a state written before this
    # field existed says nothing either way -- so an unread value must never look like a
    # change.
    panel_response_monitor: int | None = None

    @classmethod
    def neutral(cls, mode: DisplayMode) -> "ModeState":
        if mode == "SDR":
            return cls(
                profile_name="Neutral SDR",
                brightness=30.0,
                peak_luminance_nits=100.0,
                full_frame_luminance_nits=100.0,
            )
        return cls(
            profile_name="Virtual HDR OSD",
            brightness=30.0,
            peak_luminance_nits=1000.0,
            full_frame_luminance_nits=400.0,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any], fallback_mode: DisplayMode) -> "ModeState":
        base = cls.neutral(fallback_mode)
        if not isinstance(data, dict):
            data = {}
        merged = base.to_dict()
        allowed = {f.name for f in fields(cls)}
        merged.update({k: v for k, v in data.items() if k in allowed})

        # Older development profiles stored Gamma as a log2 trim around 0. Convert only
        # that legacy representation; current profiles store an explicit gamma value.
        if "gamma" in data and "gamma_fix_enabled" not in data:
            try:
                legacy_gamma = float(data["gamma"])
                if -1.5 <= legacy_gamma <= 1.5:
                    merged["gamma"] = 2.2 * (2.0 ** legacy_gamma)
            except (TypeError, ValueError):
                pass

        limits = {
            "temperature": (-3000.0, 3000.0),
            "red_channel": (-25.0, 25.0),
            "green_channel": (-25.0, 25.0),
            "blue_channel": (-25.0, 25.0),
            "tint": (-25.0, 25.0),
            "gamma": (1.6, 3.0),
            "saturation": (-50.0, 50.0),
            "brightness_trim": (-30.0, 30.0),
            "contrast": (-30.0, 30.0),
            "brightness": (0.0, 100.0),
            "minimum_luminance_nits": (0.0, 100.0),
            "peak_luminance_nits": (80.0, 10000.0),
            "full_frame_luminance_nits": (80.0, 10000.0),
        }
        for key, (low, high) in limits.items():
            try:
                value = float(merged[key])
            except Exception:
                value = float(getattr(base, key))
            # NaN survives float() and then defeats the clamp, because min(high, nan)
            # is high: a corrupt file turned every control into the most extreme
            # setting it allows -- gamma 3.0, a 10,000-nit peak -- rather than the
            # neutral one. JSON accepts a bare NaN, so this is one hand-edit away.
            if not math.isfinite(value):
                value = float(getattr(base, key))
            merged[key] = max(low, min(high, value))

        merged["panel_primaries"] = normalize_primaries(merged.get("panel_primaries"))

        # Round-tripped through the validator rather than merely length-checked, so the
        # pair can never be half-valid: a response without its weights corrects grey
        # towards nothing, and weights without a response are inert but look meaningful
        # to anything that reads them.
        response = greyscale.from_values(
            merged.get("panel_response") or (), merged.get("panel_response_weights") or ()
        )
        if response is None:
            merged["panel_response"] = ()
            merged["panel_response_weights"] = ()
            merged["panel_response_shaping"] = ()
            merged["panel_response_monitor"] = None
        else:
            merged["panel_response"] = greyscale.to_values(response)
            merged["panel_response_weights"] = tuple(response.weights)
            try:
                merged["panel_response_shaping"] = tuple(
                    float(v) for v in (merged.get("panel_response_shaping") or ())
                )
            except (TypeError, ValueError):
                merged["panel_response_shaping"] = ()
            try:
                recorded = merged.get("panel_response_monitor")
                merged["panel_response_monitor"] = None if recorded is None else int(recorded)
            except (TypeError, ValueError, OverflowError):
                # int(Infinity) is an OverflowError, which nothing above this catches:
                # a state file holding one stopped the window being built at all.
                merged["panel_response_monitor"] = None

        # Removed controls are neutralized when legacy profiles are imported.
        merged["exposure"] = 0.0
        merged["low_lights"] = 0.0
        merged["mid_lights"] = 0.0
        merged["high_lights"] = 0.0
        merged["gamma_conversion"] = "None"
        allowed_corrections = {"Off", "Auto (Recommended)", "100 nits / Brightness 5", "200 nits / Brightness 30", "300 nits / Brightness 55", "400 nits / Brightness 80", "Unspecified", "SDR"}
        merged["sdr_gamma_correction"] = _text(merged.get("sdr_gamma_correction"), "Off")
        if merged["sdr_gamma_correction"] not in allowed_corrections:
            merged["sdr_gamma_correction"] = "Off"

        merged["minimum_luminance_nits"] = min(
            merged["minimum_luminance_nits"], merged["peak_luminance_nits"]
        )
        merged["full_frame_luminance_nits"] = min(
            merged["full_frame_luminance_nits"], merged["peak_luminance_nits"]
        )
        merged["profile_name"] = (_text(merged.get("profile_name")) or base.profile_name)[:160]
        merged["imported_profile"] = _text(merged.get("imported_profile"))
        merged["base_profile"] = _text(merged.get("base_profile"))
        merged["base_profile_name"] = _text(merged.get("base_profile_name"))[:240]
        # base_profile is the authoritative full path, so the name is always its
        # basename. Deriving it repairs state written by earlier versions, which
        # stored the ICC description here instead: Windows HDR Calibration describes
        # a profile as "... 8/14/2026 ..." while naming the file "... 8-14-2026.icc",
        # so the stored value was not a filename and every consumer that handed it
        # back to Windows failed silently.
        if merged["base_profile"]:
            merged["base_profile_name"] = PurePath(merged["base_profile"]).name
        try:
            merged["lut_entries"] = max(256, min(4096, int(merged["lut_entries"])))
        except Exception:
            merged["lut_entries"] = 4096
        return cls(**merged)


@dataclass(slots=True)
class DisplayBinding:
    """The SDR and HDR profiles a user has pinned to one physical display.

    Without this the app can only *infer* both: it reads whatever Windows happens
    to have associated at the moment it looks, which means the SDR profile is
    unknown until an HDR→SDR transition is observed, and the HDR base drifts
    whenever Windows' default changes. Pinning them makes both explicit and
    survives restarts.
    """

    sdr_profile: str = ""       # filename inside the Windows colour directory
    hdr_profile: str = ""       # filename, or a full path for an imported file
    display_label: str = ""     # for showing stale bindings when the display is absent

    def to_dict(self) -> dict[str, Any]:
        return {
            "sdr_profile": self.sdr_profile,
            "hdr_profile": self.hdr_profile,
            "display_label": self.display_label,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DisplayBinding":
        return cls(
            sdr_profile=_text(data.get("sdr_profile"))[:260],
            hdr_profile=_text(data.get("hdr_profile"))[:260],
            display_label=_text(data.get("display_label"))[:120],
        )


@dataclass(slots=True)
class ApplicationState:
    """The editor's saved state. Only HDR is edited: the SDR association is pinned or
    left alone, never generated, so there is no SDR state to keep."""

    follow_windows_mode: bool
    auto_refresh_after_mode_change: bool
    live_mode: bool
    selected_display_key: str
    hdr: ModeState
    # Keyed by DisplayInfo.stable_key, not .key: adapter LUIDs are reissued on
    # reboot, so anything keyed on those would be lost every restart.
    display_bindings: dict[str, DisplayBinding] = field(default_factory=dict)
    # Directory holding ArgyllCMS's executables, when the user has one. Argyll
    # ships on Windows as a zip with no installer, so it commonly lives somewhere
    # only the user knows about and PATH is often not set. Empty means "look in
    # PATH and the usual places".
    argyll_path: str = ""

    @classmethod
    def neutral(cls) -> "ApplicationState":
        return cls(
            follow_windows_mode=True,
            auto_refresh_after_mode_change=True,
            live_mode=False,
            selected_display_key="",
            hdr=ModeState.neutral("HDR"),
        )

    def binding(self, stable_key: str) -> DisplayBinding:
        """Return the binding for a display, creating an empty one on demand."""
        existing = self.display_bindings.get(stable_key)
        if existing is None:
            existing = DisplayBinding()
            self.display_bindings[stable_key] = existing
        return existing

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "virtual-hdr-osd-state-v1",
            "follow_windows_mode": self.follow_windows_mode,
            "auto_refresh_after_mode_change": self.auto_refresh_after_mode_change,
            "live_mode": self.live_mode,
            "selected_display_key": self.selected_display_key,
            "hdr": self.hdr.to_dict(),
            "display_bindings": {
                key: binding.to_dict() for key, binding in self.display_bindings.items()
            },
            "argyll_path": self.argyll_path,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ApplicationState":
        # The state file is JSON a person can edit, so its top level is not always an
        # object. A list or a string reached data.get and raised AttributeError, which
        # nothing caught: the window failed to construct and the app would not start
        # again until someone found this file and deleted it by hand.
        if not isinstance(data, dict):
            return cls.neutral()
        # Files from earlier builds also carry "current_mode" and an "sdr" section.
        # Neither was ever read back, and both are ignored here.
        return cls(
            follow_windows_mode=_flag(data.get("follow_windows_mode"), True),
            auto_refresh_after_mode_change=_flag(data.get("auto_refresh_after_mode_change"), True),
            # live_mode was hardcoded False here, and forced False again in the window's
            # constructor, so the preference was discarded twice over and to_dict wrote
            # a field nothing ever read back. Turning Live Apply on had to be repeated
            # every session, which is the guide's own step 4.
            live_mode=_flag(data.get("live_mode"), False),
            selected_display_key=_text(data.get("selected_display_key")),
            # One malformed section costs that section, not the whole file: dict() of a
            # string raised, and the loader then threw away every binding and setting.
            hdr=ModeState.from_dict(data.get("hdr"), "HDR"),
            display_bindings=cls._bindings_from_dict(data.get("display_bindings")),
            argyll_path=_text(data.get("argyll_path")),
        )

    @staticmethod
    def _bindings_from_dict(data: Any) -> dict[str, "DisplayBinding"]:
        if not isinstance(data, dict):
            return {}
        result: dict[str, DisplayBinding] = {}
        for key, value in data.items():
            if isinstance(key, str) and key and isinstance(value, dict):
                result[key] = DisplayBinding.from_dict(value)
        return result
