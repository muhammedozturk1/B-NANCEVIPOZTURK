"""SQLAlchemy modellerinden PostgreSQL DDL üretir (migrations/0001_initial.sql)."""
from pathlib import Path

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from backend.smc.database.models import Base

if __name__ == "__main__":
    dialect = postgresql.dialect()
    parts = ["-- Otomatik üretildi: python -m scripts.smc.export_schema\n"]
    for table in Base.metadata.sorted_tables:
        parts.append(str(CreateTable(table, if_not_exists=True).compile(dialect=dialect)).strip() + ";\n")
        for idx in table.indexes:
            parts.append(str(CreateIndex(idx, if_not_exists=True).compile(dialect=dialect)).strip() + ";\n")
    out = Path(__file__).resolve().parent.parent.parent / "migrations" / "smc_0001_initial.sql"
    out.write_text("\n".join(parts))
    print(f"yazıldı: {out}")
