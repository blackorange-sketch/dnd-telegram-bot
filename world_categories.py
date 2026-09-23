"""World categories offered when starting a new adventure."""

# key -> (button label, description used in the prompt to Gemini)
CATEGORIES: dict[str, tuple[str, str]] = {
    "fantasy": ("🧙 Фентезі", "класичне фентезі з магією, мечами та міфічними істотами"),
    "scifi": ("🚀 Sci-Fi", "науково-фантастичний світ з космічними кораблями та технологіями майбутнього"),
    "postapo": ("☢️ Постапокаліпсис", "світ після глобальної катастрофи, руїни, виживання, мутанти"),
    "horror": ("👻 Хоррор", "похмурий світ жахів з надприродними загрозами"),
    "cyberpunk": ("🤖 Кіберпанк", "мегаполіс майбутнього, корпорації, хакери, кібернетичні імпланти"),
    "steampunk": ("⚙️ Стімпанк", "світ парових машин, дирижаблів та вікторіанської естетики"),
}

RANDOM_KEY = "random"
RANDOM_LABEL = "🎲 Випадкова категорія"


def label_for(key: str) -> str:
    return CATEGORIES[key][0]


def description_for(key: str) -> str:
    return CATEGORIES[key][1]


def all_keys() -> list[str]:
    return list(CATEGORIES.keys())
