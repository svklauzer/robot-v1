"""Добавляет в apps/api/main.py маршрут GET /ml/outcomes/download (только владелец).

  /ml/outcomes/download            — файл целиком как есть
  /ml/outcomes/download?slim=true  — без lifecycle.traj (в разы меньше)

Запуск:  python scripts/patch_main_outcomes_download.py [путь к main.py]
Бэкап .bak-before-outcomes-download; повторный запуск ничего не меняет.
"""
import shutil
import sys
from pathlib import Path

path = Path(sys.argv[1] if len(sys.argv) > 1 else "apps/api/main.py")
raw = path.read_bytes()
crlf = b"\r\n" in raw
src = raw.decode("utf-8").replace("\r\n", "\n")
if '"/ml/outcomes/download"' in src:
    print("уже применено")
    sys.exit(0)
anchor = '@app.get("/ml/features/analysis", dependencies=[Depends(require_owner_action)])\n'
if src.count(anchor) != 1:
    sys.exit("ЯКОРЬ не найден или не единственный — файл не изменён")
block = '''@app.get("/ml/outcomes/download", dependencies=[Depends(require_owner_action)])
def ml_outcomes_download(slim: bool = False):
    """(#outcomes-download-2026-10-17) Скачать trade_outcomes.jsonl с персистентного диска.

    slim=true — без lifecycle.traj/traj_step. Файл целиком может расти вместе с
    траекториями; для разбора признаков и меток траектория не нужна.
    """
    from fastapi import HTTPException
    from fastapi.responses import FileResponse, StreamingResponse
    from services.ml_outcomes_export import iter_slim_lines
    from services.ml_trade_logger import MLTradeLogger

    p = MLTradeLogger().path
    if not p.exists():
        raise HTTPException(status_code=404, detail="trade_outcomes.jsonl не найден")
    if slim:
        return StreamingResponse(
            iter_slim_lines(p), media_type="application/x-ndjson",
            headers={"Content-Disposition": 'attachment; filename="trade_outcomes_slim.jsonl"'},
        )
    return FileResponse(p, media_type="application/x-ndjson", filename="trade_outcomes.jsonl")


'''
out = src.replace(anchor, block + anchor)
shutil.copy2(path, str(path) + ".bak-before-outcomes-download")
path.write_bytes((out.replace("\n", "\r\n") if crlf else out).encode("utf-8"))
print("OK:", path)
