"""World categories offered when starting a new adventure.

`hint` is sent to Gemini in English (it's just meta-instruction, doesn't
need to match the story language). `labels` are the button text shown to
the player, per UI language.
"""

CATEGORIES: dict[str, dict] = {
    "fantasy": {
        "hint": (
            "classic high/low fantasy: magic, swords, mythical creatures, kingdoms, knights, "
            "taverns, ancient ruins, prophecies. Medieval-adjacent tech only — no guns, no "
            "electricity. Invent fantasy-sounding names for people and places. Typical threats: "
            "dark sorcerers, dragons, cursed knights, goblins, bandits. Currency is gold/silver "
            "coins or gems. Tone: adventurous, mythic, a little grand."
        ),
        "labels": {
            "uk": "🧙 Фентезі", "en": "🧙 Fantasy",
            "ru": "🧙 Фэнтези", "pl": "🧙 Fantasy",
        },
    },
    "scifi": {
        "hint": (
            "science fiction: starships, alien species, advanced/futuristic technology, space "
            "colonies, AI, energy weapons, zero-g, distant planets. Name places/tech/species with "
            "futuristic, scientific-sounding coinages. Typical threats: hostile aliens, rogue AI, "
            "corporate fleets, cosmic hazards, malfunctioning systems. Currency is credits. Tone: "
            "wonder mixed with the cold vastness of space."
        ),
        "labels": {
            "uk": "🚀 Sci-Fi", "en": "🚀 Sci-Fi",
            "ru": "🚀 Sci-Fi", "pl": "🚀 Sci-Fi",
        },
    },
    "postapo": {
        "hint": (
            "post-apocalyptic wasteland after civilization's collapse: ruins, radiation, "
            "scavenged and patched-together gear, makeshift settlements, mutants, raiders, "
            "scarce resources, harsh weather. Currency is bottle caps, scrap, or ammo. Tone: "
            "gritty, desperate, survivalist — every resource matters and nothing is reliable."
        ),
        "labels": {
            "uk": "☢️ Постапокаліпсис", "en": "☢️ Post-apocalypse",
            "ru": "☢️ Постапокалипсис", "pl": "☢️ Postapokalipsa",
        },
    },
    "horror": {
        "hint": (
            "supernatural horror: dread, unease, things half-seen in the dark, cursed places, "
            "malevolent entities, sanity-fraying events, isolation. Slow-burn tension punctuated "
            "by sudden shocks — NOT a heroic-fantasy tone, this should feel unsettling and "
            "vulnerable rather than empowering. Ambiguity over clear answers. No convenient "
            "magic-item currency — money (if any) is mundane (cash, coins)."
        ),
        "labels": {
            "uk": "👻 Хоррор", "en": "👻 Horror",
            "ru": "👻 Хоррор", "pl": "👻 Horror",
        },
    },
    "cyberpunk": {
        "hint": (
            "cyberpunk megacity: neon-lit rain-soaked streets, megacorporations, cybernetic "
            "implants, hacking/netrunning, stark class inequality, gangs, synthetic drugs, "
            "ubiquitous surveillance. Currency is credits/eddies. Tone: noir, cynical, "
            "high-tech-low-life — technology is everywhere but society is rotten underneath."
        ),
        "labels": {
            "uk": "🤖 Кіберпанк", "en": "🤖 Cyberpunk",
            "ru": "🤖 Киберпанк", "pl": "🤖 Cyberpunk",
        },
    },
    "steampunk": {
        "hint": (
            "steampunk: steam-and-clockwork technology, airships, brass and gears, "
            "Victorian-adjacent society and manners, eccentric inventors, alternate-history "
            "flavor. Currency is coins with an era-appropriate name (e.g. sovereigns, guineas). "
            "Tone: adventurous and inventive, equal parts elegant and grimy industrial."
        ),
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


# A compact one-line reminder for ONGOING turns. The full hint_for() text is
# used just once, at the opening, where establishing genre in real detail
# matters most and the cost is paid only a single time. Resending that full
# paragraph on every subsequent turn would repeat ~60-80 words of static
# text on every single API call for the rest of the game — this short
# version keeps the genre-consistency nudge without that recurring cost.
SHORT_HINTS = {
    "fantasy": "medieval fantasy — magic, swords, gold coins, mythic tone",
    "scifi": "sci-fi — starships, aliens, futuristic tech, credits, sense of wonder",
    "postapo": "post-apocalypse — ruins, scavenged gear, mutants, gritty survival",
    "horror": "horror — dread, unease, ambiguity; unsettling, not heroic",
    "cyberpunk": "cyberpunk — neon megacity, corporations, implants, noir cynicism",
    "steampunk": "steampunk — steam/clockwork tech, airships, brass, Victorian flavor",
}


def short_hint_for(key: str) -> str:
    return SHORT_HINTS.get(key, hint_for(key))


def random_label(lang_key: str) -> str:
    return RANDOM_LABELS.get(lang_key, RANDOM_LABELS["uk"])


def all_keys() -> list[str]:
    return list(CATEGORIES.keys())
