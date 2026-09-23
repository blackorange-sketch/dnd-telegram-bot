"""World categories offered when starting a new adventure.

`hint` is sent to Gemini in English (it's just meta-instruction, doesn't
need to match the story language). `labels` are the button text shown to
the player, per UI language.
"""

CATEGORIES: dict[str, dict] = {
    "fantasy": {
        "hint": "classic fantasy world with magic, swords and mythical creatures",
        "labels": {
            "uk": "🧙 Фентезі", "en": "🧙 Fantasy",
            "ru": "🧙 Фэнтези", "pl": "🧙 Fantasy",
        },
    },
    "scifi": {
        "hint": "science-fiction world with spaceships and future technology",
        "labels": {
            "uk": "🚀 Sci-Fi", "en": "🚀 Sci-Fi",
            "ru": "🚀 Sci-Fi", "pl": "🚀 Sci-Fi",
        },
    },
    "postapo": {
        "hint": "post-apocalyptic world after a global catastrophe, ruins, survival, mutants",
        "labels": {
            "uk": "☢️ Постапокаліпсис", "en": "☢️ Post-apocalypse",
            "ru": "☢️ Постапокалипсис", "pl": "☢️ Postapokalipsa",
        },
    },
    "horror": {
        "hint": "grim world of horror with supernatural threats",
        "labels": {
            "uk": "👻 Хоррор", "en": "👻 Horror",
            "ru": "👻 Хоррор", "pl": "👻 Horror",
        },
    },
    "cyberpunk": {
        "hint": "futuristic megacity, corporations, hackers, cybernetic implants",
        "labels": {
            "uk": "🤖 Кіберпанк", "en": "🤖 Cyberpunk",
            "ru": "🤖 Киберпанк", "pl": "🤖 Cyberpunk",
        },
    },
    "steampunk": {
        "hint": "world of steam-powered machines, airships and Victorian aesthetics",
        "labels": {
            "uk": "⚙️ Стімпанк", "en": "⚙️ Steampunk",
            "ru": "⚙️ Стимпанк", "pl": "⚙️ Steampunk",
        },
    },
}

RANDOM_KEY = "random"
RANDOM_LABELS = {
    "uk": "🎲 Випадкова категорія", "en": "🎲 Random category",
    "ru": "🎲 Случайная категория", "pl": "🎲 Losowa kategoria",
}


def label_for(key: str, lang_key: str) -> str:
    labels = CATEGORIES[key]["labels"]
    return labels.get(lang_key, labels["uk"])


def hint_for(key: str) -> str:
    return CATEGORIES[key]["hint"]


def random_label(lang_key: str) -> str:
    return RANDOM_LABELS.get(lang_key, RANDOM_LABELS["uk"])


def all_keys() -> list[str]:
    return list(CATEGORIES.keys())
