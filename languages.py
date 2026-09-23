"""Language choices for the adventure. The `prompt_name` value is what gets
sent to Gemini as an instruction (e.g. "Respond in: English")."""

# key -> (button label, prompt_name)
LANGUAGES: dict[str, tuple[str, str]] = {
    "en": ("🇬🇧 English", "English"),
    "uk": ("🇺🇦 Українська", "Ukrainian"),
    "ru": ("🇷🇺 Русский", "Russian"),
    "pl": ("🇵🇱 Polski", "Polish"),
}

DEFAULT_LANGUAGE = "uk"


def label_for(key: str) -> str:
    return LANGUAGES[key][0]


def prompt_name_for(key: str) -> str:
    return LANGUAGES[key][1]


def all_keys() -> list[str]:
    return list(LANGUAGES.keys())
