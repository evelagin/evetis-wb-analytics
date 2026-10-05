"""Source product identity; optional SKU is never synthesized from another field."""


class CatalogIdentityError(ValueError):
    pass


def product_id(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).isdigit() or int(value) <= 0:
        raise CatalogIdentityError("catalog product lacks stable product_id")
    return str(int(value))


def optional_sku(value):
    if value is None or value == "" or (not isinstance(value, bool) and value in (0, "0")):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).isdigit() or int(value) <= 0:
        raise CatalogIdentityError("catalog SKU malformed; never substitute product identity")
    return str(int(value))


def optional_offer_id(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not value.strip():
        raise CatalogIdentityError("catalog offer_id malformed")
    return value
