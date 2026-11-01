import os
import sys

import pymysql


FROM_REVISION = "9b7c6d5e4f3a"
TO_REVISION = "5578e028b2f2"
MIGRATION_TABLES = (
    "provider_models",
    "provider_model_credentials",
    "tenant_default_models",
    "provider_model_settings",
    "load_balancing_model_configs",
)


def main():
    connection = pymysql.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ["DB_PORT"]),
        user=os.environ["DB_USERNAME"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ["DB_DATABASE"],
        connect_timeout=10,
        autocommit=False,
        charset="utf8mb4",
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT version_num FROM alembic_version")
            revisions = tuple(row[0] for row in cursor.fetchall())
            if revisions == (TO_REVISION,):
                print(f"revision_already_applied={TO_REVISION}")
                return
            if revisions != (FROM_REVISION,):
                raise RuntimeError(f"unexpected_alembic_revisions={revisions}")

            for table in MIGRATION_TABLES:
                cursor.execute(f"SELECT COUNT(*) FROM `{table}`")
                row_count = cursor.fetchone()[0]
                print(f"{table}_rows={row_count}")
                if row_count:
                    raise RuntimeError(
                        f"refusing_to_skip_data_migration_nonempty_table={table}"
                    )

            cursor.execute(
                "UPDATE alembic_version SET version_num = %s WHERE version_num = %s",
                (TO_REVISION, FROM_REVISION),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("alembic_revision_changed_during_compatibility_check")
        connection.commit()
        print(f"empty_data_migration_marked_applied={TO_REVISION}")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"compatibility_migration_refused={exc}", file=sys.stderr)
        sys.exit(1)
