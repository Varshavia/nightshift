from jinja2 import Markup


def render(fragment: str) -> str:
    return str(Markup(fragment))
