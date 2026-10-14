import io
import zipfile

import openpyxl
import pytest

import loader

HEADER = ["Код_товару", "Назва_позиції", "Ціна", "Одиниця_виміру", "Наявність",
          "Назва_групи", "Посилання_зображення"]


def build_xlsx(rows, header=HEADER) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(header)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def prom_style_xlsx() -> bytes:
    """Мінімальний xlsx як у експорті Проми: у rels вказано `sharedStrings.xml`,
    а в архіві файл названий `xl/SharedStrings.xml` (з великої літери)."""
    strings = ["Код_товару", "Назва_позиції", "Ціна", "Одиниця_виміру", "Назва_групи",
               "Ковбаса Київська", "кг", "Алан"]
    sst = "".join(f"<si><t>{x}</t></si>" for x in strings)
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    files = {
        "[Content_Types].xml": (
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
            "</Types>"),
        "_rels/.rels": (
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>"),
        "xl/workbook.xml": (
            f'<?xml version="1.0"?><workbook xmlns="{ns}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="TDSheet" sheetId="1" r:id="rId1"/></sheets></workbook>'),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>'
            "</Relationships>"),
        "xl/SharedStrings.xml": f'<?xml version="1.0"?><sst xmlns="{ns}">{sst}</sst>',
        "xl/worksheets/sheet1.xml": (
            f'<?xml version="1.0"?><worksheet xmlns="{ns}"><sheetData>'
            '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="C1" t="s"><v>2</v></c>'
            '<c r="D1" t="s"><v>3</v></c><c r="E1" t="s"><v>4</v></c></row>'
            '<row r="2"><c r="A2"><v>514</v></c><c r="B2" t="s"><v>5</v></c><c r="C2"><v>314.6</v></c>'
            '<c r="D2" t="s"><v>6</v></c><c r="E2" t="s"><v>7</v></c></row>'
            "</sheetData></worksheet>"),
    }
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content)
    return out.getvalue()


def test_reads_prom_style_archive_with_capital_shared_strings():
    data = prom_style_xlsx()
    with pytest.raises(KeyError):  # без виправлення openpyxl на Linux падає — саме це й чинимо
        openpyxl.load_workbook(io.BytesIO(data))
    df = loader.read_catalog(data)
    assert list(df["name"]) == ["Ковбаса Київська"]
    assert list(df["code"]) == [514] and df["price"].iloc[0] == 314.6


def test_archived_flag():
    df = loader.read_catalog(build_xlsx([
        [1, "Ковбаса А", 1, "кг", "+", "Алан", None],
        [2, "Я!ВИВЕДЕНО Ковбаса Б", 1, "кг", "-", "Алан", None],
        [3, "Ковбаса В", 1, "кг", "-", "Я!Архів виведених кодів ", None],
        [4, "_ВИВЕДЕНО", None, "шт", "-", "Алан", None],
        [5, "_виведено Ковбаса Г", None, "кг", "-", "Алан", None]]))
    assert list(df["archived"]) == [False, True, True, True, True]


def test_changed_format_raises_clear_error():
    with pytest.raises(loader.CatalogFormatError, match="Ціна"):
        loader.read_catalog(build_xlsx([[1, "x", "y", "z"]],
                                       header=["Код_товару", "Назва_позиції", "Одиниця_виміру", "Назва_групи"]))
