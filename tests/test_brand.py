"""Фирменный стиль (app/brand.py, 27.10): файлы бренда есть — подключаются; нет — сервис работает как раньше."""
from app import brand


def test_without_brand_files(monkeypatch, tmp_path):
    """Папки var/brand/ нет: без эмблем и шрифта, значок вкладки прежний; общие стили — всё равно."""
    monkeypatch.setattr(brand, "BRAND_DIR", tmp_path / "нет")
    monkeypatch.setattr(brand, "_available", brand.scan(tmp_path / "нет"))
    assert brand.url("header.svg") is None
    assert brand.favicon() == "📊"
    head = brand.head_html()
    assert "@font-face" not in head
    assert "text-transform: none" in head and ".p10-upload .q-uploader__subtitle" in head


def test_with_brand_files(monkeypatch, tmp_path):
    """Шрифт подключается только теми начертаниями, чьи файлы лежат; значок вкладки — SVG прямо в странице."""
    (tmp_path / "fonts").mkdir()
    for style in ("Regular", "Bold"):
        (tmp_path / "fonts" / f"TTMoscowEconomy-{style}.woff2").write_bytes(b"x")
    (tmp_path / "favicon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    monkeypatch.setattr(brand, "BRAND_DIR", tmp_path)
    monkeypatch.setattr(brand, "_available", brand.scan(tmp_path))
    assert brand.scan(tmp_path) == {"favicon.svg", "fonts/TTMoscowEconomy-Regular.woff2",
                                    "fonts/TTMoscowEconomy-Bold.woff2"}
    assert brand.url("favicon.svg") == "/brand/favicon.svg"
    assert brand.url("header.svg") is None
    assert brand.favicon().startswith("<svg")
    head = brand.head_html()
    assert head.count("@font-face") == 2
    assert "font-weight: 400" in head and "font-weight: 700" in head and "font-weight: 500" not in head
    assert "/brand/fonts/TTMoscowEconomy-Regular.woff2" in head


def test_files_added_after_start_are_not_linked(monkeypatch, tmp_path):
    """Код-ревью 27.10: какие файлы есть — решено при запуске; положенный позже файл не даёт битую ссылку."""
    monkeypatch.setattr(brand, "BRAND_DIR", tmp_path)
    monkeypatch.setattr(brand, "_available", brand.scan(tmp_path))     # при запуске папка пустая
    (tmp_path / "header.svg").write_text("<svg/>", encoding="utf-8")   # файл положили на работающем сервисе
    assert brand.url("header.svg") is None
