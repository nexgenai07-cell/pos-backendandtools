"""
imports app ke MCP tools: Excel (Stock / Menu / Recipes) se bulk setup, aur AI se workbook draft.

Backend routes (imports/urls.py), SAB owner/manager:
  GET  imports/template/   current data se pre-filled .xlsx          -> imports_template_download
  GET  imports/guide/      columns ka matlab, setup progress, AI prompt -> imports_guide_get
  POST imports/ai-draft/   {text} -> AI draft wala .xlsx (base64)    -> imports_ai_draft
  POST imports/            multipart file; commit=1 par SAVE            -> imports_preview / imports_commit
  POST imports/marked/     file + marks -> galat cells laal rang mein  -> imports_mark_problems

Workflow (hamesha yehi tarteeb):
  1. imports_guide_get              -> kya kaisa hona chahiye
  2. imports_template_download      -> pre-filled workbook (ya imports_ai_draft)
  3. (workbook edit karein)
  4. imports_preview(file)          -> har row: create / update (old->new) / unchanged / delete / error;
                                       KUCH SAVE NAHI HOTA
  5. imports_commit(file)           -> wahi plan ek transaction mein lagta hai; koi bhi row error mein ho
                                       to poora refuse (400), kuch nahi badalta

DHYAN: imports_commit menu, stock aur recipes mein bulk create/update/DELETE karta hai. Hamesha pehle
imports_preview chalayein aur 'delete' rows ko dhyan se parhein.

File paths MCP server wali machine ke hain (stdio mein = aapki apni machine). Binary files (xlsx) chat
mein nahi aati, is liye disk par save hoti hain aur tool unka path wapas deta hai.
"""
from __future__ import annotations

import base64
import json
import tempfile
from pathlib import Path
from typing import Any, Literal

from typing_extensions import TypedDict

from client import POSClient, POSAPIError

RowFilter = Literal["errors", "changes", "all"]
MAX_ROWS_PER_SHEET = 200


class ProblemMark(TypedDict):
    """Ek galat cell jise laal rang aur comment dena hai."""

    sheet: Literal["stock", "products", "recipes"]
    row: int  # Excel row number, >= 2 (row 1 headers hain)
    field: str  # column key (jaise 'name_en'); na mile to sheet ka naam wala column
    text: str  # cell comment (<=1000 chars)


def _folder(save_dir: str | None) -> Path:
    folder = Path(save_dir).expanduser() if save_dir else Path(tempfile.gettempdir()) / "pos-mcp"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _save(content: bytes, name: str, save_dir: str | None) -> dict[str, Any]:
    target = _folder(save_dir) / name
    target.write_bytes(content)
    return {"path": str(target), "bytes": len(content)}


def _require_xlsx(file_path: str) -> None:
    """Sirf Excel workbook upload hone do: koi bhi aur local file backend ko na bheji jaye."""
    if Path(file_path).suffix.lower() not in (".xlsx", ".xlsm"):
        raise ValueError(f"Sirf .xlsx workbook upload ho sakta hai (mila: {Path(file_path).name})")


def _shape(payload: dict[str, Any], rows: RowFilter) -> dict[str, Any]:
    """
    Preview/commit response ko chhota karta hai: har sheet ke saare rows chat mein bhejna bohot bara
    ho sakta hai. rows='errors' (default) sirf error rows; 'changes' = create/update/delete/error
    (unchanged ke baghair); 'all' = sab. Har sheet se max 200 rows; `rows_truncated` batata hai.
    """
    keep = {
        "errors": {"error"},
        "changes": {"create", "update", "delete", "error"},
        "all": {"create", "update", "unchanged", "delete", "error"},
    }[rows]
    shaped_sheets = []
    for sheet in payload.get("sheets", []):
        selected = [r for r in sheet.get("rows", []) if r.get("action") in keep]
        shaped_sheets.append(
            {
                "key": sheet.get("key"),
                "title": sheet.get("title"),
                "summary": sheet.get("summary"),
                "rows": selected[:MAX_ROWS_PER_SHEET],
                "rows_truncated": max(len(selected) - MAX_ROWS_PER_SHEET, 0),
            }
        )
    return {**{k: v for k, v in payload.items() if k != "sheets"}, "sheets": shaped_sheets}


