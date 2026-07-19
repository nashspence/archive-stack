"""Generic media-ingest primitives for Riverhog-adjacent workflows."""

__all__ = [
    "MUNCHY_PROFILE_TARGET",
    "EncodeProfile",
]


def __getattr__(name: str) -> object:
    if name in {"MUNCHY_PROFILE_TARGET", "EncodeProfile"}:
        from munchy.profiles import MUNCHY_PROFILE_TARGET, EncodeProfile

        return {"MUNCHY_PROFILE_TARGET": MUNCHY_PROFILE_TARGET, "EncodeProfile": EncodeProfile}[
            name
        ]
    raise AttributeError(name)
