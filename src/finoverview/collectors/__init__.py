def get_collector(name: str):
    """Lazy import so a broken optional dependency can't take down the whole CLI."""
    if name == "saxo":
        from .saxo import SaxoCollector
        return SaxoCollector
    if name == "enablebanking":
        from .enablebanking import EnableBankingCollector
        return EnableBankingCollector
    if name == "manual":
        from .manual import ManualCollector
        return ManualCollector
    if name == "fx":
        from .fx import FxCollector
        return FxCollector
    raise KeyError(f"Unknown collector: {name}")


ALL = ("fx", "manual", "enablebanking", "saxo")