def register(mcp, client: POSClient) -> None:
    @mcp.tool()
    async def imports_guide_get() -> Any:
        """Import ki guide (owner/manager): har sheet (stock, products, recipes) ke columns ka matlab aur
        rules, branch ka setup kitna mukammal hai, aur taiyar-shuda AI prompt; saath mein
        ai: {enabled, left_today}. Workbook banane/bharne se pehle yahan se shuru karein."""
        return await client.get("imports/guide/")

    @mcp.tool()
    async def imports_template_download(save_dir: str | None = None) -> Any:
        """Restaurant workbook (.xlsx) download (owner/manager): Stock, Menu aur Recipes sheets, branch ke
        MOUJUDA data se pre-filled (blank nahi), taake jo hai usi ko edit karein. Disk par save hoti hai
        (`save_dir` na do to temp folder ke 'pos-mcp' mein); {path, bytes} wapas. Har download ek code
        record karta hai jise upload par pehchana jata hai (90 din tak)."""
        content = await client.get_bytes("imports/template/")
        return _save(content, "restaurant-template.xlsx", save_dir)

    @mcp.tool()
    async def imports_ai_draft(text: str, save_dir: str | None = None) -> Any:
        """AI se workbook ka draft (owner/manager): `text` (<=20000 chars) mein admin ki request/menu likhein
        (jaise "10 burgers aur unki recipes"); backend AI ke draft ko workbook mein merge karke .xlsx deta
        hai jo yahan disk par save hoti hai. Kuch DB mein save NAHI hota (sirf usage log).
        Result: {path, bytes, filename, summary, failed_recipes, estimated: [{sheet,row,fields}]}.
        `estimated` wo cells hain jin ki value AI ka andaza hai (file mein highlighted + comment) -- save karne
        se pehle insaan check kare. Agla qadam imports_preview.
        Errors: 400 imports.aiTextRequired (text khali), 400 imports.aiTextTooLong (>20000 chars),
        429 imports.aiDailyLimit (roz ki hadd; imports_guide_get ke ai.left_today mein bacha hua dekhein),
        503 imports.aiNotConfigured, 502 imports.aiTimeout / imports.aiFailed / imports.aiTooLong.
        Fail hui request bhi din ki hadd mein ginti hai."""
        data = await client.post("imports/ai-draft/", {"text": text})
        content = base64.b64decode(data.pop("file"))
        saved = _save(content, data.get("filename") or "restaurant-ai-draft.xlsx", save_dir)
        return {**saved, **data}

    @mcp.tool()
    async def imports_preview(file_path: str, rows: RowFilter = "errors") -> Any:
        """Workbook ka PREVIEW (owner/manager): file upload hoti hai par KUCH SAVE NAHI HOTA. Har row ka
        action: create / update (old -> new `changes`) / unchanged / delete / error, plus `summary` (poori
        file ke counts), `file_errors` (file hi ghalat) aur `to_buy` (jo ingredients khareedne padenge).
        Har row: {row (Excel row number), name, action, changes, errors, notes}; error rows ke `errors` mein
        column key hota hai, wohi imports_mark_problems ke `field` mein dein.
        file_errors codes: fileRequired, fileTooLarge (max 5 MB), notExcel, missingSheet, fileCodeUnknown
        (template 90 din se purana ya kisi aur branch ka), noRows. Inme se koi ho to rows nahi aate.
        `file_path` local .xlsx ka raasta (doosri file type refuse).
        `rows` response ka size control karta hai: 'errors' (default, sirf error rows), 'changes'
        (unchanged ke baghair), 'all'; har sheet se max 200 rows (rows_truncated mein baqi ki ginti)."""
        _require_xlsx(file_path)
        data = await client.post_file("imports/", file_path)
        return _shape(data, rows)

    @mcp.tool()
    async def imports_commit(file_path: str, rows: RowFilter = "changes") -> Any:
        """Workbook ko SAVE karta hai (owner/manager): wahi plan jo preview dikhata hai, ek transaction mein
        -- menu, stock aur recipes mein bulk create/update/DELETE. DESTRUCTIVE: pehle imports_preview chalayein
        aur 'delete' rows dekhein. Agar file_errors ya koi bhi error row ho to poora REFUSE (400): kuch nahi
        badalta aur wo errors yahan wapas aate hain ({committed: False}). Kamyabi par committed=True,
        summary aur to_buy. `rows` response ka size control karta hai (preview jaisa; default 'changes')."""
        _require_xlsx(file_path)
        try:
            data = await client.post_file("imports/", file_path, data={"commit": "1"})
        except POSAPIError as exc:
            # 400 par backend poora preview payload deta hai (errors ke saath); use wapas dena behtar hai.
            if exc.status == 400 and isinstance(exc.payload, dict) and "sheets" in exc.payload:
                return _shape(exc.payload, "errors")
            raise
        return _shape(data, rows)

    @mcp.tool()
    async def imports_mark_problems(
        file_path: str,
        marks: list[ProblemMark],
        save_dir: str | None = None,
    ) -> Any:
        """Apni file wapas, har galat cell laal rang mein aur cell comment ke saath (owner/manager) --
        preview ke errors dekh kar admin ko batane ke liye. `marks`: [{sheet, row (>=2), field, text}];
        `text` us language mein jis mein admin parhta hai. Naya .xlsx disk par save hota hai; {path, bytes}.
        Ghalat marks (anjaan sheet, row < 2) chup-chaap chhor di jati hain; file hi kharab ho to 400
        file_errors. Error: 400 imports.marksInvalid."""
        _require_xlsx(file_path)
        content = await client.post_file(
            "imports/marked/", file_path, data={"marks": json.dumps([dict(m) for m in marks])}, raw=True
        )
        return _save(content, f"{Path(file_path).stem}-to-fix.xlsx", save_dir)
