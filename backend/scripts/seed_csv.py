"""Import sample CSV into the default portfolio."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.database import SessionLocal, Base, engine
from app.main import init_db
from app.services.csv_io import import_csv


def main() -> None:
    init_db()
    csv_path = ROOT.parent / "sample-data" / "Code_20260726.csv"
    db = SessionLocal()
    try:
        result = import_csv(db, csv_path.read_bytes(), replace=True)
        print(result)
    finally:
        db.close()


if __name__ == "__main__":
    main()
